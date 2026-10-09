"""Tests for WooCommerce tax rate handling in connector.py:
- 'odoo_tax_rate_create_or_retrieve()' reuses an existing Odoo tax with the same rate even when its name differs (e.g. French '20% G'), instead of creating a bare '20%' tax.
- 'woocommerce_tax_rates_retrieve()' keeps the store country's rate when a tax class holds rates for several countries.
- Decimal tax rates (e.g. 5.5%) are stored without truncation.
"""

from unittest.mock import MagicMock

from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon, make_woocommerce_product_payload


@tagged('post_install', '-at_install')
class TestTaxRates(WoocommerceSyncCommon):
    def create_sale_tax(self, name: str, amount: float):
        return self.env['account.tax'].create(
            {
                'company_id': self.env.company.id,
                'name': name,
                'amount': amount,
                'type_tax_use': 'sale',
                'price_include': self.connector.odoo_tax_calculation_price_include(),
            },
        )

    def test_existing_tax_with_different_name_is_reused(self):
        existing_tax = self.create_sale_tax('7.7% G', 7.7)
        tax_count = self.env['account.tax'].search_count([])

        odoo_tax = self.connector.odoo_tax_rate_create_or_retrieve(7.7)

        self.assertEqual(odoo_tax, existing_tax)
        self.assertEqual(self.env['account.tax'].search_count([]), tax_count, 'No new tax should be created when a tax with the same rate already exists')

    def test_tax_with_exact_name_is_preferred(self):
        self.create_sale_tax('7.7% G', 7.7)
        exact_name_tax = self.create_sale_tax('7.7%', 7.7)

        self.assertEqual(self.connector.odoo_tax_rate_create_or_retrieve(7.7), exact_name_tax)

    def test_company_default_sale_tax_is_preferred(self):
        self.create_sale_tax('7.7% S', 7.7)
        default_tax = self.create_sale_tax('7.7% G', 7.7)
        self.env.company.account_sale_tax_id = default_tax

        self.assertEqual(self.connector.odoo_tax_rate_create_or_retrieve(7.7), default_tax)

    def test_missing_tax_is_created(self):
        odoo_tax = self.connector.odoo_tax_rate_create_or_retrieve(7.7)

        self.assertTrue(odoo_tax)
        self.assertEqual(odoo_tax.name, '7.7%')
        self.assertEqual(odoo_tax.amount, 7.7)

    def test_store_country_rate_wins_per_tax_class(self):
        woocommerce_api = MagicMock()
        woocommerce_api.get.return_value.json.return_value = {'value': 'FR:75'}
        woocommerce_api.get_all_items.return_value = [
            {'country': 'FR', 'rate': '20.0000', 'class': 'standard'},
            {'country': 'BE', 'rate': '21.0000', 'class': 'standard'},
            {'country': '', 'rate': '5.5000', 'class': 'reduced-rate'},
            {'country': 'DE', 'rate': '7.0000', 'class': 'reduced-rate'},
        ]

        self.assertEqual(self.connector.woocommerce_tax_rates_retrieve(woocommerce_api), {'standard': 20.0, 'reduced-rate': 5.5})

    def test_decimal_tax_rate_is_not_truncated(self):
        odoo_products = {}
        self.connector.woocommerce_to_odoo_product_sync(
            make_woocommerce_product_payload(id=92001, sku='TEST-TAX-DECIMAL', tax_status='taxable', tax_class='reduced-rate'),
            woocommerce_currency=None,
            woocommerce_tax_rates={'reduced-rate': 5.5},
            woocommerce_prices_include_tax=False,
            woocommerce_weight_unit='kg',
            woocommerce_dimension_unit='cm',
            odoo_products=odoo_products,
        )

        odoo_product = self.env['product.template'].search([('default_code', '=', 'TEST-TAX-DECIMAL')])
        self.assertEqual(odoo_product.woocommerce_tax_rate, 5.5)
        self.assertEqual(odoo_product.taxes_id.amount, 5.5)
