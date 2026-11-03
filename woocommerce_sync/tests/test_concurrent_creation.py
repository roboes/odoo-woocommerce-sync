"""Tests for records created by a concurrent sync job (polling chunk vs. webhook):
- The chunk jobs refresh their lookup map, read when the chunk was enqueued, so a product or order created since then is updated instead of failing on the unique WooCommerce ID index.
- A duplicate insert on a WooCommerce unique index (the concurrent record not visible to this transaction) raises a 'RetryableJobError' ('woocommerce_unique_violation_retry()' in connector.py), so queue_job retries instead of failing the job.
"""

from typing import ClassVar
from unittest.mock import patch

import psycopg2
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.woocommerce_sync.models.connector import (
    woocommerce_unique_violation_retry,
)
from odoo.tests.common import tagged
from odoo.tools import mute_logger

from .common import (
    WoocommerceSyncCommon,
    make_woocommerce_customer_payload,
    make_woocommerce_order_payload,
    make_woocommerce_product_payload,
)


def hide_woocommerce_id_lookups(model_class, method_name='search_read'):
    """Makes WooCommerce ID lookups find nothing, as when the concurrent job's record is not visible to this transaction's snapshot."""
    original_method = getattr(model_class, method_name)

    def method(self, domain=None, *args, **kwargs):
        if any(isinstance(term, (list, tuple)) and term[0] == 'woocommerce_id' and term[2] for term in domain or []):
            return [] if method_name == 'search_read' else self.browse()
        return original_method(self, domain, *args, **kwargs)

    return patch.object(model_class, method_name, method)


