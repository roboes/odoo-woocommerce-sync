"""Tests for WooCommerce order fee lines/coupon lines being synced into real 'sale.order.line' rows (connector.py's 'woocommerce_to_odoo_order_sync'), instead of only the raw 'woocommerce_fee_lines'/'woocommerce_coupon_lines' JSON fields."""

from typing import ClassVar

from odoo.tests.common import tagged

from .common import (
    WoocommerceSyncCommon,
    bump_iso_datetime,
    make_woocommerce_order_payload,
)


@tagged('post_install', '-at_install')
class TestOrderFeeCouponLines(WoocommerceSyncCommon):
    connector_values: ClassVar[dict] = {'settings_woocommerce_orders_customers_map': False, 'settings_woocommerce_line_items_product_map': False}

    def _sync_order(self, woocommerce_order, odoo_sale_orders=None):
        self.connector.woocommerce_to_odoo_order_sync(woocommerce_order, woocommerce_tax_rates={}, woocommerce_weight_unit='kg', woocommerce_shipping_methods=[], odoo_sale_orders=odoo_sale_orders or {})
        return self.env['sale.order'].search([('woocommerce_site_url', '=', self.connector.settings_woocommerce_connection_url), ('woocommerce_id', '=', str(woocommerce_order['id']))], limit=1)

    def test_fee_line_creates_order_line(self):
        woocommerce_order = make_woocommerce_order_payload(id=93001, number='93001', fee_lines=[{'id': 501, 'name': 'Card surcharge', 'total': '2.50'}])

        odoo_sale_order = self._sync_order(woocommerce_order)

        fee_order_line = odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '501')
        self.assertTrue(fee_order_line, 'A fee line should have been created as a sale order line')
        self.assertEqual(fee_order_line.name, 'Card surcharge')
        self.assertEqual(fee_order_line.price_unit, 2.50)

    def test_coupon_line_creates_negative_order_line(self):
        woocommerce_order = make_woocommerce_order_payload(id=93002, number='93002', coupon_lines=[{'id': 601, 'code': 'SAVE10', 'discount': '10.00'}])

        odoo_sale_order = self._sync_order(woocommerce_order)

        coupon_order_line = odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '601')
        self.assertTrue(coupon_order_line, 'A coupon line should have been created as a sale order line')
        self.assertIn('SAVE10', coupon_order_line.name)
        self.assertEqual(coupon_order_line.price_unit, -10.00)

    def test_resyncing_order_updates_fee_line_instead_of_duplicating(self):
        woocommerce_order = make_woocommerce_order_payload(id=93003, number='93003', fee_lines=[{'id': 701, 'name': 'Card surcharge', 'total': '2.50'}])

        first_sale_order = self._sync_order(woocommerce_order)

        # Simulate the fee amount changing on a resync (e.g. a retried webhook/queue job), with a 'date_modified_gmt' newer than the first sync's 'write_date' so it isn't skipped as unmodified
        woocommerce_order['fee_lines'][0]['total'] = '3.75'
        woocommerce_order['date_modified_gmt'] = bump_iso_datetime(woocommerce_order['date_modified_gmt'])
        odoo_sale_order = self._sync_order(woocommerce_order, odoo_sale_orders={str(woocommerce_order['id']): {'id': first_sale_order.id}})

        self.assertEqual(odoo_sale_order, first_sale_order, 'Resyncing must update the existing order, not create a new one')

        fee_order_lines = odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '701')
        self.assertEqual(len(fee_order_lines), 1, 'Resyncing the same order must not duplicate the fee line')
        self.assertEqual(fee_order_lines.price_unit, 3.75)

    def test_resyncing_order_removes_missing_fee_line(self):
        woocommerce_order = make_woocommerce_order_payload(id=93004, number='93004', fee_lines=[{'id': 801, 'name': 'Temporary fee', 'total': '2.50'}])
        first_sale_order = self._sync_order(woocommerce_order)

        woocommerce_order['fee_lines'] = []
        woocommerce_order['date_modified_gmt'] = bump_iso_datetime(woocommerce_order['date_modified_gmt'])
        odoo_sale_order = self._sync_order(woocommerce_order, odoo_sale_orders={str(woocommerce_order['id']): {'id': first_sale_order.id}})

        self.assertFalse(odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '801'))
