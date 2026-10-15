"""Tests that chunk jobs only receive the slice of the lookup maps they need ('dict_subset()' in connector.py).

Queue jobs store their arguments: passing the catalogue-wide 'odoo_products' map to each of the ~750 chunk jobs of a 7,500-product store made the dispatching job run out of memory and filled the 'queue_job' table.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon, make_woocommerce_product_payload


@tagged('post_install', '-at_install')
class TestChunkJobPayload(WoocommerceSyncCommon):
    def test_dict_subset_keeps_only_requested_keys(self):
        mapping = {'1': {'id': 10}, '2': {'id': 20}, '3': {'id': 30}}

        self.assertEqual(self.connector.dict_subset(mapping, [1, '3', 4]), {'1': {'id': 10}, '3': {'id': 30}})

    def test_products_chunk_job_gets_only_its_odoo_products(self):
        self.connector.write({'settings_job_chunk_size': 2, 'sync_summary_started_at': datetime(2026, 1, 1)})
        for woocommerce_id in range(93001, 93011):
            self.env['product.template'].create({'name': f'Chunk payload {woocommerce_id}', 'woocommerce_site_url': self.connector.settings_woocommerce_connection_url, 'woocommerce_id': str(woocommerce_id)})
        woocommerce_page = [make_woocommerce_product_payload(id=woocommerce_id, sku=f'CHUNK-{woocommerce_id}') for woocommerce_id in (93001, 93002, 93003)]

        with (
            patch.object(type(self.connector), 'woocommerce_api_get', return_value=MagicMock()),
            patch.object(type(self.connector), 'woocommerce_api_get_items_in_batches', return_value=iter([woocommerce_page])),
            patch.object(type(self.connector), 'sync_summary_chunk_dispatched'),
            patch.object(type(self.connector), 'with_delay', return_value=MagicMock()) as mocked_with_delay,
        ):
            self.connector.woocommerce_to_odoo_products_sync_batch('EUR', {}, False, 'kg', 'cm')

        chunk_calls = mocked_with_delay.return_value.woocommerce_to_odoo_products_chunk_sync.call_args_list
        self.assertEqual([sorted(call.args[6]) for call in chunk_calls], [['93001', '93002'], ['93003']])
