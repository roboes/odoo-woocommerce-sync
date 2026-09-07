"""Tests for building the WooCommerce product payload from an Odoo product (connector.py's 'odoo_to_woocommerce_product_values'), used by the Odoo -> WooCommerce batch sync. Mocks 'woocommerce_attribute_create_or_retrieve' so no real WooCommerce REST API calls are made (every Odoo product has a category, which would otherwise trigger a live HTTP call)."""

from base64 import b64encode
from unittest.mock import MagicMock, patch

import requests
from odoo.tests.common import tagged

from .common import FAKE_IMAGE_BYTES, WoocommerceSyncCommon, storable_product_values


@tagged('post_install', '-at_install')
class TestOdooToWooCommerceProductValues(WoocommerceSyncCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connector.write(
            {
                'settings_wordpress_username': 'test-user',
                'settings_wordpress_user_application_password': 'test-password',
            }
        )

    def test_wordpress_upload_image_uses_bounded_raw_media_requests(self):
        session = MagicMock()
        lookup_response = session.get.return_value
        lookup_response.json.return_value = []
        upload_response = session.post.return_value
        upload_response.json.return_value = {'id': 321}

        image_id = self.connector.wordpress_upload_image(b64encode(FAKE_IMAGE_BYTES), 'test-image', session=session)

        self.assertEqual(image_id, 321)
        session.get.assert_called_once_with(
            url='https://example.test/wp-json/wp/v2/media',
            params={'slug': 'test-image'},
            auth=session.get.call_args.kwargs['auth'],
            timeout=(5, 30),
        )
        upload_headers = session.post.call_args.kwargs['headers']
        self.assertEqual(upload_headers['Content-Type'], 'image/png')
        self.assertEqual(upload_headers['Content-Disposition'], 'attachment; filename="test-image.png"')
        self.assertEqual(session.post.call_args.kwargs['data'], FAKE_IMAGE_BYTES)
        self.assertEqual(session.post.call_args.kwargs['timeout'], (5, 30))
        lookup_response.raise_for_status.assert_called_once_with()
        upload_response.raise_for_status.assert_called_once_with()

    def test_wordpress_upload_image_stops_after_lookup_http_error(self):
        session = MagicMock()
        session.get.return_value.raise_for_status.side_effect = requests.HTTPError('lookup failed')

        image_id = self.connector.wordpress_upload_image(b64encode(FAKE_IMAGE_BYTES), 'test-image', session=session)

        self.assertIsNone(image_id)
        session.post.assert_not_called()

    def test_chunk_uses_stored_woocommerce_id_after_sku_change(self):
        odoo_product = self.env['product.template'].create(
            {
                'name': 'Renamed SKU Product',
                'default_code': 'NEW-SKU',
                'woocommerce_id': '123',
                'woocommerce_site_url': self.connector.settings_woocommerce_connection_url,
            }
        )
        woocommerce_api = MagicMock()
        remote_product = {'id': 123, 'sku': 'OLD-SKU', 'date_modified_gmt': '2000-01-01T00:00:00'}
        woocommerce_api.batch.return_value = {'update': [{'id': 123, 'sku': 'NEW-SKU'}]}

        with (
            patch.object(type(self.connector), 'woocommerce_api_get', return_value=woocommerce_api),
            patch.object(type(self.connector), 'woocommerce_api_request', return_value=remote_product) as mocked_request,
            patch.object(type(self.connector), 'woocommerce_api_get_all_items') as mocked_collection_request,
            patch.object(type(self.connector), 'odoo_to_woocommerce_product_values', return_value={'sku': 'NEW-SKU'}),
            patch.object(type(self.connector), 'woocommerce_product_fields', return_value={'woocommerce_id': '123'}),
        ):
            self.connector.odoo_to_woocommerce_products_chunk_sync(odoo_product.ids, 'USD', {}, False, 'kg', 'cm')

        mocked_request.assert_called_once_with(woocommerce_api, endpoint='products/123')
        mocked_collection_request.assert_not_called()
        self.assertEqual(woocommerce_api.batch.call_args.kwargs['update'][0]['id'], 123)

    def test_chunk_falls_back_to_sku_when_mapped_product_deleted_remotely(self):
        odoo_product = self.env['product.template'].create(
            {
                'name': 'Dangling Mapping Product',
                'default_code': 'DANGLING-SKU',
                'woocommerce_id': '999',
                'woocommerce_site_url': self.connector.settings_woocommerce_connection_url,
            }
        )
        woocommerce_api = MagicMock()
        not_found_error = requests.HTTPError(response=MagicMock(status_code=404))
        woocommerce_api.batch.return_value = {'create': [{'id': 1001, 'sku': 'DANGLING-SKU'}]}

        with (
            patch.object(type(self.connector), 'woocommerce_api_get', return_value=woocommerce_api),
            patch.object(type(self.connector), 'woocommerce_api_request', side_effect=not_found_error) as mocked_request,
            patch.object(type(self.connector), 'woocommerce_api_get_all_items', return_value=[]) as mocked_collection_request,
            patch.object(type(self.connector), 'odoo_to_woocommerce_product_values', return_value={'sku': 'DANGLING-SKU'}),
            patch.object(type(self.connector), 'woocommerce_product_fields', return_value={'woocommerce_id': '1001'}),
        ):
            self.connector.odoo_to_woocommerce_products_chunk_sync(odoo_product.ids, 'USD', {}, False, 'kg', 'cm')

        # A 404 on the stored woocommerce_id must not abort the chunk; it falls back to the SKU search and recreates the product.
        mocked_request.assert_called_once_with(woocommerce_api, endpoint='products/999')
        mocked_collection_request.assert_called_once()
        self.assertEqual(woocommerce_api.batch.call_args.kwargs['create'][0]['sku'], 'DANGLING-SKU')
        self.assertEqual(odoo_product.woocommerce_id, '1001')

    def test_chunk_writes_back_woocommerce_id_for_product_without_sku(self):
        odoo_product = self.env['product.template'].create(
            {
                'name': 'No SKU Product',
                'default_code': False,
                'woocommerce_site_url': self.connector.settings_woocommerce_connection_url,
            }
        )
        woocommerce_api = MagicMock()
        woocommerce_api.batch.return_value = {'create': [{'id': 2002, 'sku': ''}]}

        with (
            patch.object(type(self.connector), 'woocommerce_api_get', return_value=woocommerce_api),
            patch.object(type(self.connector), 'woocommerce_api_get_all_items', return_value=[]),
            patch.object(type(self.connector), 'odoo_to_woocommerce_product_values', return_value={'sku': ''}),
            patch.object(type(self.connector), 'woocommerce_product_fields', return_value={'woocommerce_id': '2002'}),
        ):
            self.connector.odoo_to_woocommerce_products_chunk_sync(odoo_product.ids, 'USD', {}, False, 'kg', 'cm')

        # Result write-back is by batch position, so a SKU-less product still receives its woocommerce_id (otherwise it would be recreated on every run).
        self.assertEqual(odoo_product.woocommerce_id, '2002')

    def test_attribute_lookup_uses_single_request_not_pagination(self):
        woocommerce_api = MagicMock()

        with (
            patch.object(type(self.connector), 'woocommerce_api_request', return_value=[{'id': 7, 'name': 'color'}]) as mocked_request,
            patch.object(type(self.connector), 'woocommerce_api_get_all_items') as mocked_get_all_items,
        ):
            result = self.connector.woocommerce_attribute_create_or_retrieve(woocommerce_api, 'attributes', 'color')

        # A single request avoids the unbounded pagination loop against endpoints that ignore the 'page' parameter.
        self.assertEqual(result, {'id': 7, 'name': 'color'})
        mocked_request.assert_called_once()
        self.assertEqual(mocked_request.call_args.kwargs['endpoint'], 'products/attributes')
        mocked_get_all_items.assert_not_called()

    def test_simple_storable_product_values(self):
        odoo_product = self.env['product.template'].create(
            {
                'name': 'Test Odoo Product',
                'default_code': 'ODOO-TEST-1',
                'list_price': 42.5,
                'categ_id': self.env.ref('product.product_category_all').id,
                **storable_product_values(is_storable=True),
            },
        )

        with patch.object(type(self.connector), 'woocommerce_attribute_create_or_retrieve', return_value=None) as mocked_attribute_call:
            product_values = self.connector.odoo_to_woocommerce_product_values(odoo_product, woocommerce_api=None, woocommerce_tax_rates={})

        # The product's (always-set) default category triggers a create-or-retrieve call
        mocked_attribute_call.assert_called()

        self.assertEqual(product_values['name'], 'Test Odoo Product')
        self.assertEqual(product_values['sku'], 'ODOO-TEST-1')
        self.assertEqual(product_values['regular_price'], '42.50')
        self.assertEqual(product_values['type'], 'simple')
        self.assertEqual(product_values['tax_class'], 'standard')
        self.assertTrue(product_values['manage_stock'])
        self.assertTrue(product_values['date_created_gmt'].endswith('Z'))

    def test_non_storable_product_has_manage_stock_false(self):
        odoo_product = self.env['product.template'].create(
            {
                'name': 'Test Odoo Service-Like Product',
                'default_code': 'ODOO-TEST-2',
                'list_price': 10.0,
                **storable_product_values(is_storable=False),
            },
        )

        with patch.object(type(self.connector), 'woocommerce_attribute_create_or_retrieve', return_value=None):
            product_values = self.connector.odoo_to_woocommerce_product_values(odoo_product, woocommerce_api=None, woocommerce_tax_rates={})

        self.assertFalse(product_values['manage_stock'])

    def test_meta_data_carries_over_existing_woocommerce_metadata(self):
        odoo_product = self.env['product.template'].create({'name': 'Test Odoo Product Update', 'default_code': 'ODOO-TEST-3', 'list_price': 5.0})

        existing_woocommerce_product = {'meta_data': [{'key': 'some_other_key', 'value': 'keep-me'}]}

        with patch.object(type(self.connector), 'woocommerce_attribute_create_or_retrieve', return_value=None):
            product_values = self.connector.odoo_to_woocommerce_product_values(odoo_product, woocommerce_api=None, woocommerce_tax_rates={}, woocommerce_product=existing_woocommerce_product)

        meta_keys = {meta['key'] for meta in product_values['meta_data']}

        self.assertIn('odoo_id', meta_keys)
        self.assertIn('some_other_key', meta_keys)
