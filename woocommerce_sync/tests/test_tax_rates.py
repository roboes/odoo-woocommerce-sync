"""Tests for WooCommerce tax rate handling in connector.py:
- 'odoo_tax_rate_create_or_retrieve()' reuses an existing percentage Odoo tax with the same rate even when its name differs (e.g. French '20% G', German '19% USt'), instead of creating a bare '20%' tax, and ranks same-rate taxes by scope, tax account, default sale tax and name.
- 'woocommerce_tax_rates_retrieve()' keeps the rate matching the store's country/state (e.g. 'BR:SP') when a tax class holds rates for several locations.
- Decimal tax rates (e.g. 5.5%) are stored without truncation.
"""

from unittest.mock import MagicMock

from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon, make_woocommerce_product_payload


@tagged('post_install', '-at_install')
class TestTaxRates(WoocommerceSyncCommon):
    def create_sale_tax(self, name: str, amount: float, **values):
        price_include = self.connector.odoo_tax_calculation_price_include()
        if 'price_include_override' in self.env['account.tax']._fields:
            values['price_include_override'] = 'tax_included' if price_include else 'tax_excluded'
        else:
            values['price_include'] = price_include
        return self.env['account.tax'].create({'company_id': self.env.company.id, 'name': name, 'amount': amount, 'type_tax_use': 'sale', **values})

    def woocommerce_api_mock(self, store_location: str, tax_rates: list[dict]) -> MagicMock:
        woocommerce_api = MagicMock()
        woocommerce_api.get.return_value.json.return_value = {'value': store_location}
        woocommerce_api.get_all_items.return_value = tax_rates
        return woocommerce_api

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
        self.assertEqual(odoo_tax.price_include, self.connector.odoo_tax_calculation_price_include())

    def test_fixed_amount_tax_is_not_matched(self):
        self.create_sale_tax('7.7 EUR eco-fee', 7.7, amount_type='fixed')

        odoo_tax = self.connector.odoo_tax_rate_create_or_retrieve(7.7)

        self.assertEqual(odoo_tax.amount_type, 'percent')
        self.assertEqual(odoo_tax.name, '7.7%')

    def test_tax_scope_is_preferred(self):
        goods_tax = self.create_sale_tax('7.7% G', 7.7, tax_scope='consu')
        services_tax = self.create_sale_tax('7.7% S', 7.7, tax_scope='service')

        self.assertEqual(self.connector.odoo_tax_rate_create_or_retrieve(7.7, tax_scope='consu'), goods_tax)
        self.assertEqual(self.connector.odoo_tax_rate_create_or_retrieve(7.7, tax_scope='service'), services_tax)

    def test_tax_with_account_is_preferred_over_exact_name(self):
        tax_account = self.env.company.account_sale_tax_id.invoice_repartition_line_ids.filtered(lambda line: line.repartition_type == 'tax').account_id[:1]
        if not tax_account:
            self.skipTest('No chart of accounts with a default sale tax account installed')

        self.create_sale_tax('7.7%', 7.7)
        localized_tax = self.create_sale_tax('7.7% USt', 7.7)
        localized_tax.invoice_repartition_line_ids.filtered(lambda line: line.repartition_type == 'tax').account_id = tax_account
        localized_tax.refund_repartition_line_ids.filtered(lambda line: line.repartition_type == 'tax').account_id = tax_account

        self.assertEqual(self.connector.odoo_tax_rate_create_or_retrieve(7.7), localized_tax)

    def test_store_country_rate_wins_per_tax_class(self):
        woocommerce_api = self.woocommerce_api_mock(
            'FR:75',
            [
                {'country': 'FR', 'rate': '20.0000', 'class': 'standard'},
                {'country': 'BE', 'rate': '21.0000', 'class': 'standard'},
                {'country': '', 'rate': '5.5000', 'class': 'reduced-rate'},
                {'country': 'DE', 'rate': '7.0000', 'class': 'reduced-rate'},
            ],
        )

        self.assertEqual(self.connector.woocommerce_tax_rates_retrieve(woocommerce_api), {'standard': 20.0, 'reduced-rate': 5.5})

    def test_store_state_rate_wins_per_tax_class(self):
        woocommerce_api = self.woocommerce_api_mock(
            'BR:SP',
            [
                {'country': 'BR', 'state': '', 'rate': '17.0000', 'class': 'standard'},
                {'country': 'BR', 'state': 'RJ', 'rate': '20.0000', 'class': 'standard'},
                {'country': 'BR', 'state': 'SP', 'rate': '18.0000', 'class': 'standard'},
                {'country': 'BR', 'state': 'RJ', 'rate': '12.0000', 'class': 'reduced-rate'},
            ],
        )

        # A class without any rate for the store's state or country keeps the old "any rate" fallback
        self.assertEqual(self.connector.woocommerce_tax_rates_retrieve(woocommerce_api), {'standard': 18.0, 'reduced-rate': 12.0})

    def test_unrestricted_first_rate_wins_per_tax_class(self):
        woocommerce_api = self.woocommerce_api_mock(
            'DE',
            [
                {'country': 'DE', 'state': '', 'postcodes': ['27498'], 'cities': [], 'rate': '0.0000', 'class': 'standard', 'priority': 1, 'order': 0},
                {'country': 'DE', 'state': '', 'postcodes': [], 'cities': [], 'rate': '19.0000', 'class': 'standard', 'priority': 1, 'order': 1},
                {'country': 'DE', 'state': '', 'postcodes': [], 'cities': [], 'rate': '16.0000', 'class': 'standard', 'priority': 1, 'order': 2},
            ],
        )

        self.assertEqual(self.connector.woocommerce_tax_rates_retrieve(woocommerce_api), {'standard': 19.0})

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
