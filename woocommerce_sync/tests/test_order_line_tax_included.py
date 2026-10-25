"""Tests for WooCommerce order shipping/fee lines on tax-included Odoo taxes (connector.py's 'woocommerce_order_line_total_to_odoo_price()'): WooCommerce reports their 'total' tax-excluded with the tax in 'total_tax', so the Odoo line must carry 'total' + 'total_tax' when all its taxes are tax-included (group taxes included), and 'total' alone when they are tax-excluded - in both cases the Odoo order and invoice totals match WooCommerce. Lines mixing both kinds keep 'total' and log a warning."""

from typing import ClassVar

from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon, make_woocommerce_order_payload


@tagged('post_install', '-at_install')
class TestOrderLineTaxIncluded(WoocommerceSyncCommon):
    connector_values: ClassVar[dict] = {'settings_woocommerce_orders_customers_map': False, 'settings_woocommerce_line_items_product_map': False}

    def _sync_order(self, woocommerce_order):
        self.connector.woocommerce_to_odoo_order_sync(woocommerce_order, woocommerce_tax_rates={}, woocommerce_weight_unit='kg', woocommerce_shipping_methods=[], odoo_sale_orders={})
        return self.env['sale.order'].search([('woocommerce_site_url', '=', self.connector.settings_woocommerce_connection_url), ('woocommerce_id', '=', str(woocommerce_order['id']))], limit=1)

    def _tax(self, amount: float, price_include: bool, **values):
        values = {'company_id': self.env.company.id, 'name': f'Test {amount:g}% {"incl." if price_include else "excl."}', 'amount': amount, 'type_tax_use': 'sale', **values}
        if 'price_include_override' in self.env['account.tax']._fields:
            values['price_include_override'] = 'tax_included' if price_include else 'tax_excluded'
        else:
            values['price_include'] = price_include
        return self.env['account.tax'].create(values)

    def _set_service_products_tax(self, taxes):
        for default_code, name in (('woocommerce_shipping_fee', 'WooCommerce Shipping Fee'), ('woocommerce_fee', 'WooCommerce Fee')):
            self.env['product.product'].create(
                {
                    'woocommerce_site_url': self.connector.settings_woocommerce_connection_url,
                    'name': name,
                    'default_code': default_code,
                    'type': 'service',
                    'taxes_id': [(6, 0, taxes.ids)],
                }
            )

    def _order_payload(self, order_id: int):
        return make_woocommerce_order_payload(
            id=order_id,
            number=str(order_id),
            total='9.90',
            total_tax='1.65',
            shipping_lines=[
                {'id': order_id * 10 + 1, 'method_title': 'Mondial Relay', 'total': '4.08', 'total_tax': '0.82'},
                {'id': order_id * 10 + 2, 'method_title': 'Express', 'total': '2.50', 'total_tax': '0.50'},
            ],
            fee_lines=[{'id': order_id * 10 + 3, 'name': 'Gift wrap', 'total': '1.67', 'total_tax': '0.33'}],
        )

    def test_tax_included_lines_get_their_tax_back(self):
        self._set_service_products_tax(self._tax(20.0, price_include=True))

        woocommerce_order = self._order_payload(97001)
        odoo_sale_order = self._sync_order(woocommerce_order)

        lines = {line.woocommerce_id: line for line in odoo_sale_order.order_line}
        self.assertTrue(lines['970011'].is_delivery)
        self.assertEqual(lines['970011'].price_unit, 4.90)
        self.assertEqual(lines['970011'].price_total, 4.90)
        self.assertEqual(lines['970012'].price_total, 3.00)
        self.assertEqual(lines['970013'].price_total, 2.00)
        self.assertAlmostEqual(odoo_sale_order.amount_total, float(woocommerce_order['total']), places=2)

        odoo_invoice = odoo_sale_order._create_invoices()
        self.assertAlmostEqual(odoo_invoice.amount_total, float(woocommerce_order['total']), places=2)

    def test_tax_excluded_lines_keep_the_net_total(self):
        self._set_service_products_tax(self._tax(20.0, price_include=False))

        woocommerce_order = self._order_payload(97002)
        odoo_sale_order = self._sync_order(woocommerce_order)

        lines = {line.woocommerce_id: line for line in odoo_sale_order.order_line}
        self.assertEqual(lines['970021'].price_unit, 4.08)
        self.assertEqual(lines['970021'].price_total, 4.90)
        self.assertEqual(lines['970022'].price_total, 3.00)
        self.assertEqual(lines['970023'].price_total, 2.00)
        self.assertAlmostEqual(odoo_sale_order.amount_total, float(woocommerce_order['total']), places=2)

    def test_tax_included_group_tax_children_get_their_tax_back(self):
        child_tax = self._tax(20.0, price_include=True, type_tax_use='none')
        group_tax = self.env['account.tax'].create({'company_id': self.env.company.id, 'name': 'Test 20% incl. group', 'amount_type': 'group', 'type_tax_use': 'sale', 'children_tax_ids': [(6, 0, child_tax.ids)]})
        self._set_service_products_tax(group_tax)

        woocommerce_order = self._order_payload(97003)
        odoo_sale_order = self._sync_order(woocommerce_order)

        self.assertEqual(odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '970031').price_unit, 4.90)
        self.assertAlmostEqual(odoo_sale_order.amount_total, float(woocommerce_order['total']), places=2)

    def test_mixed_tax_included_and_excluded_lines_keep_the_net_total(self):
        self._set_service_products_tax(self._tax(20.0, price_include=True) | self._tax(5.0, price_include=False))

        with self.assertLogs('odoo.addons.woocommerce_sync.models.connector', level='WARNING') as logs:
            odoo_sale_order = self._sync_order(self._order_payload(97004))

        lines = {line.woocommerce_id: line for line in odoo_sale_order.order_line}
        # WooCommerce's single 'total_tax' cannot be split between the included and excluded taxes, so the net total is kept instead of being taxed twice
        self.assertEqual(lines['970041'].price_unit, 4.08)
        self.assertEqual(lines['970042'].price_unit, 2.50)
        self.assertEqual(lines['970043'].price_unit, 1.67)
        self.assertTrue(any('both tax-included and tax-excluded' in message for message in logs.output))
