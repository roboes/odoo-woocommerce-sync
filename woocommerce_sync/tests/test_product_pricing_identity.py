"""Tests for WooCommerce <-> Odoo product prices and identity (connector.py):
- Per-variation prices map onto the template list price + attribute value extra prices ('odoo_product_variant_prices_update()'), instead of every variant getting the last variation's price.
- The Odoo list price comes from the WooCommerce regular price (not a temporary sale price), and only taxable products are converted between tax conventions.
- Exported prices are converted to the store's tax convention with Odoo's tax engine (mixed, group and fixed taxes) and use the variant's own price.
- A first import links an unambiguous existing Odoo product with the same internal reference instead of creating a duplicate, never another company's product.
- Related product IDs stored as text are parsed instead of iterated character by character.
"""

from typing import ClassVar

from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon, make_woocommerce_product_payload


@tagged('post_install', '-at_install')
class TestProductPricing(WoocommerceSyncCommon):
    connector_values: ClassVar[dict] = {'settings_odoo_tax_calculation': 'tax_excluded'}

    def _template_with_variants(self, attribute_names):
        attribute_lines = []
        for attribute_name in attribute_names:
            attribute = self.env['product.attribute'].create({'name': attribute_name, 'value_ids': [(0, 0, {'name': f'{attribute_name} A'}), (0, 0, {'name': f'{attribute_name} B'})]})
            attribute_lines.append((0, 0, {'attribute_id': attribute.id, 'value_ids': [(6, 0, attribute.value_ids.ids)]}))
        return self.env['product.template'].create(
            {'name': 'Variable product', 'woocommerce_id': '98000', 'woocommerce_site_url': self.connector.settings_woocommerce_connection_url, 'attribute_line_ids': attribute_lines}
        )

    def _link_variants(self, template, regular_prices):
        for index, (variant, regular_price) in enumerate(zip(template.product_variant_ids, regular_prices)):
            variant.write(
                {
                    'woocommerce_site_url': self.connector.settings_woocommerce_connection_url,
                    'woocommerce_id': str(98001 + index),
                    'woocommerce_regular_price': regular_price,
                    'woocommerce_tax_status': 'none',
                }
            )

    def test_single_attribute_variations_keep_their_own_price(self):
        template = self._template_with_variants(['Size'])
        self._link_variants(template, ['10.00', '15.00'])

        self.connector.odoo_product_variant_prices_update(template, woocommerce_prices_include_tax=False)

        self.assertEqual(template.list_price, 10.0)
        self.assertEqual(sorted(template.product_variant_ids.mapped('lst_price')), [10.0, 15.0])

    def test_multi_attribute_variations_use_lowest_price(self):
        template = self._template_with_variants(['Size', 'Color'])
        self._link_variants(template, ['12.00', '10.00', '14.00', '16.00'])

        self.connector.odoo_product_variant_prices_update(template, woocommerce_prices_include_tax=False)

        self.assertEqual(template.list_price, 10.0)
        self.assertFalse(any(template.mapped('attribute_line_ids.product_template_value_ids.price_extra')))

    def test_list_price_uses_regular_price_not_sale_price(self):
        values = {'woocommerce_regular_price': '20.00', 'woocommerce_price': '15.00', 'woocommerce_tax_rate': 0.0}
        self.assertEqual(self.connector.woocommerce_list_price_to_odoo_price(values, {'tax_status': 'taxable'}, False), 20.0)

    def test_list_price_only_converted_for_taxable_products(self):
        values = {'woocommerce_regular_price': '120.00', 'woocommerce_price': '120.00', 'woocommerce_tax_rate': 20.0}
        self.assertAlmostEqual(self.connector.woocommerce_list_price_to_odoo_price(values, {'tax_status': 'taxable'}, True), 100.0)
        self.assertEqual(self.connector.woocommerce_list_price_to_odoo_price(values, {'tax_status': 'none'}, True), 120.0)

    def test_exported_price_converted_to_store_tax_convention(self):
        tax_values = {'company_id': self.env.company.id, 'name': 'Export test 20%', 'amount': 20.0, 'amount_type': 'percent', 'type_tax_use': 'sale'}
        if 'price_include_override' in self.env['account.tax']._fields:
            tax_values['price_include_override'] = 'tax_excluded'
        else:
            tax_values['price_include'] = False
        tax = self.env['account.tax'].create(tax_values)

        self.assertEqual(self.connector.odoo_price_to_woocommerce_price(100.0, tax, woocommerce_prices_include_tax=True), '120.00')
        self.assertEqual(self.connector.odoo_price_to_woocommerce_price(100.0, tax, woocommerce_prices_include_tax=False), '100.00')
        self.assertEqual(self.connector.odoo_price_to_woocommerce_price(100.0, self.env['account.tax'], woocommerce_prices_include_tax=True), '100.00')

    def test_export_uses_tax_engine_for_mixed_grouped_and_fixed_taxes(self):
        def tax(name, amount, included=False, **values):
            include_values = {'price_include_override': 'tax_included' if included else 'tax_excluded'} if 'price_include_override' in self.env['account.tax']._fields else {'price_include': included}
            return self.env['account.tax'].create({'name': name, 'amount': amount, 'amount_type': 'percent', 'type_tax_use': 'sale', **include_values, **values})

        mixed = tax('Included 10%', 10, True) | tax('Excluded 20%', 20)
        fixed = tax('Fixed charge', 3, amount_type='fixed')
        group = tax('Grouped mixed taxes', 0, amount_type='group', children_tax_ids=[(6, 0, mixed.ids)])
        for taxes in (mixed, group, fixed):
            expected = taxes.with_context(round=False).compute_all(110, currency=self.env.company.currency_id)
            for included in (False, True):
                self.assertAlmostEqual(float(self.connector.odoo_price_to_woocommerce_price(110, taxes, included)), expected['total_included' if included else 'total_excluded'], places=2)


