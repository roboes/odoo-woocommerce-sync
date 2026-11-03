"""Tests for products in the WooCommerce trash ('woocommerce_webhook_product_sync()' in connector.py):
- A product trashed in WooCommerce and already in Odoo is archived with the 'trash' status instead of failing on the status selection.
- A product trashed before it ever reached Odoo is not imported.
"""

from unittest.mock import MagicMock, patch

from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon, make_woocommerce_product_payload

WEBHOOK_SETTINGS = {
    'woocommerce_currency': None,
    'woocommerce_tax_rates': {},
    'woocommerce_prices_include_tax': False,
    'woocommerce_weight_unit': 'kg',
    'woocommerce_dimension_unit': 'cm',
}


@tagged('post_install', '-at_install')
class TestWebhookTrashedProducts(WoocommerceSyncCommon):
    def _webhook_product_sync(self, woocommerce_product):
        woocommerce_api = MagicMock()
        woocommerce_api.request.return_value = woocommerce_product
        with (
            patch.object(type(self.connector), 'woocommerce_api_get', return_value=woocommerce_api),
            patch.object(type(self.connector), 'woocommerce_webhook_settings_retrieve', return_value=WEBHOOK_SETTINGS) as settings_retrieve,
        ):
            self.connector.woocommerce_webhook_product_sync(woocommerce_product['id'])
        return settings_retrieve

    def _products(self, woocommerce_id):
        return self.env['product.template'].with_context(active_test=False).search([('woocommerce_site_url', '=', self.connector.settings_woocommerce_connection_url), ('woocommerce_id', '=', str(woocommerce_id))])

    def test_trashed_product_already_in_odoo_is_archived(self):
        self._webhook_product_sync(make_woocommerce_product_payload(id=96001, sku='TRASH-SKU-1', status='publish'))
        product = self._products(96001)
        self.assertTrue(product.active)

        self._webhook_product_sync(make_woocommerce_product_payload(id=96001, sku='TRASH-SKU-1', status='trash', date_modified_gmt='2099-01-01T00:00:00'))

        self.assertEqual(self._products(96001), product)
        self.assertFalse(product.active)
        self.assertEqual(product.woocommerce_status, 'trash')

    def test_trashed_product_not_in_odoo_is_not_imported(self):
        settings_retrieve = self._webhook_product_sync(make_woocommerce_product_payload(id=96002, sku='TRASH-SKU-2', status='trash'))

        self.assertFalse(self._products(96002))
        settings_retrieve.assert_not_called()

    def test_trashed_variable_product_skips_variation_sync(self):
        self._webhook_product_sync(make_woocommerce_product_payload(id=96004, sku='TRASH-VARIABLE-1', type='variable'))
        self.connector.settings_woocommerce_to_odoo_products_variations_sync = True

        with patch.object(type(self.connector), 'woocommerce_to_odoo_product_variations_sync') as variation_sync:
            self._webhook_product_sync(
                make_woocommerce_product_payload(
                    id=96004,
                    sku='TRASH-VARIABLE-1',
                    type='variable',
                    status='trash',
                    date_modified_gmt='2099-01-01T00:00:00',
                )
            )

        product = self._products(96004)
        self.assertFalse(product.active)
        self.assertEqual(product.woocommerce_status, 'trash')
        variation_sync.assert_not_called()

    def test_scheduled_product_is_imported_archived(self):
        self._webhook_product_sync(make_woocommerce_product_payload(id=96003, sku='TRASH-SKU-3', status='future'))

        product = self._products(96003)
        self.assertTrue(product)
        self.assertFalse(product.active)
        self.assertEqual(product.woocommerce_status, 'future')
