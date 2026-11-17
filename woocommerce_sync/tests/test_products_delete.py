"""Tests for 'woocommerce_to_odoo_products_delete()': products no longer published in WooCommerce (draft, private...) are archived in Odoo, and products are checked by ID in chunks of one page, so products trashed or deleted during the check cannot hide other products. The delete-or-archive fallback for products gone from WooCommerce is covered in 'test_sync_robustness.py'."""

from unittest.mock import MagicMock, patch

from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon


@tagged('post_install', '-at_install')
class TestProductsDelete(WoocommerceSyncCommon):
    def _create_odoo_product(self, woocommerce_id: int, name: str):
        return self.env['product.template'].create(
            {
                'name': name,
                'woocommerce_site_url': self.connector.settings_woocommerce_connection_url,
                'woocommerce_id': str(woocommerce_id),
                'woocommerce_status': 'publish',
            }
        )

    def _run_products_delete(self, woocommerce_products: list[dict]) -> None:
        with (
            patch.object(type(self.connector), 'woocommerce_api_get', return_value=MagicMock()),
            patch.object(type(self.connector), 'woocommerce_api_get_all_items', return_value=woocommerce_products) as mocked_get_all_items,
        ):
            self.connector.woocommerce_to_odoo_products_delete()

        self.assertEqual(mocked_get_all_items.call_args.kwargs['params']['_fields'], 'id,status')

    def test_unpublished_product_is_archived(self):
        published = self._create_odoo_product(94001, 'Published product')
        draft = self._create_odoo_product(94002, 'Draft product')
        private = self._create_odoo_product(94003, 'Private product')

        self._run_products_delete([{'id': 94001, 'status': 'publish'}, {'id': 94002, 'status': 'draft'}, {'id': 94003, 'status': 'private'}])

        self.assertTrue(published.active)
        self.assertFalse(draft.active, 'A product set back to draft in WooCommerce is archived even if its webhook was missed')
        self.assertEqual(draft.woocommerce_status, 'draft')
        self.assertFalse(draft.product_variant_ids.active)
        self.assertFalse(private.active)

    def test_products_are_checked_by_id_in_chunks(self):
        # 101 products: 2 chunks of at most one page each, so products trashed or deleted during the check cannot shift a page and hide other products
        woocommerce_ids = [str(woocommerce_id) for woocommerce_id in range(94100, 94201)]
        products = self.env['product.template'].create(
            [
                {'name': f'Product {woocommerce_id}', 'woocommerce_site_url': self.connector.settings_woocommerce_connection_url, 'woocommerce_id': woocommerce_id, 'woocommerce_status': 'publish'}
                for woocommerce_id in woocommerce_ids
            ]
        )
        published, draft, deleted = products[0], products[1], products[-1]
        woocommerce_statuses = dict.fromkeys(woocommerce_ids, 'publish') | {draft.woocommerce_id: 'draft'}
        del woocommerce_statuses[deleted.woocommerce_id]

        def get_all_items(woocommerce_api, endpoint, params=None):
            requested_ids = params['include'].split(',')
            self.assertLessEqual(len(requested_ids), params['per_page'])
            self.assertEqual(params['status'], 'any')
            return [{'id': int(woocommerce_id), 'status': woocommerce_statuses[woocommerce_id]} for woocommerce_id in requested_ids if woocommerce_id in woocommerce_statuses]

        with (
            patch.object(type(self.connector), 'woocommerce_api_get', return_value=MagicMock()),
            patch.object(type(self.connector), 'woocommerce_api_get_all_items', side_effect=get_all_items) as mocked_get_all_items,
        ):
            self.connector.woocommerce_to_odoo_products_delete()

        requested_ids = [woocommerce_id for call in mocked_get_all_items.call_args_list for woocommerce_id in call.kwargs['params']['include'].split(',')]
        self.assertEqual(mocked_get_all_items.call_count, 2)
        self.assertEqual(sorted(requested_ids), woocommerce_ids, 'Every Odoo product is requested by ID exactly once')
        self.assertTrue(published.active)
        self.assertFalse(draft.active, 'A product found by ID with a non-published status is archived with that status')
        self.assertEqual(draft.woocommerce_status, 'draft')
        self.assertFalse(deleted.exists(), 'A product not found by ID is deleted')
