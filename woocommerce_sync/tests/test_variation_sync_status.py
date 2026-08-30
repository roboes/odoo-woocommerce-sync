"""Regression tests for WooCommerce variation status aggregation and test-mode deletion safety."""

from unittest.mock import Mock, patch

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon, make_woocommerce_product_payload


@tagged('post_install', '-at_install')
class TestVariationSyncStatus(WoocommerceSyncCommon):
    def test_variation_batch_waits_for_product_chunks(self):
        with (
            patch.object(type(self.connector), 'sync_chunk_jobs_pending', return_value=True),
            patch.object(type(self.connector), 'woocommerce_api_get') as mocked_api_get,
            self.assertRaises(RetryableJobError),
        ):
            self.connector.woocommerce_to_odoo_products_variations_sync_batch(
                woocommerce_currency=None,
                woocommerce_tax_rates={},
                woocommerce_prices_include_tax=False,
                woocommerce_weight_unit='kg',
                woocommerce_dimension_unit='cm',
            )

        mocked_api_get.assert_not_called()

    def test_variations_are_new_once_then_skipped(self):
        parent_payload = make_woocommerce_product_payload(id=92000, sku='TEST-VARIABLE', name='Variable Product', type='variable', variations=[92001, 92002])
        self.connector.woocommerce_to_odoo_product_sync(
            parent_payload,
            woocommerce_currency=None,
            woocommerce_tax_rates={},
            woocommerce_prices_include_tax=False,
            woocommerce_weight_unit='kg',
            woocommerce_dimension_unit='cm',
            odoo_products={},
        )

        variations = [
            make_woocommerce_product_payload(
                id=92001,
                parent_id=92000,
                sku='TEST-VARIABLE-RED',
                name='Variable Product - Red',
                type='variation',
                image=None,
                attributes=[{'id': 1, 'name': 'Color', 'option': 'Red'}],
            ),
            make_woocommerce_product_payload(
                id=92002,
                parent_id=92000,
                sku='TEST-VARIABLE-BLUE',
                name='Variable Product - Blue',
                type='variation',
                image=None,
                attributes=[{'id': 1, 'name': 'Color', 'option': 'Blue'}],
            ),
        ]

        with (
            patch.object(type(self.connector), 'woocommerce_api_get', return_value=Mock()),
            patch.object(type(self.connector), 'woocommerce_api_get_all_items', return_value=variations),
        ):
            first_counts = self.connector.woocommerce_to_odoo_product_variations_sync(
                parent_payload,
                woocommerce_currency=None,
                woocommerce_tax_rates={},
                woocommerce_prices_include_tax=False,
                woocommerce_weight_unit='kg',
                woocommerce_dimension_unit='cm',
            )
            second_counts = self.connector.woocommerce_to_odoo_product_variations_sync(
                parent_payload,
                woocommerce_currency=None,
                woocommerce_tax_rates={},
                woocommerce_prices_include_tax=False,
                woocommerce_weight_unit='kg',
                woocommerce_dimension_unit='cm',
            )

        self.assertEqual(first_counts, {'processed': 2, 'new': 2, 'updated': 0, 'skipped': 0})
        self.assertEqual(second_counts, {'processed': 2, 'new': 0, 'updated': 0, 'skipped': 2})

    def test_existing_unmapped_odoo_combination_is_counted_as_new_mapping(self):
        parent_payload = make_woocommerce_product_payload(id=92100, sku='TEST-VARIABLE-EXISTING', name='Variable Product Existing', type='variable', variations=[92101])
        self.connector.woocommerce_to_odoo_product_sync(
            parent_payload,
            woocommerce_currency=None,
            woocommerce_tax_rates={},
            woocommerce_prices_include_tax=False,
            woocommerce_weight_unit='kg',
            woocommerce_dimension_unit='cm',
            odoo_products={},
        )
        variation = make_woocommerce_product_payload(
            id=92101,
            parent_id=92100,
            sku='TEST-VARIABLE-EXISTING-RED',
            name='Variable Product Existing - Red',
            type='variation',
            image=None,
            attributes=[{'id': 1, 'name': 'Color', 'option': 'Red'}],
        )

        with (
            patch.object(type(self.connector), 'woocommerce_api_get', return_value=Mock()),
            patch.object(type(self.connector), 'woocommerce_api_get_all_items', return_value=[variation]),
        ):
            counts = self.connector.woocommerce_to_odoo_product_variations_sync(
                parent_payload,
                woocommerce_currency=None,
                woocommerce_tax_rates={},
                woocommerce_prices_include_tax=False,
                woocommerce_weight_unit='kg',
                woocommerce_dimension_unit='cm',
            )

        self.assertEqual(counts, {'processed': 1, 'new': 1, 'updated': 0, 'skipped': 0})

    def test_test_mode_never_runs_product_deletion(self):
        self.connector.settings_woocommerce_test_mode = True

        with patch.object(type(self.connector), 'woocommerce_api_get') as mocked_api_get:
            self.connector.woocommerce_to_odoo_products_delete()

        mocked_api_get.assert_not_called()
