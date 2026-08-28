"""Tests for converting WooCommerce order refunds into real Odoo credit notes (connector.py's 'woocommerce_to_odoo_order_refunds_sync'). Covers the full-refund happy path, idempotency (a refund must not create a duplicate credit note if processed twice, e.g. via a retried queue job), and the safety behavior for partial refunds / multi-invoice orders, which are intentionally left for manual handling rather than risking an inaccurate credit note."""

from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon


@tagged('post_install', '-at_install')
class TestRefundCreditNoteSync(WoocommerceSyncCommon):
    def _create_confirmed_order_with_posted_invoice(self, price: float = 100.0):
        partner = self.env['res.partner'].create({'name': 'Test WooCommerce Customer'})
        # 'taxes_id': [] so the invoice total always equals 'price' regardless of the target database's configured default sale tax
        product = self.env['product.product'].create({'name': 'Test Refundable Product', 'list_price': price, 'invoice_policy': 'order', 'taxes_id': [(6, 0, [])]})

        order = self.env['sale.order'].create(
            {
                'partner_id': partner.id,
                'woocommerce_site_url': self.connector.settings_woocommerce_connection_url,
                'order_line': [(0, 0, {'product_id': product.id, 'product_uom_qty': 1, 'price_unit': price})],
            },
        )
        order.action_confirm()

        invoice = order._create_invoices()
        invoice.action_post()

        return order, invoice

    def test_full_refund_creates_posted_credit_note(self):
        order, invoice = self._create_confirmed_order_with_posted_invoice(price=100.0)
        woocommerce_order = {'refunds': [{'id': 555001, 'reason': 'Customer request', 'total': '-100.00'}]}

        self.connector.woocommerce_to_odoo_order_refunds_sync(order, woocommerce_order)

        credit_note = self.env['account.move'].search([('woocommerce_refund_id', '=', '555001')])

        self.assertTrue(credit_note, 'A credit note should have been created for the full refund')
        self.assertEqual(credit_note.move_type, 'out_refund')
        self.assertEqual(credit_note.state, 'posted')
        self.assertEqual(credit_note.reversed_entry_id, invoice)
        self.assertEqual(credit_note.woocommerce_site_url, self.connector.settings_woocommerce_connection_url)

    def test_refund_already_processed_is_not_duplicated(self):
        order, _invoice = self._create_confirmed_order_with_posted_invoice(price=50.0)
        woocommerce_order = {'refunds': [{'id': 555002, 'reason': 'Customer request', 'total': '-50.00'}]}

        self.connector.woocommerce_to_odoo_order_refunds_sync(order, woocommerce_order)
        self.connector.woocommerce_to_odoo_order_refunds_sync(order, woocommerce_order)  # Simulates a retried/duplicate webhook or queue job

        credit_notes = self.env['account.move'].search([('woocommerce_refund_id', '=', '555002')])

        self.assertEqual(len(credit_notes), 1, 'Reprocessing the same WooCommerce refund id must not create a second credit note')

    def test_partial_refund_is_skipped(self):
        order, _invoice = self._create_confirmed_order_with_posted_invoice(price=100.0)
        woocommerce_order = {'refunds': [{'id': 555003, 'reason': 'Partial refund', 'total': '-40.00'}]}

        self.connector.woocommerce_to_odoo_order_refunds_sync(order, woocommerce_order)

        credit_note = self.env['account.move'].search([('woocommerce_refund_id', '=', '555003')])

        self.assertFalse(credit_note, 'Partial refunds must be left for manual handling, not auto-converted into a (potentially inaccurate) credit note')

    def test_no_refunds_is_a_no_op(self):
        order, _invoice = self._create_confirmed_order_with_posted_invoice(price=20.0)

        # Should not raise even without a 'refunds' key
        self.connector.woocommerce_to_odoo_order_refunds_sync(order, {})
