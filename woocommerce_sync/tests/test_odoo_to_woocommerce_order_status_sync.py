"""Tests for Odoo-to-WooCommerce order status export safety."""

from unittest.mock import MagicMock, patch

from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon, storable_product_values


@tagged('post_install', '-at_install')
class TestOdooToWooCommerceOrderStatusSync(WoocommerceSyncCommon):
    def _create_confirmed_woocommerce_order(self, woocommerce_status: str = 'pending'):
        partner = self.env['res.partner'].create({'name': 'Test WooCommerce Customer'})
        product = self.env['product.product'].create({'name': 'Test Status Product', 'list_price': 10.0, 'taxes_id': [(6, 0, [])]})
        order = self.env['sale.order'].create(
            {
                'partner_id': partner.id,
                'woocommerce_id': '555',
                'woocommerce_site_url': self.connector.settings_woocommerce_connection_url,
                'woocommerce_status': woocommerce_status,
                'order_line': [(0, 0, {'product_id': product.id, 'product_uom_qty': 1, 'price_unit': 10.0})],
            }
        )
        order.action_confirm()

        return order

    def test_skips_push_when_remote_status_was_cancelled_or_refunded_since_last_import(self):
        order = self._create_confirmed_woocommerce_order(woocommerce_status='pending')
        woocommerce_api = MagicMock()

        for remote_status in ('cancelled', 'refunded'):
            with self.subTest(remote_status=remote_status):
                woocommerce_api.reset_mock()
                with (
                    patch.object(type(self.connector), 'woocommerce_api_get', return_value=woocommerce_api),
                    patch.object(type(self.connector), 'woocommerce_api_request', return_value={'id': 555, 'status': remote_status}) as request_mock,
                ):
                    self.connector.odoo_to_woocommerce_orders_status_chunk_sync(order.ids)

                request_mock.assert_called_once_with(woocommerce_api, endpoint='orders/555', params={'_fields': 'id,status'})
                woocommerce_api.batch.assert_not_called()
                self.assertEqual(order.woocommerce_status, 'pending')

    def test_pushes_status_when_remote_status_still_matches_last_import(self):
        order = self._create_confirmed_woocommerce_order(woocommerce_status='pending')
        woocommerce_api = MagicMock()
        woocommerce_api.batch.return_value = {'update': [{'id': 555, 'status': 'processing'}]}

        with (
            patch.object(type(self.connector), 'woocommerce_api_get', return_value=woocommerce_api),
            patch.object(type(self.connector), 'woocommerce_api_request', return_value={'id': 555, 'status': 'pending'}) as request_mock,
        ):
            self.connector.odoo_to_woocommerce_orders_status_chunk_sync(order.ids)

        request_mock.assert_called_once_with(woocommerce_api, endpoint='orders/555', params={'_fields': 'id,status'})
        woocommerce_api.batch.assert_called_once_with('orders', update=[{'id': '555', 'status': 'processing'}])
        self.assertEqual(order.woocommerce_status, 'processing')


@tagged('post_install', '-at_install')
class TestOrderCompletedOnDelivery(WoocommerceSyncCommon):
    def setUp(self):
        super().setUp()
        partner = self.env['res.partner'].create({'name': 'Test Delivery Customer'})
        product = self.env['product.product'].create({'name': 'Test Delivery Product', 'list_price': 10.0, 'taxes_id': [(6, 0, [])], **storable_product_values()})
        self.order = self.env['sale.order'].create(
            {
                'partner_id': partner.id,
                'woocommerce_id': '556',
                'woocommerce_site_url': self.connector.settings_woocommerce_connection_url,
                'woocommerce_status': 'processing',
                'order_line': [(0, 0, {'product_id': product.id, 'product_uom_qty': 1, 'price_unit': 10.0})],
            }
        )
        self.order.action_confirm()

    def deliver(self):
        for move in self.order.picking_ids.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        self.order.picking_ids._action_done()

    def test_delivered_order_stays_processing_by_default(self):
        self.deliver()

        self.assertEqual(self.connector.odoo_to_woocommerce_order_status_map(self.order), 'processing')

    def test_delivered_order_is_completed_when_option_enabled(self):
        self.connector.settings_odoo_to_woocommerce_orders_completed_on_delivery = True
        self.assertEqual(self.connector.odoo_to_woocommerce_order_status_map(self.order), 'processing', 'Not delivered yet')

        self.deliver()

        self.assertEqual(self.connector.odoo_to_woocommerce_order_status_map(self.order), 'completed')
