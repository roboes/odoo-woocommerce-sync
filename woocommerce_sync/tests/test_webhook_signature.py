"""Unit tests for the WooCommerce webhook signature validation in controllers/main.py."""

import base64
import hashlib
import hmac
import unittest

from odoo.addons.woocommerce_sync.controllers.main import (
    WoocommerceSyncWebhookController,
)


class TestWebhookSignatureValidation(unittest.TestCase):
    def _sign(self, raw_body: bytes, secret: str) -> str:
        return base64.b64encode(hmac.new(secret.encode('utf-8'), raw_body, hashlib.sha256).digest()).decode('utf-8')

    def test_valid_signature_is_accepted(self):
        raw_body = b'{"id": 123}'
        secret = 'super-secret'

        signature = self._sign(raw_body, secret)

        self.assertTrue(WoocommerceSyncWebhookController._signature_valid(raw_body, signature, secret))

    def test_signature_with_wrong_secret_is_rejected(self):
        raw_body = b'{"id": 123}'

        signature = self._sign(raw_body, 'correct-secret')

        self.assertFalse(WoocommerceSyncWebhookController._signature_valid(raw_body, signature, 'wrong-secret'))

    def test_tampered_body_is_rejected(self):
        secret = 'super-secret'
        signature = self._sign(b'{"id": 123}', secret)

        self.assertFalse(WoocommerceSyncWebhookController._signature_valid(b'{"id": 456}', signature, secret))

    def test_missing_signature_is_rejected(self):
        self.assertFalse(WoocommerceSyncWebhookController._signature_valid(b'{"id": 123}', '', 'some-secret'))


if __name__ == '__main__':
    unittest.main()