@tagged('post_install', '-at_install')
class TestProductIdentity(WoocommerceSyncCommon):
    def _sync_product(self, woocommerce_product):
        return self.connector.woocommerce_to_odoo_product_sync(
            woocommerce_product,
            woocommerce_currency=None,
            woocommerce_tax_rates={},
            woocommerce_prices_include_tax=False,
            woocommerce_weight_unit='kg',
            woocommerce_dimension_unit='cm',
            odoo_products={},
        )

    def test_first_import_links_existing_product_by_sku(self):
        existing_product = self.env['product.template'].create({'name': 'Existing Odoo product', 'default_code': 'LINK-SKU-1'})

        self._sync_product(make_woocommerce_product_payload(id=98101, sku='LINK-SKU-1'))

        products = self.env['product.template'].with_context(active_test=False).search([('default_code', '=', 'LINK-SKU-1')])
        self.assertEqual(products, existing_product)
        self.assertEqual(existing_product.woocommerce_id, '98101')

    def test_sku_match_ignores_another_company_product(self):
        company = self.env['res.company'].create({'name': 'SKU other company'})
        foreign_product = self.env['product.template'].with_company(company).create({'name': 'Foreign product', 'default_code': 'COMPANY-SKU', 'company_id': company.id})
        self._sync_product(make_woocommerce_product_payload(id=98104, sku='COMPANY-SKU', tax_status='none'))
        self.assertFalse(foreign_product.woocommerce_id)
        imported = self.env['product.template'].search([('woocommerce_id', '=', '98104')])
        self.assertTrue(imported)
        self.assertNotEqual(imported, foreign_product)

    def test_ambiguous_sku_creates_a_new_product(self):
        self.env['product.template'].create([{'name': 'Duplicate A', 'default_code': 'LINK-SKU-2'}, {'name': 'Duplicate B', 'default_code': 'LINK-SKU-2'}])

        self._sync_product(make_woocommerce_product_payload(id=98102, sku='LINK-SKU-2'))

        products = self.env['product.template'].with_context(active_test=False).search([('default_code', '=', 'LINK-SKU-2')])
        self.assertEqual(len(products), 3)
        self.assertEqual(products.filtered('woocommerce_id').woocommerce_id, '98102')

    def test_related_product_ids_are_parsed(self):
        self.assertEqual(self.connector.woocommerce_ids_parse('[12, 34]'), ['12', '34'])
        self.assertEqual(self.connector.woocommerce_ids_parse('not a list'), [])

        site_url = self.connector.settings_woocommerce_connection_url
        related_product = self.env['product.template'].create({'name': 'Related 12', 'woocommerce_site_url': site_url, 'woocommerce_id': '12'})
        self.env['product.template'].create({'name': 'Wrong match 1', 'woocommerce_site_url': site_url, 'woocommerce_id': '1'})
        main_product = self.env['product.template'].create({'name': 'Main', 'woocommerce_site_url': site_url, 'woocommerce_id': '98103', 'woocommerce_related_ids': [12]})

        self.connector.woocommerce_to_odoo_products_related_ids()

        self.assertEqual(main_product.optional_product_ids, related_product)
