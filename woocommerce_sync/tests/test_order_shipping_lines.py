"""Tests for WooCommerce orders with multiple shipping lines (connector.py's 'woocommerce_to_odoo_order_sync'): the first shipping line uses the native 'set_delivery_line' and any additional shipping lines are added as extra 'sale.order.line' rows instead of being silently dropped, keyed by 'woocommerce_id' so a resync updates them instead of duplicating them."""

from typing import ClassVar

from odoo.tests.common import tagged

from .common import (
    WoocommerceSyncCommon,
    bump_iso_datetime,
    make_woocommerce_order_payload,
)


@tagged('post_install', '-at_install')
class TestOrderMultipleShippingLines(WoocommerceSyncCommon):
    connector_values: ClassVar[dict] = {'settings_woocommerce_orders_customers_map': False, 'settings_woocommerce_line_items_product_map': False}

    def setUp(self):
        super().setUp()
        if 'fiscal_operation_id' in self.env['sale.order']._fields:
            self.connector = self.connector.with_context(default_fiscal_operation_id=False)

    def _sync_order(self, woocommerce_order, odoo_sale_orders=None):
        self.connector.woocommerce_to_odoo_order_sync(woocommerce_order, woocommerce_tax_rates={}, woocommerce_weight_unit='kg', woocommerce_shipping_methods=[], odoo_sale_orders=odoo_sale_orders or {})
        return self.env['sale.order'].search([('woocommerce_site_url', '=', self.connector.settings_woocommerce_connection_url), ('woocommerce_id', '=', str(woocommerce_order['id']))], limit=1)

    def test_real_cost_carrier_preserves_woocommerce_shipping_total(self):
        carriers = self.env['delivery.carrier']
        invoice_policy = carriers._fields.get('invoice_policy')
        if not invoice_policy or 'real' not in invoice_policy.get_values(self.env):
            self.skipTest('This Odoo version has no real-cost carrier invoice policy')

        product = self.env['product.product'].create({'name': 'Real Cost Shipping', 'type': 'service'})
        carrier = carriers.create(
            {
                'name': 'Real Cost Shipping',
                'woocommerce_site_url': self.connector.settings_woocommerce_connection_url,
                'product_id': product.id,
                'delivery_type': 'fixed',
                'invoice_policy': 'real',
            }
        )
        woocommerce_order = make_woocommerce_order_payload(
            id=94005,
            number='94005',
            shipping_lines=[{'id': 1201, 'method_title': carrier.name, 'total': '4.08'}],
        )

        odoo_sale_order = self._sync_order(woocommerce_order)

        self.assertTrue(odoo_sale_order, 'Importing an order with a real-cost carrier must succeed')
        self.assertEqual(odoo_sale_order.carrier_id, carrier)
        delivery_line = odoo_sale_order.order_line.filtered(lambda line: line.is_delivery and line.woocommerce_id == '1201')
        self.assertEqual(len(delivery_line), 1)
        self.assertAlmostEqual(delivery_line.price_unit, 4.08)

    def test_extra_shipping_lines_are_added_as_order_lines(self):
        woocommerce_order = make_woocommerce_order_payload(
            id=94001,
            number='94001',
            shipping_lines=[
                {'id': 801, 'method_title': 'Flat Rate', 'total': '5.00'},
                {'id': 802, 'method_title': 'Local Pickup', 'total': '3.00'},
            ],
        )

        odoo_sale_order = self._sync_order(woocommerce_order)

        extra_shipping_line = odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '802')
        self.assertTrue(extra_shipping_line, 'The second shipping line should have been added as an order line')
        self.assertEqual(extra_shipping_line.name, 'Local Pickup')
        self.assertEqual(extra_shipping_line.price_unit, 3.00)

        # The first shipping line goes through 'set_delivery_line' and is marked as connector-owned
        first_line_as_order_line = odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '801')
        self.assertEqual(len(first_line_as_order_line), 1, 'The first shipping line must be marked without being duplicated by the extra-lines loop')
        self.assertTrue(first_line_as_order_line.is_delivery)

    def test_resyncing_order_updates_extra_shipping_line_instead_of_duplicating(self):
        woocommerce_order = make_woocommerce_order_payload(
            id=94002,
            number='94002',
            shipping_lines=[
                {'id': 901, 'method_title': 'Flat Rate', 'total': '5.00'},
                {'id': 902, 'method_title': 'Express', 'total': '10.00'},
            ],
        )

        first_sale_order = self._sync_order(woocommerce_order)

        # Simulate the extra shipping line's price changing on a resync (e.g. a retried webhook/queue job)
        woocommerce_order['shipping_lines'][1]['total'] = '12.50'
        woocommerce_order['date_modified_gmt'] = bump_iso_datetime(woocommerce_order['date_modified_gmt'])
        odoo_sale_order = self._sync_order(woocommerce_order, odoo_sale_orders={str(woocommerce_order['id']): {'id': first_sale_order.id}})

        self.assertEqual(odoo_sale_order, first_sale_order, 'Resyncing must update the existing order, not create a new one')

        extra_shipping_lines = odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '902')
        self.assertEqual(len(extra_shipping_lines), 1, 'Resyncing the same order must not duplicate the extra shipping line')
        self.assertEqual(extra_shipping_lines.price_unit, 12.50)

    def test_resyncing_order_removes_shipping_lines_missing_from_woocommerce(self):
        woocommerce_order = make_woocommerce_order_payload(
            id=94003,
            number='94003',
            shipping_lines=[
                {'id': 1001, 'method_title': 'Flat Rate', 'total': '5.00'},
                {'id': 1002, 'method_title': 'Express', 'total': '10.00'},
            ],
        )
        first_sale_order = self._sync_order(woocommerce_order)

        woocommerce_order['shipping_lines'] = []
        woocommerce_order['date_modified_gmt'] = bump_iso_datetime(woocommerce_order['date_modified_gmt'])
        odoo_sale_order = self._sync_order(woocommerce_order, odoo_sale_orders={str(woocommerce_order['id']): {'id': first_sale_order.id}})

        self.assertFalse(odoo_sale_order.order_line.filtered(lambda line: line.is_delivery or line.woocommerce_id == '1002'))

    def test_resync_preserves_manual_delivery_line_when_adding_woocommerce_shipping(self):
        woocommerce_order = make_woocommerce_order_payload(id=94004, number='94004', status='pending', shipping_lines=[])
        odoo_sale_order = self._sync_order(woocommerce_order)
        manual_product = self.env['product.product'].create({'name': 'Manual Delivery Service', 'type': 'service'})
        manual_line = self.env['sale.order.line'].create(
            {
                'order_id': odoo_sale_order.id,
                'name': 'Manual delivery adjustment',
                'product_id': manual_product.id,
                'product_uom_qty': 1,
                'price_unit': 7.5,
                'is_delivery': True,
            }
        )

        woocommerce_order['shipping_lines'] = [{'id': 1101, 'method_title': 'Flat Rate', 'total': '5.00'}]
        woocommerce_order['date_modified_gmt'] = bump_iso_datetime(woocommerce_order['date_modified_gmt'])
        self._sync_order(woocommerce_order, odoo_sale_orders={str(woocommerce_order['id']): {'id': odoo_sale_order.id}})

        self.assertTrue(manual_line.exists())
        self.assertTrue(odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '1101' and line.is_delivery))

    def test_oca_fiscal_freight_is_charged_once_and_cleared_on_resync(self):
        if 'amount_freight_value' not in self.env['sale.order']._fields:
            self.skipTest('OCA l10n_br_sale is not installed')
        operation = self.env['l10n_br_fiscal.operation'].create({'code': 'WC_TEST', 'name': 'WooCommerce Test Sale', 'fiscal_operation_type': 'out', 'fiscal_type': 'sale'})
        self.connector.settings_woocommerce_line_items_product_map = True
        self.connector = self.connector.with_context(default_fiscal_operation_id=operation.id)
        line_items = []
        for index, price in enumerate(('72.90', '89.80'), start=1):
            self.env['product.template'].create({'name': f'Freight Test {index}', 'type': 'consu', 'woocommerce_id': str(94500 + index), 'woocommerce_site_url': self.connector.settings_woocommerce_connection_url})
            line_items.append(
                {
                    'id': 1300 + index,
                    'name': f'Freight Test {index}',
                    'product_id': 94500 + index,
                    'variation_id': 0,
                    'quantity': 1,
                    'tax_class': '',
                    'subtotal': price,
                    'total': price,
                    'subtotal_tax': '0.00',
                    'total_tax': '0.00',
                    'taxes': [],
                    'meta_data': [],
                    'sku': '',
                    'price': float(price),
                }
            )
        payload = make_woocommerce_order_payload(id=94006, status='pending', line_items=line_items, shipping_total='40.97', shipping_lines=[{'id': 1303, 'method_title': 'Test Freight', 'total': '40.97'}])
        order = self._sync_order(payload)

        self.assertTrue(order)
        self.assertFalse(order.order_line.filtered('is_delivery'))
        self.assertFalse(order.order_line.filtered(lambda line: line.woocommerce_id == '1303'))
        self.assertEqual(order.order_line.mapped('freight_value'), [18.36, 22.61])
        self.assertAlmostEqual(order.amount_total, 203.67)

        payload['line_items'][0].update({'total': '65.61', 'price': 65.61})
        payload['discount_total'] = '7.29'
        for freight in ('40.97', '0.01', '0.00'):
            payload['shipping_total'] = freight
            payload['shipping_lines'][0]['total'] = freight
            payload['date_modified_gmt'] = bump_iso_datetime(payload['date_modified_gmt'])
            self._sync_order(payload, {str(payload['id']): {'id': order.id}})
            self.assertAlmostEqual(sum(order.order_line.mapped('freight_value')), float(freight))
            self.assertAlmostEqual(order.amount_total, 155.41 + float(freight))
            self.assertEqual(len(order.order_line), 2)
