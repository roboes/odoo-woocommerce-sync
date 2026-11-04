"""Tests for database errors while storing gallery images: a skipped image must not leave the transaction aborted, and a concurrent update must reach queue_job so it retries the job."""

from unittest.mock import patch

import psycopg2
from odoo.tests.common import tagged
from odoo.tools import mute_logger

from .common import WoocommerceSyncCommon, make_fake_image_download


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
        def create(records, values):
            raise psycopg2.errors.SerializationFailure('could not serialize access due to concurrent update')

        with (
            patch.object(type(self.connector), 'image_download', make_fake_image_download()),
            patch.object(type(self.env[self.image_model]), 'create', create),
            self.assertRaises(psycopg2.errors.SerializationFailure),
        ):
            self.connector.image_process_attachments(self.images, self.product)
