import logging

from odoo import models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class FiscalDocument(models.Model):
    _inherit = 'l10n_br_fiscal.document'

    def write(self, values):
        was_authorized = {document.id: document.state_edoc == 'autorizada' for document in self}
        result = super().write(values)

        if values.get('state_edoc') == 'autorizada':
            newly_authorized = self.filtered(lambda document: not was_authorized.get(document.id) and document.state_edoc == 'autorizada' and document.document_type_id.code == '55' and document.issuer == 'company')
            invoices = self.env['account.move'].search(
                [
                    ('fiscal_document_id', 'in', newly_authorized.ids),
                    ('move_type', '=', 'out_invoice'),
                ]
            )
            for invoice in invoices:
                sale_orders = invoice.invoice_line_ids.mapped('sale_line_ids.order_id')
                if not any(order.woocommerce_site_url and order.woocommerce_id for order in sale_orders):
                    continue
                try:
                    invoice.sudo()._queue_woocommerce_nfe_sync()
                except UserError as error:
                    invoice._woocommerce_nfe_sync_mark_error(str(error))
                    _logger.warning(
                        'Could not queue NF-e access-key sync for invoice %s: %s',
                        invoice.display_name,
                        error,
                    )

        return result
