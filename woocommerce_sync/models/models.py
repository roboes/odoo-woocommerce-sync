from base64 import b64decode, b64encode
from collections.abc import Generator
from datetime import datetime
from io import BytesIO
import logging
from PIL import Image
import pytz
import requests
from requests.auth import HTTPBasicAuth
from typing import Any
from werkzeug.utils import secure_filename

import filetype

from odoo import _, api, fields, models
from odoo.addons.queue_job.delay import chain
from odoo.exceptions import UserError, ValidationError
from odoo.release import version_info

from woocommerce import API

# Settings
_logger = logging.getLogger(__name__)


class WoocommerceConnector(models.Model):
    _name = 'woocommerce.configuration'
    _description = 'WooCommerce Configuration'

    # View settings
    woocommerce_connection_sequence = fields.Char(string='Connection ID', required=True, copy=False, readonly=True, index=True, default=lambda self: _('New'))

    # WooCommerce REST API settings
    settings_woocommerce_connection_name = fields.Char(string='Instance Name')
    settings_woocommerce_connection_url = fields.Char(string='Store URL', help='WordPress URL. Example: https://www.mystore.com')
    settings_woocommerce_consumer_key = fields.Char(string='Consumer Key')
    settings_woocommerce_consumer_secret = fields.Char(string='Consumer Secret')
    settings_woocommerce_timeout = fields.Integer(string='Timeout (in seconds)', default=30)

    # WordPress REST API settings
    settings_wordpress_username = fields.Char(string='WordPress Username')
    settings_wordpress_user_application_password = fields.Char(string='WordPress User App Password', help='Can be generated from WordPress Admin > Users > Profile > Application Passwords.')

    # Sync items settings
    settings_woocommerce_to_odoo_products_sync = fields.Boolean(default=True)
    settings_odoo_to_woocommerce_products_sync = fields.Boolean(default=False)
    settings_woocommerce_to_odoo_products_variations_sync = fields.Boolean(default=True)
    settings_odoo_to_woocommerce_variations_sync = fields.Boolean(default=True, readonly=True)
    settings_woocommerce_to_odoo_customers_sync = fields.Boolean(default=True)
    settings_woocommerce_to_odoo_orders_sync = fields.Boolean(default=True)

    # General settings
    settings_woocommerce_user_responsible = fields.Many2one(
        comodel_name='res.users',
        string='Responsible',
        help='Default responsible user for WooCommerce operations.',
        default=lambda self: self.env.user,
        ondelete='set null',
    )
    settings_woocommerce_modified_records_import = fields.Boolean(
        string='Import only modified records?',
        help="If enabled, only records modified since the last import will be retrieved from WooCommerce using the 'modified_after' WooCommerce REST API parameter. Only enable this option after the first import.",
        default=False,
    )
    settings_woocommerce_images_sync = fields.Boolean(string='Sync images?', default=True)

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
        comodel_name='woocommerce.order.status',
        string='Order statuses to import',
        help='Select which order statuses to import from WooCommerce.',
        default=lambda self: self.env['woocommerce.order.status'].search([('status', '=', 'any')]),
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
        default=False,
    )
    settings_woocommerce_line_items_product_map = fields.Boolean(
        string='Map products to existing Odoo products in line items?',
        help="If enabled, line items products will be mapped to existing Odoo products by 'woocommerce_id'. If no match is found, a product placeholder will be used. If disabled, all order line items will be assigned to a placeholder product, but the WooCommerce product name will still be displayed. Not recommended, given that products in WooCommerce may have changed since purchase, making mapping difficult.",
        default=False,
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
        self.ensure_one()
        sync_log = self.env['woocommerce.sync.log'].search([], limit=1)
        self.odoo_woocommerce_last_sync = sync_log.odoo_woocommerce_last_sync if sync_log else False

    @api.model_create_multi
    def create(self: models.Model, values_list: list[dict[str, Any]]) -> models.Model:
        for values in values_list:
            if values.get('woocommerce_connection_sequence', _('New')) == _('New'):
                values['woocommerce_connection_sequence'] = self.env['ir.sequence'].next_by_code('woocommerce.configuration.sequence') or _('New')

        records = super().create(values_list)

        # Run post-creation logic per record
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
                    f'model.with_context(cron_running=True).browse({self.id}).with_delay().woocommerce_sync()'
                    if self.env['ir.module.module'].search([('name', '=', 'queue_job'), ('state', '=', 'installed')], limit=1)
                    else f'model.with_context(cron_running=True).browse({self.id}).woocommerce_sync()'
                ),
                'active': self.settings_woocommerce_sync_scheduled,
                'interval_number': self.settings_woocommerce_sync_scheduled_interval_minutes,
                'interval_type': 'minutes',
                'numbercall': -1,
                'doall': True,
            }

        elif version_info[0] == 18:
            cron_values = {
                'name': f'WooCommerce Auto-Sync - {self.settings_woocommerce_connection_url}',
                'model_id': self.env['ir.model']._get(self._name).id,
                'code': (
                    f'model.with_context(cron_running=True).browse({self.id}).with_delay().woocommerce_sync()'
                    if self.env['ir.module.module'].search([('name', '=', 'queue_job'), ('state', '=', 'installed')], limit=1)
                    else f'model.with_context(cron_running=True).browse({self.id}).woocommerce_sync()'
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
        if self.env['ir.module.module'].search([('name', '=', 'queue_job'), ('state', '=', 'installed')], limit=1):
            self.with_delay().woocommerce_sync()

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Sync Started (Queue Job)'),
                    'message': _('WooCommerce sync process has been started in the background. %s'),
                    'links': [{'label': _('Open Job Queue'), 'url': '/web#action=%d&model=queue.job&view_type=list' % self.env['ir.actions.act_window'].search([('res_model', '=', 'queue.job')], limit=1).id}],
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
        woocommerce_prices_include_tax = True if woocommerce_api.get(endpoint='settings/tax/woocommerce_prices_include_tax').json()['value'].lower() == 'yes' else False
        woocommerce_tax_rates = woocommerce_api.get(endpoint='taxes').json()
        woocommerce_tax_rates = {woocommerce_tax_rate['class']: float(woocommerce_tax_rate['rate']) for woocommerce_tax_rate in woocommerce_tax_rates}

        ## WooCommerce shipping methods
        woocommerce_shipping_methods = woocommerce_api.get(endpoint='shipping_methods').json()

        queue_jobs_run_in_sequence = []

        # WooCommerce to Odoo

        ## Products
        if self.settings_woocommerce_to_odoo_products_sync:
            ### Products delete
            if self.settings_woocommerce_to_odoo_products_delete:
                queue_jobs_run_in_sequence.append(self.delayable(priority=None, description=None).woocommerce_to_odoo_products_delete())

            ### Products
            queue_jobs_run_in_sequence.append(
                self.delayable(priority=None, description=None).woocommerce_to_odoo_products_sync_batch(
                    woocommerce_currency, woocommerce_tax_rates, woocommerce_prices_include_tax, woocommerce_weight_unit, woocommerce_dimension_unit
                )
            )

            ## Product variations
            if self.settings_woocommerce_to_odoo_products_variations_sync:
                queue_jobs_run_in_sequence.append(
                    self.delayable(priority=None, description=None).woocommerce_to_odoo_products_variations_sync_batch(
                        woocommerce_currency, woocommerce_tax_rates, woocommerce_prices_include_tax, woocommerce_weight_unit, woocommerce_dimension_unit
                    )
                )

        ## Products related ids map
        if self.settings_woocommerce_products_related_ids_map:
            queue_jobs_run_in_sequence.append(self.delayable(priority=None, description=None).woocommerce_to_odoo_products_related_ids())

        ## Customers
        if self.settings_woocommerce_to_odoo_customers_sync:
            queue_jobs_run_in_sequence.append(self.delayable(priority=None, description=None).woocommerce_to_odoo_customers_sync_batch())

        ## Orders
        if self.settings_woocommerce_to_odoo_orders_sync:
            queue_jobs_run_in_sequence.append(self.delayable(priority=None, description=None).woocommerce_to_odoo_orders_sync_batch(woocommerce_tax_rates, woocommerce_weight_unit, woocommerce_shipping_methods))

        # Odoo to WooCommerce

        ## Products
        if self.settings_odoo_to_woocommerce_products_sync:
            queue_jobs_run_in_sequence.append(
                self.delayable(priority=None, description=None).odoo_to_woocommerce_products_sync(
                    woocommerce_currency, woocommerce_tax_rates, woocommerce_prices_include_tax, woocommerce_weight_unit, woocommerce_dimension_unit
                )
            )

        # Stock quantity
        if self.settings_woocommerce_products_stock_management:
            queue_jobs_run_in_sequence.append(self.delayable(priority=None, description=None).odoo_woocommerce_products_stock_quantity_sync_batch())
            queue_jobs_run_in_sequence.append(self.delayable(priority=None, description=None).update_sync_last_log(model_name='woocommerce.stock.sync.log', field_name='odoo_woocommerce_last_sync'))

        # Store 'odoo_woocommerce_last_sync'
        queue_jobs_run_in_sequence.append(self.delayable(priority=None, description=None).update_sync_last_log(model_name='woocommerce.sync.log', field_name='odoo_woocommerce_last_sync'))

        # Create chain and delay the jobs
        if queue_jobs_run_in_sequence:
            chain(*queue_jobs_run_in_sequence).delay()

    @api.model
    def update_sync_last_log(self: models.Model, model_name: str, field_name: str) -> None:
        sync_log = self.env[model_name].search([], limit=1)

        if sync_log:
            sync_log.write({field_name: fields.Datetime.now()})

        else:
            self.env[model_name].create({field_name: fields.Datetime.now()})

    def woocommerce_api_get(self: models.Model) -> API | None:
        """Retrieves WooCommerce REST API instance."""

        self.ensure_one()

        if not self.settings_woocommerce_connection_url or not self.settings_woocommerce_consumer_key or not self.settings_woocommerce_consumer_secret or not self.settings_woocommerce_timeout:
            _logger.error('Missing WooCommerce REST API configuration details (url, consumer key, consumer secret or timeout). Cannot retrieve API instance')
            return False

        try:
            woocommerce_api = API(
                url=self.settings_woocommerce_connection_url,
                consumer_key=self.settings_woocommerce_consumer_key,
                consumer_secret=self.settings_woocommerce_consumer_secret,
                version='wc/v3',
                timeout=self.settings_woocommerce_timeout,
                # query_string_auth=False,
                user_agent='Odoo-Woocommerce Sync',
            )

            response = woocommerce_api.get(endpoint='system_status')
            response.raise_for_status()

        except requests.RequestException as error:
            _logger.error(f'WooCommerce REST API connection failed: {error}')
            return False

        _logger.info('WooCommerce REST API connection successful')

        return woocommerce_api

    @api.model
    def woocommerce_api_get_all_items(self: models.Model, woocommerce_api: API, endpoint: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.ensure_one()

        # Set default records per page if not already provided
        if params is None:
            params = {}
        params.setdefault('per_page', 100)

        # If "settings_woocommerce_test_mode" is enabled, limit to 10 items
        if self.settings_woocommerce_test_mode:
            params['per_page'] = 10

        records_all = []
        page = 1
        while True:
            # Update the page number for each request
            params['page'] = page
            records = woocommerce_api.get(endpoint=endpoint, params=params).json()

            records_all.extend(records)

            # If no records are returned, or "settings_woocommerce_test_mode" is enabled (fetch only first page), break the loop
            if not records or self.settings_woocommerce_test_mode:
                break

            page += 1

        return records_all

    @api.model
    def woocommerce_api_get_items_in_batches(self: models.Model, woocommerce_api: API, endpoint: str, params: dict[str, Any] | None = None, batch_size: int = 10) -> Generator[list[dict[str, Any]], Any, None]:
        self.ensure_one()

        if params is None:
            params = {}

        # Use the provided batch_size
        params['per_page'] = batch_size

        # If "settings_woocommerce_test_mode" is enabled, limit to 10 items
        if self.settings_woocommerce_test_mode:
            params['per_page'] = 10

        page = 1
        while True:
            params['page'] = page
            records = woocommerce_api.get(endpoint=endpoint, params=params).json()

            if records:
                yield records
            else:
                break

            if self.settings_woocommerce_test_mode:
                break

            page += 1

    @api.model
    def odoo_shorepos_last_sync_retrieve(self: models.Model) -> datetime | bool:
        woocommerce_sync_log = self.env['woocommerce.sync.log'].search([], limit=1)

        if woocommerce_sync_log.odoo_woocommerce_last_sync:
            return woocommerce_sync_log.odoo_woocommerce_last_sync.astimezone(pytz.timezone(self.env.user.tz or 'UTC')).replace(tzinfo=None)

        else:
            False

    @staticmethod
    def datetime_convert(date_string: str) -> datetime | bool:
        """Convert ISO 8601 date format string to Odoo datetime format."""
        if date_string:
            try:
                # Replace 'T' with space and then convert to datetime
                date_string = date_string.replace('T', ' ')
                return datetime.strptime(date_string, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                raise ValidationError(f'Invalid WooCommerce date format: {date_string}')
        return False

    @api.model
    def image_download_file_to_base64(self: models.Model, woocommerce_images: dict[str, Any]) -> str | None:
        """Downloads the featured image file from WooCommerce and returns it as a base64-encoded string."""
        if not woocommerce_images:
            return None

        # Get the image URL
        image_url = woocommerce_images.get('src')

        # Download and process the image
        try:
            response = requests.get(image_url, timeout=10)

            # Ensure the request was successful
            response.raise_for_status()

            # Open image with PIL
            img = Image.open(BytesIO(response.content))

            # Convert to base64 encoding
            buffered = BytesIO()
            img.save(buffered, format='PNG')
            img_base64 = b64encode(buffered.getvalue()).decode('utf-8')

            return img_base64

        except requests.exceptions.RequestException as error:
            _logger.error(f'Failed to download image from {image_url}: {error}')
        except Exception as error:
            _logger.error(f'Error processing the image from {image_url}: {error}')
        return None

    @api.model
    def image_process_attachments(self: models.Model, woocommerce_images: list[dict[str, Any]], product: models.Model, create_attachments: bool = False) -> list[int | dict[str, Any]] | None:
        """Downloads images and either creates ir.attachment records or prepares data for product.image records."""
        if not woocommerce_images:
            return None

        images = []

        for index, image_data in enumerate(woocommerce_images):
            if not image_data['src'] or not image_data['name']:
                continue

            try:
                response = requests.get(image_data['src'], timeout=10)
                response.raise_for_status()

                img_base64 = b64encode(response.content)

                if create_attachments:
                    # Odoo < 17: Create attachments and append the ID
                    attachment = self.env['ir.attachment'].create(
                        {
                            'name': image_data['name'],
                            'type': 'binary',
                            'datas': img_base64,
                            'mimetype': response.headers.get('Content-Type', 'image/jpeg'),
                            'res_model': 'product.template',
                            'res_id': product.id,
                        },
                    )
                    images.append(attachment.id)

                else:
                    images.append(
                        {
                            'name': image_data['name'],
                            'sequence': index,
                            'image_1920': img_base64,
                        },
                    )

            except requests.exceptions.RequestException as error:
                _logger.error(f'Failed to download image from {image_data["src"]}: {error}')
            except Exception as error:
                _logger.error(f'Error processing the image from {image_data["src"]}: {error}')

        return images if images else None

    @api.model
    def odoo_brand_create_or_retrieve(self: models.Model, brand_name: str) -> models.Model | bool:
        """Create or retrieve an Odoo brand."""
        if not brand_name:
            return False

        try:
            odoo_brand = self.env['product.brand'].search([('name', '=', brand_name)], limit=1)

            if not odoo_brand:
                odoo_brand = self.env['product.brand'].create({'name': brand_name})
                _logger.info(f'Created new WooCommerce brand in Odoo: {odoo_brand.name}')

            return odoo_brand

        except Exception as error:
            _logger.error(f'Failed to create or retrieve WooCommerce brand in Odoo: {brand_name}: {error}')
            return False

    @api.model
    def odoo_category_create_or_retrieve(self: models.Model, category_name: str) -> models.Model | bool:
        """Create or retrieve an Odoo category."""
        if not category_name:
            return False

        try:
            odoo_category = self.env['product.category'].search([('name', '=', category_name)], limit=1)

            if not odoo_category:
                odoo_category = self.env['product.category'].create({'name': category_name})
                _logger.info(f'Created new WooCommerce category in Odoo: {odoo_category.name}')

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
    def odoo_tag_create_or_retrieve(self: models.Model, tag_name: str) -> models.Model | bool:
        """Create or retrieve an Odoo tag."""
        if not tag_name:
            return False

        try:
            odoo_tag = self.env['product.tag'].search([('name', '=', tag_name)], limit=1)

            if not odoo_tag:
                odoo_tag = self.env['product.tag'].create({'name': tag_name})
                _logger.info(f'Created new WooCommerce tag in Odoo: {odoo_tag.name}')

            return odoo_tag

        except Exception as error:
            _logger.error(f'Failed to create or retrieve WooCommerce tag in Odoo: {tag_name}: {error}')
            return False

    @api.model
    def odoo_tax_rate_create_or_retrieve(self: models.Model, tax_rate: float | None, price_include_tax: bool = False) -> models.Model | bool:
        """Create or retrieve an Odoo tax rate."""
        if tax_rate is None:
            return False

        odoo_price_include_tax = self.env.company.account_sale_tax_id.price_include if self.env.company.account_sale_tax_id else False
        if odoo_price_include_tax != price_include_tax:
            _logger.info(f'Mismatch between Odoo and WooCommerce tax rate settings for inclusion of tax in price: {odoo_tax_rate.name}')
            return False

        try:
            odoo_tax_rate = self.env['account.tax'].search(
                [('active', '=', True), ('name', '=', f'{tax_rate}%'), ('amount', '=', tax_rate), ('type_tax_use', '=', 'sale'), ('price_include', '=', price_include_tax)], limit=1
            )

            if not odoo_tax_rate:
                odoo_tax_rate = self.env['account.tax'].create({'name': f'{tax_rate}%', 'amount': tax_rate, 'type_tax_use': 'sale', 'price_include': price_include_tax})
                _logger.info(f'Created new WooCommerce tax rate in Odoo: {odoo_tax_rate.name}')

            return odoo_tax_rate

        except Exception as error:
            _logger.error(f'Failed to create or retrieve WooCommerce tax rate in Odoo: {odoo_tax_rate}%: {error}')
            return False

    @api.model
    def odoo_unit_of_measure_create_or_retrieve(self: models.Model, unit_of_measure_name: str) -> models.Model | bool:
        """Create or retrieve an Odoo unit of measure."""
        if not unit_of_measure_name:
            return False

        try:
            odoo_unit_of_measure = self.env['uom.uom'].search([('active', '=', True), ('name', '=', unit_of_measure_name)], limit=1)

            if not odoo_unit_of_measure:
                odoo_unit_of_measure = self.env['uom.uom'].create({'name': unit_of_measure_name, 'category_id': self.env.ref('uom.uom_categ_unit').id, 'factor': 1, 'uom_type': 'reference'})
                _logger.info(f'Created new WooCommerce unit of measure in Odoo: {odoo_unit_of_measure.name}')

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
            odoo_dimensional_uom = self.env['uom.uom'].search([('active', '=', True), ('name', '=', dimensional_uom_name)], limit=1)

            if not odoo_dimensional_uom:
                _logger.warning(f'Not found WooCommerce dimensional UoM in Odoo: {dimensional_uom_name}')

            return odoo_dimensional_uom

        except Exception as error:
            _logger.error(f'Failed to retrieve WooCommerce dimensional UoM in Odoo: {dimensional_uom_name}: {error}')
            return False

    @api.model
    def odoo_customer_placeholder_create_or_retrieve(self: models.Model) -> models.Model:
        """Create or retrieve an Odoo placeholder customer for WooCommerce Order integration. The customer placeholder is archived (active=False) and can be used to satisfy the product requirement on sale orders."""

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
        """Create or retrieve an Odoo placeholder product for WooCommerce Order Line Item integration. The product placeholder is archived (active=False) and can be used to satisfy the product requirement on sale order lines."""

        odoo_product_placeholder = self.env['product.template'].with_context(active_test=False).search([('default_code', '=', 'WooCommerce_Product_Placeholder')], limit=1)

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

        return odoo_product_placeholder

    @api.model
    def odoo_delivery_carrier_create_or_retrieve(self: models.Model, woocommerce_shipping_methods: list[dict[str, Any]], shipping_line: dict[str, Any]) -> models.Model | bool:
        """Create or retrieve an Odoo delivery carrier."""

        self.ensure_one()

        if not shipping_line:
            return False

        odoo_delivery_carrier = self.env['delivery.carrier'].search([('name', '=', shipping_line['method_title'])], limit=1)

        if odoo_delivery_carrier:
            # If current view setting is "active" and delivery carrier setting is "archive", activate it
            if not self.settings_woocommerce_delivery_methods_archive and not odoo_delivery_carrier.active:
                odoo_delivery_carrier.active = True
            # If current view setting is "archive" and delivery carrier setting "active", archive it
            elif self.settings_woocommerce_delivery_methods_archive and odoo_delivery_carrier.active:
                odoo_delivery_carrier.active = False

        else:
            # woocommerce_shipping_method = next((shipping_method for shipping_method in woocommerce_shipping_methods if shipping_method.get('id') == shipping_line['method_id']), None)

            # Create a new delivery product (if it doesn't exist)
            delivery_product = self.env['product.product'].search(
                [('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('name', '=', 'Shipping Product for ' + shipping_line['method_title'])],
                limit=1,
            )

            if not delivery_product:
                # Create the product if it doesn't exist
                delivery_product = self.env['product.product'].create(
                    {
                        'woocommerce_site_url': self.settings_woocommerce_connection_url,
                        'name': 'Shipping Product for ' + shipping_line['method_title'],
                        'type': 'service',
                        'sale_ok': True,
                        'purchase_ok': False,
                        'list_price': 0.0,
                    },
                )

            # Create the delivery carrier with the associated product_id
            odoo_delivery_carrier = self.env['delivery.carrier'].create(
                {'name': shipping_line['method_title'], 'product_id': delivery_product.id, 'delivery_type': 'fixed', 'active': not (self.settings_woocommerce_delivery_methods_archive)},
            )

        return odoo_delivery_carrier

    def odoo_woocommerce_products_stock_quantity_sync(self: models.Model, odoo_product: models.Model, woocommerce_products_stock_map: dict[int, dict[str, Any]]) -> None:
        # WooCommerce REST API
        woocommerce_api = self.woocommerce_api_get()

        # Check if WooCommerce REST API connection is successful
        if not woocommerce_api:
            error_message = 'WooCommerce REST API connection failed. Sync between WooCommerce and Odoo product stock quantity levels process halted; Please check your connection settings in the WooCommerce Configuration'
            _logger.error(error_message)
            return

        product_woocommerce_id = odoo_product.woocommerce_id or odoo_product.product_tmpl_id.woocommerce_id

        woocommerce_stock_info = woocommerce_products_stock_map.get(product_woocommerce_id)

        if not woocommerce_stock_info:
            return

        # WooCommerce product stock quantity
        woocommerce_stock_quantity = woocommerce_stock_info.get('stock_quantity')
        if woocommerce_stock_quantity is None:
            _logger.warning(f'WooCommerce product {odoo_product.name} (WooCommerce product ID {product_woocommerce_id}) has a None "stock_quantity", defaulting to 0.0')
            woocommerce_stock_quantity = 0.0

        # Odoo product stock quant
        odoo_product_stock_quant = self.env['stock.quant'].search(
            [
                ('product_tmpl_id.woocommerce_site_url', '=', self.settings_woocommerce_connection_url),
                ('product_id', '=', odoo_product.id),
                ('location_id', '=', self.settings_woocommerce_products_warehouse_location.lot_stock_id.id),
            ],
            limit=1,
        )

        if odoo_product_stock_quant and woocommerce_stock_quantity == odoo_product.qty_available:
            return

        # Get last update dates
        odoo_stock_quantity_last_update = getattr(odoo_product_stock_quant, 'stock_quantity_last_update', None) if odoo_product_stock_quant else None
        odoo_stock_quantity_last_update = odoo_stock_quantity_last_update if isinstance(odoo_stock_quantity_last_update, datetime) else fields.datetime.min

        shorepos_stock_last_sync = getattr(odoo_product, 'shorepos_stock_last_sync', None)
        shorepos_stock_last_sync = shorepos_stock_last_sync if isinstance(shorepos_stock_last_sync, datetime) else fields.datetime.min

        woocommerce_date_modified_gmt = self.datetime_convert(woocommerce_stock_info['date_modified_gmt'])
        woocommerce_date_modified_gmt = woocommerce_date_modified_gmt if isinstance(woocommerce_date_modified_gmt, datetime) else fields.datetime.min

        # Determine the latest timestamp among all sources
        latest_timestamp = max(odoo_stock_quantity_last_update, shorepos_stock_last_sync, woocommerce_date_modified_gmt)

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

        # If Odoo is the most recent source, update WooCommerce
        else:
            woocommerce_product = None

            try:
                if odoo_product.woocommerce_parent_id and odoo_product.woocommerce_id:
                    woocommerce_product = woocommerce_api.put(f'products/{odoo_product.woocommerce_parent_id}/variations/{odoo_product.woocommerce_id}', data={'stock_quantity': odoo_product.qty_available}).json()

                elif product_woocommerce_id:
                    woocommerce_product = woocommerce_api.put(f'products/{product_woocommerce_id}', data={'stock_quantity': odoo_product.qty_available}).json()

                _logger.info(
                    f'Updated Odoo product stock quantity in WooCommerce: {odoo_product.name} (Odoo product ID: {odoo_product.id}, WooCommerce product ID: {product_woocommerce_id}) - Stock quantity: {odoo_product.qty_available}'
                )

            except Exception as error:
                _logger.error(f'Error updating Odoo product stock quantity in WooCommerce product {odoo_product.name} (Odoo product ID: {odoo_product.id}, WooCommerce product ID: {product_woocommerce_id}): {error}')
                return

            if woocommerce_product and 'code' in woocommerce_product and woocommerce_product['code'] == 'woocommerce_rest_authentication_error':
                _logger.warning(
                    f'Skipped updating Odoo product stock quantity in WooCommerce due to "woocommerce_rest_authentication_error": {odoo_product.name} (Odoo product ID: {odoo_product.id}, WooCommerce product ID: {product_woocommerce_id}): {woocommerce_product["message"]}'
                )
                return

            # Update the stock last sync
            if woocommerce_product and 'date_modified_gmt' in woocommerce_product:
                odoo_product.woocommerce_stock_last_sync_update(self.datetime_convert(woocommerce_product['date_modified_gmt']))

    def woocommerce_product_variations_stock_retrieve(self: models.Model, parent_id: int, temporary_sync_data_record_id: int) -> bool:
        """Retrieves all WooCommerce product variations for the given WooCommerce product ID and stores them in a temporary sync data record."""
        self.ensure_one()

        woocommerce_api = self.woocommerce_api_get()

        try:
            variations = self.woocommerce_api_get_all_items(woocommerce_api, endpoint=f'products/{parent_id}/variations', params={'_fields': 'id,date_modified_gmt,stock_quantity'})

            temporary_sync_data_record = self.env['woocommerce.sync.temp.data'].browse(temporary_sync_data_record_id)
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

        temporary_sync_data_record = self.env['woocommerce.sync.temp.data'].browse(temporary_sync_data_record_id)
        if not temporary_sync_data_record or not temporary_sync_data_record.woocommerce_products_variations_data:
            _logger.warning('Temporary sync data record not found or no data to process')
            return

        woocommerce_products_stock_map = temporary_sync_data_record.woocommerce_products_variations_data

        if version_info[0] == 16:
            odoo_products_batch = self.env['product.product'].search(
                [
                    ('product_tmpl_id.woocommerce_site_url', '=', self.settings_woocommerce_connection_url),
                    ('product_tmpl_id.sync_to_woocommerce', '=', True),
                    ('product_tmpl_id.active', '=', True),
                    ('product_tmpl_id.woocommerce_id', '!=', False),
                    ('detailed_type', '=', 'product'),
                ],
            )
        elif version_info[0] == 18:
            odoo_products_batch = self.env['product.product'].search(
                [
                    ('product_tmpl_id.woocommerce_site_url', '=', self.settings_woocommerce_connection_url),
                    ('product_tmpl_id.sync_to_woocommerce', '=', True),
                    ('product_tmpl_id.active', '=', True),
                    ('product_tmpl_id.woocommerce_id', '!=', False),
                    ('is_storable', '=', True),
                ],
            )

        for odoo_product in odoo_products_batch:
            self.with_delay().odoo_woocommerce_products_stock_quantity_sync(odoo_product, woocommerce_products_stock_map)

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
            woocommerce_stock_sync_log = self.env['woocommerce.stock.sync.log'].search([], limit=1)
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
        temporary_sync_data_record = self.env['woocommerce.sync.temp.data'].create({'woocommerce_products_variations_data': woocommerce_products_stock_map})

        woocommerce_variable_product_ids = [woocommerce_product['id'] for woocommerce_product in woocommerce_products if woocommerce_product['type'] == 'variable']

        # Chain the parallel jobs to the final processing job
        chain(
            *[self.delayable().woocommerce_product_variations_stock_retrieve(parent_id, temporary_sync_data_record.id) for parent_id in woocommerce_variable_product_ids],
            self.delayable().odoo_woocommerce_products_stock_quantity_process(temporary_sync_data_record.id),
        ).delay()

    @api.model
    def woocommerce_to_odoo_products_delete(self: models.Model) -> None:
        self.ensure_one()

        # WooCommerce REST API
        woocommerce_api = self.woocommerce_api_get()

        # Check if WooCommerce REST API connection is successful
        if not woocommerce_api:
            _logger.error('WooCommerce REST API connection failed. Cannot check for deleted products.')
            return

        # Get all Odoo products with WooCommerce product ID
        odoo_products = self.env['product.template'].search_read(
            [('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('active', '=', True), ('woocommerce_id', '!=', False)], fields=['woocommerce_id']
        )
        odoo_products = {odoo_product['woocommerce_id'] for odoo_product in odoo_products}

        # WooCommerce REST API parameters to fetch only IDs
        params = {'status': 'publish', 'fields': 'id'}

        # Get all product IDs from WooCommerce
        woocommerce_products = self.woocommerce_api_get_all_items(woocommerce_api, endpoint='products', params=params)
        woocommerce_products = {str(woocommerce_product['id']) for woocommerce_product in woocommerce_products}

        # Find IDs that exist in Odoo but not in WooCommerce
        odoo_products_to_delete_ids = odoo_products - woocommerce_products

        if odoo_products_to_delete_ids:
            odoo_products_to_delete = self.env['product.template'].search([('woocommerce_id', 'in', list(odoo_products_to_delete_ids))])
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
                'woocommerce_downloads': woocommerce_product['downloads'],
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
                'woocommerce_purchase_note': woocommerce_product['purchase_note'],
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
                'woocommerce_brands': woocommerce_product['brands'],
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
                'woocommerce_service': False,  # woocommerce_product.get('service', False), # Germanized field - https://vendidero.de/doc/woocommerce-germanized/products-rest-api
            },
        )

        # Loop through the explicitly defined columns
        for column in [
            'woocommerce_date_created',
            'woocommerce_date_created_gmt',
            'woocommerce_date_modified',
            'woocommerce_date_modified_gmt',
            'woocommerce_date_on_sale_from',
            'woocommerce_date_on_sale_from_gmt',
            'woocommerce_date_on_sale_to',
            'woocommerce_date_on_sale_to_gmt',
        ]:
            if column in product_values and product_values[column]:
                product_values[column] = self.datetime_convert(product_values[column])

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
    ) -> None:
        self.ensure_one()

        try:
            # Try to find the corresponding product in Odoo by its WooCommerce product ID
            odoo_product = odoo_products.get(str(woocommerce_product['id']))

            if odoo_product:
                odoo_product = self.env['product.template'].browse(odoo_product['id'])

                # Skip if not modified and stock setting unchanged
                if self.datetime_convert(woocommerce_product['date_modified_gmt']) <= odoo_product.write_date and odoo_product.woocommerce_manage_stock == woocommerce_product['manage_stock']:
                    _logger.info(f'Skipped import of WooCommerce product into Odoo: {odoo_product.name} (Odoo product ID: {odoo_product.id}, WooCommerce product ID: {odoo_product.woocommerce_id})')
                    return

                # Sync if modified or stock setting changed
                elif (self.datetime_convert(woocommerce_product['date_modified_gmt']) > odoo_product.write_date) or odoo_product.woocommerce_manage_stock != woocommerce_product['manage_stock']:
                    if odoo_product.woocommerce_manage_stock != woocommerce_product['manage_stock']:
                        # Remove the product from Odoo so it can be re-imported fresh
                        odoo_product.unlink()
                        odoo_product = None

            # Create new product in Odoo if it does not yet exist or update product in Odoo only if WooCommerce version is newer
            product_values = self.woocommerce_product_fields(woocommerce_product, woocommerce_currency, woocommerce_weight_unit, woocommerce_dimension_unit, woocommerce_tax_rates)

            # Brand (requires 'product_brand' Odoo add-on)
            if self.env['ir.module.module'].search([('name', '=', 'product_brand'), ('state', '=', 'installed')], limit=1):
                odoo_product_brands_ids = []
                for brand in woocommerce_product['brands']:
                    odoo_brand = self.odoo_brand_create_or_retrieve(brand['name'])
                    if odoo_brand:
                        odoo_product_brands_ids.append(odoo_brand.id)

                product_values.update({'product_brand_id': odoo_product_brands_ids[0] if odoo_product_brands_ids else False})

            # Category
            odoo_product_categories_ids = []
            for category in woocommerce_product['categories']:
                odoo_product_category = self.odoo_category_create_or_retrieve(category['name'])
                if odoo_product_category:
                    odoo_product_categories_ids.append(odoo_product_category.id)

            # Categories (requires 'product_multi_category' Odoo add-on)
            if self.env['ir.module.module'].search([('name', '=', 'product_multi_category'), ('state', '=', 'installed')], limit=1):
                product_values.update({'categ_ids': [(6, 0, odoo_product_categories_ids)]})

            # Currency
            if product_values['woocommerce_currency']:
                odoo_product_currency = self.odoo_currency_retrieve(product_values['woocommerce_currency'])

            # Dimensions (requires 'product_dimension' Odoo add-on)
            if self.env['ir.module.module'].search([('name', '=', 'product_dimension'), ('state', '=', 'installed')], limit=1):
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
                odoo_tag = self.odoo_tag_create_or_retrieve(tag['name'])
                if odoo_tag:
                    odoo_product_tags_ids.append(odoo_tag.id)

            # Tax
            odoo_product_tax_id = []
            if product_values['woocommerce_tax_rate']:
                odoo_product_tax = self.odoo_tax_rate_create_or_retrieve(product_values['woocommerce_tax_rate'], woocommerce_prices_include_tax)
                if odoo_product_tax:
                    odoo_product_tax_id = [(6, 0, [odoo_product_tax.id])]

            # Unit of measure
            if self.settings_woocommerce_products_package_size_unit_default:
                odoo_product_unit_of_measure = self.env.ref('uom.product_uom_unit')

            elif product_values['woocommerce_weight_unit']:
                odoo_product_unit_of_measure = self.odoo_unit_of_measure_create_or_retrieve(product_values['woocommerce_weight_unit'])

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
                    'active': True if product_values['woocommerce_status'] == 'publish' else False,
                    'sale_ok': product_values['woocommerce_purchasable'],
                    # Pricing
                    'currency_id': odoo_product_currency.id,
                    'taxes_id': odoo_product_tax_id,
                    'invoice_policy': 'order',
                    'list_price': product_values['woocommerce_price'],
                    # Category and tags
                    'categ_id': odoo_product_categories_ids[0] if odoo_product_categories_ids else False,
                    'product_tag_ids': [(6, 0, odoo_product_tags_ids)],
                    # Variations and attributes
                    'is_product_variant': False,
                    'has_configurable_attributes': True if product_values['woocommerce_type'] == 'variation' and len(attribute_value_ids or []) > 0 else False,
                    # Dimensions
                    'weight': product_values['woocommerce_weight'],
                    'uom_id': odoo_product_unit_of_measure.id if odoo_product_unit_of_measure else False,
                    'uom_po_id': odoo_product_unit_of_measure.id if odoo_product_unit_of_measure else False,
                    'volume': (
                        float(product_values['woocommerce_dimensions']['length']) * float(product_values['woocommerce_dimensions']['width']) * float(product_values['woocommerce_dimensions']['height'])
                        if (product_values['woocommerce_dimensions']['length'] and product_values['woocommerce_dimensions']['width'] and product_values['woocommerce_dimensions']['height'])
                        else False
                    ),
                },
            )

            # Product type
            if version_info[0] == 16:
                product_values['detailed_type'] = 'service' if product_values['woocommerce_service'] else 'product' if product_values['woocommerce_manage_stock'] else 'consu'

            elif version_info[0] == 18:
                product_values['type'] = 'service' if product_values['woocommerce_service'] else 'consu'
                product_values['is_storable'] = True if product_values['woocommerce_manage_stock'] else False

            if odoo_product:
                odoo_product.write(product_values)
                _logger.info(f'Updated WooCommerce product in Odoo: {odoo_product.name} (Odoo product ID: {odoo_product.id}, WooCommerce product ID: {odoo_product["woocommerce_id"]})')

            else:
                odoo_product = self.env['product.template'].create(product_values)
                _logger.info(f'Imported WooCommerce product into Odoo: {odoo_product.name} (Odoo product ID: {odoo_product.id}, WooCommerce product ID: {odoo_product["woocommerce_id"]})')

            # Product image gallery
            if odoo_product and self.settings_woocommerce_images_sync and len(woocommerce_product['images']) > 0:
                self.image_process_attachments(woocommerce_product['images'], odoo_product, create_attachments=True)

        except Exception as error:
            # Roll back changes
            self.env.cr.rollback()
            _logger.exception(f'Error syncing WooCommerce product {woocommerce_product["name"]} (WooCommerce product ID: {woocommerce_product["id"]}): {error}')

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
            odoo_woocommerce_last_sync = self.odoo_shorepos_last_sync_retrieve()
            if odoo_woocommerce_last_sync:
                params['modified_after'] = odoo_woocommerce_last_sync.strftime('%Y-%m-%dT%H:%M:%S')  # ISO 8601 date format

        if self.settings_woocommerce_to_odoo_products_language_code:
            params['lang'] = self.settings_woocommerce_to_odoo_products_language_code

        # Get all Odoo products with WooCommerce product ID
        odoo_products = self.env['product.template'].search_read(
            [('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('active', '=', True), ('woocommerce_id', '!=', False)],
            fields=['id', 'active', 'write_date', 'woocommerce_id', 'woocommerce_manage_stock'],
        )
        odoo_products = {odoo_product['woocommerce_id']: odoo_product for odoo_product in odoo_products}

        for woocommerce_products_batch in self.woocommerce_api_get_items_in_batches(woocommerce_api, endpoint='products', params=params):
            # Filter for WooCommerce products that have SKU
            woocommerce_products_batch = [woocommerce_product for woocommerce_product in woocommerce_products_batch if woocommerce_product['sku']]

            # Schedule a separate job for each WooCommerce product in the batch
            for woocommerce_product in woocommerce_products_batch:
                self.with_delay().woocommerce_to_odoo_product_sync(
                    woocommerce_product, woocommerce_currency, woocommerce_tax_rates, woocommerce_prices_include_tax, woocommerce_weight_unit, woocommerce_dimension_unit, odoo_products
                )

    @api.model
    def woocommerce_to_odoo_products_related_ids(self: models.Model) -> None:
        self.ensure_one()

        # Retrieve all Odoo products
        odoo_products = self.env['product.template'].search([('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('active', '=', True)])

        for odoo_product in odoo_products:
            if len(odoo_product['woocommerce_related_ids'] or []) > 0:
                odoo_products_related_ids = []
                for product_related_id in odoo_product['woocommerce_related_ids']:
                    odoo_product_related = self.env['product.template'].search(
                        [('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('active', '=', True), ('woocommerce_id', '=', product_related_id)],
                        limit=1,
                    )

                    if odoo_product_related:
                        odoo_products_related_ids.append(odoo_product_related.id)

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
                'woocommerce_service': False,  # product.get('service', False), # Germanized field - https://vendidero.de/doc/woocommerce-germanized/products-rest-api
            },
        )

        # Loop through the explicitly defined columns
        for column in [
            'woocommerce_date_created',
            'woocommerce_date_created_gmt',
            'woocommerce_date_modified',
            'woocommerce_date_modified_gmt',
            'woocommerce_date_on_sale_from',
            'woocommerce_date_on_sale_from_gmt',
            'woocommerce_date_on_sale_to',
            'woocommerce_date_on_sale_to_gmt',
        ]:
            if column in product_variation_values and product_variation_values[column]:
                product_variation_values[column] = self.datetime_convert(product_variation_values[column])

        return product_variation_values

    def woocommerce_to_odoo_product_variations_sync(
        self: models.Model,
        woocommerce_product: dict[str, Any],
        woocommerce_currency: str,
        woocommerce_tax_rates: dict[str, float],
        woocommerce_prices_include_tax: bool,
        woocommerce_weight_unit: str,
        woocommerce_dimension_unit: str,
    ) -> None:
        self.ensure_one()

        try:
            # WooCommerce REST API
            woocommerce_api = self.woocommerce_api_get()

            # Check if WooCommerce REST API connection is successful
            if not woocommerce_api:
                error_message = 'WooCommerce REST API connection failed. WooCommerce to Odoo products variations sync process halted; Please check your connection settings in the WooCommerce Configuration'
                _logger.error(error_message)
                return

            # Search for existing product in Odoo
            odoo_product = self.env['product.template'].search(
                [('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('active', '=', True), ('woocommerce_id', '=', woocommerce_product['id'])],
                limit=1,
            )
            if not odoo_product:
                _logger.warning(f'Not found Odoo product for WooCommerce product: {woocommerce_product["name"]} (WooCommerce product ID: {woocommerce_product["id"]})')
                return

            if odoo_product:
                # Store 'product.template' SKU
                odoo_product_sku = odoo_product.default_code

                # WooCommerce REST API parameters
                params = {'status': 'publish'}

                if self.settings_woocommerce_modified_records_import:
                    odoo_woocommerce_last_sync = self.odoo_shorepos_last_sync_retrieve()
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
                    # Search for or create the product attribute
                    odoo_product_attribute = self.env['product.attribute'].search([('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('name', '=', name)], limit=1)

                    if odoo_product_attribute and odoo_product_attribute.create_variant != 'dynamic':
                        _logger.warning(
                            f"The 'create_variant' mode for attribute '{odoo_product_attribute.name}' is not 'dynamic'. Odoo prevents changing this setting because the attribute is in use on other products. This may result in unintended product variants being generated"
                        )

                    # Create the attribute if it doesn't exist, with 'dynamic' setting
                    if not odoo_product_attribute:
                        odoo_product_attribute = self.env['product.attribute'].create({'woocommerce_site_url': self.settings_woocommerce_connection_url, 'name': name, 'create_variant': 'dynamic'})
                        _logger.info(f'Created WooCommerce product attribute in Odoo: {odoo_product_attribute.name}')

                    # Create or retrieve the attribute value for this attribute
                    odoo_product_attribute_value = self.env['product.attribute.value'].search(
                        [('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('attribute_id', '=', odoo_product_attribute.id), ('name', '=', option)], limit=1
                    )

                    if not odoo_product_attribute_value:
                        odoo_product_attribute_value = self.env['product.attribute.value'].create(
                            {'woocommerce_site_url': self.settings_woocommerce_connection_url, 'attribute_id': odoo_product_attribute.id, 'name': option}
                        )
                        _logger.info(f'Created WooCommerce product attribute value in Odoo: {odoo_product_attribute_value.name}')

                    # Ensure the product template has a matching attribute line and add the value
                    attribute_line = odoo_product.attribute_line_ids.filtered(lambda line: line.attribute_id.id == odoo_product_attribute.id)
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

                # Iterate through each WooCommerce variation to process it
                for woocommerce_variation in woocommerce_variations:
                    product_variation_values = self.woocommerce_product_variation_fields(
                        woocommerce_variation,
                        woocommerce_currency,
                        woocommerce_weight_unit,
                        woocommerce_dimension_unit,
                        woocommerce_tax_rates,
                    )

                    # Currency
                    if product_variation_values['woocommerce_currency']:
                        odoo_product_variant_currency = self.odoo_currency_retrieve(product_variation_values['woocommerce_currency'])

                    # Image featured
                    odoo_product_variant_image_featured = None
                    if self.settings_woocommerce_images_sync and woocommerce_variation['image'] is not None:
                        odoo_product_variant_image_featured = self.image_download_file_to_base64(woocommerce_variation['image'])

                    # Tax
                    odoo_product_variant_tax_id = []
                    if product_variation_values['woocommerce_tax_rate']:
                        odoo_product_variant_tax = self.odoo_tax_rate_create_or_retrieve(product_variation_values['woocommerce_tax_rate'], woocommerce_prices_include_tax)
                        if odoo_product_variant_tax:
                            odoo_product_variant_tax_id = [(6, 0, [odoo_product_variant_tax.id])]

                    # Unit of measure
                    if self.settings_woocommerce_products_package_size_unit_default:
                        odoo_product_variant_unit_of_measure = self.env.ref('uom.product_uom_unit')

                    elif product_variation_values['woocommerce_weight_unit']:
                        odoo_product_variant_unit_of_measure = self.odoo_unit_of_measure_create_or_retrieve(product_variation_values['woocommerce_weight_unit'])

                    # Build a list of Odoo attribute value IDs for the specific combination
                    odoo_product_template_attribute_value_ids = []
                    for woocommerce_attribute in woocommerce_variation['attributes']:
                        if not woocommerce_attribute.get('name') or not woocommerce_attribute.get('option'):
                            continue

                        odoo_product_attribute = self.env['product.attribute'].search([('name', '=', woocommerce_attribute.get('name'))], limit=1)
                        odoo_product_attribute_value = self.env['product.attribute.value'].search([('attribute_id', '=', odoo_product_attribute.id), ('name', '=', woocommerce_attribute.get('option'))], limit=1)

                        product_template_attribute_value = self.env['product.template.attribute.value'].search(
                            [
                                ('product_tmpl_id', '=', odoo_product.id),
                                ('attribute_id', '=', odoo_product_attribute.id),
                                ('product_attribute_value_id', '=', odoo_product_attribute_value.id),
                            ],
                            limit=1,
                        )
                        if product_template_attribute_value:
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
                            'active': True if product_variation_values['woocommerce_status'] == 'publish' else False,
                            'sale_ok': product_variation_values['woocommerce_purchasable'],
                            # Pricing
                            'currency_id': odoo_product_variant_currency.id,
                            'taxes_id': odoo_product_variant_tax_id,
                            'invoice_policy': 'order',
                            'list_price': product_variation_values['woocommerce_price'],
                            # Variations and attributes
                            'is_product_variant': True,
                            'has_configurable_attributes': True if product_variation_values['woocommerce_type'] == 'variation' and len(odoo_product_template_attribute_value_ids or []) > 0 else False,
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

                    elif version_info[0] == 18:
                        product_variation_values['type'] = 'service' if product_variation_values['woocommerce_service'] else 'consu'
                        product_variation_values['is_storable'] = True if product_variation_values['woocommerce_manage_stock'] else False

                    attribute_values_recset = self.env['product.template.attribute.value'].browse(odoo_product_template_attribute_value_ids)
                    odoo_product_variant = odoo_product._get_variant_for_combination(attribute_values_recset)

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
                    odoo_product.write({'taxes_id': [(6, 0, aggregated_tax_ids)]})

                # Save SKU back to 'parent.template'
                odoo_product.write({'default_code': odoo_product_sku})

        except Exception as error:
            # Roll back changes
            self.env.cr.rollback()
            _logger.exception(f'Error syncing WooCommerce product: {woocommerce_product["name"]} (WooCommerce product ID: {woocommerce_product["id"]}): {error}')

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
        params = {'status': 'publish', 'fields': 'id,variations', 'type': 'variable'}

        if self.settings_woocommerce_modified_records_import:
            odoo_woocommerce_last_sync = self.odoo_shorepos_last_sync_retrieve()
            if odoo_woocommerce_last_sync:
                params['modified_after'] = odoo_woocommerce_last_sync.strftime('%Y-%m-%dT%H:%M:%S')  # ISO 8601 date format

        if self.settings_woocommerce_to_odoo_products_language_code:
            params['lang'] = self.settings_woocommerce_to_odoo_products_language_code

        for woocommerce_products_batch in self.woocommerce_api_get_items_in_batches(woocommerce_api, endpoint='products', params=params):
            # Filter for WooCommerce products that have SKU
            woocommerce_products_batch = [woocommerce_product for woocommerce_product in woocommerce_products_batch if woocommerce_product['sku']]

            # Schedule a separate job for each WooCommerce product in the batch
            for woocommerce_product in woocommerce_products_batch:
                self.with_delay().woocommerce_to_odoo_product_variations_sync(
                    woocommerce_product, woocommerce_currency, woocommerce_tax_rates, woocommerce_prices_include_tax, woocommerce_weight_unit, woocommerce_dimension_unit
                )

    def woocommerce_to_odoo_customer_sync(self: models.Model, woocommerce_customer: dict[str, Any], odoo_customers: dict[str, Any]) -> None:
        try:
            # Try to find the corresponding partner in Odoo by its WooCommerce customer ID
            odoo_customer = odoo_customers.get(str(woocommerce_customer['id']))

            if odoo_customer:
                odoo_customer = self.env['res.partner'].browse(odoo_customer['id'])

                # Skip if not modified
                if self.datetime_convert(woocommerce_customer['date_modified_gmt']) <= odoo_customer.write_date:
                    _logger.info(f'Skipped import of WooCommerce customer into Odoo: {odoo_customer.name} (Odoo customer ID: {odoo_customer.id}, WooCommerce customer ID: {odoo_customer.woocommerce_id})')
                    return

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

            ## Brazil (requires 'l10n_br_fiscal' Odoo add-on)
            if self.env['ir.module.module'].search([('name', '=', 'l10n_br_fiscal'), ('state', '=', 'installed')], limit=1):
                if woocommerce_customer['billing']['cpf'] or woocommerce_customer['billing']['cnpj']:
                    customer_values.update({'cnpj_cpf': woocommerce_customer['billing']['cpf'] or woocommerce_customer['billing']['cnpj']})

            # Custom fields
            customer_values.update(
                {
                    'woocommerce_last_login_date': datetime.fromtimestamp(int(meta['value']))
                    if (meta := next((meta for meta in woocommerce_customer['meta_data'] if meta.get('key') == 'wfls-last-login'), None))
                    else None,  # Wordfence Security field
                },
            )

            # Loop through the explicitly defined date columns for conversion
            for column in [
                'woocommerce_date_created',
                'woocommerce_date_created_gmt',
                'woocommerce_date_modified',
                'woocommerce_date_modified_gmt',
            ]:
                if column in customer_values and customer_values[column]:
                    customer_values[column] = self.datetime_convert(customer_values[column])

            # Customer avatar
            if self.settings_woocommerce_images_sync and woocommerce_customer['avatar_url'] != '':
                odoo_avatar_url = self.image_download_file_to_base64({'src': woocommerce_customer['avatar_url']})
            else:
                odoo_avatar_url = None

            # Odoo 'res.partner' model fields
            customer_values.update(
                {
                    # General information
                    'name': f'{customer_values["woocommerce_first_name"]} {customer_values["woocommerce_last_name"]}',
                    'image_1920': odoo_avatar_url,
                    'ref': customer_values['woocommerce_id'],
                    'create_date': customer_values['woocommerce_date_created_gmt'],
                    'company_type': 'person',
                    'customer_rank': 1 if customer_values['woocommerce_is_paying_customer'] else 0,
                    'email': customer_values['woocommerce_email'],
                    'mobile': customer_values['woocommerce_billing_phone'],
                    'user_id': self.settings_woocommerce_user_responsible.id,
                    # Customer status
                    'active': True,
                    # Address
                    'street': customer_values['woocommerce_billing_address_1'],
                    'street2': customer_values['woocommerce_billing_address_2'],
                    'city': customer_values['woocommerce_billing_city'],
                    'zip': customer_values['woocommerce_billing_postcode'],
                    'country_id': self.env['res.country'].search([('code', '=', customer_values['woocommerce_billing_country'])], limit=1).id,
                },
            )

            if odoo_customer:
                if customer_values['woocommerce_date_modified_gmt'] > odoo_customer['write_date']:
                    odoo_customer.write(customer_values)
                    _logger.info(f'Updated WooCommerce customer in Odoo: {odoo_customer.name} (Odoo customer ID: {odoo_customer.id}, WooCommerce customer ID: {odoo_customer["woocommerce_id"]})')

            else:
                odoo_customer = self.env['res.partner'].create(customer_values)
                _logger.info(f'Imported WooCommerce customer into Odoo: {odoo_customer.name} (Odoo customer ID: {odoo_customer.id}, WooCommerce customer ID: {odoo_customer["woocommerce_id"]})')

        except Exception as error:
            # Roll back changes
            self.env.cr.rollback()
            _logger.exception(f'Error syncing WooCommerce customer: {woocommerce_customer["first_name"]} {woocommerce_customer["last_name"]} (WooCommerce customer ID: {woocommerce_customer["id"]}): {error}')

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
            odoo_woocommerce_last_sync = self.odoo_shorepos_last_sync_retrieve()
            if odoo_woocommerce_last_sync:
                params['modified_after'] = odoo_woocommerce_last_sync.strftime('%Y-%m-%dT%H:%M:%S')  # ISO 8601 date format

        # Get all Odoo partners with WooCommerce customer ID
        odoo_customers = self.env['res.partner'].search_read(
            [('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('active', '=', True), ('woocommerce_id', '!=', False)],
            fields=['id', 'name', 'active', 'write_date', 'woocommerce_id'],
        )
        odoo_customers = {odoo_customer['woocommerce_id']: odoo_customer for odoo_customer in odoo_customers}

        for woocommerce_customers_batch in self.woocommerce_api_get_items_in_batches(woocommerce_api, endpoint='customers', params=params):
            # Schedule a separate job for each WooCommerce customer in the batch
            for woocommerce_customer in woocommerce_customers_batch:
                self.with_delay().woocommerce_to_odoo_customer_sync(woocommerce_customer, odoo_customers)

    def woocommerce_to_odoo_order_sync(
        self: models.Model, woocommerce_order: dict[str, Any], woocommerce_tax_rates: dict[str, float], woocommerce_weight_unit: str, woocommerce_shipping_methods: list[dict[str, Any]], odoo_sale_orders: dict[str, Any]
    ) -> None:
        try:
            # Try to find the corresponding sale order in Odoo by its WooCommerce order ID
            odoo_sale_order = odoo_sale_orders.get(str(woocommerce_order['id']))

            if odoo_sale_order:
                odoo_sale_order = self.env['sale.order'].browse(odoo_sale_order['id'])

                # Skip if not modified
                if self.datetime_convert(woocommerce_order['date_modified_gmt']) <= odoo_sale_order.write_date:
                    _logger.info(f'Skipped import of WooCommerce order into Odoo: {odoo_sale_order.name} (Odoo sale order ID: {odoo_sale_order.id}, WooCommerce order ID: {odoo_sale_order.woocommerce_number})')
                    return

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

            # Loop through the explicitly defined date columns for conversion
            for column in [
                'woocommerce_date_created',
                'woocommerce_date_created_gmt',
                'woocommerce_date_modified',
                'woocommerce_date_modified_gmt',
                'woocommerce_date_paid',
                'woocommerce_date_paid_gmt',
                'woocommerce_date_completed',
                'woocommerce_date_completed_gmt',
            ]:
                if column in order_values and order_values[column]:
                    order_values[column] = self.datetime_convert(order_values[column])

            # Currency
            if order_values['woocommerce_currency']:
                odoo_order_currency = self.odoo_currency_retrieve(order_values['woocommerce_currency'])

            # Odoo customer reference
            odoo_customer = self.env['res.partner'].search(
                [
                    ('woocommerce_site_url', '=', self.settings_woocommerce_connection_url),
                    ('active', '=', True),
                    ('woocommerce_id', '=', woocommerce_order['customer_id']),
                ],
                limit=1,
            )

            if not odoo_customer:
                if self.settings_woocommerce_orders_customers_map:
                    customer_values = {
                        'woocommerce_id': woocommerce_order['customer_id'],
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

                    ## Brazil (requires 'l10n_br_fiscal' Odoo add-on)
                    if self.env['ir.module.module'].search([('name', '=', 'l10n_br_fiscal'), ('state', '=', 'installed')], limit=1):
                        if woocommerce_order['billing']['cpf'] or woocommerce_order['billing']['cnpj']:
                            customer_values.update({'cnpj_cpf': woocommerce_order['billing']['cpf'] or woocommerce_order['billing']['cnpj']})

                    # Odoo 'res.partner' model fields
                    customer_values.update(
                        {
                            # General information
                            'name': f'{customer_values["woocommerce_billing_first_name"]} {customer_values["woocommerce_billing_last_name"]}',
                            'ref': customer_values['woocommerce_id'],
                            'company_type': 'person',
                            'email': customer_values['woocommerce_billing_email'],
                            'mobile': customer_values['woocommerce_billing_phone'],
                            'user_id': self.settings_woocommerce_user_responsible.id,
                            # Customer status
                            'active': True,
                            # Address
                            'street': customer_values['woocommerce_billing_address_1'],
                            'street2': customer_values['woocommerce_billing_address_2'],
                            'city': customer_values['woocommerce_billing_city'],
                            'zip': customer_values['woocommerce_billing_postcode'],
                            'country_id': self.env['res.country'].search([('code', '=', customer_values['woocommerce_billing_country'])], limit=1).id,
                        },
                    )

                    # Check for duplicate email
                    if customer_values['email']:
                        odoo_customer = self.env['res.partner'].search(
                            [('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('active', '=', True), ('email', '=', customer_values['email'])],
                            limit=1,
                        )

                        if not odoo_customer:
                            odoo_customer = self.env['res.partner'].create(customer_values)

                else:
                    # Create/retrieve customer placeholder
                    odoo_customer = self.odoo_customer_placeholder_create_or_retrieve()

            # Localization

            ## Brazil (requires 'l10n_br_fiscal' Odoo add-on)
            if self.env['ir.module.module'].search([('name', '=', 'l10n_br_fiscal'), ('state', '=', 'installed')], limit=1):
                if woocommerce_order['billing']['cpf'] or woocommerce_order['billing']['cnpj']:
                    order_values.update({'cnpj_cpf': woocommerce_order['billing']['cpf'] or woocommerce_order['billing']['cnpj']})

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
                    'partner_shipping_id': odoo_customer.id,
                    # Shipping and stock
                    'picking_policy': 'direct',
                    # 'warehouse_id': self.settings_woocommerce_products_warehouse_location.id,
                    # Payment
                    # 'currency_id': odoo_order_currency.id,
                    # 'tax_country_id': self.env['res.country'].search([('code', '=', order_values['woocommerce_billing_country'])], limit=1).id,
                    # 'amount_tax': order_values['woocommerce_total_tax'],
                    # 'amount_total': order_values['woocommerce_total'],
                },
            )

            if odoo_sale_order:
                odoo_sale_order.write(order_values)
                _logger.info(f'Updated WooCommerce order in Odoo: {odoo_sale_order.name} (Odoo sale order ID: {odoo_sale_order.id}, WooCommerce order ID: {odoo_sale_order["woocommerce_number"]})')

            else:
                odoo_sale_order = self.env['sale.order'].create(order_values)
                _logger.info(f'Imported WooCommerce order into Odoo: {odoo_sale_order.name} (Odoo sale order ID: {odoo_sale_order.id}, WooCommerce order ID: {odoo_sale_order["woocommerce_number"]})')

            # Confirm order if WooCommerce status is 'processing', 'on-hold' or 'completed' (move order 'state' to 'sale')
            if order_values['woocommerce_status'] in ('processing', 'on-hold', 'completed') and odoo_sale_order.state in ('draft', 'sent'):
                odoo_sale_order.action_confirm()

            # Cancel order if WooCommerce status is 'cancelled', 'refunded', 'failed' or 'trash' (move order 'state' to 'cancel')
            elif order_values['woocommerce_status'] in ('cancelled', 'refunded', 'failed', 'trash') and odoo_sale_order.state not in ('cancel', 'done'):
                odoo_sale_order.action_cancel()

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
                        odoo_product_mapped = self.env['product.template'].search(
                            [
                                ('woocommerce_site_url', '=', self.settings_woocommerce_connection_url),
                                ('active', '=', True),
                                ('woocommerce_id', '=', order_line_values['woocommerce_product_id']),
                            ],
                            limit=1,
                        )

                    # Product variation
                    else:
                        odoo_product_mapped = self.env['product.product'].search(
                            [
                                ('woocommerce_site_url', '=', self.settings_woocommerce_connection_url),
                                ('active', '=', True),
                                ('woocommerce_id', '=', order_line_values['woocommerce_variation_id']),
                            ],
                            limit=1,
                        )

                if not odoo_product_mapped:
                    # Create/retrieve product placeholder
                    odoo_product = self.odoo_product_placeholder_create_or_retrieve()

                # Tax
                odoo_order_line_item_tax_id = []
                if order_line_values['woocommerce_tax_class']:
                    odoo_order_line_item_tax = self.odoo_tax_rate_create_or_retrieve(order_line_values['woocommerce_tax_class'], order_values['woocommerce_prices_include_tax'])
                    if odoo_order_line_item_tax:
                        odoo_order_line_item_tax_id = [(6, 0, [odoo_order_line_item_tax.id])]

                # Unit of measure
                if self.settings_woocommerce_products_package_size_unit_default:
                    odoo_order_line_item_unit_of_measure = self.env.ref('uom.product_uom_unit')

                elif order_line_values['woocommerce_weight_unit']:
                    odoo_order_line_item_unit_of_measure = self.odoo_unit_of_measure_create_or_retrieve(order_line_values['woocommerce_weight_unit'])

                # Localization

                ## Brazil (requires 'l10n_br_fiscal' Odoo add-on)
                if self.env['ir.module.module'].search([('name', '=', 'l10n_br_fiscal'), ('state', '=', 'installed')], limit=1):
                    if woocommerce_order['shipping_total']:
                        order_line_values.update({'freight_value': float(woocommerce_order['shipping_total']) * (float(line_item['total']) / order_line_items_total)})

                # Odoo 'sale.order.line' model fields
                order_line_values.update(
                    {
                        # General information
                        'order_id': odoo_sale_order.id,
                        'name': order_line_values['woocommerce_name'],
                        'product_id': odoo_product_mapped.id if self.settings_woocommerce_line_items_product_map else odoo_product.product_variant_ids[:1].id,
                        # Shipping and stock
                        'warehouse_id': self.settings_woocommerce_products_warehouse_location.id,
                        # Dimensions
                        'product_uom': odoo_order_line_item_unit_of_measure.id if odoo_order_line_item_unit_of_measure else False,
                        # Payment
                        'currency_id': odoo_order_currency.id,
                        'tax_id': odoo_order_line_item_tax_id,
                        'product_uom_qty': order_line_values['woocommerce_quantity'],
                        'price_unit': (
                            order_line_values['woocommerce_price'] + (float(order_line_values['woocommerce_subtotal_tax']) / order_line_values['woocommerce_quantity'])
                            if order_values['woocommerce_prices_include_tax']
                            else order_line_values['woocommerce_price']
                        ),
                        # 'discount'
                    },
                )

                odoo_sale_order_line = self.env['sale.order.line'].search(
                    [
                        ('woocommerce_site_url', '=', self.settings_woocommerce_connection_url),
                        ('order_id', '=', odoo_sale_order.id),
                        ('woocommerce_id', '=', order_line_values['woocommerce_id']),
                    ],
                    limit=1,
                )

                # Update the sale order line
                if odoo_sale_order_line:
                    odoo_sale_order_line.write(order_line_values)

                else:
                    self.env['sale.order.line'].create(order_line_values)

            # Delivery carrier
            if woocommerce_order['shipping_lines']:
                odoo_delivery_carrier = self.odoo_delivery_carrier_create_or_retrieve(woocommerce_shipping_methods, woocommerce_order['shipping_lines'][0])

                if odoo_delivery_carrier:
                    odoo_sale_order.set_delivery_line(odoo_delivery_carrier, woocommerce_order['shipping_lines'][0]['total'])

        except Exception as error:
            # Roll back changes
            self.env.cr.rollback()
            _logger.exception(f'Error syncing WooCommerce order {woocommerce_order["id"]}: {error}')

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
            odoo_woocommerce_last_sync = self.odoo_shorepos_last_sync_retrieve()
            if odoo_woocommerce_last_sync:
                params['modified_after'] = odoo_woocommerce_last_sync.strftime('%Y-%m-%dT%H:%M:%S')  # ISO 8601 date format

        # Get all Odoo sale orders with WooCommerce order ID
        odoo_sale_orders = self.env['sale.order'].search_read(
            [('woocommerce_site_url', '=', self.settings_woocommerce_connection_url), ('woocommerce_id', '!=', False)],
            fields=['id', 'name', 'write_date', 'woocommerce_id'],
        )
        odoo_sale_orders = {odoo_sale_order['woocommerce_id']: odoo_sale_order for odoo_sale_order in odoo_sale_orders}

        for woocommerce_orders_batch in self.woocommerce_api_get_items_in_batches(woocommerce_api, endpoint='orders', params=params):
            # Schedule a separate job for each WooCommerce order in the batch
            for woocommerce_order in woocommerce_orders_batch:
                self.with_delay().woocommerce_to_odoo_order_sync(woocommerce_order, woocommerce_tax_rates, woocommerce_weight_unit, woocommerce_shipping_methods, odoo_sale_orders)

    def woocommerce_attribute_create_or_retrieve(self: models.Model, woocommerce_api: API, attribute_type: str, attribute_name: str, language_code: str | None = None) -> dict[str, Any] | None:
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
        """Upload main image ('image_1920') + gallery images ('product_image_ids') and return list of WordPress media IDs."""
        self.ensure_one()

        wordpress_uploaded_image_ids = []
        counter = 1

        # Collect images
        images = []

        if odoo_product.image_1920:
            images.append(odoo_product.image_1920)

        if odoo_product.product_image_ids and len(odoo_product.product_image_ids) > 0:
            gallery_images = odoo_product.product_image_ids.sorted(key=lambda attachment: attachment.id)
            images.extend(gallery_images.mapped('datas'))

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
        odoo_products = self.env['product.template'].search(search_conditions) | self.env['product.product'].search(search_conditions + [('product_tmpl_id.default_code', '!=', False)]).mapped('product_tmpl_id')
        odoo_products_default_code = odoo_products.mapped('default_code')

        # Get all WooCommerce products with Odoo default code
        params = {'status': 'publish'}

        if self.settings_woocommerce_to_odoo_products_language_code:
            params['lang'] = self.settings_woocommerce_to_odoo_products_language_code

        if odoo_products_default_code:
            params['sku'] = ','.join(odoo_products_default_code)

        woocommerce_products = self.woocommerce_api_get_all_items(woocommerce_api, endpoint='products', params=params)
        woocommerce_products = {woocommerce_product['sku']: woocommerce_product for woocommerce_product in woocommerce_products}

        for odoo_product in odoo_products:
            try:
                # Try to find the corresponding product in WooCommerce by its Odoo default code
                woocommerce_product = woocommerce_products.get(odoo_product.default_code)

                if woocommerce_product and odoo_product['write_date'] <= self.datetime_convert(woocommerce_product['date_modified_gmt']):
                    _logger.info(f'Skipped import of Odoo product into WooCommerce: {odoo_product["name"]} (Odoo product ID: {odoo_product.id})')
                    continue

                # Create new product in WooCommerce if it does not yet exist or update product in WooCommerce only if Odoo version is newer
                else:
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
                        product_values['manage_stock'] = True if odoo_product.detailed_type == 'product' else False

                    elif version_info[0] == 18:
                        product_values['manage_stock'] = True if odoo_product.is_storable else False

                    # Check if product has multiple variants
                    if len(odoo_product.product_variant_ids) > 1:
                        product_values['type'] = 'variable'

                        woocommerce_attributes = []
                        for line in odoo_product.attribute_line_ids:
                            odoo_attributes = [value.name for value in line.value_ids]

                            for attribute in odoo_attributes:
                                woocommerce_attribute = self.woocommerce_attribute_create_or_retrieve(
                                    woocommerce_api,
                                    'attributes',
                                    attribute,
                                    odoo_product.language_code if odoo_product.language_code else None,
                                )
                                if woocommerce_attribute:
                                    woocommerce_attributes.append({'id': woocommerce_attribute['id'], 'name': line.attribute_id.name, 'variation': True, 'visible': True, 'options': odoo_attributes})

                            if woocommerce_attributes:
                                product_values['attributes'] = woocommerce_attributes

                    # Brand (requires 'product_brand' Odoo add-on)
                    if self.env['ir.module.module'].search([('name', '=', 'product_brand'), ('state', '=', 'installed')], limit=1) and len(odoo_product.product_brand_id) > 0:
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
                    if self.env['ir.module.module'].search([('name', '=', 'product_multi_category'), ('state', '=', 'installed')], limit=1) and len(odoo_product.categ_ids) > 0:
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
                    if self.env['ir.module.module'].search([('name', '=', 'product_dimension'), ('state', '=', 'installed')], limit=1):
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
                    if (
                        self.settings_woocommerce_images_sync
                        and self.settings_wordpress_username
                        and self.settings_wordpress_user_application_password
                        and (odoo_product.image_1920 or len(odoo_product.product_image_ids) > 0)
                    ):
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

                    # Update product in WooCommerce only if Odoo version is newer
                    if woocommerce_product:
                        woocommerce_product = woocommerce_api.put(f'products/{woocommerce_product["id"]}', data=product_values).json()

                    # Create new product in WooCommerce if it does not yet exist
                    else:
                        woocommerce_product = woocommerce_api.post('products', data=product_values).json()

                        if woocommerce_product['id']:
                            woocommerce_product_fields = self.woocommerce_product_fields(woocommerce_product, woocommerce_currency, woocommerce_weight_unit, woocommerce_dimension_unit, woocommerce_tax_rates)
                            woocommerce_product_fields.update({'odoo_to_woocommerce_last_sync': fields.Datetime.now()})
                            odoo_product.write(woocommerce_product_fields)

                            _logger.info(f'Imported Odoo product into WooCommerce: {odoo_product.name} (Odoo product ID: {odoo_product.id}, WooCommerce product ID: {odoo_product["woocommerce_id"]})')

                    # For variable products, handle variations
                    if product_values.get('type') == 'variable':
                        # Retrieve existing variations from WooCommerce
                        woocommerce_variations = self.woocommerce_api_get_all_items(woocommerce_api, endpoint=f'products/{woocommerce_product["id"]}/variations', params={'status': 'publish'})

                        # Build a mapping by SKU for easier lookup
                        variations_by_sku = {variation.get('sku'): variation for variation in woocommerce_variations if variation.get('sku')}

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
                            if self.env['ir.module.module'].search([('name', '=', 'product_dimension'), ('state', '=', 'installed')], limit=1):
                                variation_data.update(
                                    {
                                        'dimensions': {
                                            'length': odoo_product_variant.product_length if odoo_product_variant.product_length != 0.0 else '',
                                            'width': odoo_product_variant.product_width if odoo_product_variant.product_width != 0.0 else '',
                                            'height': odoo_product_variant.product_height if odoo_product_variant.product_height != 0.0 else '',
                                        }
                                    }
                                )

                            # Product variation meta data "odoo_id"
                            woocommerce_product_variation_meta_data = []

                            variation_existing = variations_by_sku.get(odoo_product_variant.default_code)
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
                                variation_data['manage_stock'] = True if odoo_product.detailed_type == 'product' else False

                            elif version_info[0] == 18:
                                variation_data['manage_stock'] = True if odoo_product.is_storable else False

                            # Check if a variation with this SKU already exists
                            variation_existing = variations_by_sku.get(odoo_product_variant.default_code)
                            if variation_existing:
                                if odoo_product_variant['write_date'] > self.datetime_convert(variation_existing['date_modified_gmt']):
                                    # Update product variation in WooCommerce only if Odoo version is newer
                                    woocommerce_variant = woocommerce_api.put(f'products/{woocommerce_product["id"]}/variations/{variation_existing["id"]}', data=variation_data).json()

                                else:
                                    _logger.info(f'Odoo product variant in WooCommerce is up-to-date: {odoo_product_variant.display_name} (Odoo product variant ID: {odoo_product_variant.id})')

                            else:
                                # Create the product variation if it doesn't exist
                                woocommerce_variant = woocommerce_api.post(f'products/{woocommerce_product["id"]}/variations', data=variation_data).json()

                                if woocommerce_variant['id']:
                                    woocommerce_product_variation_fields = self.woocommerce_product_variation_fields(
                                        woocommerce_variant, woocommerce_currency, woocommerce_weight_unit, woocommerce_dimension_unit, woocommerce_tax_rates
                                    )
                                    woocommerce_product_variation_fields.update({'odoo_to_woocommerce_last_sync': fields.Datetime.now()})
                                    odoo_product_variant.write(woocommerce_product_variation_fields)

                                    _logger.info(
                                        f'Imported Odoo product variant into WooCommerce: {odoo_product_variant.name} (Odoo product variant ID: {odoo_product_variant.id}, WooCommerce product variation ID: {woocommerce_variant["id"]})'
                                    )

            except Exception as error:
                _logger.exception(f'Error syncing Odoo product {odoo_product.id} into WooCommerce: {error}')
