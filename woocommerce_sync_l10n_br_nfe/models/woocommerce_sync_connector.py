import logging

import requests
from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class WoocommerceSyncConnector(models.Model):
    _inherit = 'woocommerce.sync.connector'

    def woocommerce_nfe_access_key_sync_job(self, invoice_id, sale_order_id, access_key):
        self.ensure_one()
        invoice = self.env['account.move'].browse(invoice_id).exists()
        sale_order = self.env['sale.order'].browse(sale_order_id).exists()

        if not invoice:
            raise UserError(_('The invoice or WooCommerce sales order no longer exists.'))
        if not sale_order:
            message = _('The WooCommerce sales order no longer exists.')
            invoice._woocommerce_nfe_sync_mark_error(message)
            raise UserError(message)

        try:
            if invoice.woocommerce_nfe_sync_key != access_key:
                raise UserError(_('The NF-e access key changed after this sync was queued.'))

            connector, current_order, current_key = invoice._woocommerce_nfe_sync_values()
            if connector.id != self.id or current_order.id != sale_order.id or current_key != access_key:
                raise UserError(_('The invoice no longer matches this WooCommerce connector and order.'))

            api = self.woocommerce_api_get(validate=False)
            if not api:
                raise UserError(_('WooCommerce API settings are missing or unavailable.'))

            endpoint = f'orders/{sale_order.woocommerce_id}/notes'
            notes = api.get_all_items(endpoint)
            note_text = f'NF-e: {access_key}'
            note_exists = any(not note.get('customer_note') and note_text in (note.get('note') or '') for note in notes)
            if not note_exists:
                response = api.post(
                    endpoint,
                    data={
                        'note': note_text,
                        'customer_note': False,
                    },
                )
                response.raise_for_status()
        except UserError as error:
            if invoice.woocommerce_nfe_sync_key == access_key:
                invoice._woocommerce_nfe_sync_mark_error(str(error))
            raise
        except (requests.RequestException, TypeError, ValueError) as error:
            if invoice.woocommerce_nfe_sync_key == access_key:
                invoice._woocommerce_nfe_sync_mark_error(str(error))
            _logger.exception(
                'WooCommerce API request failed for NF-e access-key sync on invoice %s',
                invoice.display_name,
            )
            raise

        invoice.sudo().write(
            {
                'woocommerce_nfe_sync_state': 'sent',
                'woocommerce_nfe_sync_key': access_key,
                'woocommerce_nfe_sync_date': fields.Datetime.now(),
                'woocommerce_nfe_sync_error': False,
            }
        )
        invoice.sudo().message_post(body=_('NF-e access key sent to WooCommerce.'))
