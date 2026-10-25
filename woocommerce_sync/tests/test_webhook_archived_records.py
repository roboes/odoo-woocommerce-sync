"""Tests for the record lookup of the webhook jobs in connector.py ('woocommerce_webhook_product_sync()', 'woocommerce_webhook_customer_sync()'): a record archived in Odoo (e.g. a WooCommerce draft product, imported archived) must be found and updated like the scheduled sync does, not created again (unique index on (woocommerce_site_url, woocommerce_id))."""

from unittest.mock import MagicMock, patch

from odoo.tests.common import tagged

from .common import (
    WoocommerceSyncCommon,
    bump_iso_datetime,
    make_woocommerce_customer_payload,
    make_woocommerce_product_payload,
)

WEBHOOK_SETTINGS = {
    'woocommerce_currency': 'EUR',
    'woocommerce_weight_unit': 'kg',
    'woocommerce_dimension_unit': 'cm',
    'woocommerce_prices_include_tax': False,
    'woocommerce_tax_rates': {},
    'woocommerce_shipping_methods': [],
}


@tagged('post_install', '-at_install')
class TestWebhookArchivedRecords(WoocommerceSyncCommon):
    def _run_webhook_job(self, method_name: str, woocommerce_payload: dict) -> None:
        woocommerce_api = MagicMock()
        woocommerce_api.request.return_value = woocommerce_payload

        with (
            patch.object(type(self.connector), 'woocommerce_api_get', return_value=woocommerce_api),
            patch.object(type(self.connector), 'woocommerce_webhook_settings_retrieve', return_value=WEBHOOK_SETTINGS),
        ):
            getattr(self.connector, method_name)(woocommerce_payload['id'])

    def _odoo_records(self, model_name: str, woocommerce_id: int):
        return self.env[model_name].with_context(active_test=False).search([('woocommerce_site_url', '=', self.connector.settings_woocommerce_connection_url), ('woocommerce_id', '=', str(woocommerce_id))])

    def test_draft_product_published_later_is_updated_not_created_again(self):
        woocommerce_product = make_woocommerce_product_payload(id=92001, sku='TEST-WEBHOOK-DRAFT', status='draft')
        self._run_webhook_job('woocommerce_webhook_product_sync', woocommerce_product)

        odoo_product = self._odoo_records('product.template', 92001)
        self.assertEqual(len(odoo_product), 1)
        self.assertFalse(odoo_product.active, 'A WooCommerce draft product is imported archived')

        woocommerce_product.update(status='publish', date_modified_gmt=bump_iso_datetime(woocommerce_product['date_modified_gmt']))
        self._run_webhook_job('woocommerce_webhook_product_sync', woocommerce_product)

        self.assertEqual(self._odoo_records('product.template', 92001), odoo_product)
        self.assertTrue(odoo_product.active, 'Publishing the WooCommerce product reactivates the archived Odoo product')

    def test_archived_customer_is_updated_not_created_again(self):
        woocommerce_customer = make_woocommerce_customer_payload(id=93001, email='archived.customer@example.test', first_name='Archived', last_name='Customer')
        self._run_webhook_job('woocommerce_webhook_customer_sync', woocommerce_customer)

        odoo_customer = self._odoo_records('res.partner', 93001)
        self.assertEqual(len(odoo_customer), 1)
        odoo_customer.active = False

        woocommerce_customer.update(last_name='Renamed', date_modified_gmt=bump_iso_datetime(woocommerce_customer['date_modified_gmt']))
        self._run_webhook_job('woocommerce_webhook_customer_sync', woocommerce_customer)

        self.assertEqual(self._odoo_records('res.partner', 93001), odoo_customer)
        self.assertIn('Renamed', odoo_customer.name)
