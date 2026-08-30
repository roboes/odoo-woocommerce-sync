"""Unit tests for 'WooCommerceClient' (woocommerce_client.py). Unlike the other test files, this one needs no Odoo database/registry (WooCommerceClient has no ORM dependency), only Odoo itself importable, so it runs fast as part of the normal test-tags run (auto-discovered via tests/__init__.py)."""

import unittest
from unittest.mock import MagicMock, patch

import requests
from odoo.addons.woocommerce_sync.models import woocommerce_client
from odoo.addons.woocommerce_sync.models.woocommerce_client import WooCommerceClient


def _make_client(test_mode: bool = False) -> WooCommerceClient:
    """Builds a WooCommerceClient without calling '__init__' (which constructs the real 'woocommerce.API' object), so no network/credentials are needed."""
    client = WooCommerceClient.__new__(WooCommerceClient)
    client.test_mode = test_mode
    client.api = MagicMock()

    return client


def _make_response(status_code: int, json_data=None, headers=None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data if json_data is not None else {}
    response.headers = headers or {}
    response.raise_for_status.side_effect = requests.HTTPError(f'HTTP {status_code}') if status_code >= 400 else None

    return response


class TestListChunks(unittest.TestCase):
    def test_splits_into_chunks_of_requested_size(self):
        chunks = list(WooCommerceClient.list_chunks(list(range(10)), 3))

        self.assertEqual(chunks, [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9]])

    def test_empty_list_yields_no_chunks(self):
        self.assertEqual(list(WooCommerceClient.list_chunks([], 5)), [])

    def test_chunk_size_larger_than_list_yields_single_chunk(self):
        self.assertEqual(list(WooCommerceClient.list_chunks([1, 2], 100)), [[1, 2]])


class TestBatch(unittest.TestCase):
    def test_empty_create_update_delete_returns_empty_dict_without_calling_api(self):
        client = _make_client()

        result = client.batch('products/batch')

        self.assertEqual(result, {})
        client.api.post.assert_not_called()

    def test_sends_only_non_empty_lists(self):
        client = _make_client()
        client.api.post.return_value = _make_response(200, {'create': [{'id': 1}]})

        client.batch('products/batch', create=[{'name': 'A'}], update=None, delete=None)

        client.api.post.assert_called_once_with(endpoint='products/batch', data={'create': [{'name': 'A'}]})

    def test_appends_batch_to_resource_endpoint(self):
        client = _make_client()
        client.api.post.return_value = _make_response(200, {'update': [{'id': 1}]})

        client.batch('products', update=[{'id': 1}])

        client.api.post.assert_called_once_with(endpoint='products/batch', data={'update': [{'id': 1}]})

    def test_retries_on_429_then_succeeds(self):
        client = _make_client()
        client.api.post.side_effect = [
            _make_response(429, headers={'Retry-After': '0'}),
            _make_response(200, {'create': [{'id': 1}]}),
        ]

        with patch.object(woocommerce_client.time, 'sleep'):
            result = client.batch('products/batch', update=[{'id': 1}])

        self.assertEqual(result, {'create': [{'id': 1}]})
        self.assertEqual(client.api.post.call_count, 2)

    def test_gives_up_after_max_retries(self):
        client = _make_client()
        error_response = _make_response(500)
        error_response.raise_for_status.side_effect = RuntimeError('server error')
        client.api.post.return_value = error_response

        with patch.object(woocommerce_client.time, 'sleep'), self.assertRaises(RuntimeError):
            client.batch('products/batch', update=[{'id': 1}], max_retries=1)

        self.assertEqual(client.api.post.call_count, 2)  # Initial attempt + 1 retry

    def test_does_not_retry_create_after_server_error(self):
        client = _make_client()
        client.api.post.return_value = _make_response(500)

        with self.assertRaises(requests.HTTPError):
            client.batch('products', create=[{'name': 'A'}])

        client.api.post.assert_called_once()


class TestRequest(unittest.TestCase):
    def test_raises_on_non_retryable_http_error(self):
        client = _make_client()
        client.api.get.return_value = _make_response(401, {'code': 'woocommerce_rest_cannot_view'})

        with self.assertRaises(requests.HTTPError):
            client.request('products')

    def test_rejects_non_list_pagination_response(self):
        client = _make_client()
        client.api.get.return_value = _make_response(200, {'code': 'unexpected'})

        with self.assertRaises(ValueError):
            client.get_all_items('products')


class TestGetItemsInBatches(unittest.TestCase):
    def test_yields_pages_until_empty_page(self):
        client = _make_client()
        client.api.get.side_effect = [
            _make_response(200, [{'id': 1}, {'id': 2}]),
            _make_response(200, [{'id': 3}]),
            _make_response(200, []),
        ]

        pages = list(client.get_items_in_batches(endpoint='products', batch_size=2))

        self.assertEqual(pages, [[{'id': 1}, {'id': 2}], [{'id': 3}]])

    def test_stops_after_first_page_in_test_mode(self):
        client = _make_client(test_mode=True)
        client.api.get.return_value = _make_response(200, [{'id': 1}])

        pages = list(client.get_items_in_batches(endpoint='products'))

        self.assertEqual(len(pages), 1)
        self.assertEqual(client.api.get.call_count, 1)

    def test_default_page_size_is_100(self):
        client = _make_client()
        client.api.get.side_effect = [_make_response(200, [{'id': 1}]), _make_response(200, [])]

        list(client.get_items_in_batches(endpoint='products'))

        self.assertEqual(client.api.get.call_args_list[0].kwargs['params']['per_page'], 100)


if __name__ == '__main__':
    unittest.main()
