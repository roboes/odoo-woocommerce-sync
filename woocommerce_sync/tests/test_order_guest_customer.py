"""Tests for the customer of WooCommerce guest orders ('customer_id' 0) in connector.py's 'woocommerce_to_odoo_order_sync()': a guest order must never be attached to an existing partner that merely has no WooCommerce ID (Odoo turns ('woocommerce_id', '=', 0) into "= '0' OR IS NULL"), only to the partner with the same billing email, or to a new one."""

from typing import ClassVar

from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon, make_woocommerce_order_payload


@tagged('post_install', '-at_install')
class TestOrderGuestCustomer(WoocommerceSyncCommon):
    connector_values: ClassVar[dict] = {'settings_woocommerce_orders_customers_map': True, 'settings_woocommerce_line_items_product_map': False}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Guest customer created by an earlier guest order: same site, no WooCommerce ID
        cls.earlier_guest = cls.env['res.partner'].create(
            {
                'name': 'Aaron Earlier Guest',
                'email': 'earlier.guest@example.test',
                'woocommerce_site_url': cls.connector.settings_woocommerce_connection_url,
            },
        )

    def _sync_order(self, woocommerce_order):
        self.connector.woocommerce_to_odoo_order_sync(woocommerce_order, woocommerce_tax_rates={}, woocommerce_weight_unit='kg', woocommerce_shipping_methods=[], odoo_sale_orders={})
        return self.env['sale.order'].search([('woocommerce_site_url', '=', self.connector.settings_woocommerce_connection_url), ('woocommerce_id', '=', str(woocommerce_order['id']))], limit=1)

    def _guest_order_payload(self, order_id: int, first_name: str, last_name: str, email: str):
        billing = {'first_name': first_name, 'last_name': last_name, 'email': email, 'city': 'Bages', 'postcode': '66670', 'country': ''}
        payload = make_woocommerce_order_payload(id=order_id, number=str(order_id), customer_id=0)
        payload['billing'].update(billing)
        return payload

    def test_guest_order_does_not_reuse_another_guest(self):
        odoo_sale_order = self._sync_order(self._guest_order_payload(98001, 'New', 'Guest', 'new.guest@example.test'))

        self.assertNotEqual(odoo_sale_order.partner_id, self.earlier_guest)
        self.assertEqual(odoo_sale_order.partner_id.email, 'new.guest@example.test')
        self.assertEqual(odoo_sale_order.partner_id.name, 'New Guest')
        self.assertFalse(odoo_sale_order.partner_id.parent_id)
        self.assertEqual(odoo_sale_order.partner_invoice_id, odoo_sale_order.partner_id)

    def test_guest_order_with_same_email_reuses_the_guest(self):
        odoo_sale_order = self._sync_order(self._guest_order_payload(98002, 'Aaron', 'Earlier Guest', 'earlier.guest@example.test'))

        self.assertEqual(odoo_sale_order.partner_id, self.earlier_guest)

    def test_guest_order_with_existing_guest_key_reuses_the_guest(self):
        # Guest whose email was edited in Odoo: the email match misses it and its guest key makes the creation fail, so the fallback must match it by guest key, not any partner without a WooCommerce ID
        same_guest = self.env['res.partner'].create(
            {
                'name': 'Zoe Same Guest',
                'email': 'zoe.edited@example.test',
                'woocommerce_site_url': self.connector.settings_woocommerce_connection_url,
                'woocommerce_guest_key': 'zoe.guest@example.test',
            },
        )

        odoo_sale_order = self._sync_order(self._guest_order_payload(98003, 'Zoe', 'Guest', 'Zoe.Guest@example.test'))

        self.assertEqual(odoo_sale_order.partner_id, same_guest)
