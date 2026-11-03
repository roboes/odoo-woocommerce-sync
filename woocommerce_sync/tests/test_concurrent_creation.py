"""Tests for records created by a concurrent sync job ('woocommerce_unique_violation_retry()' in connector.py):
- A product or order missing from the lookup map passed to the job, but already in Odoo, is updated instead of failing on the unique WooCommerce ID index.
- A duplicate WooCommerce ID insert (the other job's transaction not committed yet) raises a 'RetryableJobError', so queue_job retries instead of failing the job.
"""

from typing import ClassVar
from unittest.mock import patch

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.tests.common import tagged
from odoo.tools import mute_logger

from .common import WoocommerceSyncCommon, make_woocommerce_order_payload, make_woocommerce_product_payload


def hide_woocommerce_id_lookups(model_class):
    """Makes 'search_read' on WooCommerce IDs find nothing, as when the other job's insert is not committed yet."""
    original_search_read = model_class.search_read

    def search_read(self, domain=None, *args, **kwargs):
        if any(isinstance(term, (list, tuple)) and term[0] == 'woocommerce_id' for term in domain or []):
            return []
        return original_search_read(self, domain, *args, **kwargs)

    return patch.object(model_class, 'search_read', search_read)


@tagged('post_install', '-at_install')
class TestConcurrentCreation(WoocommerceSyncCommon):
    connector_values: ClassVar[dict] = {'settings_woocommerce_orders_customers_map': False, 'settings_woocommerce_line_items_product_map': False}

    def _sync_product(self, woocommerce_product):
        return self.connector.woocommerce_to_odoo_product_sync(
            woocommerce_product,
            woocommerce_currency=None,
            woocommerce_tax_rates={},
            woocommerce_prices_include_tax=False,
            woocommerce_weight_unit='kg',
            woocommerce_dimension_unit='cm',
            odoo_products={},
        )

    def _sync_order(self, woocommerce_order):
        return self.connector.woocommerce_to_odoo_order_sync(woocommerce_order, woocommerce_tax_rates={}, woocommerce_weight_unit='kg', woocommerce_shipping_methods=[], odoo_sale_orders={})

    def _site_domain(self, woocommerce_id):
        return [('woocommerce_site_url', '=', self.connector.settings_woocommerce_connection_url), ('woocommerce_id', '=', str(woocommerce_id))]

    def test_product_created_since_the_job_was_enqueued_is_updated(self):
        existing_product = self.env['product.template'].create({'name': 'Created by the webhook', 'woocommerce_site_url': self.connector.settings_woocommerce_connection_url, 'woocommerce_id': '97001'})

        self._sync_product(make_woocommerce_product_payload(id=97001, sku='RACE-SKU-1', name='Synced by the chunk job'))

        products = self.env['product.template'].with_context(active_test=False).search(self._site_domain(97001))
        self.assertEqual(products, existing_product)
        self.assertEqual(existing_product.name, 'Synced by the chunk job')

    def test_product_duplicate_insert_is_retried(self):
        self.env['product.template'].create({'name': 'Not committed yet', 'woocommerce_site_url': self.connector.settings_woocommerce_connection_url, 'woocommerce_id': '97002'})

        with hide_woocommerce_id_lookups(type(self.env['product.template'])), mute_logger('odoo.sql_db'), self.assertRaises(RetryableJobError):
            self._sync_product(make_woocommerce_product_payload(id=97002, sku='RACE-SKU-2'))

        self.assertEqual(self.env['product.template'].with_context(active_test=False).search_count(self._site_domain(97002)), 1)

    def test_order_created_since_the_job_was_enqueued_is_updated(self):
        woocommerce_order = make_woocommerce_order_payload(id=97101)
        self._sync_order(woocommerce_order)
        existing_order = self.env['sale.order'].search(self._site_domain(97101))
        self.assertEqual(len(existing_order), 1)

        self._sync_order(woocommerce_order)

        self.assertEqual(self.env['sale.order'].search(self._site_domain(97101)), existing_order)

    def test_order_duplicate_insert_is_retried(self):
        woocommerce_order = make_woocommerce_order_payload(id=97102)
        self._sync_order(woocommerce_order)

        with hide_woocommerce_id_lookups(type(self.env['sale.order'])), mute_logger('odoo.sql_db', 'odoo.addons.woocommerce_sync.models.connector'), self.assertRaises(RetryableJobError):
            self._sync_order(woocommerce_order)

        self.assertEqual(self.env['sale.order'].search_count(self._site_domain(97102)), 1)

    def test_other_errors_are_not_retried(self):
        with patch.object(type(self.env['product.template']), 'create', side_effect=ValueError('boom')), mute_logger('odoo.addons.woocommerce_sync.models.connector'), self.assertRaises(ValueError):
            self._sync_product(make_woocommerce_product_payload(id=97003, sku='RACE-SKU-3'))
