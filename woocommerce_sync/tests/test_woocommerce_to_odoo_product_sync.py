"""Tests for WooCommerce -> Odoo product field mapping fixes in connector.py:
- 'woocommerce_service' now reflects WooCommerce's 'virtual'/'downloadable' flags instead of always being False.
- Product-level tax is only applied when WooCommerce's 'tax_status' is 'taxable' (not 'none'/'shipping').
"""

from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon, make_woocommerce_product_payload


@tagged('post_install', '-at_install')
class TestWooCommerceToOdooProductSync(WoocommerceSyncCommon):
    def test_virtual_product_maps_to_woocommerce_service(self):
        product_values = self.connector.woocommerce_product_fields(make_woocommerce_product_payload(virtual=True, downloadable=False))
        self.assertTrue(product_values['woocommerce_service'])

    def test_downloadable_product_maps_to_woocommerce_service(self):
        product_values = self.connector.woocommerce_product_fields(make_woocommerce_product_payload(virtual=False, downloadable=True))
        self.assertTrue(product_values['woocommerce_service'])

    def test_physical_product_does_not_map_to_woocommerce_service(self):
        product_values = self.connector.woocommerce_product_fields(make_woocommerce_product_payload(virtual=False, downloadable=False))
        self.assertFalse(product_values['woocommerce_service'])

    def test_product_variation_fields_also_maps_virtual_to_woocommerce_service(self):
        variation_values = self.connector.woocommerce_product_variation_fields(make_woocommerce_product_payload(virtual=True, downloadable=False, image=None))
        self.assertTrue(variation_values['woocommerce_service'])

    def test_taxable_product_gets_odoo_tax(self):
        odoo_products = {}
        self.connector.woocommerce_to_odoo_product_sync(
            make_woocommerce_product_payload(id=91001, sku='TEST-TAX-1', tax_status='taxable', tax_class=''),
            woocommerce_currency=None,
            woocommerce_tax_rates={'standard': 21.0},
            woocommerce_prices_include_tax=False,
            woocommerce_weight_unit='kg',
            woocommerce_dimension_unit='cm',
            odoo_products=odoo_products,
        )

        odoo_product = self.env['product.template'].search([('default_code', '=', 'TEST-TAX-1')])
        self.assertTrue(odoo_product)
        self.assertTrue(odoo_product.taxes_id, 'A taxable WooCommerce product should get an Odoo sale tax applied')

    def test_tax_status_none_does_not_get_odoo_tax(self):
        odoo_products = {}
        self.connector.woocommerce_to_odoo_product_sync(
            make_woocommerce_product_payload(id=91002, sku='TEST-TAX-2', tax_status='none', tax_class=''),
            woocommerce_currency=None,
            woocommerce_tax_rates={'standard': 21.0},
            woocommerce_prices_include_tax=False,
            woocommerce_weight_unit='kg',
            woocommerce_dimension_unit='cm',
            odoo_products=odoo_products,
        )

        odoo_product = self.env['product.template'].search([('default_code', '=', 'TEST-TAX-2')])
        self.assertTrue(odoo_product)
        self.assertFalse(odoo_product.taxes_id, "A WooCommerce product with tax_status='none' should not get any Odoo sale tax applied, even though a tax rate mapping exists")