@tagged('post_install', '-at_install')
class TestConcurrentCreation(WoocommerceSyncCommon):
    connector_values: ClassVar[dict] = {'settings_woocommerce_orders_customers_map': False, 'settings_woocommerce_line_items_product_map': False}

    def _sync_products_chunk(self, woocommerce_products):
        self.connector.woocommerce_to_odoo_products_chunk_sync(
            woocommerce_products,
            woocommerce_currency=None,
            woocommerce_tax_rates={},
            woocommerce_prices_include_tax=False,
            woocommerce_weight_unit='kg',
            woocommerce_dimension_unit='cm',
            odoo_products={},
        )

    def _sync_orders_chunk(self, woocommerce_orders):
        self.connector.woocommerce_to_odoo_orders_chunk_sync(woocommerce_orders, woocommerce_tax_rates={}, woocommerce_weight_unit='kg', woocommerce_shipping_methods=[], odoo_sale_orders={})

    def _site_domain(self, woocommerce_id):
        return [('woocommerce_site_url', '=', self.connector.settings_woocommerce_connection_url), ('woocommerce_id', '=', str(woocommerce_id))]

    def test_product_created_since_the_chunk_was_enqueued_is_updated(self):
        existing_product = self.env['product.template'].create({'name': 'Created by the webhook', 'woocommerce_site_url': self.connector.settings_woocommerce_connection_url, 'woocommerce_id': '97001'})

        self._sync_products_chunk([make_woocommerce_product_payload(id=97001, sku='RACE-SKU-1', name='Synced by the chunk job')])

        products = self.env['product.template'].with_context(active_test=False).search(self._site_domain(97001))
        self.assertEqual(products, existing_product)
        self.assertEqual(existing_product.name, 'Synced by the chunk job')

    def test_product_duplicate_insert_is_retried(self):
        self.env['product.template'].create({'name': 'Not visible yet', 'woocommerce_site_url': self.connector.settings_woocommerce_connection_url, 'woocommerce_id': '97002'})

        with hide_woocommerce_id_lookups(type(self.env['product.template'])), mute_logger('odoo.sql_db'), self.assertRaises(RetryableJobError):
            self._sync_products_chunk([make_woocommerce_product_payload(id=97002, sku='RACE-SKU-2')])

        self.assertEqual(self.env['product.template'].with_context(active_test=False).search_count(self._site_domain(97002)), 1)

    def test_order_created_since_the_chunk_was_enqueued_is_updated(self):
        woocommerce_order = make_woocommerce_order_payload(id=97101)
        self._sync_orders_chunk([woocommerce_order])
        existing_order = self.env['sale.order'].search(self._site_domain(97101))
        self.assertEqual(len(existing_order), 1)

        self._sync_orders_chunk([woocommerce_order])

        self.assertEqual(self.env['sale.order'].search(self._site_domain(97101)), existing_order)

    def test_order_duplicate_insert_is_retried(self):
        woocommerce_order = make_woocommerce_order_payload(id=97102)
        self._sync_orders_chunk([woocommerce_order])

        with hide_woocommerce_id_lookups(type(self.env['sale.order'])), mute_logger('odoo.sql_db'), self.assertRaises(RetryableJobError):
            self._sync_orders_chunk([woocommerce_order])

        self.assertEqual(self.env['sale.order'].search_count(self._site_domain(97102)), 1)

    def test_customer_duplicate_insert_is_retried(self):
        self.env['res.partner'].create({'name': 'Not visible yet', 'woocommerce_site_url': self.connector.settings_woocommerce_connection_url, 'woocommerce_id': '97201'})

        with hide_woocommerce_id_lookups(type(self.env['res.partner']), 'search'), mute_logger('odoo.sql_db'), self.assertRaises(RetryableJobError):
            self.connector.woocommerce_to_odoo_customer_sync(make_woocommerce_customer_payload(id=97201, email='race-customer@example.test'), {})

        self.assertEqual(self.env['res.partner'].with_context(active_test=False).search_count(self._site_domain(97201)), 1)

    def test_archived_customer_with_same_woocommerce_id_is_reused(self):
        archived_customer = self.env['res.partner'].create({'name': 'Archived', 'active': False, 'woocommerce_site_url': self.connector.settings_woocommerce_connection_url, 'woocommerce_id': '97202'})

        with mute_logger('odoo.sql_db'):
            self.connector.woocommerce_to_odoo_customer_sync(make_woocommerce_customer_payload(id=97202, email='archived-customer@example.test'), {})

        self.assertEqual(self.env['res.partner'].with_context(active_test=False).search(self._site_domain(97202)), archived_customer)

    def test_only_woocommerce_unique_indexes_are_retried(self):
        site_url = self.connector.settings_woocommerce_connection_url
        self.env['res.partner'].create({'name': 'Guest', 'woocommerce_site_url': site_url, 'woocommerce_guest_key': 'race-guest@example.test'})
        with mute_logger('odoo.sql_db'), self.assertRaises(psycopg2.errors.UniqueViolation) as guest_key_violation, self.env.cr.savepoint():
            self.env['res.partner'].create({'name': 'Guest', 'woocommerce_site_url': site_url, 'woocommerce_guest_key': 'race-guest@example.test'})
        with self.assertRaises(RetryableJobError):
            woocommerce_unique_violation_retry(guest_key_violation.exception, 'WooCommerce guest')

        self.env.cr.execute('CREATE TEMPORARY TABLE woocommerce_unrelated_unique_test (value integer UNIQUE)')
        self.env.cr.execute('INSERT INTO woocommerce_unrelated_unique_test VALUES (1)')
        with mute_logger('odoo.sql_db'), self.assertRaises(psycopg2.errors.UniqueViolation) as unrelated_violation, self.env.cr.savepoint():
            self.env.cr.execute('INSERT INTO woocommerce_unrelated_unique_test VALUES (1)')
        woocommerce_unique_violation_retry(unrelated_violation.exception, 'unrelated record')

    def test_other_errors_are_not_retried(self):
        with patch.object(type(self.env['product.template']), 'create', side_effect=ValueError('boom')), mute_logger('odoo.addons.woocommerce_sync.models.connector'), self.assertRaises(ValueError):
            self.connector.woocommerce_to_odoo_product_sync(
                make_woocommerce_product_payload(id=97003, sku='RACE-SKU-3'),
                woocommerce_currency=None,
                woocommerce_tax_rates={},
                woocommerce_prices_include_tax=False,
                woocommerce_weight_unit='kg',
                woocommerce_dimension_unit='cm',
                odoo_products={},
            )
