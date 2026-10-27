"""Tests for WooCommerce order line prices and taxes ('woocommerce_to_odoo_order_sync()' in connector.py):
- Coupon discounts are carried by the line 'discount' column (WooCommerce line 'price' is already discounted), not deducted a second time.
- The tax rate actually charged on each line ('tax_lines[].rate_percent', matched by 'taxes[].id') is used instead of the store's base rate for the tax class, and lines without tax get no Odoo tax.
"""

from typing import ClassVar

from odoo.release import version_info
from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon, make_woocommerce_order_payload


def make_line_item(**overrides):
    line_item = {
        'id': 1,
        'name': 'Item',
        'product_id': 0,
        'variation_id': 0,
        'quantity': 1,
        'tax_class': '',
        'subtotal': '100.00',
        'subtotal_tax': '0.00',
        'total': '100.00',
        'total_tax': '0.00',
        'taxes': [],
        'meta_data': [],
        'sku': '',
        'price': 100.0,
    }
    line_item.update(overrides)
    return line_item


@tagged('post_install', '-at_install')
class TestOrderLinePricingTaxes(WoocommerceSyncCommon):
    connector_values: ClassVar[dict] = {'settings_woocommerce_orders_customers_map': False, 'settings_woocommerce_line_items_product_map': False, 'settings_odoo_tax_calculation': 'tax_excluded'}

    def _sync_order(self, woocommerce_order, woocommerce_tax_rates=None):
        self.connector.woocommerce_to_odoo_order_sync(woocommerce_order, woocommerce_tax_rates=woocommerce_tax_rates or {}, woocommerce_weight_unit='kg', woocommerce_shipping_methods=[], odoo_sale_orders={})
        return self.env['sale.order'].search([('woocommerce_site_url', '=', self.connector.settings_woocommerce_connection_url), ('woocommerce_id', '=', str(woocommerce_order['id']))], limit=1)

    @staticmethod
    def _line_taxes(order_line):
        return order_line.tax_id if version_info[0] in (16, 18) else order_line.tax_ids

    def test_discounted_line_uses_undiscounted_price_and_discount(self):
        woocommerce_order = make_woocommerce_order_payload(id=96001, number='96001', line_items=[make_line_item(id=11, quantity=2, subtotal='50.00', total='40.00', price=20.0)])

        odoo_sale_order = self._sync_order(woocommerce_order)

        order_line = odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '11')
        self.assertEqual(order_line.price_unit, 25.0)
        self.assertEqual(order_line.discount, 20.0)
        self.assertAlmostEqual(odoo_sale_order.amount_untaxed, 40.0)

    def test_discount_not_reproducible_keeps_net_price(self):
        # A 1-cent coupon on 1000.00 is a 0.001% discount, rounded to 0.00% by the 'Discount' precision: the discounted net price is kept instead
        woocommerce_order = make_woocommerce_order_payload(id=96002, number='96002', line_items=[make_line_item(id=12, quantity=1, subtotal='1000.00', total='999.99', price=999.99)])

        odoo_sale_order = self._sync_order(woocommerce_order)

        order_line = odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '12')
        self.assertEqual(order_line.discount, 0.0)
        self.assertAlmostEqual(order_line.price_unit, 999.99)
        self.assertAlmostEqual(odoo_sale_order.amount_untaxed, 999.99)

    def test_line_uses_rate_actually_charged(self):
        # Store base rate 20% (e.g. France), but the customer was charged 19% (e.g. EU OSS sale to Germany)
        woocommerce_order = make_woocommerce_order_payload(
            id=96003,
            number='96003',
            line_items=[make_line_item(id=13, total_tax='19.00', subtotal_tax='19.00', taxes=[{'id': 5, 'total': '19.00', 'subtotal': '19.00'}])],
            tax_lines=[{'id': 900, 'rate_code': 'DE-VAT-1', 'rate_id': 5, 'label': 'VAT', 'compound': False, 'tax_total': '19.00', 'shipping_tax_total': '0.00', 'rate_percent': 19.0, 'meta_data': []}],
        )

        odoo_sale_order = self._sync_order(woocommerce_order, woocommerce_tax_rates={'standard': 20.0})

        order_line = odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '13')
        self.assertEqual(self._line_taxes(order_line).mapped('amount'), [19.0])
        self.assertAlmostEqual(odoo_sale_order.amount_tax, 19.0)

    def test_untaxed_line_gets_no_tax(self):
        woocommerce_order = make_woocommerce_order_payload(id=96004, number='96004', line_items=[make_line_item(id=14)])

        odoo_sale_order = self._sync_order(woocommerce_order, woocommerce_tax_rates={'standard': 20.0})

        order_line = odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '14')
        self.assertFalse(self._line_taxes(order_line), 'A line WooCommerce did not tax must not get an Odoo tax')

    def test_line_without_rate_percent_falls_back_to_class_rate(self):
        # Orders created before WooCommerce 5.7 have no 'rate_percent' on their tax lines
        woocommerce_order = make_woocommerce_order_payload(
            id=96005,
            number='96005',
            line_items=[make_line_item(id=15, total_tax='20.00', subtotal_tax='20.00', taxes=[{'id': 7, 'total': '20.00', 'subtotal': '20.00'}])],
            tax_lines=[{'id': 901, 'rate_code': 'FR-TVA-1', 'rate_id': 7, 'label': 'TVA', 'compound': False, 'tax_total': '20.00', 'shipping_tax_total': '0.00', 'meta_data': []}],
        )

        odoo_sale_order = self._sync_order(woocommerce_order, woocommerce_tax_rates={'standard': 20.0})

        order_line = odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '15')
        self.assertEqual(self._line_taxes(order_line).mapped('amount'), [20.0])

    def _check_stacked_taxes(self, rates, amounts, compound):
        for index, included in enumerate((False, True)):
            with self.subTest(tax_included=included):
                self.connector.settings_odoo_tax_calculation = 'tax_included' if included else 'tax_excluded'
                total_tax = sum(amounts)
                order_id = 96010 + index
                woocommerce_order = make_woocommerce_order_payload(
                    id=order_id,
                    line_items=[
                        make_line_item(
                            id=order_id,
                            total_tax=str(total_tax),
                            subtotal_tax=str(total_tax),
                            taxes=[{'id': i + 1, 'total': str(amount), 'subtotal': str(amount)} for i, amount in enumerate(amounts)],
                        )
                    ],
                    tax_lines=[{'rate_id': i + 1, 'rate_percent': rate, 'compound': compound[i], 'label': f'Tax {i + 1}'} for i, rate in enumerate(rates)],
                )
                order = self._sync_order(woocommerce_order)
                taxes = self._line_taxes(order.order_line.filtered(lambda line, order_id=order_id: line.woocommerce_id == str(order_id)))
                self.assertEqual(len(taxes), len(rates))
                self.assertAlmostEqual(order.amount_untaxed, 100.0, places=2)
                self.assertAlmostEqual(order.amount_tax, total_tax, places=2)

    def test_equal_rate_components_are_not_collapsed(self):
        self._check_stacked_taxes([5.0, 5.0], [5.0, 5.0], [False, False])

    def test_compound_taxes_include_previous_taxes(self):
        self._check_stacked_taxes([5.0, 10.0, 2.0], [5.0, 10.5, 2.31], [False, True, True])


