"""Regression test for the per-record savepoint fix in connector.py: a failure syncing one WooCommerce product within a chunk job must only roll back that product's own changes, not the changes already written for other products processed earlier in the same chunk (the bug this fix replaced used a whole-transaction 'self.env.cr.rollback()', which discarded everything done so far in the job)."""

from typing import ClassVar
from unittest.mock import patch

from odoo.tests.common import tagged

from .common import (
    WoocommerceSyncCommon,
    make_fake_image_download,
    make_woocommerce_product_payload,
)


@tagged('post_install', '-at_install')
class TestSavepointIsolation(WoocommerceSyncCommon):
    connector_values: ClassVar[dict] = {'settings_woocommerce_images_sync': True}

    def test_failure_on_second_product_does_not_roll_back_first_product(self):
        good_product = make_woocommerce_product_payload(id=90001, sku='TEST-GOOD-1', name='Good Product')

        # This one has images, so it reaches the (patched-to-fail) gallery image processing step after the product record is already created
        bad_product = make_woocommerce_product_payload(
            id=90002,
            sku='TEST-BAD-1',
            name='Bad Product',
            images=[{'src': 'https://example.test/a.jpg'}, {'src': 'https://example.test/b.jpg'}],
        )

        with (
            patch.object(type(self.connector), 'image_download', make_fake_image_download()),
            patch.object(type(self.connector), 'image_process_attachments', side_effect=RuntimeError('Simulated failure to test savepoint isolation')),
        ):
            self.connector.woocommerce_to_odoo_products_chunk_sync(
                [good_product, bad_product],
                woocommerce_currency=None,
                woocommerce_tax_rates={},
                woocommerce_prices_include_tax=False,
                woocommerce_weight_unit='kg',
                woocommerce_dimension_unit='cm',
                odoo_products={},
            )

        good_odoo_product = self.env['product.template'].search([('default_code', '=', 'TEST-GOOD-1')])
        bad_odoo_product = self.env['product.template'].search([('default_code', '=', 'TEST-BAD-1')])

        self.assertTrue(good_odoo_product, 'The product processed before the failure must survive the sibling failure (this is what the savepoint fix guarantees)')
        self.assertFalse(bad_odoo_product, "The product whose own sync raised must be rolled back by its own savepoint (it shouldn't be left half-created)")

    def test_successful_chunk_creates_all_products(self):
        products = [
            make_woocommerce_product_payload(id=90101, sku='TEST-CHUNK-1', name='Chunk Product 1'),
            make_woocommerce_product_payload(id=90102, sku='TEST-CHUNK-2', name='Chunk Product 2'),
            make_woocommerce_product_payload(id=90103, sku='TEST-CHUNK-3', name='Chunk Product 3'),
        ]

        self.connector.woocommerce_to_odoo_products_chunk_sync(
            products,
            woocommerce_currency=None,
            woocommerce_tax_rates={},
            woocommerce_prices_include_tax=False,
            woocommerce_weight_unit='kg',
            woocommerce_dimension_unit='cm',
            odoo_products={},
        )

        created_products = self.env['product.template'].search([('default_code', 'in', ['TEST-CHUNK-1', 'TEST-CHUNK-2', 'TEST-CHUNK-3'])])

        self.assertEqual(len(created_products), 3)
