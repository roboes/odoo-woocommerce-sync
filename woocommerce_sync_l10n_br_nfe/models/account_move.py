import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

from ..utils import is_nfe_access_key, normalize_nfe_access_key

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    woocommerce_nfe_sync_state = fields.Selection(
        selection=[
            ('not_sent', 'Not sent'),
            ('queued', 'Queued'),
            ('sent', 'Sent to WooCommerce'),
            ('error', 'Error'),
        ],
        string='WooCommerce NF-e sync',
        default='not_sent',
        copy=False,
        readonly=True,
    )
    woocommerce_nfe_sync_key = fields.Char(
        string='NF-e access key',
        copy=False,
        readonly=True,
    )
    woocommerce_nfe_sync_date = fields.Datetime(
        string='Last NF-e sync',
        copy=False,
        readonly=True,
    )
    woocommerce_nfe_sync_error = fields.Text(
        string='NF-e sync error',
        copy=False,
        readonly=True,
    )

    def _woocommerce_nfe_sync_values(self):
        self.ensure_one()
        document = self.fiscal_document_id

        if self.move_type != 'out_invoice':
            raise UserError(_('Only customer invoices can send an NF-e access key.'))
        if not document or not document.document_type_id or document.document_type_id.code != '55':
            raise UserError(_('This invoice has no related NF-e (model 55).'))
        if document.issuer != 'company' or document.state_edoc != 'autorizada':
            raise UserError(_('The company-issued NF-e must be authorized before it can be sent.'))

        access_key = normalize_nfe_access_key(document.document_key)
        if not is_nfe_access_key(access_key):
            raise UserError(_('The authorized NF-e does not have a valid 44-digit model 55 access key.'))

        sale_orders = self.invoice_line_ids.mapped('sale_line_ids.order_id')
        if len(sale_orders) != 1:
            raise UserError(_('The invoice must be linked to exactly one sales order.'))

        sale_order = sale_orders[0]
        if not sale_order.woocommerce_site_url or not sale_order.woocommerce_id:
            raise UserError(_('The related sales order was not imported from WooCommerce.'))

        # Authorization may run as an accounting user without connector access.
        connectors = (
            self.env['woocommerce.sync.connector']
            .sudo()
            .search(
                [
                    ('company_id', '=', sale_order.company_id.id),
                    ('settings_woocommerce_connection_url', '=', sale_order.woocommerce_site_url),
                ],
                limit=2,
            )
        )
        if len(connectors) != 1:
            raise UserError(_('There must be exactly one WooCommerce connector for this company and store.'))

        return connectors, sale_order, access_key

    def _queue_woocommerce_nfe_sync(self):
        self.ensure_one()
        connector, sale_order, access_key = self._woocommerce_nfe_sync_values()

        if self.woocommerce_nfe_sync_key and self.woocommerce_nfe_sync_key != access_key:
            raise UserError(_('A different NF-e access key was already queued or sent for this invoice. Review it before syncing another key.'))
        if self.woocommerce_nfe_sync_key == access_key:
            if self.woocommerce_nfe_sync_state == 'sent':
                return 'sent'
            if self.woocommerce_nfe_sync_state == 'queued':
                identity_key = f'woocommerce-nfe-{self.id}-{access_key}'
                active_job = (
                    self.env['queue.job']
                    .sudo()
                    .search(
                        [
                            ('identity_key', '=', identity_key),
                            ('state', 'in', ('pending', 'enqueued', 'started', 'wait_dependencies')),
                        ],
                        limit=1,
                    )
                )
                if active_job:
                    return 'queued'

        self.write(
            {
                'woocommerce_nfe_sync_state': 'queued',
                'woocommerce_nfe_sync_key': access_key,
                'woocommerce_nfe_sync_error': False,
            }
        )
        connector.with_delay(
            channel='root.woocommerce_sync_export',
            identity_key=f'woocommerce-nfe-{self.id}-{access_key}',
        ).woocommerce_nfe_access_key_sync_job(self.id, sale_order.id, access_key)
        return 'queued'

    def action_send_woocommerce_nfe_access_key(self):
        self.ensure_one()
        sync_state = self._queue_woocommerce_nfe_sync()
        messages = {
            'queued': _('The NF-e access key was queued for WooCommerce.'),
            'sent': _('This NF-e access key was already sent to WooCommerce.'),
        }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('WooCommerce NF-e'),
                'message': messages[sync_state],
                'type': 'success',
                'sticky': False,
            },
        }

    def _woocommerce_nfe_sync_mark_error(self, message):
        self.ensure_one()
        self.sudo().write(
            {
                'woocommerce_nfe_sync_state': 'error',
                'woocommerce_nfe_sync_error': message,
            }
        )
        _logger.error('WooCommerce NF-e sync failed for invoice %s: %s', self.display_name, message)
