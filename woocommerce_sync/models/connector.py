from __future__ import annotations

import logging
import secrets
import time
from base64 import b64decode, b64encode
from collections.abc import Generator
from datetime import UTC, datetime, timedelta, timezone
from io import BytesIO

from PIL import Image, features

try:
    from PIL import (
        AvifImagePlugin,  # noqa: F401 - force AVIF plugin registration in Odoo worker
    )

    _pil_avif_supported = features.check('avif')
except ImportError:
    _pil_avif_supported = False


from typing import Any, ClassVar

import filetype
import psycopg2
import requests
from markupsafe import Markup, escape
from odoo import _, api, fields, models
from odoo.addons.queue_job.delay import chain
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.exceptions import UserError, ValidationError
from odoo.release import version_info
from odoo.tools import float_compare
from requests.auth import HTTPBasicAuth
from werkzeug.utils import secure_filename

from .woocommerce_client import WooCommerceClient

# Settings
_logger = logging.getLogger(__name__)

if not _pil_avif_supported:
    _logger.warning('Pillow version on this system lacks AVIF support. Images served as AVIF will fail to process.')
del _pil_avif_supported


class WoocommerceSyncConnector(models.Model):
    _name = 'woocommerce.sync.connector'
    _description = 'WooCommerce Sync Connector'
    _inherit: ClassVar[list[str]] = ['mail.thread']

    @staticmethod
    def list_chunks(items: list, chunk_size: int) -> Generator[list, Any, None]:
        """Splits 'items' into consecutive chunks of at most 'chunk_size' elements."""
        for index in range(0, len(items), chunk_size):
            yield items[index : index + chunk_size]

    def job_description(self: models.Model, method_name: str) -> str:
        """Returns a '<model>.<method>' description for 'with_delay()'/'delayable()' calls, so the Job Queue view shows the function name instead of falling back to the function's docstring."""
        return f'{self._name}.{method_name}'

    def sync_summary_reset(self: models.Model) -> None:
        """Resets the aggregate sync-summary state at the start of a full sync run (posted once as a single chatter message when every dispatched chunk job has completed).

        No-ops if a previous run's tracking is still active (i.e. this 'woocommerce_sync()' call started before the previous one's chunk jobs all finished, e.g. the scheduled interval is shorter than a full sync takes) - resetting in that case would permanently orphan the previous run's already-dispatched/in-flight chunk jobs (their eventual 'sync_summary_chunk_completed()' calls would never satisfy a 'total_completed == total_dispatched' check again), so the summary message would never post. Instead, this run's own dispatched/completed events simply accumulate into the still-active previous run and get reported together in a single, later message.

        Wrapped in a savepoint so a failure here can never block the sync itself.
        """
        if not self.settings_sync_summary_chatter_enable:
            return

        # A stale run (e.g. a chunk job permanently failed/got lost, so it never called 'sync_summary_chunk_completed()') would otherwise leave 'sync_summary_run_active' stuck True forever, silencing every future summary message - treat a run older than 6 hours as stale and reset anyway instead of skipping
        stale_cutoff = fields.Datetime.now() - timedelta(hours=6)
        if self.sync_summary_run_active and self.sync_summary_started_at and self.sync_summary_started_at > stale_cutoff:
            _logger.debug(f"Sync-summary run already active for connector {self.id}; skipping reset (this run's events will be reported together with the still-in-progress previous run)")
            return

        try:
            with self.env.cr.savepoint():
                self.write({'sync_summary_run_active': True, 'sync_summary_all_dispatched': False, 'sync_summary_started_at': fields.Datetime.now()})
                # Drop any leftover events from a previous run that never fully completed (e.g. a permanently failed chunk job)
                self.env['woocommerce.sync.summary.event'].sudo().search([('connector_id', '=', self.id)]).unlink()
        except Exception:
            _logger.exception('Failed to reset sync-summary tracking for this run (harmless - only affects the chatter summary, not the actual sync)')

    def sync_summary_chunk_dispatched(self: models.Model) -> None:
        """Registers, as an independent event row, that one more chunk job has been dispatched for the current sync run.

        Uses an insert rather than incrementing a shared counter column, since many chunk-dispatch calls happen in quick succession and inserts never conflict with each other the way concurrent updates to the same row do. Wrapped in a savepoint so any failure here (this is a best-effort reporting feature) can never poison the surrounding job's transaction or affect the actual sync work.
        """
        if not self.settings_sync_summary_chatter_enable:
            return

        try:
            with self.env.cr.savepoint():
                self.env['woocommerce.sync.summary.event'].sudo().create({'connector_id': self.id, 'run_started_at': self.sync_summary_started_at, 'event_type': 'dispatched'})
            _logger.debug(f'Registered a dispatched sync-summary event for connector {self.id}')
        except Exception:
            _logger.exception('Failed to register a dispatched sync-summary event (harmless - only affects the chatter summary, not the actual sync)')

    def sync_summary_finalize_dispatch(self: models.Model) -> None:
        """Marks that every stage has finished dispatching its chunk jobs, so the summary can be posted once the last chunk job completes."""
        if not self.settings_sync_summary_chatter_enable:
            return

        try:
            with self.env.cr.savepoint():
                self.sync_summary_all_dispatched = True
        except Exception:
            _logger.exception('Failed to finalize sync-summary dispatch tracking (harmless - only affects the chatter summary, not the actual sync)')
            return

        # Dispatched as its own job (rather than called inline) so a transient DB conflict (see 'sync_summary_maybe_post()') only requires retrying this small, self-contained job instead of jeopardizing this chunk job's own real sync work
        self.with_delay(description=self.job_description('sync_summary_maybe_post')).sync_summary_maybe_post()

    def sync_summary_chunk_completed(self: models.Model, direction: str, processed: int, new_count: int, updated_count: int, errors: list[str]) -> None:
        """Registers, as an independent event row, one chunk job's own results, then attempts to post the final chatter message.

        'direction' is one of 'products'/'variations'/'customers'/'orders' (matches 'woocommerce.sync.summary.event.sync_direction'), used to break the posted summary down per sync direction instead of only a single combined total.

        Uses an insert rather than incrementing shared counter columns, for the same concurrency reason as 'sync_summary_chunk_dispatched()' above - many chunk jobs can finish at nearly the same time. Wrapped in a savepoint (see 'sync_summary_chunk_dispatched()') so a failure here can never affect the actual sync work.
        """
        if not self.settings_sync_summary_chatter_enable:
            return

        # No active run to attach this event to (e.g. this chunk method was called directly, bypassing 'sync_summary_chunk_dispatched()') - nothing to register
        if not self.sync_summary_started_at:
            _logger.debug(f'Skipping sync-summary event registration for connector {self.id}: no active run (sync_summary_started_at is not set)')
            return

        try:
            with self.env.cr.savepoint():
                self.env['woocommerce.sync.summary.event'].sudo().create(
                    {
                        'connector_id': self.id,
                        'run_started_at': self.sync_summary_started_at,
                        'event_type': 'completed',
                        'sync_direction': direction,
                        'processed': processed,
                        'new_count': new_count,
                        'updated_count': updated_count,
                        'errors_count': len(errors),
                        'errors_text': (f'{chr(10).join(errors)}\n') if errors else '',
                    }
                )
            _logger.debug(f'Registered a completed sync-summary event for connector {self.id} ({direction}): {processed} processed, {len(errors)} error(s)')
        except Exception:
            _logger.exception('Failed to register a completed sync-summary event (harmless - only affects the chatter summary, not the actual sync)')
            return

        # Dispatched as its own job (rather than called inline) so a transient DB conflict (see 'sync_summary_maybe_post()') only requires retrying this small, self-contained job instead of jeopardizing this chunk job's own real sync work
        self.with_delay(description=self.job_description('sync_summary_maybe_post')).sync_summary_maybe_post()

    def sync_summary_maybe_post(self: models.Model) -> None:
        """Posts the aggregate sync-summary chatter message once all chunk jobs dispatched for the current run have completed (no-op otherwise, or if already posted).

        Dispatched as its own dedicated queue job (see 'sync_summary_chunk_completed()'/'sync_summary_finalize_dispatch()') rather than called inline within a chunk job, so a 'SerializationFailure' (several chunk jobs finishing at nearly the same time and racing on the same connector row - expected under queue_job's REPEATABLE READ isolation) can be safely left to propagate: queue_job automatically retries this small, self-contained job with backoff, without risking any real synced data (unlike if this ran inline in a chunk job and had to be silently swallowed instead). Any other, non-transient failure (SQL, message_post, etc.) is still caught/logged instead of retried forever.
        """
        if not self.settings_sync_summary_chatter_enable or not self.sync_summary_run_active or not self.sync_summary_all_dispatched:
            _logger.debug(
                f'Skipping sync-summary post for connector {self.id}: enabled={self.settings_sync_summary_chatter_enable}, run_active={self.sync_summary_run_active}, all_dispatched={self.sync_summary_all_dispatched}'
            )
            return

        try:
            with self.env.cr.savepoint():
                self.env.cr.execute(
                    """
                    SELECT count(*) FILTER (WHERE event_type = 'dispatched'),
                           count(*) FILTER (WHERE event_type = 'completed')
                    FROM woocommerce_sync_summary_event
                    WHERE connector_id = %s AND run_started_at = %s
                    """,
                    (self.id, self.sync_summary_started_at),
                )
                total_dispatched, total_completed = self.env.cr.fetchone()

                _logger.debug(f'Sync-summary progress for connector {self.id}: {total_completed}/{total_dispatched} chunk job(s) completed')

                if total_completed < total_dispatched:
                    return

                # Claim the right to post exactly once for this run: only the caller that flips 'run_active' True -> False proceeds (a lost race here is harmless - another chunk job finishing at the same time will post it instead)
                self.env.cr.execute('UPDATE woocommerce_sync_connector SET sync_summary_run_active = false WHERE id = %s AND sync_summary_run_active = true RETURNING true', (self.id,))
                claimed = self.env.cr.fetchone()
                if not claimed:
                    _logger.debug(f'Sync-summary post for connector {self.id} already claimed by another chunk job finishing at the same time')
                    return

                # Per-direction breakdown (products/variations/customers/orders), in a fixed display order
                self.env.cr.execute(
                    """
                    SELECT sync_direction,
                           coalesce(sum(processed), 0),
                           coalesce(sum(new_count), 0),
                           coalesce(sum(updated_count), 0),
                           coalesce(sum(errors_count), 0),
                           left(coalesce(string_agg(NULLIF(errors_text, ''), ''), ''), 10000)
                    FROM woocommerce_sync_summary_event
                    WHERE connector_id = %s AND run_started_at = %s AND event_type = 'completed' AND sync_direction IS NOT NULL
                    GROUP BY sync_direction
                    """,
                    (self.id, self.sync_summary_started_at),
                )
                direction_labels = {'products': 'Products', 'variations': 'Product Variations', 'customers': 'Customers', 'orders': 'Orders'}
                direction_rows = {row[0]: row[1:] for row in self.env.cr.fetchall()}

                processed = sum(row[0] for row in direction_rows.values())
                errors_count = sum(row[3] for row in direction_rows.values())
                errors_text = ''.join(row[4] for row in direction_rows.values() if row[4])

                duration = fields.Datetime.now() - self.sync_summary_started_at if self.sync_summary_started_at else None
                last_sync = self.odoo_woocommerce_last_sync_retrieve() if self.settings_woocommerce_modified_records_import else False

                # Odoo 'Datetime' fields are stored/returned in naive UTC; converting to the posting user's timezone here is required since these values are interpolated as plain text below, unlike an actual 'Datetime' field widget (which converts automatically)
                sync_summary_started_at_local = fields.Datetime.context_timestamp(self, self.sync_summary_started_at).strftime('%Y-%m-%d %H:%M:%S') if self.sync_summary_started_at else None
                last_sync_local = fields.Datetime.context_timestamp(self, last_sync).strftime('%Y-%m-%d %H:%M:%S') if last_sync else None

                duration_text = ''
                if duration is not None:
                    hours, remainder = divmod(int(duration.total_seconds()), 3600)
                    minutes, seconds = divmod(remainder, 60)
                    duration_text = f' in {hours:02d}:{minutes:02d}:{seconds:02d}'

                started_at_text = f'that started at {sync_summary_started_at_local} ' if sync_summary_started_at_local else ''
                body = f'<p>WooCommerce sync {started_at_text}completed: {processed} record(s) processed, {errors_count} error(s){duration_text}.</p>'
                body += '<ul>'
                for direction, label in direction_labels.items():
                    if direction not in direction_rows:
                        continue
                    direction_processed, direction_new, direction_updated, direction_errors, _direction_errors_text = direction_rows[direction]
                    body += f'<li>{label}: {direction_processed} processed ({direction_new} new, {direction_updated} updated), {direction_errors} error(s).</li>'
                body += '</ul>'
                if last_sync_local:
                    body += f'<p>Synced records modified since last sync at {last_sync_local}.</p>'
                if self.settings_woocommerce_test_mode:
                    body += '<p>Note: Test mode is enabled - only the first 10 items per WooCommerce endpoint were retrieved, so these counts do not reflect a full sync.</p>'
                if errors_text:
                    # 'Markup.replace()' escapes its replacement argument too, so escape/stringify first and only wrap the final result as trusted HTML below
                    items = ''.join(f'<li>{line.strip(".")}.</li>' for line in str(escape(errors_text)).splitlines() if line.strip())
                    body += f'<p>Error(s):</p><ul>{items}</ul>'
                # 'message_post()' treats a plain string 'body' as untrusted text and HTML-escapes it (showing literal '<p>' tags); mark it as trusted HTML instead, now that any untrusted data within it ('errors_text' above) has already been escaped
                body = Markup(body)

                self.message_post(body=body, subtype_xmlid='mail.mt_comment', author_id=self.env.ref('woocommerce_sync.res_partner_woocommerce_sync').id)
                if self.settings_sync_summary_discuss_chat_enable:
                    self.sync_summary_notify_discuss(body)
                _logger.info(f'Posted sync-summary chatter message on connector {self.id}: {processed} record(s) processed, {errors_count} error(s)')
                self.env['woocommerce.sync.summary.event'].sudo().search([('connector_id', '=', self.id), ('run_started_at', '=', self.sync_summary_started_at)]).unlink()
        except psycopg2.errors.SerializationFailure:
            _logger.warning(f'Serialization conflict while posting sync-summary for connector {self.id}; letting queue_job automatically retry this job')
            raise
        except Exception:
            _logger.exception('Failed to post the sync-summary chatter message (harmless - only affects the chatter summary, not the actual sync); the run will remain marked active until the next sync run resets it')

    def sync_summary_notify_discuss(self: models.Model, body: str) -> None:
        """Sends the sync-summary as a direct-message Discuss chat from the 'WooCommerce Sync' partner.
        A plain 'message_post()' on this record (see 'sync_summary_maybe_post()' above) only ever shows up on this record's own chatter - the Discuss app's 'Chats' sidebar only lists direct-message channels a user actually belongs to, so posting a message on an unrelated business record never appears there. Recipients are this record's own followers plus whichever user created it (so it works out of the box without requiring the user to manually follow this record first).
        """
        woocommerce_sync_partner = self.env.ref('woocommerce_sync.res_partner_woocommerce_sync')
        recipient_partners = (self.message_partner_ids | self.create_uid.partner_id) - woocommerce_sync_partner
        # 'mail.channel' was renamed to 'discuss.channel' in Odoo 18; 'channel_get()' was renamed to '_get_or_create_chat()' in Odoo 19
        discuss_channel_model = 'discuss.channel' if version_info[0] in [18, 19] else 'mail.channel'
        channel_get_method_name = '_get_or_create_chat' if version_info[0] == 19 else 'channel_get'
        for target_user in recipient_partners.filtered('user_ids').mapped('user_ids'):
            try:
                with self.env.cr.savepoint():
                    # Get/create the private chat between the *calling* user and 'partners_to', so impersonate the recipient here
                    # On Odoo 16 ('mail.channel', 'channel_get') this returns a plain dict (channel_info); on 18/19 ('discuss.channel', 'channel_get'/'_get_or_create_chat') it returns the channel record itself - normalize both to an actual record before posting
                    channel_get_method = getattr(self.env[discuss_channel_model].with_user(target_user).sudo(), channel_get_method_name)
                    channel_result = channel_get_method(partners_to=[woocommerce_sync_partner.id])
                    channel = channel_result if isinstance(channel_result, models.BaseModel) else self.env[discuss_channel_model].browse(channel_result['id'])
                    channel.sudo().message_post(body=body, message_type='comment', subtype_xmlid='mail.mt_comment', author_id=woocommerce_sync_partner.id)
            except Exception:
                _logger.exception(f'Failed to send the sync-summary Discuss chat message to user {target_user.id} (harmless - the chatter message on this record was still posted)')

    # WooCommerce REST API settings
    settings_woocommerce_connection_name = fields.Char(string='Instance Name')
    settings_woocommerce_connection_url = fields.Char(string='Store URL', help='WordPress URL. Example: https://www.mystore.com')
    settings_woocommerce_consumer_key = fields.Char(string='Consumer Key')
    settings_woocommerce_consumer_secret = fields.Char(string='Consumer Secret')
    settings_woocommerce_timeout = fields.Integer(string='Timeout (in seconds)', default=30)
    settings_user_agent = fields.Char(string='User Agent', default='Odoo-Woocommerce Sync', help="HTTP 'User-Agent' header sent with every WooCommerce REST API request.")
    settings_job_chunk_size = fields.Integer(
        string='Queue Job Chunk Size',
        default=10,
        help='Number of records grouped into a single queue job when dispatching per-record sync jobs, to reduce per-job overhead (worker pickup, transaction/commit, connection re-validation) versus one job per record.',
    )

    @api.constrains('settings_job_chunk_size')
    def settings_job_chunk_size_check(self: models.Model) -> None:
        for record in self:
            if record.settings_job_chunk_size <= 0:
                raise ValidationError(_('Queue Job Chunk Size must be greater than zero.'))

    settings_sync_summary_chatter_enable = fields.Boolean(
        string='Post Sync Summary to Chatter',
        default=True,
        help=(
            "If enabled, a summary message (records processed, new/updated, errors) is posted to this record's chatter once per full sync run (products, "
            'product variations, customers and orders directions only; triggered by "Sync Now"/the scheduled cron, not by webhooks).'
        ),
    )
    settings_sync_summary_discuss_chat_enable = fields.Boolean(
        string='Post Sync Summary to Discuss Chat',
        default=True,
        help=(
            'If enabled, the summary message is sent as a direct-message Discuss chat from "WooCommerce Sync" (similar to the built-in "OdooBot" '
            "conversation), to this record's followers and to whichever user created it."
        ),
    )

    # Sync summary (chatter) tracking - covers the WooCommerce<->Odoo products/variations/customers/orders chunk syncs only. Per-chunk counts/errors are tracked separately in 'woocommerce.sync.summary.event' (append-only, to avoid concurrent-update conflicts); these 3 fields only track the current run's own state
    sync_summary_run_active = fields.Boolean(default=False)
    sync_summary_all_dispatched = fields.Boolean(default=False)
    sync_summary_started_at = fields.Datetime()

    # WordPress REST API settings
    settings_wordpress_username = fields.Char(string='WordPress Username')
    settings_wordpress_user_application_password = fields.Char(string='WordPress User App Password', help='Can be generated from WordPress Admin → Users → Profile → Application Passwords.')

    # WooCommerce webhooks settings
    settings_woocommerce_webhooks_enable = fields.Boolean(
        string='Enable WooCommerce webhooks?',
        help='If enabled, order/product/customer changes in WooCommerce are synced to Odoo as soon as WooCommerce sends the corresponding webhook, in addition to the regular polling sync. Requires the webhooks to be registered (see the related button) and this Odoo instance to be reachable from WooCommerce.',
        default=False,
    )
    settings_woocommerce_webhooks_secret = fields.Char(
        string='Webhook Secret', readonly=True, help='Secret used to validate the X-WC-Webhook-Signature header on incoming WooCommerce webhook requests. Auto-generated when webhooks are registered.'
    )

    # Sync items settings
    settings_woocommerce_to_odoo_products_sync = fields.Boolean(default=True)
    settings_odoo_to_woocommerce_products_sync = fields.Boolean(default=False)
    settings_woocommerce_to_odoo_products_variations_sync = fields.Boolean(default=True)
    settings_odoo_to_woocommerce_variations_sync = fields.Boolean(default=True, readonly=True)
    settings_woocommerce_to_odoo_customers_sync = fields.Boolean(default=True)
    settings_woocommerce_to_odoo_orders_sync = fields.Boolean(default=True)

    # General settings
    settings_woocommerce_user_responsible = fields.Many2one(
        comodel_name='res.users', string='Responsible', help='Default responsible user for WooCommerce operations.', default=lambda self: self.env.user, ondelete='set null'
    )
    settings_woocommerce_modified_records_import = fields.Boolean(
        string='Import only modified records?',
        help="If enabled, only records modified since the last import will be retrieved from WooCommerce using the 'modified_after' WooCommerce REST API parameter. Only enable this option after the first import.",
        default=False,
    )
    settings_woocommerce_images_sync = fields.Boolean(string='Sync images?', default=True)
    settings_odoo_tax_calculation = fields.Selection(
        selection=[('company', 'Match Odoo Company Settings'), ('tax_included', 'Tax Included'), ('tax_excluded', 'Tax Excluded')],
        string='Tax Calculation',
        default='company',
        required=True,
        help=(
            "Whether taxes created/looked up by this sync are tax-included or tax-excluded. 'Match Odoo Company Settings' (the default) uses the company's own setting "
            '(Home Menu → Settings → Invoicing → Taxes → Prices). WooCommerce product/variation/order line prices are always automatically converted to match this '
            'setting before being stored in Odoo, regardless of whether WooCommerce itself sends tax-included or tax-excluded prices.'
        ),
    )

    # Stock management
    settings_woocommerce_products_stock_management = fields.Boolean(string='Sync stock quantity?', default=True)
    settings_woocommerce_products_warehouse_location = fields.Many2one(
        comodel_name='stock.warehouse',
        string='Warehouse',
        help='Warehouse for syncing WooCommerce products stock quantity.',
        default=lambda self: self.env.ref('stock.warehouse0'),
        ondelete='set null',
    )

    # WooCommerce to Odoo products import settings
    settings_woocommerce_products_related_ids_map = fields.Boolean(string='Map related products?', help="Automatically map WooCommerce 'related_ids' products to their Odoo equivalents.", default=False)
    settings_woocommerce_to_odoo_products_language_code = fields.Char(string='Filter WooCommerce products by language (requires Polylang WordPress plugin)', help="2-digit language code (ISO 639-1) (e.g. 'en').")
    settings_woocommerce_products_package_size_unit_default = fields.Boolean(
        string="Default WooCommerce package size to 'Unit'", help="If enabled, newly synced WooCommerce products to Odoo will have their package size unit of measure set to 'Unit(s)'.", default=True
    )
    settings_woocommerce_to_odoo_products_delete = fields.Boolean(
        string='Delete products from Odoo if deleted from WooCommerce?', help='Detects deleted products from WooCommerce and deletes them from Odoo.', default=True
    )

    # WooCommerce to Odoo orders import settings

    ## WooCommerce Order Status
    settings_woocommerce_order_status = fields.Many2many(
        comodel_name='woocommerce.sync.order.status',
        string='Order statuses to import',
        help='Select which order statuses to import from WooCommerce.',
        default=lambda self: self.env['woocommerce.sync.order.status'].search([('status', '=', 'any')]),
    )

    @api.onchange('settings_woocommerce_order_status')
    def order_status_selection_onchange(self: models.Model) -> None:
        if self.settings_woocommerce_order_status:
            # Settings
            field_attribute = 'status'
            field_exclusive = 'any'

            selected = self.settings_woocommerce_order_status.mapped(field_attribute)

            if field_exclusive in selected:
                self.settings_woocommerce_order_status = self.settings_woocommerce_order_status.filtered(lambda record: getattr(record, field_attribute) == field_exclusive)

    @api.constrains('settings_woocommerce_order_status')
    def order_status_selection_check(self: models.Model) -> None:
        for record in self:
            if not record.settings_woocommerce_order_status:
                raise ValidationError(f"At least one value must be selected for the '{record._fields['settings_woocommerce_order_status'].string}' field.")

    settings_woocommerce_delivery_methods_archive = fields.Boolean(string='Archive imported delivery methods?', help='If enabled, imported shipping methods will be created as archived (inactive).', default=True)
    settings_woocommerce_orders_customers_map = fields.Boolean(
        string='Map guest customers to Odoo customers in orders?',
        help='If enabled, orders purchased by guest (unregistered) customers will be mapped to existing Odoo customers by email address. If the customer does not exist in the database, a new customer will be created automatically. If disabled, a customer placeholder will be assigned to the order.',
        default=True,
    )
    settings_woocommerce_line_items_product_map = fields.Boolean(
        string='Map products to existing Odoo products in line items?',
        help="If enabled, line items products will be mapped to existing Odoo products by 'woocommerce_id'. If no match is found, a product placeholder will be used. If disabled, all order line items will be assigned to a placeholder product, but the WooCommerce product name will still be displayed. Warning: Since product details in WooCommerce may have changed after the order was placed, relying only on names can lead to incorrect or inconsistent product mapping.",
        default=True,
    )

    # Odoo to WooCommerce products import settings
    settings_woocommerce_odoo_to_woocommerce_products_language_code = fields.Char(
        string="Filter Odoo products by language defined in the 'language_code' field (requires Polylang WordPress plugin)",
        help="2-digit language code (ISO 639-1) (e.g. 'en').",
    )

    # Scheduled sync settings
    settings_woocommerce_sync_scheduled = fields.Boolean('Enable auto-sync')
    settings_woocommerce_sync_scheduled_interval_minutes = fields.Integer(string='Interval (in Minutes)', default=5)
    ir_cron_id = fields.Many2one(comodel_name='ir.cron', string='Scheduled Cron Job', ondelete='cascade')

    # Test mode settings
    settings_woocommerce_test_mode = fields.Boolean(string='Test mode?', help='If enabled, only the first 10 items of the WooCommerce REST API will be retrieved.', default=False)

    # Last synced
    odoo_woocommerce_last_sync = fields.Datetime(string='Last Synced', compute='odoo_woocommerce_last_sync_assign', store=False, readonly=True)

    def odoo_woocommerce_last_sync_assign(self: models.Model) -> None:
        for record in self:
            sync_log = self.env['woocommerce.sync.log'].search([('woocommerce_connection_id', '=', record.id)], limit=1)
            record.odoo_woocommerce_last_sync = sync_log.odoo_woocommerce_last_sync if sync_log else False

    @api.model_create_multi
    def create(self: models.Model, values_list: list[dict[str, Any]]) -> models.Model:
        records = super().create(values_list)
        for record in records:
            record.cron_job_update()
        return records

    def write(self: models.Model, values: dict[str, Any]) -> bool:
        # Skip cron update if called from cron context
        if self.env.context.get('ir_cron'):
            return super().write(values)

        success = super().write(values)
        for record in self:
            record.cron_job_update()
        return success

    def unlink(self: models.Model) -> bool:
        """Deletes associated cron jobs when a configuration record is deleted."""
        for record in self:
            if record.ir_cron_id:
                record.ir_cron_id.unlink()
        return super().unlink()

    def cron_job_update(self: models.Model) -> None:
        self.ensure_one()

        if version_info[0] == 16:
            cron_values = {
                'name': f'WooCommerce Auto-Sync - {self.settings_woocommerce_connection_url}',
                'model_id': self.env['ir.model']._get(self._name).id,
                'code': (
                    f'model.with_context(cron_running=True).browse({self.id}).with_delay().woocommerce_sync()' if 'queue.job' in self.env else f'model.with_context(cron_running=True).browse({self.id}).woocommerce_sync()'
                ),
                'active': self.settings_woocommerce_sync_scheduled,
                'interval_number': self.settings_woocommerce_sync_scheduled_interval_minutes,
                'interval_type': 'minutes',
                'numbercall': -1,
                'doall': True,
            }

        elif version_info[0] in [18, 19]:
            cron_values = {
                'name': f'WooCommerce Auto-Sync - {self.settings_woocommerce_connection_url}',
                'model_id': self.env['ir.model']._get(self._name).id,
                'code': (
                    f'model.with_context(cron_running=True).browse({self.id}).with_delay().woocommerce_sync()' if 'queue.job' in self.env else f'model.with_context(cron_running=True).browse({self.id}).woocommerce_sync()'
                ),
                'active': self.settings_woocommerce_sync_scheduled,
                'interval_number': self.settings_woocommerce_sync_scheduled_interval_minutes,
                'interval_type': 'minutes',
            }

        # Update the existing cron job
        if self.ir_cron_id:
            self.ir_cron_id.write(cron_values)
        # Create only if scheduled to avoid unnecessary cron jobs
        elif self.settings_woocommerce_sync_scheduled:
            self.ir_cron_id = self.env['ir.cron'].create(cron_values)

    def woocommerce_sync_action(self: models.Model) -> dict[str, Any]:
        self.ensure_one()
        _logger.info("Manual 'Sync Now' button pressed, triggering background sync.")

        # Run woocommerce_sync in the background (requires 'queue_job' Odoo add-on)
        if 'queue.job' in self.env:
            self.with_delay(description=self.job_description('woocommerce_sync')).woocommerce_sync()

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Sync Started (Queue Job)'),
                    'message': _('WooCommerce sync process has been started in the background. %s'),
                    'links': [
                        {
                            'label': _('Open Job Queue'),
                            'url': f'/web#action={self.env["ir.actions.act_window"].with_context(lang=False).search([("res_model", "=", "queue.job")], limit=1).id}&model=queue.job&view_type=list',
                        }
                    ],
                    'sticky': False,
                },
            }

        else:
            self.woocommerce_sync()

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Sync Started (Synchronous)'),
                    'message': _('WooCommerce sync process has been started and is running synchronously.'),
                    'sticky': False,
                },
            }

    @api.model
    def woocommerce_sync(self: models.Model) -> None:
        self.ensure_one()

        # WooCommerce REST API
        woocommerce_api = self.woocommerce_api_get()

        # Check if WooCommerce REST API connection is successful
        if not woocommerce_api:
            error_message = 'WooCommerce REST API connection failed. Sync process halted; Please check your connection settings in the WooCommerce Configuration'
            _logger.error(error_message)
            raise UserError(_(error_message))

        # WooCommerce Settings

        ## WooCommerce currency
        woocommerce_currency = woocommerce_api.get(endpoint='settings/general/woocommerce_currency').json()['value']

        ## WooCommerce measurements
        woocommerce_weight_unit = woocommerce_api.get(endpoint='settings/products/woocommerce_weight_unit').json()['value']
        woocommerce_dimension_unit = woocommerce_api.get(endpoint='settings/products/woocommerce_dimension_unit').json()['value']

        ## WooCommerce tax rates
        woocommerce_prices_include_tax = woocommerce_api.get(endpoint='settings/tax/woocommerce_prices_include_tax').json()['value'].lower() == 'yes'
        woocommerce_tax_rates = woocommerce_api.get(endpoint='taxes').json()
        woocommerce_tax_rates = {woocommerce_tax_rate['class']: float(woocommerce_tax_rate['rate']) for woocommerce_tax_rate in woocommerce_tax_rates}

        ## WooCommerce shipping methods
        woocommerce_shipping_methods = woocommerce_api.get(endpoint='shipping_methods').json()

        self.sync_summary_reset()

        queue_jobs_run_in_sequence = []

        # WooCommerce to Odoo

        ## Products
        if self.settings_woocommerce_to_odoo_products_sync:
            ### Products delete
            if self.settings_woocommerce_to_odoo_products_delete:
                queue_jobs_run_in_sequence.append(self.delayable(priority=None, description=self.job_description('woocommerce_to_odoo_products_delete')).woocommerce_to_odoo_products_delete())

            ### Products
            queue_jobs_run_in_sequence.append(
                self.delayable(priority=None, description=self.job_description('woocommerce_to_odoo_products_sync_batch')).woocommerce_to_odoo_products_sync_batch(
                    woocommerce_currency, woocommerce_tax_rates, woocommerce_prices_include_tax, woocommerce_weight_unit, woocommerce_dimension_unit
                )
            )

            ## Product variations
            if self.settings_woocommerce_to_odoo_products_variations_sync:
                queue_jobs_run_in_sequence.append(
                    self.delayable(priority=None, description=self.job_description('woocommerce_to_odoo_products_variations_sync_batch')).woocommerce_to_odoo_products_variations_sync_batch(
                        woocommerce_currency, woocommerce_tax_rates, woocommerce_prices_include_tax, woocommerce_weight_unit, woocommerce_dimension_unit
                    )
                )

        ## Products related ids map
        if self.settings_woocommerce_products_related_ids_map:
            queue_jobs_run_in_sequence.append(self.delayable(priority=None, description=self.job_description('woocommerce_to_odoo_products_related_ids')).woocommerce_to_odoo_products_related_ids())

        ## Customers
        if self.settings_woocommerce_to_odoo_customers_sync:
            queue_jobs_run_in_sequence.append(self.delayable(priority=None, description=self.job_description('woocommerce_to_odoo_customers_sync_batch')).woocommerce_to_odoo_customers_sync_batch())

        ## Orders
        if self.settings_woocommerce_to_odoo_orders_sync:
            queue_jobs_run_in_sequence.append(
                self.delayable(priority=None, description=self.job_description('woocommerce_to_odoo_orders_sync_batch')).woocommerce_to_odoo_orders_sync_batch(
                    woocommerce_tax_rates, woocommerce_weight_unit, woocommerce_shipping_methods
                )
            )

        # Odoo to WooCommerce

        ## Products
        if self.settings_odoo_to_woocommerce_products_sync:
            queue_jobs_run_in_sequence.append(
                self.delayable(priority=None, description=self.job_description('odoo_to_woocommerce_products_sync')).odoo_to_woocommerce_products_sync(
                    woocommerce_currency, woocommerce_tax_rates, woocommerce_prices_include_tax, woocommerce_weight_unit, woocommerce_dimension_unit
                )
            )

        # Stock quantity
        if self.settings_woocommerce_products_stock_management:
            queue_jobs_run_in_sequence.append(self.delayable(priority=None, description=self.job_description('odoo_woocommerce_products_stock_quantity_sync_batch')).odoo_woocommerce_products_stock_quantity_sync_batch())
            queue_jobs_run_in_sequence.append(
                self.delayable(priority=None, description=self.job_description('update_sync_last_log')).update_sync_last_log(
                    woocommerce_connection_id=self.id, model_name='woocommerce.sync.stock.log', field_name='odoo_woocommerce_last_sync'
                )
            )

        # Store 'odoo_woocommerce_last_sync'
        queue_jobs_run_in_sequence.append(
            self.delayable(priority=None, description=self.job_description('update_sync_last_log')).update_sync_last_log(
                woocommerce_connection_id=self.id, model_name='woocommerce.sync.log', field_name='odoo_woocommerce_last_sync'
            )
        )

        # Marks that every stage above has finished dispatching its chunk jobs, so the aggregate summary chatter message can be posted once the last one completes
        queue_jobs_run_in_sequence.append(self.delayable(priority=None, description=self.job_description('sync_summary_finalize_dispatch')).sync_summary_finalize_dispatch())

        # Create chain and delay the jobs
        if queue_jobs_run_in_sequence:
            chain(*queue_jobs_run_in_sequence).delay()

    @api.model
    def update_sync_last_log(self: models.Model, woocommerce_connection_id: int, model_name: str, field_name: str) -> None:
        sync_log = self.env[model_name].search([('woocommerce_connection_id', '=', woocommerce_connection_id)], limit=1)

        if sync_log:
            sync_log.write({field_name: fields.Datetime.now()})

        else:
            self.env[model_name].create({'woocommerce_connection_id': woocommerce_connection_id, field_name: fields.Datetime.now()})

    def woocommerce_api_get(self: models.Model, validate: bool = True) -> WooCommerceClient | None:
        """Retrieves a WooCommerce REST API client. Set 'validate=False' to skip the 'system_status' connectivity ping (e.g. in per-record queue jobs where the connection was already validated by the batch job that scheduled them)."""

        self.ensure_one()

        if not self.settings_woocommerce_connection_url or not self.settings_woocommerce_consumer_key or not self.settings_woocommerce_consumer_secret or not self.settings_woocommerce_timeout:
            _logger.error('Missing WooCommerce REST API configuration details (url, consumer key, consumer secret or timeout). Cannot retrieve API instance')
            return False

        woocommerce_api = WooCommerceClient(
            url=self.settings_woocommerce_connection_url,
            consumer_key=self.settings_woocommerce_consumer_key,
            consumer_secret=self.settings_woocommerce_consumer_secret,
            timeout=self.settings_woocommerce_timeout,
            user_agent=self.settings_user_agent or 'Odoo-Woocommerce Sync',
            test_mode=self.settings_woocommerce_test_mode,
        )

        if validate and not woocommerce_api.validate_connection():
            return False

        return woocommerce_api

    @api.model
    def woocommerce_api_request(self: models.Model, woocommerce_api: WooCommerceClient, endpoint: str, params: dict[str, Any] | None = None, max_retries: int = 5) -> Any:
        """Performs a single WooCommerce REST API GET request with rate-limit (HTTP 429) and transient server error (5xx) retry/backoff handling."""
        self.ensure_one()
        return woocommerce_api.request(endpoint=endpoint, params=params, max_retries=max_retries)

    @api.model
    def woocommerce_api_get_all_items(self: models.Model, woocommerce_api: WooCommerceClient, endpoint: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.ensure_one()
        return woocommerce_api.get_all_items(endpoint=endpoint, params=params)

    @api.model
    def woocommerce_api_get_items_in_batches(
        self: models.Model, woocommerce_api: WooCommerceClient, endpoint: str, params: dict[str, Any] | None = None, batch_size: int = 10
    ) -> Generator[list[dict[str, Any]], Any, None]:
        self.ensure_one()
        yield from woocommerce_api.get_items_in_batches(endpoint=endpoint, params=params, batch_size=batch_size)

    @api.model
    def odoo_woocommerce_last_sync_retrieve(self: models.Model) -> datetime | bool:
        """Returns the last sync's naive UTC datetime, as-is (Odoo 'Datetime' fields are always stored/returned in naive UTC).

        Used exclusively to build the 'modified_after' WooCommerce REST API filter, which is compared against WooCommerce's own GMT-based 'date_modified_gmt' field - converting to the current user's local timezone here would silently shift that filter by the user's UTC offset, causing incorrect results.
        """
        woocommerce_sync_log = self.env['woocommerce.sync.log'].search([('woocommerce_connection_id', '=', self.id)], limit=1)
        return woocommerce_sync_log.odoo_woocommerce_last_sync or False

    @staticmethod
    def datetime_convert(date_string: str, tz: timezone | None = UTC) -> datetime | bool:
        """Convert ISO 8601 date format string to a naive datetime, as required by Odoo Datetime fields."""
        if date_string:
            try:
                parsed_date = datetime.fromisoformat(date_string)
                if tz is not None:
                    if parsed_date.tzinfo is None:
                        parsed_date = parsed_date.replace(tzinfo=tz)
                    parsed_date = parsed_date.astimezone(tz)
                # Odoo Datetime fields require naive datetimes
                return parsed_date.replace(tzinfo=None)
            except ValueError:
                raise ValidationError(f'Invalid WooCommerce date format: {date_string}')
        return False

    @classmethod
    def datetime_gmt_pairs_convert(cls, values: dict[str, Any], base_columns: list[str]) -> None:
        """Converts each '<base>'/'<base>_gmt' column pair in 'values' (in place) to the same naive-UTC instant, taken from the '_gmt' value.

        WooCommerce's non-'_gmt' variant is reported in the store's own local timezone with no UTC offset attached, so naively treating it as UTC (as this used to do) silently mislabels local time as UTC - Odoo's timezone-aware 'Datetime' widgets then shift it a second time on display, making it look 2x the store's UTC offset ahead of the real time. Reusing the '_gmt' value (always true UTC) for both columns avoids that double-shift.
        """
        for column in base_columns:
            gmt_column = f'{column}_gmt'
            if values.get(gmt_column):
                values[gmt_column] = cls.datetime_convert(values[gmt_column], tz=UTC)
                values[column] = values[gmt_column]
            elif values.get(column):
                values[column] = cls.datetime_convert(values[column], tz=UTC)

    @api.model
    def image_download(self: models.Model, image_url: str, attempts: int = 3, backoff_seconds: float = 1.0) -> requests.Response:
        """Downloads a URL with a small retry/backoff loop, matching the resilience already applied to WooCommerce REST API calls in 'WooCommerceClient.request()'."""
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                response = requests.get(url=image_url, timeout=10)
                response.raise_for_status()
                return response
            except requests.exceptions.RequestException as error:
                last_error = error
                if attempt < attempts:
                    _logger.warning(f'Attempt {attempt}/{attempts} to download image from {image_url} failed: {error}. Retrying...')
                    time.sleep(backoff_seconds * attempt)

        raise last_error

    @api.model
    def image_download_file_to_base64(self: models.Model, woocommerce_images: dict[str, Any]) -> str | None:
        """Downloads the featured image file from WooCommerce and returns it as a base64-encoded string."""
        if not woocommerce_images:
            return None

        # Get the image URL
        image_url = woocommerce_images.get('src')

        # Download and process the image
        try:
            response = self.image_download(image_url)

            # Open image with PIL
            img = Image.open(BytesIO(response.content))

            # Convert to base64 encoding
            buffered = BytesIO()
            img.convert('RGB').save(buffered, format='PNG')
            img_base64 = b64encode(buffered.getvalue()).decode('utf-8')

            return img_base64

        except requests.exceptions.RequestException as error:
            _logger.error(f'Failed to download image from {image_url}: {error}')
        except Exception as error:
            _logger.error(f'Error processing the image from {image_url}: {error}')
        return None

    @api.model
    def image_process_attachments(self: models.Model, woocommerce_images: list[dict[str, Any]], product: models.Model) -> None:
        """Downloads WooCommerce gallery images and stores them as 'base_multi_image.image' records (if 'base_multi_image' is installed) or as 'ir.attachment' records."""
        if not woocommerce_images:
            return

        for image_data in woocommerce_images:
            if not image_data['src'] or not image_data['name']:
                continue

            try:
                response = self.image_download(image_data['src'])

                # Normalize to PNG via PIL - handles AVIF, WebP, RGBA, etc.
                img = Image.open(BytesIO(response.content))
                buffered = BytesIO()
                img.convert('RGB').save(buffered, format='PNG')
                img_base64 = b64encode(buffered.getvalue()).decode('utf-8')

                # Multiple Images Base (requires 'base_multi_image' Odoo add-on)
                if 'base_multi_image.image' in self.env:
                    existing = self.env['base_multi_image.image'].with_context(lang=False).search([('owner_model', '=', 'product.template'), ('owner_id', '=', product.id), ('name', '=', image_data['name'])], limit=1)

                    if not existing:
                        if version_info[0] == 16:
                            self.env['base_multi_image.image'].create({'owner_model': 'product.template', 'owner_id': product.id, 'name': image_data['name'], 'image_1920': img_base64})

                        elif version_info[0] == 18:  # TODO Odoo v19: check https://github.com/OCA/server-tools/tree/19.0/base_multi_image when available
                            self.env['base_multi_image.image'].create({'owner_model': 'product.template', 'owner_id': product.id, 'name': image_data['name'], 'storage': 'filestore', 'attachment_image': img_base64})

                else:
                    existing = self.env['ir.attachment'].search([('res_model', '=', 'product.template'), ('res_id', '=', product.id), ('name', '=', image_data['name'])], limit=1)

                    if not existing:
                        self.env['ir.attachment'].create({'name': image_data['name'], 'type': 'binary', 'datas': img_base64, 'mimetype': 'image/png', 'res_model': 'product.template', 'res_id': product.id})

            except requests.exceptions.RequestException as error:
                _logger.error(f'Failed to download image from {image_data["src"]}: {error}')
            except Exception as error:
                _logger.error(f'Error processing image from {image_data["src"]}: {error}')

    @api.model
    def odoo_brand_create_or_retrieve(self: models.Model, brand_name: str, cache: dict[str, int] | None = None) -> models.Model | bool:
        """Create or retrieve an Odoo brand. If 'cache' (name -> id) is provided, it is used to avoid repeated searches/creates for the same name within a batch."""
        if not brand_name:
            return False

        if cache is not None and brand_name in cache:
            return self.env['product.brand'].browse(cache[brand_name])

        try:
            odoo_brand = self.env['product.brand'].with_context(lang=False).search([('name', '=', brand_name)], limit=1)

            if not odoo_brand:
                odoo_brand = self.env['product.brand'].create({'name': brand_name})
                _logger.info(f'Created new WooCommerce brand in Odoo: {odoo_brand.name}')

            if cache is not None:
                cache[brand_name] = odoo_brand.id

            return odoo_brand

        except Exception as error:
            _logger.error(f'Failed to create or retrieve WooCommerce brand in Odoo: {brand_name}: {error}')
            return False

    @api.model
    def odoo_category_create_or_retrieve(self: models.Model, category_name: str, cache: dict[str, int] | None = None) -> models.Model | bool:
        """Create or retrieve an Odoo category. If 'cache' (name -> id) is provided, it is used to avoid repeated searches/creates for the same name within a batch."""
        if not category_name:
            return False

        if cache is not None and category_name in cache:
            return self.env['product.category'].browse(cache[category_name])

        try:
            odoo_category = self.env['product.category'].search([('name', '=', category_name)], limit=1)

            if not odoo_category:
                odoo_category = self.env['product.category'].create({'name': category_name})
                _logger.info(f'Created new WooCommerce category in Odoo: {odoo_category.name}')

            if cache is not None:
                cache[category_name] = odoo_category.id

            return odoo_category

        except Exception as error:
            _logger.error(f'Failed to create or retrieve WooCommerce category in Odoo: {category_name}: {error}')
            return False

    @api.model
    def odoo_currency_retrieve(self: models.Model, currency: str) -> models.Model | bool:
        """Retrieve an Odoo currency."""
        if not currency:
            return False

        try:
            odoo_currency = self.env['res.currency'].search([('active', '=', True), ('name', '=', currency)], limit=1)

            if odoo_currency:
                return odoo_currency

            else:
                _logger.warning(f'Not found WooCommerce currency in Odoo: {currency}')
                return False

        except Exception as error:
            _logger.error(f'Failed to retrieve WooCommerce currency in Odoo: {currency}: {error}')
            return False

    @api.model
    def odoo_country_retrieve(self: models.Model, country_code: str, cache: dict[str, int] | None = None) -> models.Model:
        """Retrieve an Odoo country by its ISO code. Returns an empty 'res.country' recordset if not found or 'country_code' is falsy. If 'cache' (country_code -> id) is provided, it is used to avoid repeated searches for the same code within a batch."""
        if cache is not None and country_code in cache:
            return self.env['res.country'].browse(cache[country_code])

        try:
            odoo_country = self.env['res.country'].with_context(lang=False).search([('code', '=', country_code)], limit=1) if country_code else self.env['res.country']

            if cache is not None:
                cache[country_code] = odoo_country.id

            return odoo_country

        except Exception as error:
            _logger.error(f'Failed to retrieve WooCommerce country in Odoo: {country_code}: {error}')
            return self.env['res.country']

    @api.model
    def odoo_tag_create_or_retrieve(self: models.Model, tag_name: str, cache: dict[str, int] | None = None) -> models.Model | bool:
        """Create or retrieve an Odoo tag. If 'cache' (name -> id) is provided, it is used to avoid repeated searches/creates for the same name within a batch."""
        if not tag_name:
            return False

        if cache is not None and tag_name in cache:
            return self.env['product.tag'].browse(cache[tag_name])

        try:
            odoo_tag = self.env['product.tag'].with_context(lang=False).search([('name', '=', tag_name)], limit=1)

            if not odoo_tag:
                with self.env.cr.savepoint():
                    odoo_tag = self.env['product.tag'].create({'name': tag_name})
                    _logger.info(f'Created new WooCommerce tag in Odoo: {odoo_tag.name}')

            if cache is not None:
                cache[tag_name] = odoo_tag.id

            return odoo_tag

        except Exception as error:
            _logger.error(f'Failed to create or retrieve WooCommerce tag in Odoo: {tag_name}: {error}')
            return False

    @api.model
    def odoo_tax_calculation_price_include(self: models.Model) -> bool:
        """Resolves whether Odoo taxes/prices created by this sync should be tax-included, based on the 'settings_odoo_tax_calculation' setting.

        'company' matches the Odoo company's own 'Prices' setting. This setting lives on 'env.company.account_price_include' from Odoo 18 onwards; Odoo 16/17 have no equivalent company-level field, so on those versions we fall back to the price-include flag of the company's default sale tax ('env.company.account_sale_tax_id.price_include'), defaulting to tax-excluded if no default sale tax is configured.
        """
        if self.settings_odoo_tax_calculation == 'company':
            company = self.env.company
            if 'account_price_include' in company._fields:
                return company.account_price_include == 'tax_included'

            return bool(company.account_sale_tax_id.price_include)

        return self.settings_odoo_tax_calculation == 'tax_included'

    @api.model
    def odoo_tax_rate_create_or_retrieve(self: models.Model, tax_rate: float | None, cache: dict[float, int] | None = None) -> models.Model | bool:
        """Create or retrieve an Odoo tax rate.

        The tax's 'price_include' always matches the 'settings_odoo_tax_calculation' setting (see 'odoo_tax_calculation_price_include()') instead of WooCommerce's - so a freshly-created tax always matches every other tax already used in this Odoo company by default. Prices are converted to match this same convention before being stored on the Odoo side - see 'woocommerce_price_to_odoo_price()'.
        """
        if tax_rate is None:
            return False

        if cache is not None and tax_rate in cache:
            return self.env['account.tax'].browse(cache[tax_rate])

        try:
            odoo_price_include = self.odoo_tax_calculation_price_include()
            # ':g' avoids '19.0%' (which doesn't match an existing '19%' tax) for whole-number rates
            tax_name = f'{tax_rate:g}%'
            tax_domain = [('active', '=', True), ('name', '=', tax_name), ('amount', '=', tax_rate), ('type_tax_use', '=', 'sale'), ('price_include', '=', odoo_price_include)]
            odoo_tax_rate = self.env['account.tax'].search(tax_domain, limit=1)

            if not odoo_tax_rate:
                try:
                    with self.env.cr.savepoint():
                        odoo_tax_rate = self.env['account.tax'].create({'name': tax_name, 'amount': tax_rate, 'type_tax_use': 'sale', 'price_include': odoo_price_include})
                    _logger.info(f'Created new WooCommerce tax rate in Odoo: {odoo_tax_rate.name}')
                except Exception:
                    # A concurrent sync job already created this tax rate between the search above and this create() - reuse it instead of failing
                    odoo_tax_rate = self.env['account.tax'].search(tax_domain, limit=1)
                    if not odoo_tax_rate:
                        raise

            if cache is not None:
                cache[tax_rate] = odoo_tax_rate.id

            return odoo_tax_rate

        except Exception as error:
            _logger.error(f'Failed to create or retrieve WooCommerce tax rate in Odoo: {tax_rate}%: {error}')
            return False

    @api.model
    def woocommerce_price_to_odoo_price(self: models.Model, price: str | float | None, tax_rate: float | None, woocommerce_prices_include_tax: bool) -> float:
        """Converts a WooCommerce product/variation price to match whichever tax convention the Odoo tax created by 'odoo_tax_rate_create_or_retrieve()' uses (see 'odoo_tax_calculation_price_include()'), so the stored 'list_price' and its attached tax always agree on whether tax is included.

        WooCommerce's product/variation 'price' and 'regular_price' fields hold whatever the shop admin entered, interpreted as tax-included/tax-excluded based on the store's own 'Prices entered with tax' setting - unlike order line items, whose 'price' is always tax-excluded regardless of that setting (see 'woocommerce_to_odoo_order_sync()').
        """
        if not price:
            return 0.0

        price = float(price)
        if not tax_rate:
            return price

        odoo_price_include = self.odoo_tax_calculation_price_include()

        if woocommerce_prices_include_tax and not odoo_price_include:
            price /= 1 + (tax_rate / 100)
        elif not woocommerce_prices_include_tax and odoo_price_include:
            price *= 1 + (tax_rate / 100)

        return price

    @api.model
    def odoo_unit_of_measure_create_or_retrieve(self: models.Model, unit_of_measure_name: str, cache: dict[str, int] | None = None) -> models.Model | bool:
        """Create or retrieve an Odoo unit of measure."""
        if not unit_of_measure_name:
            return False

        if cache is not None and unit_of_measure_name in cache:
            return self.env['uom.uom'].browse(cache[unit_of_measure_name])

        try:
            odoo_unit_of_measure = self.env['uom.uom'].with_context(lang=False).search([('active', '=', True), ('name', '=', unit_of_measure_name)], limit=1)

            if not odoo_unit_of_measure:
                odoo_unit_of_measure = self.env['uom.uom'].create({'name': unit_of_measure_name, 'category_id': self.env.ref('uom.uom_categ_unit').id, 'factor': 1, 'uom_type': 'reference'})
                _logger.info(f'Created new WooCommerce unit of measure in Odoo: {odoo_unit_of_measure.name}')

            if cache is not None:
                cache[unit_of_measure_name] = odoo_unit_of_measure.id

            return odoo_unit_of_measure

        except Exception as error:
            _logger.error(f'Failed to create or retrieve WooCommerce unit of measure in Odoo: {unit_of_measure_name}: {error}')
            return False

    @api.model
    def odoo_unit_of_measure_dimension_retrieve(self: models.Model, dimensional_uom_name: str) -> models.Model | bool:
        """Retrieve an Odoo dimensional unit of measure."""
        if not dimensional_uom_name:
            return False

        try:
            odoo_dimensional_uom = self.env['uom.uom'].with_context(lang=False).search([('active', '=', True), ('name', '=', dimensional_uom_name)], limit=1)

            if not odoo_dimensional_uom:
                _logger.warning(f'Not found WooCommerce dimensional UoM in Odoo: {dimensional_uom_name}')

            return odoo_dimensional_uom

        except Exception as error:
            _logger.error(f'Failed to retrieve WooCommerce dimensional UoM in Odoo: {dimensional_uom_name}: {error}')
            return False

    @api.model
    def odoo_customer_placeholder_create_or_retrieve(self: models.Model) -> models.Model:
        """Creates or retrieves an Odoo placeholder customer for WooCommerce Order integration. The customer placeholder is archived (active=False) and can be used to satisfy the customer requirement on sale orders."""

        odoo_customer_placeholder = self.env['res.partner'].with_context(active_test=False).search([('ref', '=', 'WooCommerce_Customer_Placeholder')], limit=1)

        if not odoo_customer_placeholder:
            # Create the placeholder customer if not found
            customer_values = {
                'name': 'WooCommerce Customer Placeholder',
                'ref': 'WooCommerce_Customer_Placeholder',
                'type': 'contact',
                'customer_rank': 0,
                'active': False,
            }
            odoo_customer_placeholder = self.env['res.partner'].create(customer_values)

        else:
            # Ensure the customer is archived
            if odoo_customer_placeholder.active:
                odoo_customer_placeholder.write({'active': False})

        return odoo_customer_placeholder

    @api.model
    def odoo_product_placeholder_create_or_retrieve(self: models.Model) -> models.Model:
        """Creates or retrieves an Odoo placeholder product for WooCommerce Order Line Item integration. The product placeholder is archived (active=False) and can be used to satisfy the product requirement on sale order lines."""

        odoo_product_placeholder = self.env['product.template'].with_context(active_test=False, lang=False).search([('default_code', '=', 'WooCommerce_Product_Placeholder')], limit=1)

        if not odoo_product_placeholder:
            # Create the placeholder product if not found
            product_values = {
                'name': 'WooCommerce Product Placeholder',
                'default_code': 'WooCommerce_Product_Placeholder',
                'type': 'service',
                'list_price': 0.0,
                'active': False,
                'sync_to_woocommerce': False,
            }
            odoo_product_placeholder = self.env['product.template'].create(product_values)

        else:
            # Ensure the product is archived
            if odoo_product_placeholder.active:
                odoo_product_placeholder.write({'active': False})

        # Ensure at least one variant exists
        if not odoo_product_placeholder.product_variant_ids:
            self.env['product.product'].create({'product_tmpl_id': odoo_product_placeholder.id, 'default_code': odoo_product_placeholder.default_code})

        return odoo_product_placeholder

    @api.model
    def odoo_customer_shipping_address_create_or_update(self: models.Model, odoo_customer: models.Model, shipping_values: dict[str, Any], odoo_country_cache: dict[str, int] | None = None) -> models.Model:
        """Create or update a 'delivery' type child contact under 'odoo_customer' from WooCommerce shipping address fields (works for both 'customer_values' and 'order_values', which share the same 'woocommerce_shipping_*' keys), so 'partner_shipping_id' can point to the real shipping address instead of always reusing the billing partner. Returns 'odoo_customer' unchanged if no shipping address data is present."""
        if not odoo_customer or not any(shipping_values.get(key) for key in ('woocommerce_shipping_address_1', 'woocommerce_shipping_city', 'woocommerce_shipping_postcode')):
            return odoo_customer

        shipping_name = f'{shipping_values.get("woocommerce_shipping_first_name") or ""} {shipping_values.get("woocommerce_shipping_last_name") or ""}'.strip() or 'Shipping Address'

        shipping_partner_values = {
            'type': 'delivery',
            'parent_id': odoo_customer.id,
            'name': shipping_name,
            'street': shipping_values.get('woocommerce_shipping_address_1'),
            'street2': shipping_values.get('woocommerce_shipping_address_2'),
            'city': shipping_values.get('woocommerce_shipping_city'),
            'zip': shipping_values.get('woocommerce_shipping_postcode'),
            'country_id': self.odoo_country_retrieve(shipping_values.get('woocommerce_shipping_country'), cache=odoo_country_cache).id,
        }

        odoo_shipping_partner = (
            self.env['res.partner'].with_context(lang=False).search([('parent_id', '=', odoo_customer.id), ('type', '=', 'delivery'), ('woocommerce_site_url', '=', self.settings_woocommerce_connection_url)], limit=1)
        )

        if odoo_shipping_partner:
            odoo_shipping_partner.write(shipping_partner_values)
        else:
            shipping_partner_values['woocommerce_site_url'] = self.settings_woocommerce_connection_url
            odoo_shipping_partner = self.env['res.partner'].create(shipping_partner_values)

        return odoo_shipping_partner

    @api.model
    def odoo_delivery_carrier_create_or_retrieve(self: models.Model, woocommerce_shipping_methods: list[dict[str, Any]], shipping_line: dict[str, Any]) -> models.Model | bool:
        """Create or retrieve an Odoo delivery carrier."""

        if not shipping_line:
            return False

        odoo_delivery_carrier = (
            self.env['delivery.carrier']
            .with_context(active_test=False, lang=False)
            .search([('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('name', '=', shipping_line['method_title'])], limit=1)
        )

        if odoo_delivery_carrier:
            # If current view setting is "active" and delivery carrier setting is "archive", activate it
            if not self.settings_woocommerce_delivery_methods_archive and not odoo_delivery_carrier.active:
                odoo_delivery_carrier.active = True
            # If current view setting is "archive" and delivery carrier setting "active", archive it
            elif self.settings_woocommerce_delivery_methods_archive and odoo_delivery_carrier.active:
                odoo_delivery_carrier.active = False

        else:
            # Shared shipping service product
            delivery_product = (
                self.env['product.product']
                .with_context(lang=False)
                .search(
                    [('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('default_code', '=', 'woocommerce_shipping_fee')],
                    limit=1,
                )
            )

            if not delivery_product:
                delivery_product = self.env['product.product'].create(
                    {
                        'woocommerce_site_url': self.settings_woocommerce_connection_url,
                        'name': 'WooCommerce Shipping Fee',
                        'default_code': 'woocommerce_shipping_fee',
                        'type': 'service',
                        'sale_ok': True,
                        'purchase_ok': False,
                        'list_price': 0.0,
                    },
                )

            # Create the delivery carrier with the associated product_id
            odoo_delivery_carrier = self.env['delivery.carrier'].create(
                {
                    'woocommerce_site_url': self.settings_woocommerce_connection_url,
                    'name': shipping_line['method_title'],
                    'product_id': delivery_product.id,
                    'delivery_type': 'fixed',
                    'active': not (self.settings_woocommerce_delivery_methods_archive),
                },
            )

        return odoo_delivery_carrier

    @api.model
    def odoo_woocommerce_service_product_create_or_retrieve(self: models.Model, default_code: str, name: str) -> models.Model:
        """Create or retrieve a shared service product used to represent WooCommerce order fee/coupon lines as real 'sale.order.line' rows instead of only raw JSON."""
        odoo_product = self.env['product.product'].with_context(lang=False).search([('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('default_code', '=', default_code)], limit=1)

        if not odoo_product:
            odoo_product = self.env['product.product'].create(
                {
                    'woocommerce_site_url': self.settings_woocommerce_connection_url,
                    'name': name,
                    'default_code': default_code,
                    'type': 'service',
                    'sale_ok': True,
                    'purchase_ok': False,
                    'list_price': 0.0,
                },
            )

        return odoo_product

    def odoo_woocommerce_products_stock_quantity_sync(
        self: models.Model, odoo_product: models.Model, woocommerce_products_stock_map: dict[int, dict[str, Any]], odoo_quants_by_product_id: dict[int, models.Model] | None = None
    ) -> dict[str, Any] | None:
        """Syncs stock for a single Odoo product. If WooCommerce is the most recent source, updates Odoo directly and returns 'None'. If Odoo is the most recent source, returns a payload for the caller to push to WooCommerce in a batch request instead of one REST call per product."""
        product_woocommerce_id = odoo_product.woocommerce_id or odoo_product.product_tmpl_id.woocommerce_id

        woocommerce_stock_info = woocommerce_products_stock_map.get(product_woocommerce_id)

        if not woocommerce_stock_info:
            return None

        # WooCommerce product stock quantity
        woocommerce_stock_quantity = woocommerce_stock_info.get('stock_quantity')
        if woocommerce_stock_quantity is None:
            _logger.warning(f'WooCommerce product {odoo_product.name} (WooCommerce product ID {product_woocommerce_id}) has a None "stock_quantity", defaulting to 0.0')
            woocommerce_stock_quantity = 0.0

        # Odoo product stock quant (prefetched for the whole chunk by the caller to avoid one search per product)
        odoo_product_stock_quant = (odoo_quants_by_product_id or {}).get(odoo_product.id) or self.env['stock.quant']

        if odoo_product_stock_quant and float_compare(woocommerce_stock_quantity, odoo_product.qty_available, precision_rounding=odoo_product.uom_id.rounding) == 0:
            return None

        # Get last update dates
        odoo_stock_quantity_last_update = getattr(odoo_product_stock_quant, 'stock_quantity_last_update', None) if odoo_product_stock_quant else None
        odoo_stock_quantity_last_update = odoo_stock_quantity_last_update if isinstance(odoo_stock_quantity_last_update, datetime) else fields.datetime.min

        odoo_woocommerce_stock_last_sync = odoo_product.woocommerce_stock_last_sync
        odoo_woocommerce_stock_last_sync = odoo_woocommerce_stock_last_sync if isinstance(odoo_woocommerce_stock_last_sync, datetime) else fields.datetime.min

        woocommerce_date_modified_gmt = self.datetime_convert(woocommerce_stock_info['date_modified_gmt'])
        woocommerce_date_modified_gmt = woocommerce_date_modified_gmt if isinstance(woocommerce_date_modified_gmt, datetime) else fields.datetime.min

        # Determine the latest timestamp among all sources
        latest_timestamp = max(odoo_stock_quantity_last_update, odoo_woocommerce_stock_last_sync, woocommerce_date_modified_gmt)

        # If WooCommerce is the most recent source of truth, update Odoo
        if latest_timestamp == woocommerce_date_modified_gmt:
            if odoo_product_stock_quant:
                odoo_product_stock_quant.with_context(from_external_sync=True).with_company(self.env.company).write({'quantity': woocommerce_stock_quantity})
                _logger.info(
                    f'Updated WooCommerce product stock quantity in Odoo: {odoo_product.name} (Odoo product ID: {odoo_product.id}, WooCommerce product ID: {product_woocommerce_id}) - Stock quantity: {woocommerce_stock_quantity}'
                )

            else:
                self.env['stock.quant'].create(
                    {
                        'woocommerce_site_url': self.settings_woocommerce_connection_url,
                        'product_id': odoo_product.id,
                        'quantity': woocommerce_stock_quantity,
                        'location_id': self.settings_woocommerce_products_warehouse_location.lot_stock_id.id,
                    }
                )
                _logger.info(
                    f'Created WooCommerce product stock quantity object in Odoo: {odoo_product.name} (Odoo product ID: {odoo_product.id}, WooCommerce product ID: {product_woocommerce_id}) - Stock quantity: {woocommerce_stock_quantity}'
                )

            # Update the stock last sync
            odoo_product.woocommerce_stock_last_sync_update(woocommerce_date_modified_gmt)

        # If Odoo is the most recent source, return a payload for the caller to batch-push to WooCommerce (instead of one REST call per product)
        else:
            return {
                'odoo_product': odoo_product,
                'woocommerce_parent_id': odoo_product.woocommerce_parent_id,
                'woocommerce_id': product_woocommerce_id,
                'stock_quantity': odoo_product.qty_available,
            }

        return None

    @api.model
    def odoo_woocommerce_products_stock_quantity_chunk_sync(self: models.Model, odoo_product_ids: list[int], woocommerce_products_stock_map: dict[int, dict[str, Any]]) -> None:
        """Processes a chunk of Odoo products' stock quantity sync within a single queue job, batching the Odoo-to-WooCommerce pushes into 'products/batch'/'products/{id}/variations/batch' requests instead of one REST call per product."""
        self.ensure_one()

        # Prefetch all relevant stock quants for this chunk in a single query instead of one search per product
        odoo_products = self.env['product.product'].browse(odoo_product_ids).exists()
        odoo_product_stock_quants = self.env['stock.quant'].search(
            [
                ('product_tmpl_id.woocommerce_site_url', '=', self.settings_woocommerce_connection_url),
                ('product_id', 'in', odoo_products.ids),
                ('location_id', '=', self.settings_woocommerce_products_warehouse_location.lot_stock_id.id),
            ],
        )
        odoo_quants_by_product_id = {quant.product_id.id: quant for quant in odoo_product_stock_quants}

        pending_pushes = []
        for odoo_product in odoo_products:
            try:
                push_payload = self.odoo_woocommerce_products_stock_quantity_sync(odoo_product, woocommerce_products_stock_map, odoo_quants_by_product_id)
                if push_payload:
                    pending_pushes.append(push_payload)
            except Exception:
                _logger.exception(f'Error syncing stock quantity for Odoo product ID {odoo_product.id} within chunk job')

        if not pending_pushes:
            return

        woocommerce_api = self.woocommerce_api_get(validate=False)
        if not woocommerce_api:
            _logger.error('WooCommerce REST API connection failed. Cannot push Odoo stock quantities to WooCommerce for this chunk')
            return

        # Simple products go through 'products/batch'; variations are grouped by parent product and go through 'products/{parent_id}/variations/batch'
        simple_product_pushes = [push for push in pending_pushes if not push['woocommerce_parent_id']]
        variation_pushes_by_parent_id: dict[str, list[dict[str, Any]]] = {}
        for push in pending_pushes:
            if push['woocommerce_parent_id']:
                variation_pushes_by_parent_id.setdefault(push['woocommerce_parent_id'], []).append(push)

        synced_by_woocommerce_id: dict[str, dict[str, Any]] = {}

        for push_chunk in self.list_chunks(simple_product_pushes, 100):
            response = woocommerce_api.batch('products', update=[{'id': push['woocommerce_id'], 'stock_quantity': push['stock_quantity']} for push in push_chunk])
            for woocommerce_product in response.get('update', []):
                synced_by_woocommerce_id[str(woocommerce_product.get('id'))] = woocommerce_product

        for parent_id, parent_pushes in variation_pushes_by_parent_id.items():
            for push_chunk in self.list_chunks(parent_pushes, 100):
                response = woocommerce_api.batch(f'products/{parent_id}/variations', update=[{'id': push['woocommerce_id'], 'stock_quantity': push['stock_quantity']} for push in push_chunk])
                for woocommerce_product in response.get('update', []):
                    synced_by_woocommerce_id[str(woocommerce_product.get('id'))] = woocommerce_product

        for push in pending_pushes:
            woocommerce_product = synced_by_woocommerce_id.get(str(push['woocommerce_id']))
            odoo_product = push['odoo_product']

            if not woocommerce_product or woocommerce_product.get('error'):
                _logger.error(
                    f'Failed to update Odoo product stock quantity in WooCommerce (Odoo product ID: {odoo_product.id}, WooCommerce product ID: {push["woocommerce_id"]}): '
                    f'{woocommerce_product.get("error") if woocommerce_product else "no response"}'
                )
                continue

            _logger.info(
                f'Updated Odoo product stock quantity in WooCommerce: {odoo_product.name} (Odoo product ID: {odoo_product.id}, WooCommerce product ID: {push["woocommerce_id"]}) - Stock quantity: {push["stock_quantity"]}'
            )

            if 'date_modified_gmt' in woocommerce_product:
                odoo_product.woocommerce_stock_last_sync_update(self.datetime_convert(woocommerce_product['date_modified_gmt']))

    def woocommerce_product_variations_stock_retrieve(self: models.Model, parent_id: int, temporary_sync_data_record_id: int) -> bool:
        """Retrieves all WooCommerce product variations for the given WooCommerce product ID and stores them in a temporary sync data record."""
        self.ensure_one()

        woocommerce_api = self.woocommerce_api_get()

        try:
            variations = self.woocommerce_api_get_all_items(woocommerce_api, endpoint=f'products/{parent_id}/variations', params={'_fields': 'id,date_modified_gmt,stock_quantity'})

            temporary_sync_data_record = self.env['woocommerce.sync.data.temp'].browse(temporary_sync_data_record_id)
            if not temporary_sync_data_record:
                _logger.error(f'Temporary sync data record not found: {temporary_sync_data_record_id}')
                return False

            with self.env.cr.savepoint():
                current_data = temporary_sync_data_record.woocommerce_products_variations_data or {}
                for variation in variations:
                    current_data[variation['id']] = variation
                temporary_sync_data_record.woocommerce_products_variations_data = current_data

            return True

        except Exception as error:
            _logger.error(f'Failed to retrieve WooCommerce product variations for WooCommerce product ID {parent_id}. Error: {error}')
            return False

    def odoo_woocommerce_products_stock_quantity_process(self: models.Model, temporary_sync_data_record_id: int, *args) -> None:
        """Processes all product data after variations have been fetched and schedules individual syncs."""
        self.ensure_one()

        temporary_sync_data_record = self.env['woocommerce.sync.data.temp'].browse(temporary_sync_data_record_id)
        if not temporary_sync_data_record or not temporary_sync_data_record.woocommerce_products_variations_data:
            _logger.warning('Temporary sync data record not found or no data to process')
            return

        woocommerce_products_stock_map = temporary_sync_data_record.woocommerce_products_variations_data

        if version_info[0] == 16:
            odoo_products_batch = (
                self.env['product.product']
                .with_context(lang=False)
                .search(
                    [
                        ('product_tmpl_id.woocommerce_site_url', '=', self.settings_woocommerce_connection_url),
                        ('product_tmpl_id.sync_to_woocommerce', '=', True),
                        ('product_tmpl_id.active', '=', True),
                        ('product_tmpl_id.woocommerce_id', '!=', False),
                        ('detailed_type', '=', 'product'),
                    ],
                )
            )
        elif version_info[0] in [18, 19]:
            odoo_products_batch = (
                self.env['product.product']
                .with_context(lang=False)
                .search(
                    [
                        ('product_tmpl_id.woocommerce_site_url', '=', self.settings_woocommerce_connection_url),
                        ('product_tmpl_id.sync_to_woocommerce', '=', True),
                        ('product_tmpl_id.active', '=', True),
                        ('product_tmpl_id.woocommerce_id', '!=', False),
                        ('is_storable', '=', True),
                    ],
                )
            )

        # Schedule a job per chunk of Odoo products instead of one job per product, to reduce per-job overhead
        for odoo_products_chunk in self.list_chunks(odoo_products_batch.ids, self.settings_job_chunk_size):
            chunk_identity_key = '-'.join(str(odoo_product_id) for odoo_product_id in odoo_products_chunk)
            self.with_delay(
                identity_key=f'odoo_woocommerce_products_stock_quantity_chunk_sync-{self.id}-{chunk_identity_key}', description=self.job_description('odoo_woocommerce_products_stock_quantity_chunk_sync')
            ).odoo_woocommerce_products_stock_quantity_chunk_sync(odoo_products_chunk, woocommerce_products_stock_map)

        temporary_sync_data_record.unlink()

    @api.model
    def odoo_woocommerce_products_stock_quantity_sync_batch(self: models.Model) -> None:
        """Synchronize stock quantity levels between WooCommerce and Odoo using "product.product records". In WooCommerce, if a stock quantity level changes due to a purchase, the 'date_modified_gmt' field is updated accordingly. Note: This does not apply to Polylang product synchronization between languages - in that case, the 'date_modified_gmt' value remains unchanged despite product/product variation updates."""

        self.ensure_one()

        # WooCommerce REST API
        woocommerce_api = self.woocommerce_api_get()

        # Check if WooCommerce REST API connection is successful
        if not woocommerce_api:
            _logger.error('WooCommerce REST API connection failed. Sync between WooCommerce and Odoo product stock quantity levels process halted; Please check your connection settings in the WooCommerce Configuration')
            return

        # WooCommerce REST API parameters
        params = {'status': 'publish', 'manage_stock': 'true', '_fields': 'id,type,date_modified_gmt,stock_quantity'}

        # Retrieve last sync timestamp from the log model
        if self.settings_woocommerce_modified_records_import:
            woocommerce_stock_sync_log = self.env['woocommerce.sync.stock.log'].search([('woocommerce_connection_id', '=', self.id)], limit=1)
            if woocommerce_stock_sync_log:
                params['modified_after'] = woocommerce_stock_sync_log.odoo_woocommerce_last_sync.strftime('%Y-%m-%dT%H:%M:%S')  # ISO 8601 date format

        # Fetch WooCommerce products with stock management enabled
        try:
            woocommerce_products = self.woocommerce_api_get_all_items(woocommerce_api, endpoint='products', params=params)

        except Exception as error:
            _logger.error(f'Failed to retrieve WooCommerce products from the API. Sync process halted. Error: {error}')
            return

        # Build a single map for quick lookup of all products and variations
        woocommerce_products_stock_map = {woocommerce_product['id']: woocommerce_product for woocommerce_product in woocommerce_products}
        temporary_sync_data_record = self.env['woocommerce.sync.data.temp'].create({'woocommerce_products_variations_data': woocommerce_products_stock_map})

        woocommerce_variable_product_ids = [woocommerce_product['id'] for woocommerce_product in woocommerce_products if woocommerce_product['type'] == 'variable']

        # Chain the parallel jobs to the final processing job
        chain(
            *[
                self.delayable(description=self.job_description('woocommerce_product_variations_stock_retrieve')).woocommerce_product_variations_stock_retrieve(parent_id, temporary_sync_data_record.id)
                for parent_id in woocommerce_variable_product_ids
            ],
            self.delayable(description=self.job_description('odoo_woocommerce_products_stock_quantity_process')).odoo_woocommerce_products_stock_quantity_process(temporary_sync_data_record.id),
        ).delay()

    @api.model
    def woocommerce_to_odoo_products_delete(self: models.Model) -> None:
        self.ensure_one()

        # Test mode intentionally imports a small sample, but that incomplete remote ID set must never be used to infer deletions
        if self.settings_woocommerce_test_mode:
            _logger.info('Skipped WooCommerce product deletion check because test mode only retrieves a partial catalog.')
            return

        # WooCommerce REST API
        woocommerce_api = self.woocommerce_api_get()

        # Check if WooCommerce REST API connection is successful
        if not woocommerce_api:
            _logger.error('WooCommerce REST API connection failed. Cannot check for deleted products.')
            return

        # Get all Odoo products with WooCommerce product ID
        odoo_products = (
            self.env['product.template']
            .with_context(lang=False)
            .search_read([('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('active', '=', True), ('woocommerce_id', '!=', False)], fields=['woocommerce_id'])
        )
        odoo_products = {odoo_product['woocommerce_id'] for odoo_product in odoo_products}

        # WooCommerce REST API parameters to fetch only IDs
        params = {'status': 'publish', '_fields': 'id'}

        # Get all product IDs from WooCommerce
        woocommerce_products = self.woocommerce_api_get_all_items(woocommerce_api, endpoint='products', params=params)
        woocommerce_products = {str(woocommerce_product['id']) for woocommerce_product in woocommerce_products}

        # Find IDs that exist in Odoo but not in WooCommerce
        odoo_products_to_delete_ids = odoo_products - woocommerce_products

        if odoo_products_to_delete_ids:
            odoo_products_to_delete = (
                self.env['product.template'].with_context(lang=False).search([('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('woocommerce_id', 'in', list(odoo_products_to_delete_ids))])
            )
            if odoo_products_to_delete:
                odoo_products_to_delete.unlink()
                _logger.info(f'Deleted {len(odoo_products_to_delete)} Odoo products that were no longer found in WooCommerce.')

    @api.model
    def woocommerce_product_fields(
        self: models.Model,
        woocommerce_product: dict[str, Any],
        woocommerce_currency: str | None = None,
        woocommerce_weight_unit: str | None = None,
        woocommerce_dimension_unit: str | None = None,
        woocommerce_tax_rates: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        self.ensure_one()

        # Custom fields
        product_values = {
            'woocommerce_site_url': self.settings_woocommerce_connection_url,
            'woocommerce_to_odoo_last_sync': fields.Datetime.now(),
        }

        # WooCommerce REST API - Common fields for Products and Product Variants
        product_values.update({'woocommerce_type': woocommerce_product['type']})

        # WooCommerce REST API - Product properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#product-properties
        product_values.update(
            {
                'woocommerce_id': woocommerce_product['id'],
                'woocommerce_name': woocommerce_product['name'],
                'woocommerce_slug': woocommerce_product['slug'],
                'woocommerce_permalink': woocommerce_product['permalink'],
                'woocommerce_date_created': woocommerce_product['date_created'],
                'woocommerce_date_created_gmt': woocommerce_product['date_created_gmt'],
                'woocommerce_date_modified': woocommerce_product['date_modified'],
                'woocommerce_date_modified_gmt': woocommerce_product['date_modified_gmt'],
                'woocommerce_status': woocommerce_product['status'],
                'woocommerce_featured': woocommerce_product['featured'],
                'woocommerce_catalog_visibility': woocommerce_product['catalog_visibility'],
                'woocommerce_description': woocommerce_product['description'],
                'woocommerce_short_description': woocommerce_product['short_description'],
                'woocommerce_sku': woocommerce_product['sku'],
                'woocommerce_price': woocommerce_product['price'],
                'woocommerce_regular_price': woocommerce_product['regular_price'],
                'woocommerce_sale_price': woocommerce_product['sale_price'],
                'woocommerce_date_on_sale_from': woocommerce_product['date_on_sale_from'],
                'woocommerce_date_on_sale_from_gmt': woocommerce_product['date_on_sale_from_gmt'],
                'woocommerce_date_on_sale_to': woocommerce_product['date_on_sale_to'],
                'woocommerce_date_on_sale_to_gmt': woocommerce_product['date_on_sale_to_gmt'],
                'woocommerce_price_html': woocommerce_product['price_html'],
                'woocommerce_on_sale': woocommerce_product['on_sale'],
                'woocommerce_purchasable': woocommerce_product['purchasable'],
                'woocommerce_total_sales': woocommerce_product['total_sales'],
                'woocommerce_virtual': woocommerce_product['virtual'],
                'woocommerce_downloadable': woocommerce_product['downloadable'],
                'woocommerce_downloads': woocommerce_product.get('downloads', []),
                'woocommerce_download_limit': woocommerce_product['download_limit'],
                'woocommerce_download_expiry': woocommerce_product['download_expiry'],
                'woocommerce_external_url': woocommerce_product['external_url'],
                'woocommerce_button_text': woocommerce_product['button_text'],
                'woocommerce_tax_status': woocommerce_product['tax_status'],
                'woocommerce_tax_class': woocommerce_product['tax_class'],
                'woocommerce_manage_stock': woocommerce_product['manage_stock'],
                'woocommerce_stock_quantity': woocommerce_product['stock_quantity'],
                'woocommerce_stock_status': woocommerce_product['stock_status'],
                'woocommerce_backorders': woocommerce_product['backorders'],
                'woocommerce_backorders_allowed': woocommerce_product['backorders_allowed'],
                'woocommerce_backordered': woocommerce_product['backordered'],
                'woocommerce_sold_individually': woocommerce_product['sold_individually'],
                'woocommerce_weight': woocommerce_product['weight'],
                'woocommerce_dimensions': woocommerce_product['dimensions'],
                'woocommerce_shipping_required': woocommerce_product['shipping_required'],
                'woocommerce_shipping_taxable': woocommerce_product['shipping_taxable'],
                'woocommerce_shipping_class': woocommerce_product['shipping_class'],
                'woocommerce_shipping_class_id': woocommerce_product['shipping_class_id'],
                'woocommerce_reviews_allowed': woocommerce_product['reviews_allowed'],
                'woocommerce_average_rating': woocommerce_product['average_rating'],
                'woocommerce_rating_count': woocommerce_product['rating_count'],
                'woocommerce_related_ids': woocommerce_product['related_ids'],
                'woocommerce_upsell_ids': woocommerce_product['upsell_ids'],
                'woocommerce_cross_sell_ids': woocommerce_product['cross_sell_ids'],
                'woocommerce_parent_id': woocommerce_product['parent_id'],
                'woocommerce_purchase_note': woocommerce_product.get('purchase_note', ''),
                'woocommerce_categories': woocommerce_product['categories'],
                'woocommerce_tags': woocommerce_product['tags'],
                'woocommerce_images': woocommerce_product['images'],
                'woocommerce_attributes': woocommerce_product['attributes'],
                'woocommerce_default_attributes': woocommerce_product['default_attributes'],
                'woocommerce_variations': woocommerce_product['variations'],
                'woocommerce_grouped_products': woocommerce_product['grouped_products'],
                'woocommerce_menu_order': woocommerce_product['menu_order'],
                'woocommerce_meta_data': woocommerce_product['meta_data'],
            },
        )

        # WooCommerce REST API - Fields not mentioned in the documentation
        product_values.update(
            {
                'woocommerce_brands': woocommerce_product.get('brands', []),
            },
        )

        # Additional fields
        product_values.update(
            {
                'woocommerce_currency': woocommerce_currency if woocommerce_currency else None,
                'woocommerce_weight_unit': woocommerce_weight_unit if woocommerce_weight_unit else None,
                'woocommerce_dimension_unit': woocommerce_dimension_unit if woocommerce_dimension_unit else None,
                'woocommerce_tax_rate': woocommerce_tax_rates.get(woocommerce_product['tax_class'] if woocommerce_product['tax_class'] else 'standard') if woocommerce_tax_rates else None,
            },
        )

        # Custom fields
        product_values.update(
            {
                'sync_to_woocommerce': True,
                'source': 'WooCommerce',
                'language_code': woocommerce_product.get('lang', None),
                # 'service' is a Germanized plugin field (https://vendidero.de/doc/woocommerce-germanized/products-rest-api); fall back to 'virtual'/'downloadable' since Odoo has no direct equivalent and vanilla WooCommerce doesn't expose 'service'
                'woocommerce_service': bool(woocommerce_product.get('service', woocommerce_product.get('virtual') or woocommerce_product.get('downloadable'))),
            },
        )

        # WooCommerce's 'date_created'/'date_modified'/'date_on_sale_from'/'date_on_sale_to' are store-local; use the '_gmt' siblings for both
        self.datetime_gmt_pairs_convert(product_values, ['woocommerce_date_created', 'woocommerce_date_modified', 'woocommerce_date_on_sale_from', 'woocommerce_date_on_sale_to'])

        return product_values

    @api.model
    def woocommerce_to_odoo_product_sync(
        self: models.Model,
        woocommerce_product: dict[str, Any],
        woocommerce_currency: str,
        woocommerce_tax_rates: dict[str, float],
        woocommerce_prices_include_tax: bool,
        woocommerce_weight_unit: str,
        woocommerce_dimension_unit: str,
        odoo_products: dict[str, Any],
        odoo_brand_cache: dict[str, int] | None = None,
        odoo_category_cache: dict[str, int] | None = None,
        odoo_tag_cache: dict[str, int] | None = None,
        odoo_tax_rate_cache: dict[tuple[float, bool], int] | None = None,
        odoo_uom_cache: dict[str, int] | None = None,
    ) -> str:
        """Returns 'created', 'updated' or 'skipped', so the calling chunk job can tally per-direction sync-summary counts."""
        self.ensure_one()

        # Isolate this record's writes so a failure only rolls back to here, not the whole chunk job's transaction
        savepoint = self.env.cr.savepoint()
        try:
            # Try to find the corresponding product in Odoo by its WooCommerce product ID
            odoo_product = odoo_products.get(str(woocommerce_product['id']))

            if odoo_product:
                odoo_product = self.env['product.template'].with_context(lang=False).browse(odoo_product['id'])

                # Skip if not modified and stock setting unchanged
                if self.datetime_convert(woocommerce_product['date_modified_gmt']) <= odoo_product.write_date and odoo_product.woocommerce_manage_stock == woocommerce_product['manage_stock']:
                    _logger.info(f'Skipped import of WooCommerce product into Odoo: {odoo_product.name} (Odoo product ID: {odoo_product.id}, WooCommerce product ID: {odoo_product.woocommerce_id})')
                    return 'skipped'

            # Create new product in Odoo if it does not yet exist or update product in Odoo only if WooCommerce version is newer
            product_values = self.woocommerce_product_fields(woocommerce_product, woocommerce_currency, woocommerce_weight_unit, woocommerce_dimension_unit, woocommerce_tax_rates)

            # Brand (requires 'product_brand' Odoo add-on)
            if 'product.brand' in self.env:
                odoo_product_brands_ids = []
                for brand in woocommerce_product['brands']:
                    odoo_brand = self.odoo_brand_create_or_retrieve(brand['name'], cache=odoo_brand_cache)
                    if odoo_brand:
                        odoo_product_brands_ids.append(odoo_brand.id)

                product_values.update({'product_brand_id': odoo_product_brands_ids[0] if odoo_product_brands_ids else False})

            # Category
            odoo_product_categories_ids = []
            for category in woocommerce_product['categories']:
                odoo_product_category = self.odoo_category_create_or_retrieve(category['name'], cache=odoo_category_cache)
                if odoo_product_category:
                    odoo_product_categories_ids.append(odoo_product_category.id)

            # Categories (requires 'product_multi_category' Odoo add-on)
            if 'categ_ids' in self.env['product.template']._fields:
                product_values.update({'categ_ids': [(6, 0, odoo_product_categories_ids)]})

            # Currency
            odoo_product_currency = None
            if product_values['woocommerce_currency']:
                odoo_product_currency = self.odoo_currency_retrieve(product_values['woocommerce_currency'])

            # Dimensions (requires 'product_dimension' Odoo add-on)
            if 'product_length' in self.env['product.template']._fields:
                odoo_product_unit_of_measure_dimension = self.odoo_unit_of_measure_dimension_retrieve(product_values['woocommerce_dimension_unit'])

                product_values.update(
                    {
                        'dimensional_uom_id': odoo_product_unit_of_measure_dimension.id if odoo_product_unit_of_measure_dimension else False,
                        'product_length': woocommerce_product['dimensions']['length'],
                        'product_width': woocommerce_product['dimensions']['width'],
                        'product_height': woocommerce_product['dimensions']['height'],
                    },
                )

            # Tags
            odoo_product_tags_ids = []
            for tag in woocommerce_product['tags']:
                odoo_tag = self.odoo_tag_create_or_retrieve(tag['name'], cache=odoo_tag_cache)
                if odoo_tag:
                    odoo_product_tags_ids.append(odoo_tag.id)

            # Tax - a WooCommerce 'tax_status' of 'none'/'shipping' means the product itself is not taxable, regardless of its 'tax_class' rate
            odoo_product_tax_id = []
            if product_values['woocommerce_tax_rate'] and woocommerce_product.get('tax_status') == 'taxable':
                odoo_product_tax = self.odoo_tax_rate_create_or_retrieve(product_values['woocommerce_tax_rate'], cache=odoo_tax_rate_cache)
                if odoo_product_tax:
                    odoo_product_tax_id = [(6, 0, [odoo_product_tax.id])]

            # Unit of measure
            odoo_product_unit_of_measure = None
            if self.settings_woocommerce_products_package_size_unit_default:
                odoo_product_unit_of_measure = self.env.ref('uom.product_uom_unit')

            elif product_values['woocommerce_weight_unit']:
                odoo_product_unit_of_measure = self.odoo_unit_of_measure_create_or_retrieve(product_values['woocommerce_weight_unit'], cache=odoo_uom_cache)

            # Image featured
            odoo_product_image_featured = None
            if self.settings_woocommerce_images_sync and len(woocommerce_product['images']) > 0:
                odoo_product_image_featured = self.image_download_file_to_base64(woocommerce_product['images'][0])

            # Odoo 'product.template' model fields
            product_values.update(
                {
                    # General information
                    'name': product_values['woocommerce_name'],
                    'image_1920': odoo_product_image_featured,
                    'default_code': product_values['woocommerce_sku'],
                    'create_date': product_values['woocommerce_date_created_gmt'],
                    'description': 'Imported via Odoo-WooCommerce Sync',
                    'description_sale': product_values['woocommerce_description'],
                    'responsible_id': self.settings_woocommerce_user_responsible.id,
                    # Product status
                    'active': product_values['woocommerce_status'] == 'publish',
                    'sale_ok': product_values['woocommerce_purchasable'],
                    # Pricing
                    'currency_id': odoo_product_currency.id if odoo_product_currency else False,
                    'taxes_id': odoo_product_tax_id,
                    'invoice_policy': 'order',
                    'list_price': self.woocommerce_price_to_odoo_price(product_values['woocommerce_price'], product_values['woocommerce_tax_rate'], woocommerce_prices_include_tax),
                    # Category and tags
                    'product_tag_ids': [(6, 0, odoo_product_tags_ids)],
                    # Dimensions
                    'weight': product_values['woocommerce_weight'],
                    'uom_id': odoo_product_unit_of_measure.id if odoo_product_unit_of_measure else False,
                    'volume': (
                        float(product_values['woocommerce_dimensions']['length']) * float(product_values['woocommerce_dimensions']['width']) * float(product_values['woocommerce_dimensions']['height'])
                        if (product_values['woocommerce_dimensions']['length'] and product_values['woocommerce_dimensions']['width'] and product_values['woocommerce_dimensions']['height'])
                        else False
                    ),
                },
            )

            # Category - only set when WooCommerce provides one, so a product without categories keeps Odoo's own default 'categ_id' instead of an explicit 'False' (which violates the field's not-null constraint)
            if odoo_product_categories_ids:
                product_values['categ_id'] = odoo_product_categories_ids[0]

            # Product type
            if version_info[0] == 16:
                product_values['detailed_type'] = 'service' if product_values['woocommerce_service'] else 'product' if product_values['woocommerce_manage_stock'] else 'consu'

            elif version_info[0] in [18, 19]:
                product_values['type'] = 'service' if product_values['woocommerce_service'] else 'consu'
                product_values['is_storable'] = bool(product_values['woocommerce_manage_stock'])

            # uom_po_id
            if version_info[0] in [16, 18]:
                product_values['uom_po_id'] = odoo_product_unit_of_measure.id if odoo_product_unit_of_measure else False

            if odoo_product:
                odoo_product.write(product_values)
                sync_status = 'updated'
                _logger.info(f'Updated WooCommerce product in Odoo: {odoo_product.name} (Odoo product ID: {odoo_product.id}, WooCommerce product ID: {odoo_product["woocommerce_id"]})')

            else:
                odoo_product = self.env['product.template'].create(product_values)
                sync_status = 'created'
                _logger.info(f'Imported WooCommerce product into Odoo: {odoo_product.name} (Odoo product ID: {odoo_product.id}, WooCommerce product ID: {odoo_product["woocommerce_id"]})')

            # Product image gallery
            if odoo_product and self.settings_woocommerce_images_sync and len(woocommerce_product['images']) > 0:
                self.image_process_attachments(woocommerce_product['images'][1:], odoo_product)  # Skips the main image

        except Exception:
            # Roll back only this record's changes, keeping other records already written in this chunk job
            savepoint.rollback()
            _logger.exception(f'Error syncing WooCommerce product {woocommerce_product["name"]} (WooCommerce product ID: {woocommerce_product["id"]})')
            raise
        finally:
            # Release the savepoint (whether it was rolled back above or the record synced successfully) so it never lingers open for the rest of this chunk job's transaction
            savepoint.close(rollback=False)

        return sync_status

    @api.model
    def woocommerce_to_odoo_products_chunk_sync(
        self: models.Model,
        woocommerce_products: list[dict[str, Any]],
        woocommerce_currency: str,
        woocommerce_tax_rates: dict[str, float],
        woocommerce_prices_include_tax: bool,
        woocommerce_weight_unit: str,
        woocommerce_dimension_unit: str,
        odoo_products: dict[str, Any],
        odoo_brand_cache: dict[str, int] | None = None,
        odoo_category_cache: dict[str, int] | None = None,
        odoo_tag_cache: dict[str, int] | None = None,
    ) -> None:
        """Processes a chunk of WooCommerce products sequentially within a single queue job, sharing the caches passed by the batch job instead of re-fetching them per product."""
        self.ensure_one()

        # Shared tax rate/unit of measure caches for the products in this chunk
        odoo_tax_rate_cache: dict[tuple[float, bool], int] = {}
        odoo_uom_cache: dict[str, int] = {}
        new_count = updated_count = 0
        errors: list[str] = []

        for woocommerce_product in woocommerce_products:
            try:
                sync_status = self.woocommerce_to_odoo_product_sync(
                    woocommerce_product,
                    woocommerce_currency,
                    woocommerce_tax_rates,
                    woocommerce_prices_include_tax,
                    woocommerce_weight_unit,
                    woocommerce_dimension_unit,
                    odoo_products,
                    odoo_brand_cache,
                    odoo_category_cache,
                    odoo_tag_cache,
                    odoo_tax_rate_cache,
                    odoo_uom_cache,
                )
                if sync_status == 'created':
                    new_count += 1
                elif sync_status == 'updated':
                    updated_count += 1
            except Exception as error:
                if isinstance(error, (RetryableJobError, psycopg2.errors.SerializationFailure, psycopg2.errors.DeadlockDetected)):
                    raise
                _logger.exception(f'Error syncing WooCommerce product {woocommerce_product.get("id")} within chunk job')
                errors.append(f'Product {woocommerce_product.get("id")}: {error}')

        self.sync_summary_chunk_completed('products', len(woocommerce_products), new_count, updated_count, errors)

    @api.model
    def woocommerce_to_odoo_products_sync_batch(
        self: models.Model,
        woocommerce_currency: str,
        woocommerce_tax_rates: dict[str, float],
        woocommerce_prices_include_tax: bool,
        woocommerce_weight_unit: str,
        woocommerce_dimension_unit: str,
    ) -> None:
        self.ensure_one()

        # WooCommerce REST API
        woocommerce_api = self.woocommerce_api_get()

        # Check if WooCommerce REST API connection is successful
        if not woocommerce_api:
            error_message = 'WooCommerce REST API connection failed. WooCommerce to Odoo products sync process halted; Please check your connection settings in the WooCommerce Configuration'
            _logger.error(error_message)
            return

        # WooCommerce REST API parameters
        params = {'status': 'publish'}

        if self.settings_woocommerce_modified_records_import:
            odoo_woocommerce_last_sync = self.odoo_woocommerce_last_sync_retrieve()
            if odoo_woocommerce_last_sync:
                params['modified_after'] = odoo_woocommerce_last_sync.strftime('%Y-%m-%dT%H:%M:%S')  # ISO 8601 date format

        if self.settings_woocommerce_to_odoo_products_language_code:
            params['lang'] = self.settings_woocommerce_to_odoo_products_language_code

        # Get all Odoo products with WooCommerce product ID
        odoo_products = (
            self.env['product.template']
            .with_context(lang=False)
            .search_read(
                [('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('active', '=', True), ('woocommerce_id', '!=', False)],
                fields=['id', 'active', 'write_date', 'woocommerce_id', 'woocommerce_manage_stock'],
            )
        )
        odoo_products = {odoo_product['woocommerce_id']: odoo_product for odoo_product in odoo_products}

        for woocommerce_products_batch in self.woocommerce_api_get_items_in_batches(woocommerce_api, endpoint='products', params=params):
            # Filter for WooCommerce products that have SKU
            woocommerce_products_batch = [woocommerce_product for woocommerce_product in woocommerce_products_batch if woocommerce_product['sku']]

            if not woocommerce_products_batch:
                continue

            # Pre-resolve all distinct brands/categories/tags referenced in this page once, instead of letting every per-product job search/create them individually
            odoo_brand_cache: dict[str, int] = {}
            odoo_category_cache: dict[str, int] = {}
            odoo_tag_cache: dict[str, int] = {}

            if 'product.brand' in self.env:
                for brand_name in {brand['name'] for woocommerce_product in woocommerce_products_batch for brand in woocommerce_product.get('brands', []) if brand.get('name')}:
                    self.odoo_brand_create_or_retrieve(brand_name, cache=odoo_brand_cache)

            for category_name in {category['name'] for woocommerce_product in woocommerce_products_batch for category in woocommerce_product.get('categories', []) if category.get('name')}:
                self.odoo_category_create_or_retrieve(category_name, cache=odoo_category_cache)

            for tag_name in {tag['name'] for woocommerce_product in woocommerce_products_batch for tag in woocommerce_product.get('tags', []) if tag.get('name')}:
                self.odoo_tag_create_or_retrieve(tag_name, cache=odoo_tag_cache)

            # Schedule a job per chunk of WooCommerce products instead of one job per product, to reduce per-job overhead
            for products_chunk in self.list_chunks(woocommerce_products_batch, self.settings_job_chunk_size):
                chunk_identity_key = '-'.join(str(woocommerce_product['id']) for woocommerce_product in products_chunk)
                self.with_delay(
                    identity_key=f'woocommerce_to_odoo_products_chunk_sync-{self.id}-{chunk_identity_key}', description=self.job_description('woocommerce_to_odoo_products_chunk_sync')
                ).woocommerce_to_odoo_products_chunk_sync(
                    products_chunk,
                    woocommerce_currency,
                    woocommerce_tax_rates,
                    woocommerce_prices_include_tax,
                    woocommerce_weight_unit,
                    woocommerce_dimension_unit,
                    odoo_products,
                    odoo_brand_cache,
                    odoo_category_cache,
                    odoo_tag_cache,
                )
                self.sync_summary_chunk_dispatched()

    @api.model
    def woocommerce_to_odoo_products_related_ids(self: models.Model) -> None:
        self.ensure_one()

        # Retrieve all Odoo products
        odoo_products = self.env['product.template'].with_context(lang=False).search([('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('active', '=', True)])

        # Collect all related WooCommerce IDs upfront for a single bulk lookup
        woocommerce_ids_related = [record_id for odoo_product in odoo_products for record_id in (odoo_product.woocommerce_related_ids or [])]

        if not woocommerce_ids_related:
            return

        related_map = {
            related.woocommerce_id: related.id
            for related in self.env['product.template']
            .with_context(lang=False)
            .search([('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('active', '=', True), ('woocommerce_id', 'in', woocommerce_ids_related)])
        }

        for odoo_product in odoo_products:
            if odoo_product.woocommerce_related_ids:
                odoo_products_related_ids = [related_map[record_id] for record_id in odoo_product.woocommerce_related_ids if record_id in related_map]

                # Update the optional_product_ids field for the current Odoo product
                if odoo_products_related_ids:
                    odoo_product.write({'optional_product_ids': [(6, 0, odoo_products_related_ids)]})

    def woocommerce_product_variation_fields(
        self: models.Model,
        woocommerce_variation: dict[str, Any],
        woocommerce_currency: str | None = None,
        woocommerce_weight_unit: str | None = None,
        woocommerce_dimension_unit: str | None = None,
        woocommerce_tax_rates: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        # Custom fields
        product_variation_values = {
            'woocommerce_site_url': self.settings_woocommerce_connection_url,
            'woocommerce_to_odoo_last_sync': fields.Datetime.now(),
        }

        # WooCommerce REST API - Common fields for Products and Product Variants
        product_variation_values.update(
            {
                'woocommerce_type': woocommerce_variation['type'],
            },
        )

        # WooCommerce REST API - Product variation properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#product-variation-properties
        product_variation_values.update(
            {
                'woocommerce_id': woocommerce_variation['id'],
                'woocommerce_name': woocommerce_variation['name'],
                'woocommerce_permalink': woocommerce_variation['permalink'],
                'woocommerce_date_created': woocommerce_variation['date_created'],
                'woocommerce_date_created_gmt': woocommerce_variation['date_created_gmt'],
                'woocommerce_date_modified': woocommerce_variation['date_modified'],
                'woocommerce_date_modified_gmt': woocommerce_variation['date_modified_gmt'],
                'woocommerce_status': woocommerce_variation['status'],
                'woocommerce_description': woocommerce_variation['description'],
                'woocommerce_sku': woocommerce_variation['sku'],
                'woocommerce_price': woocommerce_variation['price'],
                'woocommerce_regular_price': woocommerce_variation['regular_price'],
                'woocommerce_sale_price': woocommerce_variation['sale_price'],
                'woocommerce_date_on_sale_from': woocommerce_variation['date_on_sale_from'],
                'woocommerce_date_on_sale_from_gmt': woocommerce_variation['date_on_sale_from_gmt'],
                'woocommerce_date_on_sale_to': woocommerce_variation['date_on_sale_to'],
                'woocommerce_date_on_sale_to_gmt': woocommerce_variation['date_on_sale_to_gmt'],
                'woocommerce_on_sale': woocommerce_variation['on_sale'],
                'woocommerce_purchasable': woocommerce_variation['purchasable'],
                'woocommerce_virtual': woocommerce_variation['virtual'],
                'woocommerce_downloadable': woocommerce_variation['downloadable'],
                'woocommerce_downloads': woocommerce_variation['downloads'],
                'woocommerce_download_limit': woocommerce_variation['download_limit'],
                'woocommerce_download_expiry': woocommerce_variation['download_expiry'],
                'woocommerce_tax_status': woocommerce_variation['tax_status'],
                'woocommerce_tax_class': woocommerce_variation['tax_class'],
                'woocommerce_manage_stock': woocommerce_variation['manage_stock'],
                'woocommerce_stock_quantity': woocommerce_variation['stock_quantity'],
                'woocommerce_stock_status': woocommerce_variation['stock_status'],
                'woocommerce_backorders': woocommerce_variation['backorders'],
                'woocommerce_backorders_allowed': woocommerce_variation['backorders_allowed'],
                'woocommerce_backordered': woocommerce_variation['backordered'],
                'woocommerce_weight': woocommerce_variation['weight'],
                'woocommerce_dimensions': woocommerce_variation['dimensions'],
                'woocommerce_shipping_class': woocommerce_variation['shipping_class'],
                'woocommerce_shipping_class_id': woocommerce_variation['shipping_class_id'],
                'woocommerce_image': woocommerce_variation['image'],
                'woocommerce_attributes': woocommerce_variation['attributes'],
                'woocommerce_menu_order': woocommerce_variation['menu_order'],
                'woocommerce_meta_data': woocommerce_variation['meta_data'],
            },
        )

        # WooCommerce REST API - Fields not mentioned in the documentation
        product_variation_values.update(
            {
                'woocommerce_parent_id': woocommerce_variation['parent_id'],
            },
        )

        # Additional fields
        product_variation_values.update(
            {
                'woocommerce_currency': woocommerce_currency if woocommerce_currency else None,
                'woocommerce_weight_unit': woocommerce_weight_unit if woocommerce_weight_unit else None,
                'woocommerce_dimension_unit': woocommerce_dimension_unit if woocommerce_dimension_unit else None,
                'woocommerce_tax_rate': woocommerce_tax_rates.get(woocommerce_variation['tax_class'] if woocommerce_variation['tax_class'] else 'standard') if woocommerce_tax_rates else None,
            },
        )

        # Custom fields
        product_variation_values.update(
            {
                # 'service' is a Germanized plugin field (https://vendidero.de/doc/woocommerce-germanized/products-rest-api); fall back to 'virtual'/'downloadable' since Odoo has no direct equivalent and vanilla WooCommerce doesn't expose 'service'
                'woocommerce_service': bool(woocommerce_variation.get('service', woocommerce_variation.get('virtual') or woocommerce_variation.get('downloadable'))),
            },
        )

        # WooCommerce's 'date_created'/'date_modified'/'date_on_sale_from'/'date_on_sale_to' are store-local; use the '_gmt' siblings for both
        self.datetime_gmt_pairs_convert(product_variation_values, ['woocommerce_date_created', 'woocommerce_date_modified', 'woocommerce_date_on_sale_from', 'woocommerce_date_on_sale_to'])

        return product_variation_values

    def woocommerce_to_odoo_product_variations_sync(
        self: models.Model,
        woocommerce_product: dict[str, Any],
        woocommerce_currency: str,
        woocommerce_tax_rates: dict[str, float],
        woocommerce_prices_include_tax: bool,
        woocommerce_weight_unit: str,
        woocommerce_dimension_unit: str,
        odoo_attribute_cache: dict[str, int] | None = None,
        odoo_attribute_value_cache: dict[tuple[int, str], int] | None = None,
        odoo_tax_rate_cache: dict[tuple[float, bool], int] | None = None,
        odoo_uom_cache: dict[str, int] | None = None,
    ) -> dict[str, int]:
        """Returns processed/new/updated/skipped counts across this product's variations."""
        self.ensure_one()

        # Caches are local to this call by default (still avoids re-querying the same attribute/value more than once across all of this product's variations); a chunk job may pass in shared dicts to also reuse them across products
        if odoo_attribute_cache is None:
            odoo_attribute_cache = {}
        if odoo_attribute_value_cache is None:
            odoo_attribute_value_cache = {}

        processed_count = new_count = updated_count = skipped_count = 0

        # Isolate this record's writes so a failure only rolls back to here, not the whole chunk job's transaction
        savepoint = self.env.cr.savepoint()
        try:
            # WooCommerce REST API (connectivity already validated by the batch job that scheduled this per-record job)
            woocommerce_api = self.woocommerce_api_get(validate=False)

            # Check if WooCommerce REST API connection is successful
            if not woocommerce_api:
                error_message = 'WooCommerce REST API connection failed. WooCommerce to Odoo products variations sync process halted; Please check your connection settings in the WooCommerce Configuration'
                _logger.error(error_message)
                raise RetryableJobError(error_message)

            # Search for existing product in Odoo
            odoo_product = (
                self.env['product.template']
                .with_context(lang=False)
                .search(
                    [('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('active', '=', True), ('woocommerce_id', '=', woocommerce_product['id'])],
                    limit=1,
                )
            )
            if not odoo_product:
                error_message = f'Not found Odoo product for WooCommerce product: {woocommerce_product["name"]} (WooCommerce product ID: {woocommerce_product["id"]}); its variations were skipped'
                _logger.warning(error_message)
                raise ValidationError(error_message)

            if odoo_product:
                # Store 'product.template' SKU
                odoo_product_sku = odoo_product.default_code

                # WooCommerce REST API parameters
                params = {'status': 'publish'}

                if self.settings_woocommerce_modified_records_import:
                    odoo_woocommerce_last_sync = self.odoo_woocommerce_last_sync_retrieve()
                    if odoo_woocommerce_last_sync:
                        params['modified_after'] = odoo_woocommerce_last_sync.strftime('%Y-%m-%dT%H:%M:%S')  # ISO 8601 date format

                # WooCommerce product variations for the product
                woocommerce_variations = self.woocommerce_api_get_all_items(woocommerce_api, endpoint=f'products/{woocommerce_product["id"]}/variations', params=params)

                # Create a temporary list to hold all unique attribute/value pairs
                all_woocommerce_attributes = set()
                for woocommerce_variation in woocommerce_variations:
                    for attribute in woocommerce_variation['attributes']:
                        if attribute.get('name') and attribute.get('option'):
                            all_woocommerce_attributes.add((attribute.get('name'), attribute.get('option')))

                # Ensure all attributes and values are created on the product template first
                for name, option in all_woocommerce_attributes:
                    # Search for or create the product attribute (cached by name, since it is looked up again below for every variation)
                    if name in odoo_attribute_cache:
                        odoo_product_attribute = self.env['product.attribute'].browse(odoo_attribute_cache[name])
                    else:
                        odoo_product_attribute = self.env['product.attribute'].with_context(lang=False).search([('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('name', '=', name)], limit=1)

                        if odoo_product_attribute and odoo_product_attribute.create_variant != 'dynamic':
                            _logger.warning(
                                f"The 'create_variant' mode for attribute '{odoo_product_attribute.name}' is not 'dynamic'. Odoo prevents changing this setting because the attribute is in use on other products. This may result in unintended product variants being generated"
                            )

                        # Create the attribute if it doesn't exist, with 'dynamic' setting
                        if not odoo_product_attribute:
                            odoo_product_attribute = self.env['product.attribute'].create({'woocommerce_site_url': self.settings_woocommerce_connection_url, 'name': name, 'create_variant': 'dynamic'})
                            _logger.info(f'Created WooCommerce product attribute in Odoo: {odoo_product_attribute.name}')

                        odoo_attribute_cache[name] = odoo_product_attribute.id

                    # Create or retrieve the attribute value for this attribute (cached by (attribute_id, option))
                    attribute_value_cache_key = (odoo_product_attribute.id, option)
                    if attribute_value_cache_key in odoo_attribute_value_cache:
                        odoo_product_attribute_value = self.env['product.attribute.value'].browse(odoo_attribute_value_cache[attribute_value_cache_key])
                    else:
                        odoo_product_attribute_value = (
                            self.env['product.attribute.value']
                            .with_context(lang=False)
                            .search([('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('attribute_id', '=', odoo_product_attribute.id), ('name', '=', option)], limit=1)
                        )

                        if not odoo_product_attribute_value:
                            with self.env.cr.savepoint():
                                odoo_product_attribute_value = self.env['product.attribute.value'].create(
                                    {'woocommerce_site_url': self.settings_woocommerce_connection_url, 'attribute_id': odoo_product_attribute.id, 'name': option}
                                )
                                _logger.info(f'Created WooCommerce product attribute value in Odoo: {odoo_product_attribute_value.name}')

                        odoo_attribute_value_cache[attribute_value_cache_key] = odoo_product_attribute_value.id

                    # Ensure the product template has a matching attribute line and add the value
                    attribute_line = odoo_product.attribute_line_ids.filtered(lambda line, odoo_product_attribute=odoo_product_attribute: line.attribute_id.id == odoo_product_attribute.id)
                    if not attribute_line:
                        attribute_line = self.env['product.template.attribute.line'].create(
                            {
                                'woocommerce_site_url': self.settings_woocommerce_connection_url,
                                'product_tmpl_id': odoo_product.id,
                                'attribute_id': odoo_product_attribute.id,
                                'value_ids': [(6, 0, [odoo_product_attribute_value.id])],
                            }
                        )
                    else:
                        if odoo_product_attribute_value.id not in attribute_line.value_ids.ids:
                            attribute_line.write({'value_ids': [(4, odoo_product_attribute_value.id)]})

                # Cache for 'product.template.attribute.value' lookups, keyed by (attribute_id, attribute_value_id), populated once per product instead of once per (variation, attribute) pair
                odoo_product_template_attribute_value_cache: dict[tuple[int, int], int] = {}

                # Existing variants for all of this product's variations, prefetched once instead of once per variation
                odoo_existing_variants_by_woocommerce_id = {
                    str(variant.woocommerce_id): variant
                    for variant in self.env['product.product']
                    .with_context(lang=False)
                    .search(
                        [
                            ('woocommerce_site_url', '=', self.settings_woocommerce_connection_url),
                            ('active', '=', True),
                            ('woocommerce_id', 'in', [woocommerce_variation['id'] for woocommerce_variation in woocommerce_variations]),
                        ]
                    )
                }

                # Iterate through each WooCommerce variation to process it
                for woocommerce_variation in woocommerce_variations:
                    processed_count += 1
                    # Skip if not modified since the last sync (mirrors the equivalent check in 'woocommerce_to_odoo_product_sync()') to avoid
                    # an unconditional 'write()' - and its log line - on every sync run even when nothing about the variation actually changed
                    odoo_existing_variant = odoo_existing_variants_by_woocommerce_id.get(str(woocommerce_variation['id'])) or self.env['product.product']
                    if odoo_existing_variant and self.datetime_convert(woocommerce_variation['date_modified_gmt']) <= odoo_existing_variant.write_date:
                        skipped_count += 1
                        _logger.info(
                            f'Skipped import of WooCommerce product variation into Odoo: {odoo_existing_variant.display_name} (Odoo product variant ID: {odoo_existing_variant.id}, WooCommerce product variation ID: {woocommerce_variation["id"]})'
                        )
                        continue

                    product_variation_values = self.woocommerce_product_variation_fields(
                        woocommerce_variation,
                        woocommerce_currency,
                        woocommerce_weight_unit,
                        woocommerce_dimension_unit,
                        woocommerce_tax_rates,
                    )

                    # Currency
                    odoo_product_variant_currency = None
                    if product_variation_values['woocommerce_currency']:
                        odoo_product_variant_currency = self.odoo_currency_retrieve(product_variation_values['woocommerce_currency'])

                    # Image featured
                    odoo_product_variant_image_featured = None
                    if self.settings_woocommerce_images_sync and woocommerce_variation['image'] is not None:
                        odoo_product_variant_image_featured = self.image_download_file_to_base64(woocommerce_variation['image'])

                    # Tax - a WooCommerce 'tax_status' of 'none'/'shipping' means the variation itself is not taxable, regardless of its 'tax_class' rate
                    odoo_product_variant_tax_id = []
                    if product_variation_values['woocommerce_tax_rate'] and woocommerce_variation.get('tax_status') == 'taxable':
                        odoo_product_variant_tax = self.odoo_tax_rate_create_or_retrieve(product_variation_values['woocommerce_tax_rate'], cache=odoo_tax_rate_cache)
                        if odoo_product_variant_tax:
                            odoo_product_variant_tax_id = [(6, 0, [odoo_product_variant_tax.id])]

                    # Unit of measure
                    odoo_product_variant_unit_of_measure = None
                    if self.settings_woocommerce_products_package_size_unit_default:
                        odoo_product_variant_unit_of_measure = self.env.ref('uom.product_uom_unit')

                    elif product_variation_values['woocommerce_weight_unit']:
                        odoo_product_variant_unit_of_measure = self.odoo_unit_of_measure_create_or_retrieve(product_variation_values['woocommerce_weight_unit'], cache=odoo_uom_cache)

                    # Build a list of Odoo attribute value IDs for the specific combination
                    odoo_product_template_attribute_value_ids = []
                    for woocommerce_attribute in woocommerce_variation['attributes']:
                        if not woocommerce_attribute.get('name') or not woocommerce_attribute.get('option'):
                            continue

                        # Reuse the attribute/attribute-value caches populated above instead of re-querying the DB for every variation
                        if woocommerce_attribute['name'] in odoo_attribute_cache:
                            odoo_product_attribute = self.env['product.attribute'].browse(odoo_attribute_cache[woocommerce_attribute['name']])
                        else:
                            odoo_product_attribute = self.env['product.attribute'].with_context(lang=False).search([('name', '=', woocommerce_attribute.get('name'))], limit=1)

                        attribute_value_cache_key = (odoo_product_attribute.id, woocommerce_attribute['option'])
                        if attribute_value_cache_key in odoo_attribute_value_cache:
                            odoo_product_attribute_value = self.env['product.attribute.value'].browse(odoo_attribute_value_cache[attribute_value_cache_key])
                        else:
                            odoo_product_attribute_value = (
                                self.env['product.attribute.value'].with_context(lang=False).search([('attribute_id', '=', odoo_product_attribute.id), ('name', '=', woocommerce_attribute.get('option'))], limit=1)
                            )

                        product_template_attribute_value_cache_key = (odoo_product_attribute.id, odoo_product_attribute_value.id)
                        if product_template_attribute_value_cache_key in odoo_product_template_attribute_value_cache:
                            odoo_product_template_attribute_value_ids.append(odoo_product_template_attribute_value_cache[product_template_attribute_value_cache_key])
                        else:
                            product_template_attribute_value = (
                                self.env['product.template.attribute.value']
                                .with_context(lang=False)
                                .search(
                                    [
                                        ('product_tmpl_id', '=', odoo_product.id),
                                        ('attribute_id', '=', odoo_product_attribute.id),
                                        ('product_attribute_value_id', '=', odoo_product_attribute_value.id),
                                    ],
                                    limit=1,
                                )
                            )
                            if product_template_attribute_value:
                                odoo_product_template_attribute_value_cache[product_template_attribute_value_cache_key] = product_template_attribute_value.id
                                odoo_product_template_attribute_value_ids.append(product_template_attribute_value.id)

                    # Odoo 'product.product' model fields
                    product_variation_values.update(
                        {
                            # General information
                            'image_1920': odoo_product_variant_image_featured,
                            'default_code': product_variation_values['woocommerce_sku'],
                            'create_date': product_variation_values['woocommerce_date_created_gmt'],
                            'description': 'Imported via Odoo-WooCommerce Sync',
                            'description_sale': product_variation_values['woocommerce_description'],
                            # Product status
                            'active': product_variation_values['woocommerce_status'] == 'publish',
                            'sale_ok': product_variation_values['woocommerce_purchasable'],
                            # Pricing
                            'currency_id': odoo_product_variant_currency.id if odoo_product_variant_currency else False,
                            'taxes_id': odoo_product_variant_tax_id,
                            'invoice_policy': 'order',
                            'list_price': self.woocommerce_price_to_odoo_price(product_variation_values['woocommerce_price'], product_variation_values['woocommerce_tax_rate'], woocommerce_prices_include_tax),
                            # Dimensions
                            'weight': product_variation_values['woocommerce_weight'],
                            'uom_id': odoo_product_variant_unit_of_measure.id if odoo_product_variant_unit_of_measure else False,
                            'volume': (
                                float(product_variation_values['woocommerce_dimensions']['length'])
                                * float(product_variation_values['woocommerce_dimensions']['width'])
                                * float(product_variation_values['woocommerce_dimensions']['height'])
                                if (
                                    product_variation_values['woocommerce_dimensions']['length']
                                    and product_variation_values['woocommerce_dimensions']['width']
                                    and product_variation_values['woocommerce_dimensions']['height']
                                )
                                else False
                            ),
                            'product_tmpl_id': odoo_product.id,
                            'product_template_attribute_value_ids': [(6, 0, odoo_product_template_attribute_value_ids)],
                        },
                    )

                    # Product type
                    if version_info[0] == 16:
                        product_variation_values['detailed_type'] = 'service' if product_variation_values['woocommerce_service'] else 'product' if product_variation_values['woocommerce_manage_stock'] else 'consu'

                    elif version_info[0] in [18, 19]:
                        product_variation_values['type'] = 'service' if product_variation_values['woocommerce_service'] else 'consu'
                        product_variation_values['is_storable'] = bool(product_variation_values['woocommerce_manage_stock'])

                    attribute_values_recset = self.env['product.template.attribute.value'].with_context(lang=False).browse(odoo_product_template_attribute_value_ids)
                    odoo_product_variant = odoo_existing_variant or odoo_product._get_variant_for_combination(attribute_values_recset)

                    if odoo_existing_variant:
                        updated_count += 1
                    else:
                        new_count += 1

                    if not odoo_product_variant:
                        # Use the safer Odoo method to create a variant from a specific combination
                        odoo_product_variant = odoo_product._create_product_variant(attribute_values_recset)

                    # Odoo product variant exists or was just created, now update it
                    odoo_product_variant.write(product_variation_values)

                    _logger.info(
                        f'Updated WooCommerce product variation in Odoo: {odoo_product_variant.display_name} (Odoo product variant ID: {odoo_product_variant.id}, WooCommerce product variation ID: {woocommerce_variation["id"]})'
                    )

                # After processing all variations for the current product
                aggregated_tax_ids = []
                for variant in odoo_product.product_variant_ids:
                    # Extend the list with the tax IDs from each variant
                    aggregated_tax_ids.extend(variant.taxes_id.ids)

                if aggregated_tax_ids:
                    # Remove duplicates by converting to a set, then back to a list
                    aggregated_tax_ids = list(set(aggregated_tax_ids))

                    # Update the parent product (product.template) with the distinct tax IDs
                    if set(odoo_product.taxes_id.ids) != set(aggregated_tax_ids):
                        odoo_product.write({'taxes_id': [(6, 0, aggregated_tax_ids)]})

                # Save SKU back to 'parent.template'
                if odoo_product.default_code != odoo_product_sku:
                    odoo_product.write({'default_code': odoo_product_sku})

        except Exception:
            # Roll back only this record's changes, keeping other records already written in this chunk job
            savepoint.rollback()
            _logger.exception(f'Error syncing WooCommerce product: {woocommerce_product["name"]} (WooCommerce product ID: {woocommerce_product["id"]})')
            raise
        finally:
            # Release the savepoint (whether it was rolled back above or the record synced successfully) so it never lingers open for the rest of this chunk job's transaction
            savepoint.close(rollback=False)

        return {'processed': processed_count, 'new': new_count, 'updated': updated_count, 'skipped': skipped_count}

    @api.model
    def woocommerce_to_odoo_products_variations_chunk_sync(
        self: models.Model,
        woocommerce_products: list[dict[str, Any]],
        woocommerce_currency: str,
        woocommerce_tax_rates: dict[str, float],
        woocommerce_prices_include_tax: bool,
        woocommerce_weight_unit: str,
        woocommerce_dimension_unit: str,
    ) -> None:
        """Processes a chunk of WooCommerce products' variations sequentially within a single queue job."""
        self.ensure_one()

        # Shared attribute/attribute-value/tax-rate/UoM caches for the products in this chunk
        odoo_attribute_cache: dict[str, int] = {}
        odoo_attribute_value_cache: dict[tuple[int, str], int] = {}
        odoo_tax_rate_cache: dict[tuple[float, bool], int] = {}
        odoo_uom_cache: dict[str, int] = {}
        errors: list[str] = []
        processed_count = new_count = updated_count = 0

        for woocommerce_product in woocommerce_products:
            try:
                variation_counts = self.woocommerce_to_odoo_product_variations_sync(
                    woocommerce_product,
                    woocommerce_currency,
                    woocommerce_tax_rates,
                    woocommerce_prices_include_tax,
                    woocommerce_weight_unit,
                    woocommerce_dimension_unit,
                    odoo_attribute_cache,
                    odoo_attribute_value_cache,
                    odoo_tax_rate_cache,
                    odoo_uom_cache,
                )
                processed_count += variation_counts['processed']
                new_count += variation_counts['new']
                updated_count += variation_counts['updated']
            except Exception as error:
                if isinstance(error, (RetryableJobError, psycopg2.errors.SerializationFailure, psycopg2.errors.DeadlockDetected)):
                    raise
                _logger.exception(f'Error syncing WooCommerce product variations for product {woocommerce_product.get("id")} within chunk job')
                errors.append(f'Product variations {woocommerce_product.get("id")}: {error}')

        self.sync_summary_chunk_completed('variations', processed_count, new_count, updated_count, errors)

    def woocommerce_to_odoo_products_variations_sync_batch(
        self: models.Model,
        woocommerce_currency: str,
        woocommerce_tax_rates: dict[str, float],
        woocommerce_prices_include_tax: bool,
        woocommerce_weight_unit: str,
        woocommerce_dimension_unit: str,
    ) -> None:
        self.ensure_one()

        # WooCommerce REST API
        woocommerce_api = self.woocommerce_api_get()

        # Check if WooCommerce REST API connection is successful
        if not woocommerce_api:
            error_message = 'WooCommerce REST API connection failed. WooCommerce to Odoo products variations sync process halted; Please check your connection settings in the WooCommerce Configuration'
            _logger.error(error_message)
            return

        # WooCommerce REST API parameters
        params = {'status': 'publish', '_fields': 'id,sku,name,type,variations'}
        if not self.settings_woocommerce_test_mode:
            params['type'] = 'variable'

        if self.settings_woocommerce_modified_records_import:
            odoo_woocommerce_last_sync = self.odoo_woocommerce_last_sync_retrieve()
            if odoo_woocommerce_last_sync:
                params['modified_after'] = odoo_woocommerce_last_sync.strftime('%Y-%m-%dT%H:%M:%S')  # ISO 8601 date format

        if self.settings_woocommerce_to_odoo_products_language_code:
            params['lang'] = self.settings_woocommerce_to_odoo_products_language_code

        for woocommerce_products_batch in self.woocommerce_api_get_items_in_batches(woocommerce_api, endpoint='products', params=params):
            # In test mode this is the same first page used by product import, so variations are only scheduled for parent products included in that sample
            woocommerce_products_batch = [woocommerce_product for woocommerce_product in woocommerce_products_batch if woocommerce_product['type'] == 'variable' and woocommerce_product['sku']]

            # Schedule a job per chunk of WooCommerce products instead of one job per product, to reduce per-job overhead
            for products_chunk in self.list_chunks(woocommerce_products_batch, self.settings_job_chunk_size):
                chunk_identity_key = '-'.join(str(woocommerce_product['id']) for woocommerce_product in products_chunk)
                self.with_delay(
                    identity_key=f'woocommerce_to_odoo_products_variations_chunk_sync-{self.id}-{chunk_identity_key}', description=self.job_description('woocommerce_to_odoo_products_variations_chunk_sync')
                ).woocommerce_to_odoo_products_variations_chunk_sync(products_chunk, woocommerce_currency, woocommerce_tax_rates, woocommerce_prices_include_tax, woocommerce_weight_unit, woocommerce_dimension_unit)
                self.sync_summary_chunk_dispatched()

    def woocommerce_to_odoo_customer_sync(self: models.Model, woocommerce_customer: dict[str, Any], odoo_customers: dict[str, Any], odoo_country_cache: dict[str, int] | None = None) -> str:
        """Returns 'created', 'updated' or 'skipped', so the calling chunk job can tally per-direction sync-summary counts."""
        # Isolate this record's writes so a failure only rolls back to here, not the whole chunk job's transaction
        savepoint = self.env.cr.savepoint()
        try:
            # Try to find the corresponding partner in Odoo by its WooCommerce customer ID
            odoo_customer = odoo_customers.get(str(woocommerce_customer['id']))

            if odoo_customer:
                odoo_customer = self.env['res.partner'].browse(odoo_customer['id'])

                # Skip if not modified
                if self.datetime_convert(woocommerce_customer['date_modified_gmt']) <= odoo_customer.write_date:
                    _logger.info(f'Skipped import of WooCommerce customer into Odoo: {odoo_customer.name} (Odoo customer ID: {odoo_customer.id}, WooCommerce customer ID: {odoo_customer.woocommerce_id})')
                    return 'skipped'

            # Create new customer in Odoo if it does not yet exist or update customer in Odoo only if WooCommerce version is newer

            # Custom fields
            customer_values = {
                'woocommerce_site_url': self.settings_woocommerce_connection_url,
                'woocommerce_to_odoo_last_sync': fields.Datetime.now(),
            }

            # WooCommerce REST API - Customer properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#customer-properties
            customer_values.update(
                {
                    'woocommerce_id': woocommerce_customer['id'],
                    'woocommerce_date_created': woocommerce_customer['date_created'],
                    'woocommerce_date_created_gmt': woocommerce_customer['date_created_gmt'],
                    'woocommerce_date_modified': woocommerce_customer['date_modified'],
                    'woocommerce_date_modified_gmt': woocommerce_customer['date_modified_gmt'],
                    'woocommerce_email': woocommerce_customer['email'],
                    'woocommerce_first_name': woocommerce_customer['first_name'],
                    'woocommerce_last_name': woocommerce_customer['last_name'],
                    'woocommerce_role': woocommerce_customer['role'],
                    'woocommerce_username': woocommerce_customer['username'],
                    'woocommerce_is_paying_customer': woocommerce_customer['is_paying_customer'],
                    'woocommerce_avatar_url': woocommerce_customer['avatar_url'],
                    'woocommerce_meta_data': woocommerce_customer['meta_data'],
                },
            )

            # WooCommerce REST API - Customer billing properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#customer-billing-properties
            customer_values.update(
                {
                    'woocommerce_billing_first_name': woocommerce_customer['billing']['first_name'],
                    'woocommerce_billing_last_name': woocommerce_customer['billing']['last_name'],
                    'woocommerce_billing_company': woocommerce_customer['billing']['company'],
                    'woocommerce_billing_address_1': woocommerce_customer['billing']['address_1'],
                    'woocommerce_billing_address_2': woocommerce_customer['billing']['address_2'],
                    'woocommerce_billing_city': woocommerce_customer['billing']['city'],
                    'woocommerce_billing_state': woocommerce_customer['billing']['state'],
                    'woocommerce_billing_postcode': woocommerce_customer['billing']['postcode'],
                    'woocommerce_billing_country': woocommerce_customer['billing']['country'],
                    'woocommerce_billing_email': woocommerce_customer['billing']['email'],
                    'woocommerce_billing_phone': woocommerce_customer['billing']['phone'],
                },
            )

            # WooCommerce REST API - Customer shipping properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#customer-shipping-properties
            customer_values.update(
                {
                    'woocommerce_shipping_first_name': woocommerce_customer['shipping']['first_name'],
                    'woocommerce_shipping_last_name': woocommerce_customer['shipping']['last_name'],
                    'woocommerce_shipping_company': woocommerce_customer['shipping']['company'],
                    'woocommerce_shipping_address_1': woocommerce_customer['shipping']['address_1'],
                    'woocommerce_shipping_address_2': woocommerce_customer['shipping']['address_2'],
                    'woocommerce_shipping_city': woocommerce_customer['shipping']['city'],
                    'woocommerce_shipping_state': woocommerce_customer['shipping']['state'],
                    'woocommerce_shipping_postcode': woocommerce_customer['shipping']['postcode'],
                    'woocommerce_shipping_country': woocommerce_customer['shipping']['country'],
                },
            )

            # Localization

            ## Brazil (requires 'l10n_br_base' Odoo add-on)
            if self.env['ir.module.module'].with_context(lang=False).search([('name', '=', 'l10n_br_base'), ('state', '=', 'installed')], limit=1):
                billing = woocommerce_customer['billing']

                # '_billing_persontype' meta: '1' = pessoa física (prefer CPF), '2' = pessoa jurídica (prefer CNPJ)
                cpf_cnpj = (billing.get('cnpj') or billing.get('cpf')) if str(billing.get('persontype') or '') == '2' else (billing.get('cpf') or billing.get('cnpj'))

                # Check for CPF/CNPJ and update if present
                if cpf_cnpj and 'cnpj_cpf' in self.env['res.partner']._fields:
                    customer_values['cnpj_cpf'] = cpf_cnpj

                # Check for RG and update if present
                if billing.get('rg') and 'l10n_br_rg_code' in self.env['res.partner']._fields:
                    customer_values['l10n_br_rg_code'] = billing['rg']

                # Check for IE and update if present
                if billing.get('ie') and 'l10n_br_ie_code' in self.env['res.partner']._fields:
                    customer_values['l10n_br_ie_code'] = billing['ie']

            # Custom fields
            customer_values.update(
                {
                    'woocommerce_last_login_date': datetime.fromtimestamp(timestamp=int(meta['value']), tz=UTC).replace(tzinfo=None)
                    if (meta := next((meta for meta in woocommerce_customer['meta_data'] if meta.get('key') == 'wfls-last-login'), None))
                    else None,  # Wordfence Security field
                },
            )

            # WooCommerce's 'date_created'/'date_modified' are store-local; use the '_gmt' siblings for both
            self.datetime_gmt_pairs_convert(customer_values, ['woocommerce_date_created', 'woocommerce_date_modified'])

            # Customer avatar
            if self.settings_woocommerce_images_sync and woocommerce_customer['avatar_url'] != '':
                odoo_avatar_url = self.image_download_file_to_base64({'src': woocommerce_customer['avatar_url']})
            else:
                odoo_avatar_url = None

            # Odoo 'res.partner' model fields
            customer_name = f'{customer_values["woocommerce_first_name"]} {customer_values["woocommerce_last_name"]}'.strip()
            customer_values.update(
                {
                    # General information
                    'name': customer_values['woocommerce_billing_company'] or customer_name or 'Unknown',
                    'image_1920': odoo_avatar_url,
                    'ref': customer_values['woocommerce_id'],
                    'create_date': customer_values['woocommerce_date_created_gmt'],
                    'company_type': 'person',
                    'customer_rank': 1 if customer_values['woocommerce_is_paying_customer'] else 0,
                    'email': customer_values['woocommerce_email'],
                    'phone': customer_values['woocommerce_billing_phone'],
                    'user_id': self.settings_woocommerce_user_responsible.id,
                    # Customer status
                    'active': True,
                    # Address
                    'street': customer_values['woocommerce_billing_address_1'],
                    'street2': customer_values['woocommerce_billing_address_2'],
                    'city': customer_values['woocommerce_billing_city'],
                    'zip': customer_values['woocommerce_billing_postcode'],
                    'country_id': self.odoo_country_retrieve(customer_values['woocommerce_billing_country'], cache=odoo_country_cache).id,
                },
            )

            if odoo_customer:
                if customer_values['woocommerce_date_modified_gmt'] > odoo_customer['write_date']:
                    odoo_customer.write(customer_values)
                    sync_status = 'updated'
                    _logger.info(f'Updated WooCommerce customer in Odoo: {odoo_customer.name} (Odoo customer ID: {odoo_customer.id}, WooCommerce customer ID: {odoo_customer["woocommerce_id"]})')
                else:
                    sync_status = 'skipped'

            else:
                try:
                    with self.env.cr.savepoint():
                        odoo_customer = self.env['res.partner'].create(customer_values)
                except Exception:
                    # A concurrent sync job (e.g. order sync creating the same customer from billing info) already created this customer - reuse it instead of failing
                    odoo_customer = self.env['res.partner'].search([('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('woocommerce_id', '=', customer_values['woocommerce_id'])], limit=1)
                    if not odoo_customer:
                        raise
                    odoo_customer.write(customer_values)
                sync_status = 'created'
                _logger.info(f'Imported WooCommerce customer into Odoo: {odoo_customer.name} (Odoo customer ID: {odoo_customer.id}, WooCommerce customer ID: {odoo_customer["woocommerce_id"]})')

            # Shipping address (only creates/updates a child contact when WooCommerce shipping fields are actually set)
            self.odoo_customer_shipping_address_create_or_update(odoo_customer, customer_values, odoo_country_cache)

        except Exception:
            # Roll back only this record's changes, keeping other records already written in this chunk job
            savepoint.rollback()
            _logger.exception(f'Error syncing WooCommerce customer: {woocommerce_customer["first_name"]} {woocommerce_customer["last_name"]} (WooCommerce customer ID: {woocommerce_customer["id"]})')
            raise
        finally:
            # Release the savepoint (whether it was rolled back above or the record synced successfully) so it never lingers open for the rest of this chunk job's transaction
            savepoint.close(rollback=False)

        return sync_status

    @api.model
    def woocommerce_to_odoo_customers_chunk_sync(self: models.Model, woocommerce_customers: list[dict[str, Any]], odoo_customers: dict[str, Any]) -> None:
        """Processes a chunk of WooCommerce customers sequentially within a single queue job."""
        self.ensure_one()

        # Shared country cache for the customers in this chunk
        odoo_country_cache: dict[str, int] = {}
        new_count = updated_count = 0
        errors: list[str] = []

        for woocommerce_customer in woocommerce_customers:
            try:
                sync_status = self.woocommerce_to_odoo_customer_sync(woocommerce_customer, odoo_customers, odoo_country_cache)
                if sync_status == 'created':
                    new_count += 1
                elif sync_status == 'updated':
                    updated_count += 1
            except Exception as error:
                if isinstance(error, (RetryableJobError, psycopg2.errors.SerializationFailure, psycopg2.errors.DeadlockDetected)):
                    raise
                _logger.exception(f'Error syncing WooCommerce customer {woocommerce_customer.get("id")} within chunk job')
                errors.append(f'Customer {woocommerce_customer.get("id")}: {error}')

        self.sync_summary_chunk_completed('customers', len(woocommerce_customers), new_count, updated_count, errors)

    def woocommerce_to_odoo_customers_sync_batch(self: models.Model) -> None:
        self.ensure_one()

        # WooCommerce REST API
        woocommerce_api = self.woocommerce_api_get()

        # Check if WooCommerce REST API connection is successful
        if not woocommerce_api:
            error_message = 'WooCommerce REST API connection failed. WooCommerce to Odoo customers sync process halted; Please check your connection settings in the WooCommerce Configuration'
            _logger.error(error_message)
            return

        # WooCommerce REST API parameters
        params = {}

        if self.settings_woocommerce_modified_records_import:
            odoo_woocommerce_last_sync = self.odoo_woocommerce_last_sync_retrieve()
            if odoo_woocommerce_last_sync:
                params['modified_after'] = odoo_woocommerce_last_sync.strftime('%Y-%m-%dT%H:%M:%S')  # ISO 8601 date format

        # Get all Odoo partners with WooCommerce customer ID
        odoo_customers = (
            self.env['res.partner']
            .with_context(lang=False)
            .search_read(
                [('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('active', '=', True), ('woocommerce_id', '!=', False)],
                fields=['id', 'name', 'active', 'write_date', 'woocommerce_id'],
            )
        )
        odoo_customers = {odoo_customer['woocommerce_id']: odoo_customer for odoo_customer in odoo_customers}

        for woocommerce_customers_batch in self.woocommerce_api_get_items_in_batches(woocommerce_api, endpoint='customers', params=params):
            # Schedule a job per chunk of WooCommerce customers instead of one job per customer, to reduce per-job overhead
            for customers_chunk in self.list_chunks(woocommerce_customers_batch, self.settings_job_chunk_size):
                chunk_identity_key = '-'.join(str(woocommerce_customer['id']) for woocommerce_customer in customers_chunk)
                self.with_delay(
                    identity_key=f'woocommerce_to_odoo_customers_chunk_sync-{self.id}-{chunk_identity_key}', description=self.job_description('woocommerce_to_odoo_customers_chunk_sync')
                ).woocommerce_to_odoo_customers_chunk_sync(customers_chunk, odoo_customers)
                self.sync_summary_chunk_dispatched()

    def woocommerce_to_odoo_order_sync(
        self: models.Model,
        woocommerce_order: dict[str, Any],
        woocommerce_tax_rates: dict[str, float],
        woocommerce_weight_unit: str,
        woocommerce_shipping_methods: list[dict[str, Any]],
        odoo_sale_orders: dict[str, Any],
        odoo_tax_rate_cache: dict[tuple[float, bool], int] | None = None,
        odoo_uom_cache: dict[str, int] | None = None,
        odoo_country_cache: dict[str, int] | None = None,
    ) -> str:
        """Returns 'created', 'updated' or 'skipped', so the calling chunk job can tally per-direction sync-summary counts."""
        # Isolate this record's writes so a failure only rolls back to here, not the whole chunk job's transaction
        savepoint = self.env.cr.savepoint()
        try:
            # Try to find the corresponding sale order in Odoo by its WooCommerce order ID
            odoo_sale_order = odoo_sale_orders.get(str(woocommerce_order['id']))

            if odoo_sale_order:
                odoo_sale_order = self.env['sale.order'].browse(odoo_sale_order['id'])

                # Skip if not modified
                if self.datetime_convert(woocommerce_order['date_modified_gmt']) <= odoo_sale_order.write_date:
                    _logger.info(f'Skipped import of WooCommerce order into Odoo: {odoo_sale_order.name} (Odoo sale order ID: {odoo_sale_order.id}, WooCommerce order ID: {odoo_sale_order.woocommerce_number})')
                    return 'skipped'

            # Create new sale order in Odoo if it does not yet exist or update sale order in Odoo only if WooCommerce version is newer

            # Custom fields
            order_values = {
                'woocommerce_site_url': self.settings_woocommerce_connection_url,
                'woocommerce_to_odoo_last_sync': fields.Datetime.now(),
            }

            # WooCommerce REST API - Order properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#order-properties
            order_values.update(
                {
                    'woocommerce_id': woocommerce_order['id'],
                    'woocommerce_parent_id': woocommerce_order['parent_id'],
                    'woocommerce_number': woocommerce_order['number'],
                    'woocommerce_order_key': woocommerce_order['order_key'],
                    'woocommerce_created_via': woocommerce_order['created_via'],
                    'woocommerce_version': woocommerce_order['version'],
                    'woocommerce_status': woocommerce_order['status'],
                    'woocommerce_currency': woocommerce_order['currency'],
                    'woocommerce_date_created': woocommerce_order['date_created'],
                    'woocommerce_date_created_gmt': woocommerce_order['date_created_gmt'],
                    'woocommerce_date_modified': woocommerce_order['date_modified'],
                    'woocommerce_date_modified_gmt': woocommerce_order['date_modified_gmt'],
                    'woocommerce_discount_total': woocommerce_order['discount_total'],
                    'woocommerce_discount_tax': woocommerce_order['discount_tax'],
                    'woocommerce_shipping_total': woocommerce_order['shipping_total'],
                    'woocommerce_shipping_tax': woocommerce_order['shipping_tax'],
                    'woocommerce_cart_tax': woocommerce_order['cart_tax'],
                    'woocommerce_total': woocommerce_order['total'],
                    'woocommerce_total_tax': woocommerce_order['total_tax'],
                    'woocommerce_prices_include_tax': woocommerce_order['prices_include_tax'],
                    'woocommerce_customer_id': woocommerce_order['customer_id'],
                    'woocommerce_customer_ip_address': woocommerce_order['customer_ip_address'],
                    'woocommerce_customer_user_agent': woocommerce_order['customer_user_agent'],
                    'woocommerce_customer_note': woocommerce_order['customer_note'],
                    'woocommerce_payment_method': woocommerce_order['payment_method'],
                    'woocommerce_payment_method_title': woocommerce_order['payment_method_title'],
                    'woocommerce_transaction_id': woocommerce_order['transaction_id'],
                    'woocommerce_date_paid': woocommerce_order['date_paid'],
                    'woocommerce_date_paid_gmt': woocommerce_order['date_paid_gmt'],
                    'woocommerce_date_completed': woocommerce_order['date_completed'],
                    'woocommerce_date_completed_gmt': woocommerce_order['date_completed_gmt'],
                    'woocommerce_cart_hash': woocommerce_order['cart_hash'],
                    'woocommerce_meta_data': woocommerce_order['meta_data'],
                    'woocommerce_line_items': woocommerce_order['line_items'],
                    'woocommerce_tax_lines': woocommerce_order['tax_lines'],
                    'woocommerce_shipping_lines': woocommerce_order['shipping_lines'],
                    'woocommerce_fee_lines': woocommerce_order['fee_lines'],
                    'woocommerce_coupon_lines': woocommerce_order['coupon_lines'],
                    'woocommerce_refunds': woocommerce_order['refunds'],
                    # 'woocommerce_set_paid': woocommerce_order['set_paid'],
                },
            )

            # WooCommerce REST API - Order billing properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#order-billing-properties
            order_values.update(
                {
                    'woocommerce_billing_first_name': woocommerce_order['billing']['first_name'],
                    'woocommerce_billing_last_name': woocommerce_order['billing']['last_name'],
                    'woocommerce_billing_company': woocommerce_order['billing']['company'],
                    'woocommerce_billing_address_1': woocommerce_order['billing']['address_1'],
                    'woocommerce_billing_address_2': woocommerce_order['billing']['address_2'],
                    'woocommerce_billing_city': woocommerce_order['billing']['city'],
                    'woocommerce_billing_state': woocommerce_order['billing']['state'],
                    'woocommerce_billing_postcode': woocommerce_order['billing']['postcode'],
                    'woocommerce_billing_country': woocommerce_order['billing']['country'],
                    'woocommerce_billing_email': woocommerce_order['billing']['email'],
                    'woocommerce_billing_phone': woocommerce_order['billing']['phone'],
                },
            )

            # WooCommerce REST API - Order shipping properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#order-shipping-properties
            order_values.update(
                {
                    'woocommerce_shipping_first_name': woocommerce_order['shipping']['first_name'],
                    'woocommerce_shipping_last_name': woocommerce_order['shipping']['last_name'],
                    'woocommerce_shipping_company': woocommerce_order['shipping']['company'],
                    'woocommerce_shipping_address_1': woocommerce_order['shipping']['address_1'],
                    'woocommerce_shipping_address_2': woocommerce_order['shipping']['address_2'],
                    'woocommerce_shipping_city': woocommerce_order['shipping']['city'],
                    'woocommerce_shipping_state': woocommerce_order['shipping']['state'],
                    'woocommerce_shipping_postcode': woocommerce_order['shipping']['postcode'],
                    'woocommerce_shipping_country': woocommerce_order['shipping']['country'],
                },
            )

            # Fees
            woocommerce_transaction_fee = None

            ## PayPal
            woocommerce_transaction_fee_paypal = next((item['value'] for item in order_values['woocommerce_meta_data'] if item.get('key') == 'PayPal Transaction Fee'), None)

            ## Stripe
            woocommerce_transaction_fee_stripe = next((item['value'] for item in order_values['woocommerce_meta_data'] if item.get('key') == '_stripe_fee'), None)

            woocommerce_transaction_fee = woocommerce_transaction_fee_paypal or woocommerce_transaction_fee_stripe

            # Custom fields
            if woocommerce_transaction_fee:
                order_values.update(
                    {
                        'woocommerce_transaction_fee': woocommerce_transaction_fee,
                    },
                )
            order_values.update(
                {
                    'language_code': woocommerce_order.get('lang', None),  # Language (requires Polylang WordPress plugin)
                },
            )

            # WooCommerce's 'date_created'/'date_modified'/'date_paid'/'date_completed' are store-local; use the '_gmt' siblings for both
            self.datetime_gmt_pairs_convert(order_values, ['woocommerce_date_created', 'woocommerce_date_modified', 'woocommerce_date_paid', 'woocommerce_date_completed'])

            # Currency
            odoo_order_currency = None
            if order_values['woocommerce_currency']:
                odoo_order_currency = self.odoo_currency_retrieve(order_values['woocommerce_currency'])

            # Odoo customer reference
            odoo_customer = (
                self.env['res.partner']
                .with_context(lang=False)
                .search(
                    [
                        ('woocommerce_site_url', '=', self.settings_woocommerce_connection_url),
                        ('active', '=', True),
                        ('woocommerce_id', '=', woocommerce_order['customer_id']),
                    ],
                    limit=1,
                )
            )

            if not odoo_customer:
                if self.settings_woocommerce_orders_customers_map:
                    customer_values = {
                        'woocommerce_site_url': self.settings_woocommerce_connection_url,
                        'woocommerce_id': woocommerce_order['customer_id'] or False,
                    }

                    # WooCommerce REST API - Customer billing properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#customer-billing-properties
                    customer_values.update(
                        {
                            'woocommerce_billing_first_name': woocommerce_order['billing']['first_name'],
                            'woocommerce_billing_last_name': woocommerce_order['billing']['last_name'],
                            'woocommerce_billing_company': woocommerce_order['billing']['company'],
                            'woocommerce_billing_address_1': woocommerce_order['billing']['address_1'],
                            'woocommerce_billing_address_2': woocommerce_order['billing']['address_2'],
                            'woocommerce_billing_city': woocommerce_order['billing']['city'],
                            'woocommerce_billing_state': woocommerce_order['billing']['state'],
                            'woocommerce_billing_postcode': woocommerce_order['billing']['postcode'],
                            'woocommerce_billing_country': woocommerce_order['billing']['country'],
                            'woocommerce_billing_email': woocommerce_order['billing']['email'],
                            'woocommerce_billing_phone': woocommerce_order['billing']['phone'],
                        },
                    )

                    # WooCommerce REST API - Customer shipping properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#customer-shipping-properties
                    customer_values.update(
                        {
                            'woocommerce_shipping_first_name': woocommerce_order['shipping']['first_name'],
                            'woocommerce_shipping_last_name': woocommerce_order['shipping']['last_name'],
                            'woocommerce_shipping_company': woocommerce_order['shipping']['company'],
                            'woocommerce_shipping_address_1': woocommerce_order['shipping']['address_1'],
                            'woocommerce_shipping_address_2': woocommerce_order['shipping']['address_2'],
                            'woocommerce_shipping_city': woocommerce_order['shipping']['city'],
                            'woocommerce_shipping_state': woocommerce_order['shipping']['state'],
                            'woocommerce_shipping_postcode': woocommerce_order['shipping']['postcode'],
                            'woocommerce_shipping_country': woocommerce_order['shipping']['country'],
                        },
                    )

                    # Localization

                    ## Brazil (requires 'l10n_br_base' Odoo add-on)
                    if self.env['ir.module.module'].with_context(lang=False).search([('name', '=', 'l10n_br_base'), ('state', '=', 'installed')], limit=1):
                        billing = woocommerce_order['billing']

                        # '_billing_persontype' meta: '1' = pessoa física (prefer CPF), '2' = pessoa jurídica (prefer CNPJ)
                        cpf_cnpj = (billing.get('cnpj') or billing.get('cpf')) if str(billing.get('persontype') or '') == '2' else (billing.get('cpf') or billing.get('cnpj'))

                        # Check for CPF/CNPJ and update if present
                        if cpf_cnpj and 'cnpj_cpf' in self.env['res.partner']._fields:
                            customer_values['cnpj_cpf'] = cpf_cnpj

                        # Check for RG and update if present
                        if billing.get('rg') and 'l10n_br_rg_code' in self.env['res.partner']._fields:
                            customer_values['l10n_br_rg_code'] = billing['rg']

                        # Check for IE and update if present
                        if billing.get('ie') and 'l10n_br_ie_code' in self.env['res.partner']._fields:
                            customer_values['l10n_br_ie_code'] = billing['ie']

                    # Odoo 'res.partner' model fields
                    customer_name = f'{customer_values["woocommerce_billing_first_name"]} {customer_values["woocommerce_billing_last_name"]}'.strip()
                    customer_values.update(
                        {
                            # General information
                            'name': customer_values['woocommerce_billing_company'] or customer_name or 'Unknown',
                            'ref': customer_values['woocommerce_id'],
                            'company_type': 'person',
                            'email': customer_values['woocommerce_billing_email'],
                            'phone': customer_values['woocommerce_billing_phone'],
                            'user_id': self.settings_woocommerce_user_responsible.id,
                            # Customer status
                            'active': True,
                            # Address
                            'street': customer_values['woocommerce_billing_address_1'],
                            'street2': customer_values['woocommerce_billing_address_2'],
                            'city': customer_values['woocommerce_billing_city'],
                            'zip': customer_values['woocommerce_billing_postcode'],
                            'country_id': self.odoo_country_retrieve(customer_values['woocommerce_billing_country'], cache=odoo_country_cache).id,
                        },
                    )

                    # Check for duplicate email
                    if customer_values['email']:
                        odoo_customer = (
                            self.env['res.partner']
                            .with_context(lang=False)
                            .search(
                                [('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('active', '=', True), ('email', '=', customer_values['email'])],
                                limit=1,
                            )
                        )

                        if not odoo_customer:
                            try:
                                with self.env.cr.savepoint():
                                    odoo_customer = self.env['res.partner'].create(customer_values)
                            except Exception:
                                # A concurrent sync job (e.g. the dedicated customer sync) already created this customer - reuse it instead of failing
                                odoo_customer = self.env['res.partner'].search(
                                    [('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('woocommerce_id', '=', customer_values['woocommerce_id'])], limit=1
                                )
                                if not odoo_customer:
                                    raise

                    if not odoo_customer:
                        odoo_customer = self.odoo_customer_placeholder_create_or_retrieve()

                else:
                    # Create/retrieve customer placeholder
                    odoo_customer = self.odoo_customer_placeholder_create_or_retrieve()

            # Shipping address - only differs from 'odoo_customer' (billing) when WooCommerce shipping fields are actually set on the order
            odoo_customer_shipping_address = self.odoo_customer_shipping_address_create_or_update(odoo_customer, order_values, odoo_country_cache)

            # Odoo 'sale.order' model fields
            order_values.update(
                {
                    # General information
                    'name': f'#{order_values["woocommerce_number"]} {order_values["woocommerce_billing_first_name"]} {order_values["woocommerce_billing_last_name"]}',
                    'country_code': order_values['woocommerce_billing_country'],
                    'client_order_ref': order_values['woocommerce_number'],
                    'origin': order_values['woocommerce_created_via'],
                    'type_name': 'Sales Order',
                    'date_order': order_values['woocommerce_date_created_gmt'],
                    'note': order_values['woocommerce_customer_note'],
                    'user_id': self.settings_woocommerce_user_responsible.id,
                    # Customer
                    'partner_id': odoo_customer.id,
                    'partner_invoice_id': odoo_customer.id,
                    'partner_shipping_id': odoo_customer_shipping_address.id,
                    # Shipping and stock
                    'picking_policy': 'direct',
                    # 'warehouse_id': self.settings_woocommerce_products_warehouse_location.id,
                    # Payment
                    # 'currency_id': odoo_order_currency.id,
                    # 'tax_country_id': self.env['res.country'].with_context(lang=False).search([('code', '=', order_values['woocommerce_billing_country'])], limit=1).id,
                    # 'amount_tax': order_values['woocommerce_total_tax'],
                    # 'amount_total': order_values['woocommerce_total'],
                },
            )

            if odoo_sale_order:
                odoo_sale_order.write(order_values)
                sync_status = 'updated'
                _logger.info(f'Updated WooCommerce order in Odoo: {odoo_sale_order.name} (Odoo sale order ID: {odoo_sale_order.id}, WooCommerce order ID: {odoo_sale_order["woocommerce_number"]})')

            else:
                odoo_sale_order = self.env['sale.order'].create(order_values)
                sync_status = 'created'
                _logger.info(f'Imported WooCommerce order into Odoo: {odoo_sale_order.name} (Odoo sale order ID: {odoo_sale_order.id}, WooCommerce order ID: {odoo_sale_order["woocommerce_number"]})')

            # Order line items
            order_line_items_total = sum(float(line_item['total']) for line_item in woocommerce_order['line_items'])

            for line_item in woocommerce_order['line_items']:
                # Custom fields
                order_line_values = {
                    'woocommerce_site_url': self.settings_woocommerce_connection_url,
                    'woocommerce_to_odoo_last_sync': fields.Datetime.now(),
                }

                # WooCommerce REST API - Order line items properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#order-line-items-properties
                order_line_values.update(
                    {
                        'woocommerce_id': line_item['id'],
                        'woocommerce_name': line_item['name'],
                        'woocommerce_product_id': line_item['product_id'],
                        'woocommerce_variation_id': line_item['variation_id'],
                        'woocommerce_quantity': line_item['quantity'],
                        'woocommerce_tax_class': woocommerce_tax_rates.get(line_item['tax_class'] if line_item['tax_class'] else 'standard'),
                        'woocommerce_subtotal': line_item['subtotal'],
                        'woocommerce_subtotal_tax': line_item['subtotal_tax'],
                        'woocommerce_total': line_item['total'],
                        'woocommerce_total_tax': line_item['total_tax'],
                        'woocommerce_taxes': line_item['taxes'],
                        'woocommerce_meta_data': line_item['meta_data'],
                        'woocommerce_sku': line_item['sku'],
                        'woocommerce_price': line_item['price'],
                    },
                )

                # Additional fields
                order_line_values.update(
                    {
                        'woocommerce_weight_unit': woocommerce_weight_unit if woocommerce_weight_unit else None,
                    },
                )

                # Odoo product default code
                odoo_product_mapped = None

                if self.settings_woocommerce_line_items_product_map:
                    # Product
                    if line_item['variation_id'] == 0:
                        odoo_product_mapped = (
                            self.env['product.template']
                            .with_context(lang=False)
                            .search(
                                [
                                    ('woocommerce_site_url', '=', self.settings_woocommerce_connection_url),
                                    ('active', '=', True),
                                    ('woocommerce_id', '=', order_line_values['woocommerce_product_id']),
                                ],
                                limit=1,
                            )
                        )

                    # Product variation
                    else:
                        odoo_product_mapped = (
                            self.env['product.product']
                            .with_context(lang=False)
                            .search(
                                [
                                    ('woocommerce_site_url', '=', self.settings_woocommerce_connection_url),
                                    ('active', '=', True),
                                    ('woocommerce_id', '=', order_line_values['woocommerce_variation_id']),
                                ],
                                limit=1,
                            )
                        )

                if not odoo_product_mapped:
                    # Create/retrieve product placeholder
                    odoo_product = self.odoo_product_placeholder_create_or_retrieve()

                if odoo_product_mapped:
                    if line_item['variation_id'] == 0:
                        product_id = odoo_product_mapped.product_variant_ids[:1].id  # product.template → product.product
                    else:
                        product_id = odoo_product_mapped.id  # Already product.product
                else:
                    product_id = odoo_product.product_variant_ids[:1].id

                # Tax
                odoo_order_line_item_tax_id = []
                if order_line_values['woocommerce_tax_class']:
                    odoo_order_line_item_tax = self.odoo_tax_rate_create_or_retrieve(order_line_values['woocommerce_tax_class'], cache=odoo_tax_rate_cache)
                    if odoo_order_line_item_tax:
                        odoo_order_line_item_tax_id = [(6, 0, [odoo_order_line_item_tax.id])]

                # Unit of measure
                odoo_order_line_item_unit_of_measure = None
                if self.settings_woocommerce_products_package_size_unit_default:
                    odoo_order_line_item_unit_of_measure = self.env.ref('uom.product_uom_unit')

                elif order_line_values['woocommerce_weight_unit']:
                    odoo_order_line_item_unit_of_measure = self.odoo_unit_of_measure_create_or_retrieve(order_line_values['woocommerce_weight_unit'], cache=odoo_uom_cache)

                # Localization

                ## Brazil (requires 'l10n_br_sale' Odoo add-on)
                if (
                    self.env['ir.module.module'].with_context(lang=False).search([('name', '=', 'l10n_br_sale'), ('state', '=', 'installed')])
                    and woocommerce_order['shipping_total']
                    and order_line_items_total
                    and 'freight_value' in self.env['sale.order.line']._fields
                ):
                    order_line_values.update({'freight_value': float(woocommerce_order['shipping_total']) * (float(line_item['total']) / order_line_items_total)})

                # Odoo 'sale.order.line' model fields
                sale_order_line_fields = {
                    # General information
                    'order_id': odoo_sale_order.id,
                    'name': order_line_values['woocommerce_name'],
                    'product_id': product_id,
                    # Shipping and stock
                    'warehouse_id': self.settings_woocommerce_products_warehouse_location.id,
                    # Payment
                    'currency_id': odoo_order_currency.id if odoo_order_currency else False,
                    'product_uom_qty': order_line_values['woocommerce_quantity'],
                    # WooCommerce order line items always report 'price' as the tax-excluded (net) per-unit amount, regardless of
                    # the store's 'Prices entered with tax' setting (unlike product/variation prices) - grossed up to match the
                    # attached Odoo tax when 'settings_odoo_tax_calculation' resolves to tax-included
                    'price_unit': (
                        round(float(order_line_values['woocommerce_price']) * (1 + (order_line_values['woocommerce_tax_class'] or 0) / 100), 2)
                        if order_line_values['woocommerce_tax_class'] and self.odoo_tax_calculation_price_include()
                        else order_line_values['woocommerce_price']
                    ),
                    # 'discount'
                }

                # Dimensions
                if version_info[0] in [16, 18]:
                    sale_order_line_fields['product_uom'] = odoo_order_line_item_unit_of_measure.id if odoo_order_line_item_unit_of_measure else False

                elif version_info[0] == 19:
                    sale_order_line_fields['product_uom_id'] = odoo_order_line_item_unit_of_measure.id if odoo_order_line_item_unit_of_measure else False

                # Tax
                if version_info[0] in [16, 18]:
                    sale_order_line_fields['tax_id'] = odoo_order_line_item_tax_id
                elif version_info[0] == 19:
                    sale_order_line_fields['tax_ids'] = odoo_order_line_item_tax_id

                order_line_values.update(sale_order_line_fields)

                odoo_sale_order_line = self.env['sale.order.line'].search(
                    [
                        ('woocommerce_site_url', '=', self.settings_woocommerce_connection_url),
                        ('order_id', '=', odoo_sale_order.id),
                        ('woocommerce_id', '=', order_line_values['woocommerce_id']),
                    ],
                    limit=1,
                )

                # Verify if product exists
                product_id = order_line_values['product_id']
                if product_id and not self.env['product.product'].browse(product_id).exists():
                    _logger.warning(f'Skipping order line: product.product({product_id},) does not exist in Odoo')
                    continue

                # Update the sale order line
                if odoo_sale_order_line:
                    odoo_sale_order_line.write(order_line_values)

                else:
                    self.env['sale.order.line'].with_context(tracking_disable=True, mail_create_nosubscribe=True).create(order_line_values)

            # Fee lines (e.g. payment gateway surcharges) - represented as real order lines instead of only the raw 'woocommerce_fee_lines' JSON
            if woocommerce_order['fee_lines']:
                odoo_fee_product = self.odoo_woocommerce_service_product_create_or_retrieve('woocommerce_fee', 'WooCommerce Fee')
                for fee_line in woocommerce_order['fee_lines']:
                    odoo_fee_order_line = self.env['sale.order.line'].search(
                        [('order_id', '=', odoo_sale_order.id), ('woocommerce_id', '=', fee_line['id']), ('woocommerce_site_url', '=', self.settings_woocommerce_connection_url)], limit=1
                    )
                    fee_line_values = {
                        'woocommerce_site_url': self.settings_woocommerce_connection_url,
                        'woocommerce_id': fee_line['id'],
                        'order_id': odoo_sale_order.id,
                        'name': fee_line.get('name') or 'WooCommerce Fee',
                        'product_id': odoo_fee_product.id,
                        'product_uom_qty': 1,
                        'price_unit': float(fee_line['total']),
                    }
                    if odoo_fee_order_line:
                        odoo_fee_order_line.write(fee_line_values)
                    else:
                        self.env['sale.order.line'].with_context(tracking_disable=True, mail_create_nosubscribe=True).create(fee_line_values)

            # Coupon lines (discounts) - represented as negative-amount order lines instead of only the raw 'woocommerce_coupon_lines' JSON
            if woocommerce_order['coupon_lines']:
                odoo_coupon_product = self.odoo_woocommerce_service_product_create_or_retrieve('woocommerce_coupon', 'WooCommerce Discount')
                for coupon_line in woocommerce_order['coupon_lines']:
                    odoo_coupon_order_line = self.env['sale.order.line'].search(
                        [('order_id', '=', odoo_sale_order.id), ('woocommerce_id', '=', coupon_line['id']), ('woocommerce_site_url', '=', self.settings_woocommerce_connection_url)], limit=1
                    )
                    coupon_line_values = {
                        'woocommerce_site_url': self.settings_woocommerce_connection_url,
                        'woocommerce_id': coupon_line['id'],
                        'order_id': odoo_sale_order.id,
                        'name': f'Coupon: {coupon_line.get("code", "")}',
                        'product_id': odoo_coupon_product.id,
                        'product_uom_qty': 1,
                        'price_unit': -abs(float(coupon_line.get('discount') or 0.0)),
                    }
                    if odoo_coupon_order_line:
                        odoo_coupon_order_line.write(coupon_line_values)
                    else:
                        self.env['sale.order.line'].with_context(tracking_disable=True, mail_create_nosubscribe=True).create(coupon_line_values)

            # Confirm order if WooCommerce status is 'processing', 'on-hold' or 'completed' (move order 'state' to 'sale')
            if order_values['woocommerce_status'] in ('processing', 'on-hold', 'completed') and odoo_sale_order.state in ('draft', 'sent'):
                odoo_sale_order.action_confirm()

            # Cancel order if WooCommerce status is 'cancelled', 'refunded', 'failed' or 'trash' (move order 'state' to 'cancel')
            elif order_values['woocommerce_status'] in ('cancelled', 'refunded', 'failed', 'trash') and odoo_sale_order.state not in ('cancel', 'done'):
                odoo_sale_order.action_cancel()

            # Lock the order once WooCommerce marks it 'completed' (fulfilled/shipped), unlock it if it moves back to an earlier status
            if odoo_sale_order.state == 'sale' and version_info[0] == 16:
                if order_values['woocommerce_status'] == 'completed':
                    odoo_sale_order.action_done()  # Odoo 16 has no action_lock(); action_done() moves state to 'done' (Odoo 16's "Locked" state)
            elif odoo_sale_order.state == 'done' and version_info[0] == 16:
                if order_values['woocommerce_status'] != 'completed':
                    odoo_sale_order.action_unlock()
            elif odoo_sale_order.state == 'sale' and version_info[0] in (18, 19):
                odoo_sale_order.locked = order_values['woocommerce_status'] == 'completed'

            # Delivery carrier(s) - the first shipping line uses the native 'set_delivery_line' (one carrier per order); any additional shipping lines are added as extra order lines on the same carrier's product, instead of silently dropping them
            if woocommerce_order['shipping_lines']:
                first_shipping_line, *extra_shipping_lines = woocommerce_order['shipping_lines']

                odoo_delivery_carrier = self.odoo_delivery_carrier_create_or_retrieve(woocommerce_shipping_methods, first_shipping_line)

                if odoo_delivery_carrier:
                    odoo_sale_order.set_delivery_line(odoo_delivery_carrier, first_shipping_line['total'])

                for shipping_line in extra_shipping_lines:
                    extra_odoo_delivery_carrier = self.odoo_delivery_carrier_create_or_retrieve(woocommerce_shipping_methods, shipping_line)
                    if not extra_odoo_delivery_carrier:
                        continue

                    # Keyed by 'woocommerce_id' so resyncing the same order updates this line instead of duplicating it
                    odoo_extra_shipping_order_line = self.env['sale.order.line'].search(
                        [('order_id', '=', odoo_sale_order.id), ('woocommerce_id', '=', shipping_line['id']), ('woocommerce_site_url', '=', self.settings_woocommerce_connection_url)], limit=1
                    )
                    extra_shipping_line_values = {
                        'woocommerce_site_url': self.settings_woocommerce_connection_url,
                        'woocommerce_id': shipping_line['id'],
                        'order_id': odoo_sale_order.id,
                        'name': shipping_line['method_title'],
                        'product_id': extra_odoo_delivery_carrier.product_id.id,
                        'product_uom_qty': 1,
                        'price_unit': float(shipping_line['total']),
                    }
                    if odoo_extra_shipping_order_line:
                        odoo_extra_shipping_order_line.write(extra_shipping_line_values)
                    else:
                        self.env['sale.order.line'].with_context(tracking_disable=True, mail_create_nosubscribe=True).create(extra_shipping_line_values)

            # Refunds
            self.woocommerce_to_odoo_order_refunds_sync(odoo_sale_order, woocommerce_order)

        except Exception:
            # Roll back only this record's changes, keeping other records already written in this chunk job
            savepoint.rollback()
            _logger.exception(f'Error syncing WooCommerce order {woocommerce_order["id"]}')
            raise
        finally:
            # Release the savepoint (whether it was rolled back above or the record synced successfully) so it never lingers open for the rest of this chunk job's transaction
            savepoint.close(rollback=False)

        return sync_status

    def woocommerce_to_odoo_order_refunds_sync(self: models.Model, odoo_sale_order: models.Model, woocommerce_order: dict[str, Any]) -> None:
        """Converts WooCommerce order refunds into real Odoo credit notes (instead of only storing the raw refund JSON on 'woocommerce_refunds').

        Only full refunds of a single posted customer invoice are auto-converted, since WooCommerce's order-level 'refunds' summary (id/reason/total) does not include a reliable per-line breakdown to build an accurate partial credit note. Partial refunds, or orders with zero/multiple posted invoices, are left for manual handling in Odoo; they remain visible in the 'woocommerce_refunds' JSON field.
        """
        woocommerce_refunds = woocommerce_order.get('refunds') or []

        if not woocommerce_refunds:
            return

        account_move = self.env['account.move']

        # Refunds already converted into Odoo credit notes for this WooCommerce site
        woocommerce_refund_ids = [str(woocommerce_refund['id']) for woocommerce_refund in woocommerce_refunds]
        already_processed_refund_ids = set(
            account_move.with_context(lang=False)
            .search(
                [
                    ('woocommerce_site_url', '=', self.settings_woocommerce_connection_url),
                    ('woocommerce_refund_id', 'in', woocommerce_refund_ids),
                ],
            )
            .mapped('woocommerce_refund_id'),
        )

        # Posted customer invoices linked to this sale order
        odoo_invoices = odoo_sale_order.invoice_ids.filtered(lambda move: move.move_type == 'out_invoice' and move.state == 'posted')

        for woocommerce_refund in woocommerce_refunds:
            woocommerce_refund_id = str(woocommerce_refund['id'])

            if woocommerce_refund_id in already_processed_refund_ids:
                continue

            refund_total = abs(float(woocommerce_refund.get('total') or 0.0))

            if refund_total == 0.0:
                continue

            if len(odoo_invoices) != 1:
                _logger.info(
                    f'Skipped automatic credit note creation for WooCommerce refund {woocommerce_refund_id} on order {odoo_sale_order.name}: '
                    f'expected exactly 1 posted customer invoice, found {len(odoo_invoices)}. Please create the credit note manually.',
                )
                continue

            odoo_invoice = odoo_invoices

            # Only auto-convert full refunds of the invoice; partial refunds cannot be reliably reconstructed from the order-level refund summary
            if abs(refund_total - odoo_invoice.amount_total) > 0.01:
                _logger.info(
                    f'Skipped automatic credit note creation for WooCommerce refund {woocommerce_refund_id} on order {odoo_sale_order.name}: '
                    f'refund total ({refund_total}) does not match the invoice total ({odoo_invoice.amount_total}). Please create the credit note manually.',
                )
                continue

            reversal_wizard = (
                self.env['account.move.reversal']
                .with_context(active_model='account.move', active_ids=odoo_invoice.ids)
                .create(
                    {
                        'move_ids': [(6, 0, odoo_invoice.ids)],
                        'reason': woocommerce_refund.get('reason') or f'WooCommerce refund {woocommerce_refund_id}',
                        'journal_id': odoo_invoice.journal_id.id,
                    },
                )
            )
            reversal_result = reversal_wizard.reverse_moves()

            credit_note = (
                account_move.browse(reversal_result['res_id'])
                if isinstance(reversal_result, dict) and reversal_result.get('res_id')
                else account_move.search([('reversed_entry_id', '=', odoo_invoice.id)], order='id desc', limit=1)
            )

            if not credit_note:
                _logger.error(f'Failed to create Odoo credit note for WooCommerce refund {woocommerce_refund_id} on order {odoo_sale_order.name}')
                continue

            credit_note.write(
                {
                    'woocommerce_site_url': self.settings_woocommerce_connection_url,
                    'woocommerce_refund_id': woocommerce_refund_id,
                },
            )

            if credit_note.state != 'posted':
                credit_note.action_post()

            _logger.info(f'Created Odoo credit note {credit_note.name} for WooCommerce refund {woocommerce_refund_id} (Odoo sale order: {odoo_sale_order.name})')

    @api.model
    def woocommerce_to_odoo_orders_chunk_sync(
        self: models.Model,
        woocommerce_orders: list[dict[str, Any]],
        woocommerce_tax_rates: dict[str, float],
        woocommerce_weight_unit: str,
        woocommerce_shipping_methods: list[dict[str, Any]],
        odoo_sale_orders: dict[str, Any],
    ) -> None:
        """Processes a chunk of WooCommerce orders sequentially within a single queue job."""
        self.ensure_one()

        # Shared tax-rate/UoM/country caches for the orders in this chunk
        odoo_tax_rate_cache: dict[tuple[float, bool], int] = {}
        odoo_uom_cache: dict[str, int] = {}
        odoo_country_cache: dict[str, int] = {}
        errors: list[str] = []
        new_count = updated_count = 0

        for woocommerce_order in woocommerce_orders:
            try:
                sync_status = self.woocommerce_to_odoo_order_sync(
                    woocommerce_order, woocommerce_tax_rates, woocommerce_weight_unit, woocommerce_shipping_methods, odoo_sale_orders, odoo_tax_rate_cache, odoo_uom_cache, odoo_country_cache
                )
                if sync_status == 'created':
                    new_count += 1
                elif sync_status == 'updated':
                    updated_count += 1
            except Exception as error:
                if isinstance(error, (RetryableJobError, psycopg2.errors.SerializationFailure, psycopg2.errors.DeadlockDetected)):
                    raise
                _logger.exception(f'Error syncing WooCommerce order {woocommerce_order.get("id")} within chunk job')
                errors.append(f'Order {woocommerce_order.get("id")}: {error}')

        self.sync_summary_chunk_completed('orders', len(woocommerce_orders), new_count, updated_count, errors)

    def woocommerce_to_odoo_orders_sync_batch(self: models.Model, woocommerce_tax_rates: dict[str, float], woocommerce_weight_unit: str, woocommerce_shipping_methods: list[dict[str, Any]]) -> bool:
        # WooCommerce REST API
        woocommerce_api = self.woocommerce_api_get()

        # Check if WooCommerce REST API connection is successful
        if not woocommerce_api:
            error_message = 'WooCommerce REST API connection failed. WooCommerce to Odoo orders sync process halted; Please check your connection settings in the WooCommerce Configuration'
            _logger.error(error_message)
            return

        # WooCommerce REST API parameters
        params = {'status': ','.join(self.settings_woocommerce_order_status.mapped('status')) or 'any'}

        if self.settings_woocommerce_modified_records_import:
            odoo_woocommerce_last_sync = self.odoo_woocommerce_last_sync_retrieve()
            if odoo_woocommerce_last_sync:
                params['modified_after'] = odoo_woocommerce_last_sync.strftime('%Y-%m-%dT%H:%M:%S')  # ISO 8601 date format

        # Get all Odoo sale orders with WooCommerce order ID
        odoo_sale_orders = self.env['sale.order'].search_read(
            [('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('woocommerce_id', '!=', False)],
            fields=['id', 'name', 'write_date', 'woocommerce_id'],
        )
        odoo_sale_orders = {odoo_sale_order['woocommerce_id']: odoo_sale_order for odoo_sale_order in odoo_sale_orders}

        for woocommerce_orders_batch in self.woocommerce_api_get_items_in_batches(woocommerce_api, endpoint='orders', params=params):
            # Schedule a job per chunk of WooCommerce orders instead of one job per order, to reduce per-job overhead
            for orders_chunk in self.list_chunks(woocommerce_orders_batch, self.settings_job_chunk_size):
                chunk_identity_key = '-'.join(str(woocommerce_order['id']) for woocommerce_order in orders_chunk)
                self.with_delay(
                    identity_key=f'woocommerce_to_odoo_orders_chunk_sync-{self.id}-{chunk_identity_key}', description=self.job_description('woocommerce_to_odoo_orders_chunk_sync')
                ).woocommerce_to_odoo_orders_chunk_sync(orders_chunk, woocommerce_tax_rates, woocommerce_weight_unit, woocommerce_shipping_methods, odoo_sale_orders)
                self.sync_summary_chunk_dispatched()

    def woocommerce_attribute_create_or_retrieve(self: models.Model, woocommerce_api: WooCommerceClient, attribute_type: str, attribute_name: str, language_code: str | None = None) -> dict[str, Any] | None:
        """Create or retrieve a WooCommerce attribute, brand, category or tag."""
        if not attribute_type and not attribute_name:
            return None

        params = {'search': attribute_name}
        if language_code is not None:
            params['lang'] = language_code

        try:
            woocommerce_attribute_values = self.woocommerce_api_get_all_items(woocommerce_api, endpoint=f'products/{attribute_type}', params=params)

            if woocommerce_attribute_values and len(woocommerce_attribute_values) > 0:
                return woocommerce_attribute_values[0]

            else:
                data = {'name': attribute_name}
                if language_code is not None:
                    data['lang'] = language_code

                response = woocommerce_api.post(f'products/{attribute_type}', data=data)

                # Check if the response is a valid JSON object
                if response and response.status_code == 201:
                    return response.json()

                else:
                    _logger.error(f'Failed to create Odoo attribute in WooCommerce: {attribute_name}. WooCommerce REST API response: {response.text}')
                    return None

        except Exception as error:
            _logger.error(f'Failed to create or retrieve Odoo attribute in WooCommerce: {attribute_name}: {error}')
            return None

    def wordpress_upload_image(self: models.Model, image: str, image_name: str) -> int | None:
        """Uploads an image to WordPress."""

        self.ensure_one()

        if not image:
            return None

        try:
            image = b64decode(image)
            image_file_type = filetype.guess(image)
            if not image_file_type:
                _logger.error(f'Failed to determine image type for {image_name}')

                return None

            # Check if image already exists in WordPress using its unique slug
            wordpress_media_existing = requests.get(
                url=f'{self.settings_woocommerce_connection_url}/wp-json/wp/v2/media?slug={image_name}', auth=HTTPBasicAuth(self.settings_wordpress_username, self.settings_wordpress_user_application_password)
            ).json()

            if wordpress_media_existing and isinstance(wordpress_media_existing, list):
                media = wordpress_media_existing[0]

                _logger.info(f'Image with slug {image_name} already exists in WordPress')

                return media.get('id')

            # Upload new image
            response = requests.post(
                url=f'{self.settings_woocommerce_connection_url}/wp-json/wp/v2/media',
                headers={'Content-Disposition': f'attachment; filename="{f"{image_name}.{image_file_type.extension}"}"'},
                data=BytesIO(image),
                auth=HTTPBasicAuth(self.settings_wordpress_username, self.settings_wordpress_user_application_password),
            )

            if response.status_code in (200, 201):
                wordpress_media = response.json()
                wordpress_image_id = wordpress_media.get('id')

                _logger.info(f'Uploaded new image from Odoo to WordPress: {image_name}.{image_file_type.extension} (WordPress image ID: {wordpress_image_id}')

                return wordpress_image_id

            else:
                _logger.error(f'Upload image from Odoo to WordPress failed: [{response.status_code}]: {response.text}')

                return None

        except Exception as error:
            _logger.error(f'Error uploading image from Odoo to WordPress: {image_name}: {error}')

            return None

    def wordpress_upload_product_images(self: models.Model, odoo_product: models.Model) -> list[int]:
        """Upload main image ('image_1920') + gallery images ('get_gallery_images()') and return list of WordPress media IDs."""
        self.ensure_one()

        wordpress_uploaded_image_ids = []
        counter = 1

        # Collect images
        images = []

        if odoo_product.image_1920:
            images.append(odoo_product.image_1920)

        images.extend(odoo_product.get_gallery_images())

        image_file_name = secure_filename(odoo_product.name.strip().replace(' ', '-').lower())

        for image_base64 in images:
            # Increment counter for each image
            image_name = f'{image_file_name}-{counter}'

            wordpress_image_id = self.wordpress_upload_image(image_base64, image_name)

            if wordpress_image_id:
                wordpress_uploaded_image_ids.append(wordpress_image_id)

            counter += 1

        return wordpress_uploaded_image_ids

    def odoo_to_woocommerce_products_sync(
        self: models.Model, woocommerce_currency: str, woocommerce_tax_rates: dict[str, float], woocommerce_prices_include_tax: bool, woocommerce_weight_unit: str, woocommerce_dimension_unit: str
    ) -> None:
        # WooCommerce REST API
        woocommerce_api = self.woocommerce_api_get()

        # Check if WooCommerce REST API connection is successful
        if not woocommerce_api:
            error_message = 'WooCommerce REST API connection failed. Odoo to WooCommerce products sync process halted; Please check your connection settings in the WooCommerce Configuration'
            _logger.error(error_message)
            return

        # Odoo search conditions
        search_conditions = [('sync_to_woocommerce', '=', True), ('active', '=', True), ('default_code', '!=', False)]

        if self.settings_woocommerce_odoo_to_woocommerce_products_language_code:
            search_conditions.append(('language_code', '=', self.settings_woocommerce_odoo_to_woocommerce_products_language_code))

        # Odoo products
        odoo_products = self.env['product.template'].with_context(lang=False).search(search_conditions) | self.env['product.product'].with_context(lang=False).search(
            search_conditions + [('product_tmpl_id.default_code', '!=', False)]
        ).mapped('product_tmpl_id')

        # Schedule a job per chunk of Odoo products instead of syncing everything in a single synchronous loop, mirroring the WooCommerce to Odoo direction
        odoo_product_ids = odoo_products.ids
        for products_chunk in self.list_chunks(odoo_product_ids, self.settings_job_chunk_size):
            chunk_identity_key = '-'.join(str(product_id) for product_id in products_chunk)
            self.with_delay(
                identity_key=f'odoo_to_woocommerce_products_chunk_sync-{self.id}-{chunk_identity_key}', description=self.job_description('odoo_to_woocommerce_products_chunk_sync')
            ).odoo_to_woocommerce_products_chunk_sync(products_chunk, woocommerce_currency, woocommerce_tax_rates, woocommerce_prices_include_tax, woocommerce_weight_unit, woocommerce_dimension_unit)

    def odoo_to_woocommerce_products_chunk_sync(
        self: models.Model,
        odoo_product_ids: list[int],
        woocommerce_currency: str,
        woocommerce_tax_rates: dict[str, float],
        woocommerce_prices_include_tax: bool,
        woocommerce_weight_unit: str,
        woocommerce_dimension_unit: str,
    ) -> None:
        """Processes a chunk of Odoo products, using the WooCommerce REST API 'products/batch' endpoint to create/update them in a single request instead of one request per product."""
        self.ensure_one()

        # WooCommerce REST API
        woocommerce_api = self.woocommerce_api_get(validate=False)

        if not woocommerce_api:
            _logger.error('WooCommerce REST API connection failed. Odoo to WooCommerce products chunk sync halted')
            return

        odoo_products = self.env['product.template'].with_context(lang=False).browse(odoo_product_ids).exists()

        # Get the matching WooCommerce products by SKU, in bounded-size sub-requests to avoid overly long query strings
        odoo_products_default_code = odoo_products.mapped('default_code')
        woocommerce_products = {}
        for sku_chunk in self.list_chunks(odoo_products_default_code, 100):
            params = {'status': 'publish', 'sku': ','.join(sku_chunk)}
            if self.settings_woocommerce_to_odoo_products_language_code:
                params['lang'] = self.settings_woocommerce_to_odoo_products_language_code

            for woocommerce_product in self.woocommerce_api_get_all_items(woocommerce_api, endpoint='products', params=params):
                woocommerce_products[woocommerce_product['sku']] = woocommerce_product

        # Build the create/update payloads for this chunk
        odoo_products_by_sku: dict[str, models.Model] = {}
        create_payloads = []
        update_payloads = []

        for odoo_product in odoo_products:
            try:
                # Try to find the corresponding product in WooCommerce by its Odoo default code
                woocommerce_product = woocommerce_products.get(odoo_product.default_code)

                if woocommerce_product and odoo_product['write_date'] <= self.datetime_convert(woocommerce_product['date_modified_gmt']):
                    _logger.info(f'Skipped import of Odoo product into WooCommerce: {odoo_product["name"]} (Odoo product ID: {odoo_product.id})')
                    continue

                product_values = self.odoo_to_woocommerce_product_values(odoo_product, woocommerce_api, woocommerce_tax_rates, woocommerce_product)
                odoo_products_by_sku[odoo_product.default_code] = odoo_product

                if woocommerce_product:
                    product_values['id'] = woocommerce_product['id']
                    update_payloads.append(product_values)
                else:
                    create_payloads.append(product_values)

            except Exception:
                _logger.exception(f'Error preparing Odoo product {odoo_product.id} for WooCommerce sync')

        # Send the create/update payloads to WooCommerce in batches of at most 100 items (the WooCommerce REST API batch endpoint limit)
        woocommerce_products_synced = []
        for create_chunk in self.list_chunks(create_payloads, 100):
            response = woocommerce_api.batch('products', create=create_chunk)
            woocommerce_products_synced.extend(response.get('create', []))

        for update_chunk in self.list_chunks(update_payloads, 100):
            response = woocommerce_api.batch('products', update=update_chunk)
            woocommerce_products_synced.extend(response.get('update', []))

        # Write WooCommerce-assigned fields back to Odoo and handle variations for variable products
        for woocommerce_product in woocommerce_products_synced:
            if woocommerce_product.get('error'):
                _logger.error(f'WooCommerce REST API batch error for product SKU {woocommerce_product.get("sku")}: {woocommerce_product["error"]}')
                continue

            odoo_product = odoo_products_by_sku.get(woocommerce_product.get('sku'))
            if not odoo_product:
                continue

            try:
                woocommerce_product_fields = self.woocommerce_product_fields(woocommerce_product, woocommerce_currency, woocommerce_weight_unit, woocommerce_dimension_unit, woocommerce_tax_rates)
                woocommerce_product_fields.update({'odoo_to_woocommerce_last_sync': fields.Datetime.now()})
                odoo_product.write(woocommerce_product_fields)

                _logger.info(f'Synced Odoo product into WooCommerce: {odoo_product.name} (Odoo product ID: {odoo_product.id}, WooCommerce product ID: {odoo_product["woocommerce_id"]})')

                if woocommerce_product.get('type') == 'variable':
                    self.odoo_to_woocommerce_product_variations_batch_sync(odoo_product, woocommerce_product['id'], woocommerce_currency, woocommerce_tax_rates, woocommerce_weight_unit, woocommerce_dimension_unit)

            except Exception:
                _logger.exception(f'Error syncing Odoo product {odoo_product.id} into WooCommerce')

    def odoo_to_woocommerce_product_values(
        self: models.Model, odoo_product: models.Model, woocommerce_api: WooCommerceClient, woocommerce_tax_rates: dict[str, float], woocommerce_product: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Builds the WooCommerce product payload for a single Odoo product, for use with the 'products' single-record or 'products/batch' endpoints."""
        product_values = {
            'name': odoo_product.name,
            'sku': odoo_product.default_code or '',
            'date_created_gmt': odoo_product.create_date.strftime('%Y-%m-%dT%H:%M:%S') if odoo_product.create_date else None,
            'description': odoo_product.description_sale if odoo_product.description_sale else None,
            'status': 'publish' if odoo_product.active else 'draft',
            'purchasable': odoo_product.sale_ok,
            'tax_class': next((tax_class for tax_class, tax_amount in woocommerce_tax_rates.items() if odoo_product.taxes_id and odoo_product.taxes_id[0].amount == tax_amount), 'standard'),
            'regular_price': f'{odoo_product.list_price:.2f}',
            'type': 'simple',
            'weight': odoo_product.weight if odoo_product.weight != 0.0 else '',
        }

        # Product meta data "odoo_id"
        woocommerce_product_meta_data = []

        if woocommerce_product and woocommerce_product.get('meta_data'):
            # If updating, merge with existing metadata
            woocommerce_product_meta_data = woocommerce_product['meta_data'][:]
            found = False
            for meta in woocommerce_product_meta_data:
                if meta.get('key') == 'odoo_id':
                    meta['value'] = odoo_product.id
                    found = True
                    break
            if not found:
                woocommerce_product_meta_data.append({'key': 'odoo_id', 'value': odoo_product.id})

        else:
            # If creating or no existing metadata, just add the new metadata
            woocommerce_product_meta_data = [{'key': 'odoo_id', 'value': odoo_product.id}]

        product_values['meta_data'] = woocommerce_product_meta_data

        # Manage stock
        if version_info[0] == 16:
            product_values['manage_stock'] = odoo_product.detailed_type == 'product'

        elif version_info[0] in [18, 19]:
            product_values['manage_stock'] = bool(odoo_product.is_storable)

        # Check if product has multiple variants
        if len(odoo_product.product_variant_ids) > 1:
            product_values['type'] = 'variable'

            woocommerce_attributes = []
            for line in odoo_product.attribute_line_ids:
                odoo_attributes = [value.name for value in line.value_ids]

                woocommerce_attribute = self.woocommerce_attribute_create_or_retrieve(
                    woocommerce_api,
                    'attributes',
                    line.attribute_id.name,
                    odoo_product.language_code if odoo_product.language_code else None,
                )
                if woocommerce_attribute:
                    woocommerce_attributes.append({'id': woocommerce_attribute['id'], 'name': line.attribute_id.name, 'variation': True, 'visible': True, 'options': odoo_attributes})

                if woocommerce_attributes:
                    product_values['attributes'] = woocommerce_attributes

        # Brand (requires 'product_brand' Odoo add-on)
        if 'product.brand' in self.env and len(odoo_product.product_brand_id) > 0:
            woocommerce_brands = []
            woocommerce_brand = self.woocommerce_attribute_create_or_retrieve(
                woocommerce_api,
                'brands',
                odoo_product.product_brand_id.name,
                odoo_product.language_code if odoo_product.language_code else None,
            )
            if woocommerce_brand:
                woocommerce_brands.append({'id': woocommerce_brand['id']})

            if len(woocommerce_brands) > 0:
                product_values.update({'brands': woocommerce_brands})

        # Categories
        woocommerce_categories = []

        ## 'categ_ids' (requires 'product_multi_category' Odoo add-on)
        if 'categ_ids' in self.env['product.template']._fields and len(odoo_product.categ_ids) > 0:
            for odoo_category in odoo_product.categ_ids:
                woocommerce_category = self.woocommerce_attribute_create_or_retrieve(
                    woocommerce_api,
                    'categories',
                    odoo_category.name,
                    odoo_product.language_code if odoo_product.language_code else None,
                )
                if woocommerce_category:
                    woocommerce_categories.append({'id': woocommerce_category['id']})

        ## 'categ_id'
        if odoo_product.categ_id:
            woocommerce_category = self.woocommerce_attribute_create_or_retrieve(
                woocommerce_api,
                'categories',
                odoo_product.categ_id.name,
                odoo_product.language_code if odoo_product.language_code else None,
            )
            if woocommerce_category:
                woocommerce_categories.append({'id': woocommerce_category['id']})

        woocommerce_categories = sorted({category['id'] for category in woocommerce_categories})

        if len(woocommerce_categories) > 0:
            product_values.update({'categories': [{'id': category_id} for category_id in woocommerce_categories]})

        # Dimensions (requires 'product_dimension' Odoo add-on)
        if 'product_length' in self.env['product.template']._fields:
            product_values.update(
                {
                    'dimensions': {
                        'length': odoo_product.product_length if odoo_product.product_length != 0.0 else '',
                        'width': odoo_product.product_width if odoo_product.product_width != 0.0 else '',
                        'height': odoo_product.product_height if odoo_product.product_height != 0.0 else '',
                    }
                }
            )

        # Images
        if self.settings_woocommerce_images_sync and self.settings_wordpress_username and self.settings_wordpress_user_application_password and (odoo_product.image_1920 or odoo_product.get_gallery_images()):
            wordpress_image_ids = self.wordpress_upload_product_images(odoo_product)
            if wordpress_image_ids:
                product_values['images'] = [{'id': image_id} for image_id in wordpress_image_ids]

        # Language
        if odoo_product.language_code:
            product_values.update({'lang': odoo_product.language_code})

        # Tags
        woocommerce_tags = []

        if len(odoo_product.product_tag_ids) > 0:
            for odoo_tag in odoo_product.product_tag_ids:
                woocommerce_tag = self.woocommerce_attribute_create_or_retrieve(woocommerce_api, 'tags', odoo_tag.name, odoo_product.language_code if odoo_product.language_code else None)
                if woocommerce_tag:
                    woocommerce_tags.append({'id': woocommerce_tag['id']})

            if len(woocommerce_tags) > 0:
                product_values.update({'tags': woocommerce_tags})

        return product_values

    def odoo_to_woocommerce_product_variations_batch_sync(
        self: models.Model,
        odoo_product: models.Model,
        woocommerce_product_id: int,
        woocommerce_currency: str,
        woocommerce_tax_rates: dict[str, float],
        woocommerce_weight_unit: str,
        woocommerce_dimension_unit: str,
    ) -> None:
        """Creates/updates all variations of a variable product in a single WooCommerce REST API 'products/{id}/variations/batch' request instead of one request per variation."""
        woocommerce_api = self.woocommerce_api_get(validate=False)

        # Retrieve existing variations from WooCommerce
        woocommerce_variations = self.woocommerce_api_get_all_items(woocommerce_api, endpoint=f'products/{woocommerce_product_id}/variations', params={'status': 'publish'})

        # Build a mapping by SKU for easier lookup
        variations_by_sku = {variation.get('sku'): variation for variation in woocommerce_variations if variation.get('sku')}

        odoo_variants_by_sku: dict[str, models.Model] = {}
        create_payloads = []
        update_payloads = []

        for odoo_product_variant in odoo_product.product_variant_ids:
            variation_attributes = []
            for variant_attribute_value in odoo_product_variant.product_template_attribute_value_ids:
                variation_attributes.append(
                    {
                        'name': variant_attribute_value.product_attribute_value_id.attribute_id.name,
                        'option': variant_attribute_value.product_attribute_value_id.name,
                    },
                )

            variation_data = {
                'sku': odoo_product_variant.default_code or '',
                'regular_price': str(odoo_product_variant.list_price or 0.0),
                'attributes': variation_attributes,
            }

            # Dimensions (requires 'product_dimension' Odoo add-on)
            if 'product_length' in self.env['product.template']._fields:
                variation_data.update(
                    {
                        'dimensions': {
                            'length': odoo_product_variant.product_length if odoo_product_variant.product_length != 0.0 else '',
                            'width': odoo_product_variant.product_width if odoo_product_variant.product_width != 0.0 else '',
                            'height': odoo_product_variant.product_height if odoo_product_variant.product_height != 0.0 else '',
                        }
                    }
                )

            variation_existing = variations_by_sku.get(odoo_product_variant.default_code)

            # Product variation meta data "odoo_id"
            if variation_existing and variation_existing.get('meta_data'):
                # If updating, merge with existing metadata
                woocommerce_product_variation_meta_data = variation_existing['meta_data'][:]
                found = False
                for meta in woocommerce_product_variation_meta_data:
                    if meta.get('key') == 'odoo_id':
                        meta['value'] = odoo_product_variant.id
                        found = True
                        break
                if not found:
                    woocommerce_product_variation_meta_data.append({'key': 'odoo_id', 'value': odoo_product_variant.id})
            else:
                # If creating or no existing metadata, just add the new metadata
                woocommerce_product_variation_meta_data = [{'key': 'odoo_id', 'value': odoo_product_variant.id}]

            variation_data['meta_data'] = woocommerce_product_variation_meta_data

            # Manage stock
            if version_info[0] == 16:
                variation_data['manage_stock'] = odoo_product.detailed_type == 'product'

            elif version_info[0] in [18, 19]:
                variation_data['manage_stock'] = bool(odoo_product.is_storable)

            odoo_variants_by_sku[odoo_product_variant.default_code] = odoo_product_variant

            if variation_existing:
                if odoo_product_variant['write_date'] > self.datetime_convert(variation_existing['date_modified_gmt']):
                    variation_data['id'] = variation_existing['id']
                    update_payloads.append(variation_data)
                else:
                    _logger.info(f'Odoo product variant in WooCommerce is up-to-date: {odoo_product_variant.display_name} (Odoo product variant ID: {odoo_product_variant.id})')
            else:
                create_payloads.append(variation_data)

        # Send the create/update payloads to WooCommerce in batches of at most 100 items (the WooCommerce REST API batch endpoint limit)
        woocommerce_variants_synced = []
        for create_chunk in self.list_chunks(create_payloads, 100):
            response = woocommerce_api.batch(f'products/{woocommerce_product_id}/variations', create=create_chunk)
            woocommerce_variants_synced.extend(response.get('create', []))

        for update_chunk in self.list_chunks(update_payloads, 100):
            response = woocommerce_api.batch(f'products/{woocommerce_product_id}/variations', update=update_chunk)
            woocommerce_variants_synced.extend(response.get('update', []))

        for woocommerce_variant in woocommerce_variants_synced:
            if woocommerce_variant.get('error'):
                _logger.error(f'WooCommerce REST API batch error for product variation SKU {woocommerce_variant.get("sku")}: {woocommerce_variant["error"]}')
                continue

            odoo_product_variant = odoo_variants_by_sku.get(woocommerce_variant.get('sku'))
            if not odoo_product_variant:
                continue

            try:
                woocommerce_product_variation_fields = self.woocommerce_product_variation_fields(woocommerce_variant, woocommerce_currency, woocommerce_weight_unit, woocommerce_dimension_unit, woocommerce_tax_rates)
                woocommerce_product_variation_fields.update({'odoo_to_woocommerce_last_sync': fields.Datetime.now()})
                odoo_product_variant.write(woocommerce_product_variation_fields)

                _logger.info(
                    f'Synced Odoo product variant into WooCommerce: {odoo_product_variant.name} (Odoo product variant ID: {odoo_product_variant.id}, WooCommerce product variation ID: {woocommerce_variant["id"]})'
                )
            except Exception:
                _logger.exception(f'Error syncing Odoo product variant {odoo_product_variant.id} into WooCommerce')

    # WooCommerce webhooks

    WEBHOOK_TOPICS: ClassVar[list[str]] = ['order.created', 'order.updated', 'product.created', 'product.updated', 'customer.created', 'customer.updated']

    def woocommerce_webhooks_register(self: models.Model) -> None:
        """Registers (or re-registers) the WooCommerce webhooks used for near-real-time sync, pointing them at this Odoo instance's webhook controller route."""
        self.ensure_one()

        woocommerce_api = self.woocommerce_api_get()

        if not woocommerce_api:
            raise UserError(_('WooCommerce REST API connection failed. Please check your connection settings in the WooCommerce Configuration'))

        if not self.settings_woocommerce_webhooks_secret:
            self.settings_woocommerce_webhooks_secret = secrets.token_hex(32)

        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        delivery_url = f'{base_url}/woocommerce_sync/webhook/{self.id}'

        existing_webhooks = self.woocommerce_api_get_all_items(woocommerce_api, endpoint='webhooks', params={'status': 'active'})
        existing_topics = {webhook['topic'] for webhook in existing_webhooks if webhook.get('delivery_url') == delivery_url}

        for topic in self.WEBHOOK_TOPICS:
            if topic in existing_topics:
                continue

            response = woocommerce_api.post(
                'webhooks',
                data={
                    'name': f'Odoo sync - {topic}',
                    'topic': topic,
                    'delivery_url': delivery_url,
                    'secret': self.settings_woocommerce_webhooks_secret,
                },
            )

            if response.status_code == 201:
                _logger.info(f'Registered WooCommerce webhook for topic {topic} -> {delivery_url}')
            else:
                _logger.error(f'Failed to register WooCommerce webhook for topic {topic}: {response.text}')

        self.settings_woocommerce_webhooks_enable = True

    def woocommerce_webhook_settings_retrieve(self: models.Model, woocommerce_api: WooCommerceClient) -> dict[str, Any]:
        """Retrieves the small set of WooCommerce store settings needed to process a single webhook-triggered record, mirroring 'woocommerce_sync()'."""
        return {
            'woocommerce_currency': woocommerce_api.get(endpoint='settings/general/woocommerce_currency').json()['value'],
            'woocommerce_weight_unit': woocommerce_api.get(endpoint='settings/products/woocommerce_weight_unit').json()['value'],
            'woocommerce_dimension_unit': woocommerce_api.get(endpoint='settings/products/woocommerce_dimension_unit').json()['value'],
            'woocommerce_prices_include_tax': woocommerce_api.get(endpoint='settings/tax/woocommerce_prices_include_tax').json()['value'].lower() == 'yes',
            'woocommerce_tax_rates': {tax_rate['class']: float(tax_rate['rate']) for tax_rate in woocommerce_api.get(endpoint='taxes').json()},
            'woocommerce_shipping_methods': woocommerce_api.get(endpoint='shipping_methods').json(),
        }

    def woocommerce_webhook_process(self: models.Model, topic: str, resource_id: int) -> None:
        """Handles an incoming WooCommerce webhook by delaying a single-record sync job, reusing the same per-record sync methods used by the polling-based sync."""
        self.ensure_one()

        if not resource_id:
            return

        if topic.startswith('order.'):
            self.with_delay(identity_key=f'woocommerce_webhook_order_sync-{self.id}-{resource_id}', description=self.job_description('woocommerce_webhook_order_sync')).woocommerce_webhook_order_sync(resource_id)
        elif topic.startswith('product.'):
            self.with_delay(identity_key=f'woocommerce_webhook_product_sync-{self.id}-{resource_id}', description=self.job_description('woocommerce_webhook_product_sync')).woocommerce_webhook_product_sync(resource_id)
        elif topic.startswith('customer.'):
            self.with_delay(identity_key=f'woocommerce_webhook_customer_sync-{self.id}-{resource_id}', description=self.job_description('woocommerce_webhook_customer_sync')).woocommerce_webhook_customer_sync(resource_id)
        else:
            _logger.info(f'Ignored WooCommerce webhook with unsupported topic: {topic}')

    def woocommerce_webhook_order_sync(self: models.Model, woocommerce_order_id: int) -> None:
        self.ensure_one()

        woocommerce_api = self.woocommerce_api_get(validate=False)
        if not woocommerce_api:
            _logger.error('WooCommerce REST API connection failed. Webhook order sync halted')
            return

        woocommerce_order = woocommerce_api.get(f'orders/{woocommerce_order_id}').json()
        if not isinstance(woocommerce_order, dict) or not woocommerce_order.get('id'):
            _logger.error(f'Failed to fetch WooCommerce order {woocommerce_order_id} for webhook sync: {woocommerce_order}')
            return

        settings = self.woocommerce_webhook_settings_retrieve(woocommerce_api)

        odoo_sale_order = self.env['sale.order'].search_read(
            [('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('woocommerce_id', '=', str(woocommerce_order_id))],
            fields=['id', 'name', 'write_date', 'woocommerce_id'],
            limit=1,
        )
        odoo_sale_orders = {odoo_sale_order[0]['woocommerce_id']: odoo_sale_order[0]} if odoo_sale_order else {}

        self.woocommerce_to_odoo_order_sync(woocommerce_order, settings['woocommerce_tax_rates'], settings['woocommerce_weight_unit'], settings['woocommerce_shipping_methods'], odoo_sale_orders)

    def woocommerce_webhook_product_sync(self: models.Model, woocommerce_product_id: int) -> None:
        self.ensure_one()

        woocommerce_api = self.woocommerce_api_get(validate=False)
        if not woocommerce_api:
            _logger.error('WooCommerce REST API connection failed. Webhook product sync halted')
            return

        woocommerce_product = woocommerce_api.get(f'products/{woocommerce_product_id}').json()
        if not isinstance(woocommerce_product, dict) or not woocommerce_product.get('id'):
            _logger.error(f'Failed to fetch WooCommerce product {woocommerce_product_id} for webhook sync: {woocommerce_product}')
            return

        settings = self.woocommerce_webhook_settings_retrieve(woocommerce_api)

        odoo_product = (
            self.env['product.template']
            .with_context(lang=False)
            .search_read(
                [('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('woocommerce_id', '=', str(woocommerce_product_id))],
                fields=['id', 'name', 'write_date', 'woocommerce_id'],
                limit=1,
            )
        )
        odoo_products = {odoo_product[0]['woocommerce_id']: odoo_product[0]} if odoo_product else {}

        self.woocommerce_to_odoo_product_sync(
            woocommerce_product,
            settings['woocommerce_currency'],
            settings['woocommerce_tax_rates'],
            settings['woocommerce_prices_include_tax'],
            settings['woocommerce_weight_unit'],
            settings['woocommerce_dimension_unit'],
            odoo_products,
        )

        if woocommerce_product.get('type') == 'variable' and self.settings_woocommerce_to_odoo_products_variations_sync:
            self.woocommerce_to_odoo_product_variations_sync(
                woocommerce_product,
                settings['woocommerce_currency'],
                settings['woocommerce_tax_rates'],
                settings['woocommerce_prices_include_tax'],
                settings['woocommerce_weight_unit'],
                settings['woocommerce_dimension_unit'],
            )

    def woocommerce_webhook_customer_sync(self: models.Model, woocommerce_customer_id: int) -> None:
        self.ensure_one()

        woocommerce_api = self.woocommerce_api_get(validate=False)
        if not woocommerce_api:
            _logger.error('WooCommerce REST API connection failed. Webhook customer sync halted')
            return

        woocommerce_customer = woocommerce_api.get(f'customers/{woocommerce_customer_id}').json()
        if not isinstance(woocommerce_customer, dict) or not woocommerce_customer.get('id'):
            _logger.error(f'Failed to fetch WooCommerce customer {woocommerce_customer_id} for webhook sync: {woocommerce_customer}')
            return

        odoo_customer = (
            self.env['res.partner']
            .with_context(lang=False)
            .search_read(
                [('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('woocommerce_id', '=', str(woocommerce_customer_id))],
                fields=['id', 'name', 'active', 'write_date', 'woocommerce_id'],
                limit=1,
            )
        )
        odoo_customers = {odoo_customer[0]['woocommerce_id']: odoo_customer[0]} if odoo_customer else {}

        self.woocommerce_to_odoo_customer_sync(woocommerce_customer, odoo_customers)
