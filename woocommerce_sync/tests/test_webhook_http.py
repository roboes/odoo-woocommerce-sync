"""End-to-end test of the webhook route in controllers/main.py: a signed WooCommerce POST goes through Odoo's real HTTP stack, so request-wrapper issues (e.g. attributes Odoo's 'HTTPRequest' does not expose) are caught, unlike the unit tests calling '_signature_valid()' or 'woocommerce_webhook_process()' directly."""

import base64
import hashlib
import hmac
import json
from unittest.mock import patch

from odoo.tests.common import HttpCase, tagged

SECRET = 'webhook-test-secret'


@tagged('post_install', '-at_install')
class TestWebhookHttp(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connector = cls.env['woocommerce.sync.connector'].create(
            {
                'settings_woocommerce_connection_name': 'Webhook HTTP test',
                'settings_woocommerce_connection_url': 'https://example.test',
                'settings_woocommerce_consumer_key': 'ck_test',
                'settings_woocommerce_consumer_secret': 'cs_test',
                'settings_woocommerce_webhooks_enable': True,
            },
        )
        cls.connector.sudo().settings_woocommerce_webhooks_secret = SECRET

    def post_webhook(self, raw_body: bytes, secret: str = SECRET, topic: str = 'order.updated'):
        signature = base64.b64encode(hmac.new(secret.encode('utf-8'), raw_body, hashlib.sha256).digest()).decode('utf-8')
        headers = {'Content-Type': 'application/json', 'X-WC-Webhook-Signature': signature, 'X-WC-Webhook-Topic': topic, 'X-WC-Webhook-Delivery-ID': '1'}
        return self.url_open(f'/woocommerce_sync/webhook/{self.connector.id}', data=raw_body, headers=headers)

    def test_signed_webhook_is_accepted_and_dispatched(self):
        with patch.object(type(self.connector), 'woocommerce_webhook_process') as mocked_process:
            response = self.post_webhook(json.dumps({'id': 555}).encode('utf-8'))

        self.assertEqual(response.status_code, 200, response.text)
        mocked_process.assert_called_once_with('order.updated', 555, delivery_id='1')

    def test_wrong_signature_is_rejected(self):
        with patch.object(type(self.connector), 'woocommerce_webhook_process') as mocked_process:
            response = self.post_webhook(json.dumps({'id': 555}).encode('utf-8'), secret='wrong-secret')

        self.assertEqual(response.status_code, 401)
        mocked_process.assert_not_called()

    def test_oversized_payload_is_rejected(self):
        response = self.post_webhook(b'{"id": 1, "padding": "' + b'x' * (2 * 1024 * 1024) + b'"}')

        self.assertEqual(response.status_code, 413)

    def test_unsigned_ping_is_acknowledged(self):
        with patch.object(type(self.connector), 'woocommerce_webhook_process') as mocked_process:
            response = self.url_open(f'/woocommerce_sync/webhook/{self.connector.id}', data={'webhook_id': '19'})

        self.assertEqual(response.status_code, 200, response.text)
        mocked_process.assert_not_called()

    def test_unsigned_payload_is_still_rejected(self):
        response = self.url_open(f'/woocommerce_sync/webhook/{self.connector.id}', data=json.dumps({'id': 555}).encode('utf-8'), headers={'Content-Type': 'application/json', 'X-WC-Webhook-Topic': 'order.updated'})

        self.assertEqual(response.status_code, 401)
