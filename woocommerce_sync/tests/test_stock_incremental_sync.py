"""Regression tests for bidirectional stock synchronization candidate selection."""

from unittest.mock import MagicMock, patch

from odoo.addons.woocommerce_sync.models import connector as connector_module
from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon


@tagged('post_install', '-at_install')
class TestStockIncrementalSync(WoocommerceSyncCommon):
    def test_incremental_mode_still_retrieves_all_remote_stock(self):
        self.connector.settings_woocommerce_modified_records_import = True
        woocommerce_api = MagicMock()

        with (
            patch.object(type(self.connector), 'woocommerce_api_get', return_value=woocommerce_api),
            patch.object(type(self.connector), 'woocommerce_api_get_all_items', return_value=[]) as mocked_get_all,
            patch.object(type(self.connector), 'delayable', return_value=MagicMock()),
            patch.object(connector_module, 'chain', return_value=MagicMock()),
        ):
            self.connector.odoo_woocommerce_products_stock_quantity_sync_batch()

        request_params = mocked_get_all.call_args.kwargs['params']
        self.assertNotIn('modified_after', request_params)
