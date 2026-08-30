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

    def _sync_order(self, woocommerce_order, odoo_sale_orders=None):
        self.connector.woocommerce_to_odoo_order_sync(woocommerce_order, woocommerce_tax_rates={}, woocommerce_weight_unit='kg', woocommerce_shipping_methods=[], odoo_sale_orders=odoo_sale_orders or {})
        return self.env['sale.order'].search([('woocommerce_site_url', '=', self.connector.settings_woocommerce_connection_url), ('woocommerce_id', '=', str(woocommerce_order['id']))], limit=1)

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

        # The first shipping line goes through 'set_delivery_line' instead of the manual loop
        first_line_as_order_line = odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '801')
        self.assertFalse(first_line_as_order_line, 'The first shipping line must not also be added by the extra-lines loop')

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
