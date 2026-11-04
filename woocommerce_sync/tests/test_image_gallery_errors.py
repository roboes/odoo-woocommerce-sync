"""Tests for database errors while storing gallery images: a skipped image must not leave the transaction aborted, and a concurrent update must reach queue_job so it retries the job."""

from unittest.mock import patch

import psycopg2
from odoo.tests.common import tagged
from odoo.tools import mute_logger

from .common import (
    WoocommerceSyncCommon,
    make_fake_image_download,
    make_woocommerce_product_payload,
)


@tagged('post_install', '-at_install')
class TestImageGalleryErrors(WoocommerceSyncCommon):
    def setUp(self):
        super().setUp()
        self.product = self.env['product.template'].create({'name': 'Gallery Product'})
        self.images = [{'src': 'https://example.test/a.jpg', 'name': 'a'}, {'src': 'https://example.test/b.jpg', 'name': 'b'}]
        self.image_model = 'base_multi_image.image' if 'base_multi_image.image' in self.env else 'ir.attachment'

    def test_failed_query_skips_the_image_without_aborting_the_transaction(self):
        real_create = type(self.env[self.image_model]).create
        calls = []

        def create(records, values):
            calls.append(values)
            if len(calls) == 1:
                records.env.cr.execute('SELECT 1 / 0')
            return real_create(records, values)

        with (
            patch.object(type(self.connector), 'image_download', make_fake_image_download()),
            patch.object(type(self.env[self.image_model]), 'create', create),
            mute_logger('odoo.sql_db'),
            self.assertLogs('odoo.addons.woocommerce_sync.models.connector', level='ERROR'),
        ):
            self.connector.image_process_attachments(self.images, self.product)

        self.assertEqual(len(calls), 2, 'The second image should still be processed after the first one failed')
        self.product.write({'name': 'Gallery Product renamed'})
        self.env.flush_all()
        self.assertEqual(self.product.name, 'Gallery Product renamed', 'The transaction should still be usable after a skipped image')

    def test_failed_product_write_is_raised_not_logged_as_image_error(self):
        site_url = self.connector.settings_woocommerce_connection_url
        self.env['product.template'].create({'name': 'Linked Product', 'woocommerce_site_url': site_url, 'woocommerce_id': '91001'})
        self.env.flush_all()
        self.product.write({'woocommerce_site_url': site_url, 'woocommerce_id': '91001'})
        image_download = make_fake_image_download()
        raised = None

        # Not 'assertRaises()': Odoo's version opens a flushing savepoint, which would write the pending change before the call
        with patch.object(type(self.connector), 'image_download', image_download), mute_logger('odoo.sql_db'):
            try:
                with self.env.cr.savepoint(flush=False):
                    self.connector.image_process_attachments(self.images, self.product)
            except psycopg2.errors.UniqueViolation as error:
                raised = error

        self.assertIsNotNone(raised, 'The failed product write should reach the product sync, not be logged as an image error')
        image_download.assert_not_called()

    def test_concurrent_update_is_raised_for_queue_job_retry(self):
        for error_class in (psycopg2.errors.SerializationFailure, psycopg2.errors.DeadlockDetected, psycopg2.errors.LockNotAvailable):
            with (
                self.subTest(error=error_class.__name__),
                patch.object(type(self.connector), 'image_download', make_fake_image_download()),
                patch.object(type(self.env[self.image_model]), 'create', side_effect=error_class('Concurrent gallery write')),
                self.assertRaises(error_class),
            ):
                self.connector.image_process_attachments(self.images, self.product)

    def test_gallery_concurrency_errors_escape_the_chunk_for_queue_job_retry(self):
        self.connector.settings_woocommerce_images_sync = True
        product = make_woocommerce_product_payload(id=91002, sku='TEST-GALLERY-RETRY', images=[self.images[0], self.images[1]])

        for error_class in (psycopg2.errors.SerializationFailure, psycopg2.errors.DeadlockDetected, psycopg2.errors.LockNotAvailable):
            with (
                self.subTest(error=error_class.__name__),
                patch.object(type(self.connector), 'image_download', make_fake_image_download()),
                patch.object(type(self.env[self.image_model]), 'create', side_effect=error_class('Concurrent gallery write')) as image_create,
                patch.object(type(self.connector), 'sync_summary_chunk_completed') as summary,
                mute_logger('odoo.addons.woocommerce_sync.models.connector'),
            ):
                with self.assertRaises(error_class):
                    self.connector.woocommerce_to_odoo_products_chunk_sync(
                        [product],
                        woocommerce_currency=None,
                        woocommerce_tax_rates={},
                        woocommerce_prices_include_tax=False,
                        woocommerce_weight_unit='kg',
                        woocommerce_dimension_unit='cm',
                        odoo_products={},
                    )

                image_create.assert_called_once()
                summary.assert_not_called()
                self.assertFalse(self.env['product.template'].search([('woocommerce_site_url', '=', self.connector.settings_woocommerce_connection_url), ('woocommerce_id', '=', '91002')]))