@tagged('post_install', '-at_install')
class TestOrderLinePricingTaxIncluded(WoocommerceSyncCommon):
    connector_values: ClassVar[dict] = {'settings_woocommerce_orders_customers_map': False, 'settings_woocommerce_line_items_product_map': False, 'settings_odoo_tax_calculation': 'tax_included'}

    def test_tax_included_discounted_line_matches_woocommerce_net_total(self):
        woocommerce_order = make_woocommerce_order_payload(
            id=96006,
            number='96006',
            line_items=[make_line_item(id=16, subtotal='100.00', total='90.00', price=90.0, total_tax='17.10', subtotal_tax='19.00', taxes=[{'id': 5, 'total': '17.10', 'subtotal': '19.00'}])],
            tax_lines=[{'id': 902, 'rate_code': 'DE-VAT-1', 'rate_id': 5, 'label': 'VAT', 'compound': False, 'tax_total': '17.10', 'shipping_tax_total': '0.00', 'rate_percent': 19.0, 'meta_data': []}],
        )

        self.connector.woocommerce_to_odoo_order_sync(woocommerce_order, woocommerce_tax_rates={'standard': 19.0}, woocommerce_weight_unit='kg', woocommerce_shipping_methods=[], odoo_sale_orders={})
        odoo_sale_order = self.env['sale.order'].search([('woocommerce_id', '=', '96006')], limit=1)

        order_line = odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '16')
        self.assertAlmostEqual(order_line.price_unit, 119.0)
        self.assertEqual(order_line.discount, 10.0)
        self.assertAlmostEqual(odoo_sale_order.amount_untaxed, 90.0, places=2)
        self.assertAlmostEqual(odoo_sale_order.amount_total, 107.1, places=2)
