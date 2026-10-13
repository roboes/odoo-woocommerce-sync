"""Tests that the stock sync ('odoo_woocommerce_products_stock_quantity_sync()' in connector.py) compares and pushes the quantity left for sale (on hand minus undelivered outgoing moves), not the on-hand quantity.

WooCommerce decrements its stock when an order is placed, Odoo only when the delivery is validated: comparing WooCommerce stock to the on-hand quantity copied each undelivered order into Odoo once more, so the delivery then decremented it a second time.
"""

from odoo import Command
from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon, storable_product_values

WOOCOMMERCE_ID = '94001'


@tagged('post_install', '-at_install')
class TestStockAvailableQuantity(WoocommerceSyncCommon):
    def setUp(self):
        super().setUp()
        self.warehouse = self.connector.settings_woocommerce_products_warehouse_location
        self.stock_location = self.warehouse.lot_stock_id
        self.product = self.env['product.product'].create({'name': 'Stock available quantity', 'woocommerce_id': WOOCOMMERCE_ID, **storable_product_values()})
        self.env['stock.quant']._update_available_quantity(self.product, self.stock_location, 3)

        # An imported, not yet delivered order of 2
        customers_location = self.env.ref('stock.stock_location_customers')
        picking = self.env['stock.picking'].create(
            {
                'picking_type_id': self.warehouse.out_type_id.id,
                'location_id': self.stock_location.id,
                'location_dest_id': customers_location.id,
                'move_ids': [Command.create({'name': self.product.name, 'product_id': self.product.id, 'product_uom_qty': 2, 'product_uom': self.product.uom_id.id, 'location_id': self.stock_location.id, 'location_dest_id': customers_location.id})],
            }
        )
        picking.action_confirm()

    def on_hand(self):
        return self.product.with_context(location=self.stock_location.id).qty_available

    def sync(self, woocommerce_stock_quantity, date_modified_gmt):
        return self.connector.odoo_woocommerce_products_stock_quantity_sync(self.product, {WOOCOMMERCE_ID: {'stock_quantity': woocommerce_stock_quantity, 'date_modified_gmt': date_modified_gmt}})

    def test_undelivered_order_is_not_copied_into_odoo(self):
        # WooCommerce already shows 3 - 2 = 1 and is the most recent source
        result = self.sync(1, '2099-01-01T00:00:00')

        self.assertIsNone(result)
        self.assertEqual(self.on_hand(), 3, 'The undelivered order must only be decremented by its delivery')

    def test_woocommerce_change_is_applied_on_top_of_undelivered_orders(self):
        # One more unit sold in WooCommerce, not imported yet: 3 - 2 - 1 = 0
        self.sync(0, '2099-01-01T00:00:00')

        self.assertEqual(self.on_hand(), 2)

    def test_odoo_pushes_quantity_left_for_sale(self):
        self.product.woocommerce_stock_last_sync = '2098-01-01 00:00:00'

        result = self.sync(3, '2000-01-01T00:00:00')

        self.assertEqual(result['stock_quantity'], 1, 'Pushing the on-hand quantity would let WooCommerce sell units already ordered')
