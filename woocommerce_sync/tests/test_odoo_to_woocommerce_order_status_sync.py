"""Tests for Odoo-to-WooCommerce order status export safety."""

from unittest.mock import MagicMock, patch

from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon


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
