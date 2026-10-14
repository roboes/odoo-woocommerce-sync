"""HTTP controller receiving WooCommerce webhooks for near-real-time order/product/customer sync, complementing the regular polling-based sync."""

import base64
import hashlib
import hmac
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class WoocommerceSyncWebhookController(http.Controller):
    @http.route('/woocommerce_sync/webhook/<int:connector_id>', type='http', auth='public', methods=['POST'], csrf=False, save_session=False)
    def woocommerce_webhook(self, connector_id, **kwargs):
        connector = request.env['woocommerce.sync.connector'].sudo().browse(connector_id).exists()

        if not connector or not connector.settings_woocommerce_webhooks_enable:
            return request.make_json_response({'error': 'Webhook not found or not enabled'}, status=404)

        maximum_payload_bytes = 2 * 1024 * 1024
        if request.httprequest.content_length and request.httprequest.content_length > maximum_payload_bytes:
            return request.make_json_response({'error': 'Payload too large'}, status=413)

        # Odoo's 'HTTPRequest' wrapper does not expose werkzeug's 'stream' (only 'get_data()', 'headers', ...)
        raw_body = request.httprequest.get_data(cache=False)
        if len(raw_body) > maximum_payload_bytes:
            return request.make_json_response({'error': 'Payload too large'}, status=413)
        signature = request.httprequest.headers.get('X-WC-Webhook-Signature', '')

        # WooCommerce pings the delivery URL with an unsigned 'webhook_id=<id>' form (parsed into kwargs by Odoo) and keeps re-pinging after every delivery until a ping gets a 200
        if not signature and kwargs.keys() == {'webhook_id'} and kwargs['webhook_id'].isdigit():
            return request.make_json_response({'status': 'ok'})

        if not connector.settings_woocommerce_webhooks_secret or not self._signature_valid(raw_body, signature, connector.settings_woocommerce_webhooks_secret):
            _logger.warning(f'WooCommerce webhook signature validation failed for connector {connector_id}')
            return request.make_json_response({'error': 'Invalid signature'}, status=401)

        topic = request.httprequest.headers.get('X-WC-Webhook-Topic', '')
        if topic not in connector.WEBHOOK_TOPICS:
            return request.make_json_response({'error': 'Unsupported webhook topic'}, status=400)

        try:
            payload = json.loads(raw_body)
        except ValueError:
            return request.make_json_response({'error': 'Invalid JSON payload'}, status=400)

        resource_id = payload.get('id') if isinstance(payload, dict) else None

        if not isinstance(resource_id, int) or isinstance(resource_id, bool) or resource_id <= 0:
            return request.make_json_response({'error': 'Invalid resource ID'}, status=400)

        delivery_id = request.httprequest.headers.get('X-WC-Webhook-Delivery-ID')
        connector.woocommerce_webhook_process(topic, resource_id, delivery_id=delivery_id)

        return request.make_json_response({'status': 'ok'})

    @staticmethod
    def _signature_valid(raw_body: bytes, signature: str, secret: str) -> bool:
        """Validates the WooCommerce 'X-WC-Webhook-Signature' header: base64-encoded HMAC-SHA256 of the raw request body, keyed with the webhook's secret."""
        if not signature:
            return False

        computed_signature = base64.b64encode(hmac.new(secret.encode('utf-8'), raw_body, hashlib.sha256).digest()).decode('utf-8')

        return hmac.compare_digest(computed_signature, signature)
