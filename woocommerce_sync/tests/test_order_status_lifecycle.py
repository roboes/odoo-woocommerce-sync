"""Tests for WooCommerce order status handling ('woocommerce_to_odoo_order_sync()' in connector.py, 'sale.order' overrides in models.py):
- An order cancelled in WooCommerce is cancelled in Odoo (Odoo 16/18 only open the cancellation wizard without the 'disable_cancel_warning' context).
- Custom/plugin WooCommerce order statuses no longer make the order import fail.
- Only WooCommerce orders bypass Odoo's restriction on deleting confirmed orders.
"""

from typing import ClassVar

from odoo.exceptions import UserError
from odoo.tests.common import tagged

from .common import (
    WoocommerceSyncCommon,
    bump_iso_datetime,
    make_woocommerce_order_payload,
)


def make_line_item(line_id: int) -> dict:
    return {
        'id': line_id,
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


@tagged('post_install', '-at_install')
class TestOrderStatusLifecycle(WoocommerceSyncCommon):
    connector_values: ClassVar[dict] = {'settings_woocommerce_orders_customers_map': False, 'settings_woocommerce_line_items_product_map': False}

    def _sync_order(self, woocommerce_order, odoo_sale_order=None):
        self.connector.woocommerce_to_odoo_order_sync(
            woocommerce_order,
            woocommerce_tax_rates={},
            woocommerce_weight_unit='kg',
            woocommerce_shipping_methods=[],
            odoo_sale_orders={str(woocommerce_order['id']): {'id': odoo_sale_order.id}} if odoo_sale_order else {},
        )
        return self.env['sale.order'].search([('woocommerce_site_url', '=', self.connector.settings_woocommerce_connection_url), ('woocommerce_id', '=', str(woocommerce_order['id']))], limit=1)

    def test_order_cancelled_in_woocommerce_is_cancelled_in_odoo(self):
        woocommerce_order = make_woocommerce_order_payload(id=97001, number='97001', status='processing', line_items=[make_line_item(21)])
        odoo_sale_order = self._sync_order(woocommerce_order)
        self.assertEqual(odoo_sale_order.state, 'sale')

        woocommerce_order['status'] = 'cancelled'
        woocommerce_order['date_modified_gmt'] = bump_iso_datetime(woocommerce_order['date_modified_gmt'])
        odoo_sale_order = self._sync_order(woocommerce_order, odoo_sale_order)

        self.assertEqual(odoo_sale_order.state, 'cancel')

    def test_custom_woocommerce_status_is_imported(self):
        woocommerce_order = make_woocommerce_order_payload(id=97002, number='97002', status='checkout-draft', line_items=[make_line_item(22)])

        odoo_sale_order = self._sync_order(woocommerce_order)

        self.assertTrue(odoo_sale_order, 'An order with a custom WooCommerce status must still be imported')
        self.assertEqual(odoo_sale_order.woocommerce_status, 'checkout-draft')
        self.assertEqual(odoo_sale_order.state, 'draft')

    def test_confirmed_woocommerce_order_can_be_deleted(self):
        odoo_sale_order = self._sync_order(make_woocommerce_order_payload(id=97003, number='97003', status='processing', line_items=[make_line_item(23)]))
        self.assertEqual(odoo_sale_order.state, 'sale')

        odoo_sale_order.unlink()

        self.assertFalse(odoo_sale_order.exists())

    def test_confirmed_odoo_order_keeps_deletion_restriction(self):
        partner = self.env['res.partner'].create({'name': 'Odoo-only customer'})
        product = self.env['product.product'].create({'name': 'Odoo-only product', 'list_price': 10.0, 'taxes_id': [(6, 0, [])]})
        odoo_sale_order = self.env['sale.order'].create({'partner_id': partner.id, 'order_line': [(0, 0, {'product_id': product.id, 'product_uom_qty': 1})]})
        odoo_sale_order.action_confirm()

        with self.assertRaises(UserError):
            odoo_sale_order.unlink()
