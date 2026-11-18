"""Tests for WooCommerce order fee lines/coupon lines being synced into real 'sale.order.line' rows (connector.py's 'woocommerce_to_odoo_order_sync'), instead of only the raw 'woocommerce_fee_lines'/'woocommerce_coupon_lines' JSON fields."""

from typing import ClassVar

from odoo.exceptions import ValidationError
from odoo.tests.common import tagged

from .common import (
    WoocommerceSyncCommon,
    bump_iso_datetime,
    make_woocommerce_order_payload,
)


@tagged('post_install', '-at_install')
class TestOrderFeeCouponLines(WoocommerceSyncCommon):
    connector_values: ClassVar[dict] = {'settings_woocommerce_orders_customers_map': False, 'settings_woocommerce_line_items_product_map': False}

    def _sync_order(self, woocommerce_order, odoo_sale_orders=None):
        self.connector.woocommerce_to_odoo_order_sync(woocommerce_order, woocommerce_tax_rates={}, woocommerce_weight_unit='kg', woocommerce_shipping_methods=[], odoo_sale_orders=odoo_sale_orders or {})
        return self.env['sale.order'].search([('woocommerce_site_url', '=', self.connector.settings_woocommerce_connection_url), ('woocommerce_id', '=', str(woocommerce_order['id']))], limit=1)

    def test_fee_line_creates_order_line(self):
        woocommerce_order = make_woocommerce_order_payload(id=93001, number='93001', fee_lines=[{'id': 501, 'name': 'Card surcharge', 'total': '2.50'}])

        odoo_sale_order = self._sync_order(woocommerce_order)

        fee_order_line = odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '501')
        self.assertTrue(fee_order_line, 'A fee line should have been created as a sale order line')
        self.assertEqual(fee_order_line.name, 'Card surcharge')
        self.assertEqual(fee_order_line.price_unit, 2.50)

    def test_coupon_line_is_informational_and_discount_is_not_double_counted(self):
        # WooCommerce line items are already discounted ('total' < 'subtotal'): the coupon line must not deduct the discount a second time
        line_item = {
            'id': 602,
            'name': 'Item',
            'product_id': 0,
            'variation_id': 0,
            'quantity': 1,
            'tax_class': '',
            'subtotal': '100.00',
            'subtotal_tax': '0.00',
            'total': '90.00',
            'total_tax': '0.00',
            'taxes': [],
            'meta_data': [],
            'sku': '',
            'price': 90.0,
        }
        woocommerce_order = make_woocommerce_order_payload(id=93002, number='93002', line_items=[line_item], coupon_lines=[{'id': 601, 'code': 'SAVE10', 'discount': '10.00'}])

        odoo_sale_order = self._sync_order(woocommerce_order)

        coupon_order_line = odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '601')
        self.assertTrue(coupon_order_line, 'A coupon line should have been created as a sale order line')
        self.assertIn('SAVE10', coupon_order_line.name)
        self.assertEqual(coupon_order_line.price_unit, 0.0)

        product_order_line = odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '602')
        self.assertEqual(product_order_line.price_unit, 100.0)
        self.assertEqual(product_order_line.discount, 10.0)
        self.assertAlmostEqual(odoo_sale_order.amount_untaxed, 90.0)

    def test_resyncing_order_updates_fee_line_instead_of_duplicating(self):
        woocommerce_order = make_woocommerce_order_payload(id=93003, number='93003', fee_lines=[{'id': 701, 'name': 'Card surcharge', 'total': '2.50'}])

        first_sale_order = self._sync_order(woocommerce_order)

        # Simulate the fee amount changing on a resync (e.g. a retried webhook/queue job), with a 'date_modified_gmt' newer than the first sync's 'write_date' so it isn't skipped as unmodified
        woocommerce_order['fee_lines'][0]['total'] = '3.75'
        woocommerce_order['date_modified_gmt'] = bump_iso_datetime(woocommerce_order['date_modified_gmt'])
        odoo_sale_order = self._sync_order(woocommerce_order, odoo_sale_orders={str(woocommerce_order['id']): {'id': first_sale_order.id}})

        self.assertEqual(odoo_sale_order, first_sale_order, 'Resyncing must update the existing order, not create a new one')

        fee_order_lines = odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '701')
        self.assertEqual(len(fee_order_lines), 1, 'Resyncing the same order must not duplicate the fee line')
        self.assertEqual(fee_order_lines.price_unit, 3.75)

    def test_resyncing_order_removes_missing_fee_line(self):
        woocommerce_order = make_woocommerce_order_payload(id=93004, number='93004', fee_lines=[{'id': 801, 'name': 'Temporary fee', 'total': '2.50'}])
        first_sale_order = self._sync_order(woocommerce_order)

        woocommerce_order['fee_lines'] = []
        woocommerce_order['date_modified_gmt'] = bump_iso_datetime(woocommerce_order['date_modified_gmt'])
        odoo_sale_order = self._sync_order(woocommerce_order, odoo_sale_orders={str(woocommerce_order['id']): {'id': first_sale_order.id}})

        self.assertFalse(odoo_sale_order.order_line.filtered(lambda line: line.woocommerce_id == '801'))

    def test_pagbank_quote_and_unknown_fee_keep_buyer_installment_charge(self):
        payload = make_woocommerce_order_payload(
            id=93005,
            total='4.00',
            payment_method='pagbank_credit_card',
            meta_data=[{'key': '_pagbank_charge_id', 'value': 'CHAR_TEST'}],
            fee_lines=[{'id': 901, 'name': 'Parcelamento', 'total': '4.00'}],
        )
        order = self._sync_order(payload)

        self.assertTrue(order)
        self.assertFalse(order.woocommerce_transaction_fee_known)
        self.assertEqual(order.woocommerce_payment_method, 'pagbank_credit_card')
        self.assertAlmostEqual(order.amount_untaxed, 4.0)
        self.assertAlmostEqual(order.woocommerce_total, 4.0)

        for fee in (16, 0, None):
            payload['meta_data'] = [{'key': '_pagbank_charge_id', 'value': 'CHAR_TEST'}]
            if fee is not None:
                payload['meta_data'].append({'key': '_pagbank_seller_fee_quote', 'value': {'fee': fee, 'amount': 400, 'currency': payload['currency'], 'charge_id': 'CHAR_TEST'}})
            payload['date_modified_gmt'] = bump_iso_datetime(payload['date_modified_gmt'])
            self._sync_order(payload, {str(payload['id']): {'id': order.id}})

            self.assertEqual(order.woocommerce_transaction_fee_known, fee is not None)
            self.assertEqual(order.woocommerce_transaction_fee_estimated, fee is not None)
            self.assertAlmostEqual(order.woocommerce_transaction_fee, fee / 100.0 if fee is not None else 0.0)
            self.assertAlmostEqual(order.woocommerce_payout, 4.0 - fee / 100.0 if fee is not None else 0.0)
            self.assertAlmostEqual(order.amount_untaxed, 4.0)

    def test_fee_selection_follows_the_payment_method(self):
        metadata = [
            {'key': 'PayPal Transaction Fee', 'value': '1.00'},
            {'key': '_stripe_fee', 'value': '2.00'},
            {'key': '_pagbank_seller_fee_quote', 'value': {'fee': 99, 'amount': 10000, 'currency': 'BRL', 'charge_id': 'CHAR_TEST'}},
        ]
        for method, fee, estimated in [
            ('paypal', 1.0, False),
            ('ppcp-gateway', 1.0, False),
            ('ppec_paypal', 1.0, False),
            ('stripe', 2.0, False),
            ('stripe_cc', 2.0, False),
            ('pagbank_pix', 0.99, True),
            ('bacs', None, False),
        ]:
            with self.subTest(method=method):
                order = self.env['sale.order'].new({'woocommerce_payment_method': method, 'woocommerce_meta_data': metadata, 'woocommerce_currency': 'BRL', 'woocommerce_total': 100.0})
                self.assertAlmostEqual(order.woocommerce_transaction_fee, fee if fee is not None else 0.0)
                self.assertEqual(order.woocommerce_transaction_fee_known, fee is not None)
                self.assertEqual(order.woocommerce_transaction_fee_estimated, estimated)
                self.assertAlmostEqual(order.woocommerce_payout, 100.0 - fee if fee is not None else 0.0)

    def test_invalid_fee_metadata_is_rejected(self):
        for method, key, value in [
            ('pagbank_pix', '_pagbank_seller_fee_quote', {'fee': 99, 'currency': 'USD'}),
            ('pagbank_pix', '_pagbank_seller_fee_quote', {'fee': '99', 'currency': 'BRL'}),
            ('stripe_cc', '_stripe_fee', 'not-an-amount'),
        ]:
            with self.subTest(method=method, value=value), self.assertRaises(ValidationError):
                order = self.env['sale.order'].new({'woocommerce_payment_method': method, 'woocommerce_meta_data': [{'key': key, 'value': value}], 'woocommerce_currency': 'BRL'})
                _ = order.woocommerce_transaction_fee

    def test_known_zero_fee_and_resync_clears_missing_fee(self):
        payload = make_woocommerce_order_payload(
            id=93006,
            total='4.00',
            payment_method='paypal',
            meta_data=[{'key': 'PayPal Transaction Fee', 'value': 0}, {'key': '_stripe_fee', 'value': '1.25'}],
        )
        order = self._sync_order(payload)
        self.assertTrue(order.woocommerce_transaction_fee_known)
        self.assertEqual(order.woocommerce_transaction_fee, 0.0)
        self.assertEqual(order.woocommerce_payout, 4.0)

        for metadata, known, fee, payout in [([{'key': '_stripe_fee', 'value': '1.25'}], True, 1.25, 2.75), ([], False, 0.0, 0.0)]:
            payload['payment_method'] = 'stripe_cc'
            payload['meta_data'] = metadata
            payload['date_modified_gmt'] = bump_iso_datetime(payload['date_modified_gmt'])
            self._sync_order(payload, {str(payload['id']): {'id': order.id}})
            self.assertEqual(order.woocommerce_transaction_fee_known, known)
            self.assertFalse(order.woocommerce_transaction_fee_estimated)
            self.assertEqual(order.woocommerce_transaction_fee, fee)
            self.assertEqual(order.woocommerce_payout, payout)
