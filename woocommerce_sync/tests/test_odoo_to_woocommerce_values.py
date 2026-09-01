"""Tests for building the WooCommerce product payload from an Odoo product (connector.py's 'odoo_to_woocommerce_product_values'), used by the Odoo -> WooCommerce batch sync. Mocks 'woocommerce_attribute_create_or_retrieve' so no real WooCommerce REST API calls are made (every Odoo product has a category, which would otherwise trigger a live HTTP call)."""

from unittest.mock import patch

from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon, storable_product_values


@tagged('post_install', '-at_install')
class TestOdooToWooCommerceProductValues(WoocommerceSyncCommon):
    def test_simple_storable_product_values(self):
        odoo_product = self.env['product.template'].create(
            {
                'name': 'Test Odoo Product',
                'default_code': 'ODOO-TEST-1',
                'list_price': 42.5,
                'categ_id': self.env.ref('product.product_category_all').id,
                **storable_product_values(is_storable=True),
            },
        )

        with patch.object(type(self.connector), 'woocommerce_attribute_create_or_retrieve', return_value=None) as mocked_attribute_call:
            product_values = self.connector.odoo_to_woocommerce_product_values(odoo_product, woocommerce_api=None, woocommerce_tax_rates={})

        # The product's (always-set) default category triggers a create-or-retrieve call
        mocked_attribute_call.assert_called()

        self.assertEqual(product_values['name'], 'Test Odoo Product')
        self.assertEqual(product_values['sku'], 'ODOO-TEST-1')
        self.assertEqual(product_values['regular_price'], '42.50')
        self.assertEqual(product_values['type'], 'simple')
        self.assertEqual(product_values['tax_class'], 'standard')
        self.assertTrue(product_values['manage_stock'])
        self.assertTrue(product_values['date_created_gmt'].endswith('Z'))

    def test_non_storable_product_has_manage_stock_false(self):
        odoo_product = self.env['product.template'].create(
            {
                'name': 'Test Odoo Service-Like Product',
                'default_code': 'ODOO-TEST-2',
                'list_price': 10.0,
                **storable_product_values(is_storable=False),
            },
        )

        with patch.object(type(self.connector), 'woocommerce_attribute_create_or_retrieve', return_value=None):
            product_values = self.connector.odoo_to_woocommerce_product_values(odoo_product, woocommerce_api=None, woocommerce_tax_rates={})

        self.assertFalse(product_values['manage_stock'])

    def test_meta_data_carries_over_existing_woocommerce_metadata(self):
        odoo_product = self.env['product.template'].create({'name': 'Test Odoo Product Update', 'default_code': 'ODOO-TEST-3', 'list_price': 5.0})

        existing_woocommerce_product = {'meta_data': [{'key': 'some_other_key', 'value': 'keep-me'}]}

        with patch.object(type(self.connector), 'woocommerce_attribute_create_or_retrieve', return_value=None):
            product_values = self.connector.odoo_to_woocommerce_product_values(odoo_product, woocommerce_api=None, woocommerce_tax_rates={}, woocommerce_product=existing_woocommerce_product)

        meta_keys = {meta['key'] for meta in product_values['meta_data']}

        self.assertIn('odoo_id', meta_keys)
        self.assertIn('some_other_key', meta_keys)
