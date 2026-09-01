"""Tests for 'woocommerce_webhook_process()' in connector.py: verifies that an incoming webhook topic dispatches the correct 'with_delay()' job (with a stable 'identity_key' so retried/duplicate webhook deliveries don't enqueue duplicate jobs), instead of running the sync inline."""

from unittest.mock import MagicMock, patch

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon


@tagged('post_install', '-at_install')
class TestWebhookQueueJobDispatch(WoocommerceSyncCommon):
    def test_order_topic_dispatches_order_sync_job_with_identity_key(self):
        with patch.object(type(self.connector), 'with_delay', return_value=MagicMock()) as mocked_with_delay:
            self.connector.woocommerce_webhook_process('order.updated', 555)

        mocked_with_delay.assert_called_once_with(identity_key=f'woocommerce_webhook_order_sync-{self.connector.id}-555', description='woocommerce.sync.connector.woocommerce_webhook_order_sync')
        mocked_with_delay.return_value.woocommerce_webhook_order_sync.assert_called_once_with(555)

    def test_product_topic_dispatches_product_sync_job_with_identity_key(self):
        with patch.object(type(self.connector), 'with_delay', return_value=MagicMock()) as mocked_with_delay:
            self.connector.woocommerce_webhook_process('product.created', 777)

        mocked_with_delay.assert_called_once_with(identity_key=f'woocommerce_webhook_product_sync-{self.connector.id}-777', description='woocommerce.sync.connector.woocommerce_webhook_product_sync')
        mocked_with_delay.return_value.woocommerce_webhook_product_sync.assert_called_once_with(777)

    def test_customer_topic_dispatches_customer_sync_job_with_identity_key(self):
        with patch.object(type(self.connector), 'with_delay', return_value=MagicMock()) as mocked_with_delay:
            self.connector.woocommerce_webhook_process('customer.updated', 42)

        mocked_with_delay.assert_called_once_with(identity_key=f'woocommerce_webhook_customer_sync-{self.connector.id}-42', description='woocommerce.sync.connector.woocommerce_webhook_customer_sync')
        mocked_with_delay.return_value.woocommerce_webhook_customer_sync.assert_called_once_with(42)

    def test_missing_resource_id_does_not_dispatch_a_job(self):
        with patch.object(type(self.connector), 'with_delay', return_value=MagicMock()) as mocked_with_delay:
            self.connector.woocommerce_webhook_process('order.updated', 0)

        mocked_with_delay.assert_not_called()

    def test_unsupported_topic_does_not_dispatch_a_job(self):
        with patch.object(type(self.connector), 'with_delay', return_value=MagicMock()) as mocked_with_delay:
            self.connector.woocommerce_webhook_process('coupon.created', 555)

        mocked_with_delay.assert_not_called()

    def test_delivery_id_distinguishes_newer_webhook_jobs(self):
        with patch.object(type(self.connector), 'with_delay', return_value=MagicMock()) as mocked_with_delay:
            self.connector.woocommerce_webhook_process('product.updated', 777, delivery_id='delivery-2')

        mocked_with_delay.assert_called_once_with(identity_key=f'woocommerce_webhook_product_sync-{self.connector.id}-777-delivery-2', description='woocommerce.sync.connector.woocommerce_webhook_product_sync')

    def test_webhook_job_retries_when_api_configuration_is_unavailable(self):
        with patch.object(type(self.connector), 'woocommerce_api_get', return_value=False), self.assertRaises(RetryableJobError):
            self.connector.woocommerce_webhook_order_sync(555)

    def test_webhook_job_retries_invalid_resource_payload(self):
        woocommerce_api = MagicMock()
        woocommerce_api.request.return_value = {}

        with patch.object(type(self.connector), 'woocommerce_api_get', return_value=woocommerce_api), self.assertRaises(RetryableJobError):
            self.connector.woocommerce_webhook_product_sync(777)
