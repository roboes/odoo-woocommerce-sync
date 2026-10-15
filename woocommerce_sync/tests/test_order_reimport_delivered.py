"""Tests that re-importing a WooCommerce order already delivered or invoiced in Odoo ('woocommerce_to_odoo_order_sync()' in connector.py) keeps its lines and only syncs its status.

Re-imports happen whenever WooCommerce changes the order (e.g. the 'completed' status pushed back from Odoo): rebuilding the lines of such an order failed on the delivered/invoiced delivery line, and the failing order then blocked the incremental orders sync.
"""

from typing import ClassVar

from odoo.release import version_info
from odoo.tests.common import tagged

from .common import (
    WoocommerceSyncCommon,
    bump_iso_datetime,
    make_woocommerce_order_payload,
)


@tagged('post_install', '-at_install')
class TestOrderReimportDelivered(WoocommerceSyncCommon):
    connector_values: ClassVar[dict] = {'settings_woocommerce_orders_customers_map': False, 'settings_woocommerce_line_items_product_map': False}

    def sync_order(self, woocommerce_order, odoo_sale_order=None):
        self.connector.woocommerce_to_odoo_order_sync(
            woocommerce_order,
            woocommerce_tax_rates={},
            woocommerce_weight_unit='kg',
            woocommerce_shipping_methods=[],
            odoo_sale_orders={str(woocommerce_order['id']): {'id': odoo_sale_order.id}} if odoo_sale_order else {},
        )
        return self.env['sale.order'].search([('woocommerce_site_url', '=', self.connector.settings_woocommerce_connection_url), ('woocommerce_id', '=', str(woocommerce_order['id']))], limit=1)

    def test_delivered_order_keeps_lines_and_gets_completed_status(self):
        woocommerce_order = make_woocommerce_order_payload(id=96001, number='96001', status='processing', shipping_lines=[{'id': 961, 'method_title': 'Shop pickup', 'total': '0.00'}])
        odoo_sale_order = self.sync_order(woocommerce_order)
        delivery_line = odoo_sale_order.order_line.filtered('is_delivery')
        delivery_line.qty_delivered = 1
        lines_before = odoo_sale_order.order_line

        woocommerce_order['status'] = 'completed'
        woocommerce_order['shipping_lines'][0]['total'] = '4.90'
        woocommerce_order['date_modified_gmt'] = bump_iso_datetime(woocommerce_order['date_modified_gmt'])
        odoo_sale_order = self.sync_order(woocommerce_order, odoo_sale_order)

        self.assertEqual(odoo_sale_order.order_line, lines_before)
        self.assertEqual(delivery_line.price_unit, 0.0)
        self.assertEqual(odoo_sale_order.woocommerce_status, 'completed')
        if version_info[0] in (18, 19):
            self.assertTrue(odoo_sale_order.locked)

    def test_undelivered_order_lines_are_still_updated(self):
        woocommerce_order = make_woocommerce_order_payload(id=96002, number='96002', status='processing', shipping_lines=[{'id': 962, 'method_title': 'Flat Rate', 'total': '5.00'}])
        odoo_sale_order = self.sync_order(woocommerce_order)

        woocommerce_order['shipping_lines'][0]['total'] = '7.50'
        woocommerce_order['date_modified_gmt'] = bump_iso_datetime(woocommerce_order['date_modified_gmt'])
        odoo_sale_order = self.sync_order(woocommerce_order, odoo_sale_order)

        self.assertEqual(odoo_sale_order.order_line.filtered('is_delivery').price_unit, 7.50)
