"""Tests for 'woocommerce_to_odoo_products_delete()': products no longer published in WooCommerce (draft, private...) are archived in Odoo. The delete-or-archive fallback for products gone from WooCommerce is covered in 'test_sync_robustness.py'."""

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
