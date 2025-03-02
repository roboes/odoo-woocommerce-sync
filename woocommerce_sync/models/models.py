from base64 import b64encode
from datetime import datetime
from io import BytesIO
import logging
from PIL import Image
import pytz
import requests

from odoo import models, fields, api, _
from odoo.exceptions import UserError

from woocommerce import API

# Settings
_logger = logging.getLogger(__name__)


class WoocommerceSyncLog(models.Model):
    _name = 'woocommerce.sync.log'
    _description = 'WooCommerce Sync Log'

    woocommerce_last_synced = fields.Datetime(string='Sync Date', readonly=True)


class WoocommerceConnector(models.Model):
    _name = 'woocommerce.configuration'
    _description = 'WooCommerce Configuration'

    # View settings
    woocommerce_connection_sequence = fields.Char(string='Connection ID', required=True, copy=False, readonly=True, index=True, default=lambda self: _('New'))

    # WooCommerce REST API settings
    settings_woocommerce_connection_name = fields.Char(string='Instance Name')
    settings_woocommerce_connection_url = fields.Char(string='Store URL')
    settings_woocommerce_consumer_key = fields.Char(string='Consumer Key')
    settings_woocommerce_consumer_secret = fields.Char(string='Consumer Secret')
    settings_woocommerce_timeout = fields.Integer(string='Timeout', default=30)

    # Sync items settings
    settings_woocommerce_to_odoo_products_sync = fields.Boolean(default=True)
    settings_odoo_to_woocommerce_products_sync = fields.Boolean(default=False)
    settings_woocommerce_to_odoo_product_variations_sync = fields.Boolean(default=True)
    settings_odoo_to_woocommerce_product_variations_sync = fields.Boolean(default=True, readonly=True)
    settings_woocommerce_to_odoo_customers_sync = fields.Boolean(default=True)
    settings_woocommerce_to_odoo_orders_sync = fields.Boolean(default=True)

    # General settings
    settings_woocommerce_user_responsible = fields.Many2one(
        comodel_name='res.users',
        string='Responsible',
        default=lambda self: self.env.user,
        help='Default responsible user for WooCommerce operations.',
    )
    settings_woocommerce_modified_records_import = fields.Boolean(
        string='Import only modified records?',
        help="If enabled, only records modified since the last import will be retrieved from WooCommerce using the 'modified_after' WooCommerce REST API parameter. Only enable this option after the first import.",
        default=False,
    )
    settings_woocommerce_images_sync = fields.Boolean(string='Sync images?', default=True)

    # WooCommerce to Odoo products import settings
    settings_woocommerce_products_stock_management = fields.Boolean(string='Sync stock quantity?', default=True)
    settings_woocommerce_products_warehouse_location = fields.Many2one(
        comodel_name='stock.location',
        string='Warehouse location',
        default=lambda self: self.env.ref('stock.stock_location_stock'),
        help='Warehouse location for syncing WooCommerce products stock quantity.',
    )
    settings_woocommerce_products_related_ids_map = fields.Boolean(string='Map related products?', help="Automatically map WooCommerce 'related_ids' products to their Odoo equivalents.", default=False)
    settings_woocommerce_to_odoo_products_language_code = fields.Char(string='Filter WooCommerce products by language (requires Polylang)', help="2-digit language code (ISO 639-1) (e.g. 'en').")

    # WooCommerce to Odoo orders import settings
    settings_woocommerce_orders_customers_map = fields.Boolean(
        string='Map guest customers to Odoo customers in orders?',
        help='If enabled, orders purchased by guest (unregistered) customers will be mapped to existing Odoo customers by email address. If the customer does not exist in the database, a new customer will be created automatically. If disabled, a customer placeholder will be assigned to the order.',
        default=False,
    )
    settings_woocommerce_order_line_items_product_map = fields.Boolean(
        string='Map products to existing Odoo products in line items?',
        help="If enabled, line items products will be mapped to existing Odoo products by 'woocommerce_product_id'. If no match is found, a product placeholder will be used. If disabled, all order line items will be assigned to a placeholder product, but the WooCommerce product name will still be displayed. Not recommended, given that products in WooCommerce may have changed since purchase, making mapping difficult.",
        default=False,
    )

    # Odoo to WooCommerce products import settings
    settings_woocommerce_odoo_to_woocommerce_products_language_code = fields.Char(
        string="Filter Odoo products by language defined in the 'product_language_code' field (requires Polylang)",
        help="2-digit language code (ISO 639-1) (e.g. 'en').",
    )

    # Scheduled sync settings
    settings_woocommerce_sync_scheduled = fields.Boolean('Enable auto-sync')
    settings_woocommerce_sync_scheduled_interval_minutes = fields.Integer(string='Interval (in Minutes)', default=5)
    ir_cron_id = fields.Many2one(comodel_name='ir.cron', string='Scheduled Cron Job', ondelete='cascade')

    # Last synced
    woocommerce_last_synced = fields.Datetime(string='Last Synced', compute='woocommerce_last_synced_retrieve', store=False, readonly=True)

    def woocommerce_last_synced_retrieve(self):
        sync_log = self.env['woocommerce.sync.log'].search([], limit=1)
        for record in self:
            record.woocommerce_last_synced = sync_log.woocommerce_last_synced if sync_log else False

    @api.model
    def create(self, values):
        if values.get('woocommerce_connection_sequence', _('New')) == _('New'):
            values['woocommerce_connection_sequence'] = self.env['ir.sequence'].next_by_code('woocommerce.configuration.sequence') or _('New')
        rec = super().create(values)
        rec.cron_job_update()
        return rec

    def write(self, values):
        # Skip cron update if called from cron context
        if self.env.context.get('ir_cron'):
            return super().write(values)

        res = super().write(values)
        for rec in self:
            rec.cron_job_update()
        return res

    def cron_job_update(self):
        # Odoo-WooCommerce settings
        woocommerce_sync_config = self.env['woocommerce.configuration'].search([], limit=1)
        cron_values = {
            'name': f'WooCommerce Auto-Sync - {self.settings_woocommerce_connection_url}',
            'model_id': self.env['ir.model']._get(self._name).id,
            'code': (
                f'model.with_context(cron_running=True).browse({self.id}).with_delay().woocommerce_sync()'
                if self.env['ir.module.module'].search([('name', '=', 'queue_job'), ('state', '=', 'installed')], limit=1)
                else f'model.with_context(cron_running=True).browse({self.id}).woocommerce_sync()'
            ),
            'active': self.settings_woocommerce_sync_scheduled,
            'interval_number': woocommerce_sync_config.settings_woocommerce_sync_scheduled_interval_minutes,
            'interval_type': 'minutes',
            'numbercall': -1,
        }

        # Update the existing cron job
        if self.ir_cron_id:
            self.ir_cron_id.write(cron_values)
        # Create only if scheduled to avoid unnecessary cron jobs
        elif self.settings_woocommerce_sync_scheduled:
            self.ir_cron_id = self.env['ir.cron'].create(cron_values)

    def woocommerce_sync_action(self):
        self.ensure_one()
        _logger.warning("Manual 'Sync Now' button pressed, triggering background sync.")

        # Run woocommerce_sync in the background (requires 'queue_job' add-on)
        if self.env['ir.module.module'].search([('name', '=', 'queue_job'), ('state', '=', 'installed')], limit=1):
            self.with_delay().woocommerce_sync()

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Sync Started (Queue Job)'),
                    'message': _('WooCommerce sync process has been started in the background. %s'),
                    'links': [{'label': _('Open Job Queue'), 'url': f'#action={self.env["ir.actions.act_window"].search([("res_model", "=", "queue.job")], limit=1).id}&model=queue.job&view_type=list'}],
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

    def woocommerce_sync(self):
        if not self:
            _logger.warning('No WooCommerce configuration records found for sync.')
            return

        # Odoo-WooCommerce settings
        woocommerce_sync_config = self.env['woocommerce.configuration'].search([], limit=1)

        # WooCommerce REST API
        woocommerce_api = self.woocommerce_api_get(woocommerce_sync_config)

        # Check if WooCommerce REST API connection is successful
        if not woocommerce_api:
            error_message = 'WooCommerce REST API connection failed. Sync process halted. Please check your connection settings in the WooCommerce Configuration.'
            _logger.error(error_message)
            raise UserError(_(error_message))

        # WooCommerce to Odoo

        ### WooCommerce currency
        woocommerce_currency = woocommerce_api.get(endpoint='settings/general/woocommerce_currency').json()['value']

        ### WooCommerce measurements
        woocommerce_weight_unit = woocommerce_api.get(endpoint='settings/products/woocommerce_weight_unit').json()['value']
        woocommerce_dimension_unit = woocommerce_api.get(endpoint='settings/products/woocommerce_dimension_unit').json()['value']

        ### WooCommerce taxes
        woocommerce_product_prices_include_tax = True if woocommerce_api.get(endpoint='settings/tax/woocommerce_prices_include_tax').json()['value'].lower() == 'yes' else False
        woocommerce_tax_rates = woocommerce_api.get(endpoint='taxes').json()
        woocommerce_tax_rates = {woocommerce_tax_rate['class']: float(woocommerce_tax_rate['rate']) for woocommerce_tax_rate in woocommerce_tax_rates}

        ## Products
        if woocommerce_sync_config.settings_woocommerce_to_odoo_products_sync:
            self.woocommerce_to_odoo_products_sync(
                woocommerce_sync_config,
                woocommerce_api,
                woocommerce_currency,
                woocommerce_tax_rates,
                woocommerce_product_prices_include_tax,
                woocommerce_weight_unit,
                woocommerce_dimension_unit,
            )

        ## Product variations
        if woocommerce_sync_config.settings_woocommerce_to_odoo_products_sync and woocommerce_sync_config.settings_woocommerce_to_odoo_product_variations_sync:
            self.woocommerce_to_odoo_products_variations_sync(
                woocommerce_sync_config,
                woocommerce_api,
                woocommerce_currency,
                woocommerce_tax_rates,
                woocommerce_product_prices_include_tax,
                woocommerce_weight_unit,
                woocommerce_dimension_unit,
            )

        ## Products related ids map
        if woocommerce_sync_config.settings_woocommerce_products_related_ids_map:
            self.woocommerce_to_odoo_product_related_ids(woocommerce_sync_config)

        ## Customers
        if woocommerce_sync_config.settings_woocommerce_to_odoo_customers_sync:
            self.woocommerce_to_odoo_customers_sync(woocommerce_sync_config, woocommerce_api)

        ## Orders
        if woocommerce_sync_config.settings_woocommerce_to_odoo_orders_sync:
            self.woocommerce_to_odoo_orders_sync(woocommerce_sync_config, woocommerce_api, woocommerce_tax_rates, woocommerce_weight_unit)

        # Odoo to WooCommerce

        ## Products
        if woocommerce_sync_config.settings_odoo_to_woocommerce_products_sync:
            self.odoo_to_woocommerce_products_sync(
                woocommerce_sync_config,
                woocommerce_api,
                woocommerce_currency,
                woocommerce_tax_rates,
                woocommerce_product_prices_include_tax,
                woocommerce_weight_unit,
                woocommerce_dimension_unit,
            )

        # Stock
        if woocommerce_sync_config.settings_woocommerce_products_stock_management:
            self.product_stock_quantity_create_or_update(woocommerce_sync_config, woocommerce_api)

        # Store 'woocommerce_last_synced'
        woocommerce_sync_log = self.env['woocommerce.sync.log'].search([], limit=1)

        if woocommerce_sync_log:
            woocommerce_sync_log.write({'woocommerce_last_synced': fields.Datetime.now()})
        else:
            self.env['woocommerce.sync.log'].create({'woocommerce_last_synced': fields.Datetime.now()})

    @api.depends('settings_woocommerce_connection_url', 'settings_woocommerce_consumer_key', 'settings_woocommerce_consumer_secret', 'settings_woocommerce_timeout')
    @api.returns('woocommerce.api')
    def woocommerce_api_get(self, woocommerce_sync_config):
        """Retrieves WooCommerce REST API instance."""

        woocommerce_api = API(
            url=woocommerce_sync_config.settings_woocommerce_connection_url,
            consumer_key=woocommerce_sync_config.settings_woocommerce_consumer_key,
            consumer_secret=woocommerce_sync_config.settings_woocommerce_consumer_secret,
            version='wc/v3',
            timeout=woocommerce_sync_config.settings_woocommerce_timeout,
            # query_string_auth=False,
            user_agent='Odoo-Woocommerce Sync',
        )

        # Test WooCommerce REST API Connection
        try:
            response = woocommerce_api.get(endpoint='system_status')
            if response.status_code == 200:
                _logger.info('WooCommerce REST API connection successful.')
                return woocommerce_api
            else:
                _logger.error(f'WooCommerce REST API returned status {response.status_code}: {response.text}')
                return False
        except requests.exceptions.RequestException as error:
            _logger.error(f'WooCommerce REST API connection failed: {error}')
            return False

    @staticmethod
    def woocommerce_api_get_all_items(woocommerce_api, endpoint, search_parameters={}, test_mode=False):
        # Set default records per page if not already provided
        search_parameters.setdefault('per_page', 100)

        # If test_mode is enabled, limit to 10 items
        if test_mode:
            search_parameters['per_page'] = 10

        records_all = []
        page = 1
        while True:
            # Update the page number for each request
            search_parameters['page'] = page
            records = woocommerce_api.get(endpoint=endpoint, params=search_parameters).json()

            records_all.extend(records)

            # If no records are returned, or test_mode is enabled (fetch only first page), break the loop
            if not records or test_mode:
                break

            page += 1

        return records_all

    def woocommerce_last_execution_datetime(self):
        woocommerce_sync_log = self.env['woocommerce.sync.log'].search([], limit=1)

        if woocommerce_sync_log.woocommerce_last_synced:
            return woocommerce_sync_log.woocommerce_last_synced.astimezone(pytz.timezone(self.env.user.tz or 'UTC')).replace(tzinfo=None)

        else:
            False

    @staticmethod
    def datetime_convert(date_string):
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
    def image_download_file_to_base64(self, woocommerce_product_images):
        """Downloads the featured image file from WooCommerce and returns it as a base64-encoded string."""
        if not woocommerce_product_images:
            return None

        # Get the image URL
        image_url = woocommerce_product_images.get('src')

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
    def image_process_attachments(self, woocommerce_product_images, product):
        """Downloads images from WooCommerce, converts them to base64 encoding, and stores them as attachments. Returns a list of attachment IDs."""
        if not woocommerce_product_images:
            return None

        attachments = []
        for image_data in woocommerce_product_images:
            if not image_data['src'] or not image_data['name']:
                continue

            try:
                response = requests.get(image_data['src'], timeout=10)
                response.raise_for_status()

                # Convert image to base64
                img = Image.open(BytesIO(response.content))
                buffered = BytesIO()
                img.save(buffered, format='PNG')  # Save as PNG
                img_base64 = b64encode(buffered.getvalue()).decode('utf-8')

                # Create attachment in Odoo
                attachment = self.env['ir.attachment'].create(
                    {
                        'name': image_data['name'],
                        'type': 'binary',
                        'datas': img_base64,
                        'mimetype': 'image/png',
                        'res_model': 'product.template',
                        'res_id': product.id,
                    },
                )
                attachments.append(attachment.id)

            except requests.exceptions.RequestException as error:
                _logger.error(f'Failed to download image from {image_data["src"]}: {error}')
            except Exception as error:
                _logger.error(f'Error processing the image from {image_data["src"]}: {error}')

        return attachments if attachments else None

    @api.returns('res.currency')
    def odoo_currency_retrieve(self, currency):
        """Retrieve an Odoo currency."""
        if not currency:
            return False

        odoo_currency = self.env['res.currency'].search([('active', '=', True), ('name', '=', currency)], limit=1)

        if odoo_currency:
            return odoo_currency

        else:
            return False

    @api.returns('account.tax')
    def odoo_tax_create_or_retrieve(self, tax_rate, price_include_flag=False):
        """Create or retrieve an Odoo tax."""
        if tax_rate is None or tax_rate == 0.0:
            return False

        odoo_tax = self.env['account.tax'].search([('active', '=', True), ('name', '=', f'{tax_rate}%'), ('amount', '=', tax_rate), ('type_tax_use', '=', 'sale'), ('price_include', '=', price_include_flag)], limit=1)

        if not odoo_tax:
            odoo_tax = self.env['account.tax'].create({'name': f'{tax_rate}%', 'amount': tax_rate, 'type_tax_use': 'sale', 'price_include': price_include_flag})

        return odoo_tax

    @api.returns('product.brand')
    def odoo_brand_create_or_retrieve(self, brand_name):
        """Create or retrieve an Odoo brand."""
        if not brand_name:
            return False

        odoo_brand = self.env['product.brand'].search([('name', '=', brand_name)], limit=1)

        if not odoo_brand:
            odoo_brand = self.env['product.brand'].create({'name': brand_name})

        return odoo_brand

    @api.returns('product.category')
    def odoo_category_create_or_retrieve(self, category_name):
        """Create or retrieve an Odoo category."""
        if not category_name:
            return False

        odoo_category = self.env['product.category'].search([('name', '=', category_name)], limit=1)

        if not odoo_category:
            odoo_category = self.env['product.category'].create({'name': category_name})

        return odoo_category

    @api.returns('product.tag')
    def odoo_tag_create_or_retrieve(self, tag_name):
        """Create or retrieve an Odoo tag."""
        if not tag_name:
            return False

        odoo_tag = self.env['product.tag'].search([('name', '=', tag_name)], limit=1)

        if not odoo_tag:
            odoo_tag = self.env['product.tag'].create({'name': tag_name})

        return odoo_tag

    @api.returns('uom.uom')
    def odoo_unit_of_measure_create_or_retrieve(self, unit_of_measure_name):
        """Create or retrieve an Odoo unit of measure."""
        if not unit_of_measure_name:
            return False

        odoo_unit_of_measure = self.env['uom.uom'].search([('active', '=', True), ('name', '=', unit_of_measure_name)], limit=1)

        if not odoo_unit_of_measure:
            odoo_unit_of_measure = self.env['uom.uom'].create({'name': unit_of_measure_name, 'category_id': self.env.ref('uom.uom_categ_unit').id, 'factor_inv': 1, 'uom_type': 'reference'})

        return odoo_unit_of_measure

    @api.returns('uom.uom')
    def odoo_unit_of_measure_dimension_retrieve(self, dimensional_uom_name):
        """Retrieve an Odoo dimensional unit of measure."""
        if not dimensional_uom_name:
            return False

        odoo_dimensional_uom = self.env['uom.uom'].search([('active', '=', True), ('name', '=', dimensional_uom_name)], limit=1)

        if not odoo_dimensional_uom:
            raise UserError(f'The dimensional UoM "{dimensional_uom_name}" does not exist.')

        return odoo_dimensional_uom

    @api.returns('res.partner')
    def odoo_customer_placeholder_create_or_retrieve(self):
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

            # Commit changes
            self.env.cr.commit()

        else:
            # Ensure the customer is archived
            if odoo_customer_placeholder.active:
                odoo_customer_placeholder.write({'active': False})

        return odoo_customer_placeholder

    @api.returns('product.template')
    def odoo_product_placeholder_create_or_retrieve(self):
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
                'product_sync_to_woocommerce': False,
            }
            odoo_product_placeholder = self.env['product.template'].create(product_values)

            # Commit changes
            self.env.cr.commit()

        else:
            # Ensure the product is archived
            if odoo_product_placeholder.active:
                odoo_product_placeholder.write({'active': False})

        return odoo_product_placeholder

    def product_stock_quantity_create_or_update(self, woocommerce_sync_config, woocommerce_api):
        """Synchronize stock quantity levels between WooCommerce and Odoo using 'product.product records'. In WooCommerce, if a stock quantity level changes due to a purchase, the 'date_modified_gmt' field is updated accordingly."""
        # Retrieve WooCommerce products with stock management enabled
        woocommerce_products = self.woocommerce_api_get_all_items(woocommerce_api, endpoint='products', search_parameters={'status': 'publish', 'manage_stock': 'true'})
        woocommerce_products_stock_map = {product['id']: product for product in woocommerce_products}

        # Fetch all Odoo 'product.product' records linked to WooCommerce
        odoo_products = self.env['product.product'].search(
            [
                ('woocommerce_product_site_url', '=', woocommerce_sync_config.settings_woocommerce_connection_url),
                ('active', '=', True),
                ('woocommerce_product_id', '!=', False),
                ('detailed_type', '=', 'product'),
            ],
        )

        for product in odoo_products:
            # Determine the corresponding WooCommerce stock info
            if product.woocommerce_product_variation_id:
                # For variations, retrieve the specific variation stock
                woocommerce_stock_info = self.product_variations_stock_retrieve(woocommerce_sync_config, woocommerce_api, product)
            else:
                # For simple products, get the stock from the parent product
                woocommerce_stock_info = woocommerce_products_stock_map.get(int(product.woocommerce_product_id))

            if not woocommerce_stock_info:
                continue

            woocommerce_product_date_modified_gmt = self.datetime_convert(woocommerce_stock_info['date_modified_gmt'])
            woocommerce_product_stock_quantity = float(woocommerce_stock_info['stock_quantity'])

            # If the WooCommerce stock quantity level is newer or has never been synced, update the stock information in Odoo
            if not product.product_stock_date_updated or (woocommerce_product_date_modified_gmt >= product.product_stock_date_updated and woocommerce_product_stock_quantity != product.qty_available):
                odoo_product_stock_quantity = self.env['stock.quant'].search(
                    [
                        ('woocommerce_product_site_url', '=', woocommerce_sync_config.settings_woocommerce_connection_url),
                        ('product_id', '=', product.id),
                        ('location_id', '=', woocommerce_sync_config.settings_woocommerce_products_warehouse_location.id),
                    ],
                    limit=1,
                )

                if odoo_product_stock_quantity:
                    odoo_product_stock_quantity.with_company(self.env.company).write({'quantity': woocommerce_product_stock_quantity})
                else:
                    self.env['stock.quant'].create(
                        {
                            'woocommerce_product_site_url': woocommerce_sync_config.settings_woocommerce_connection_url,
                            'product_id': product.id,
                            'quantity': woocommerce_product_stock_quantity,
                            'location_id': woocommerce_sync_config.settings_woocommerce_products_warehouse_location.id,
                        },
                    )

                # Update the stock date updated
                product.write({'product_stock_date_updated': woocommerce_product_date_modified_gmt})

            # Otherwise, if the Odoo stock quantity level is newer, update the stock information in WooCommerce
            elif woocommerce_product_date_modified_gmt < product.product_stock_date_updated and woocommerce_product_stock_quantity != product.qty_available:
                if product.woocommerce_product_variation_id:
                    woocommerce_product = woocommerce_api.put(
                        f'products/{product.woocommerce_product_variation_parent_id}/variations/{product.woocommerce_product_variation_id}',
                        data={'stock_quantity': product.qty_available},
                    ).json()
                elif product.woocommerce_product_id:
                    woocommerce_product = woocommerce_api.put(f'products/{product.woocommerce_product_id}', data={'stock_quantity': product.qty_available}).json()

                # Update the stock date updated
                product.write({'product_stock_date_updated': self.datetime_convert(woocommerce_product['date_modified_gmt'])})

    def product_variations_stock_retrieve(self, woocommerce_sync_config, woocommerce_api, product):
        """Retrieve WooCommerce stock info for a product variation."""
        try:
            variations = self.woocommerce_api_get_all_items(
                woocommerce_api,
                endpoint=f'products/{product.woocommerce_product_variation_parent_id}/variations',
                search_parameters={'status': 'publish', 'manage_stock': 'true'},
            )
            for variation in variations:
                if variation['id'] == int(product.woocommerce_product_variation_id):
                    return {'stock_quantity': variation['stock_quantity'], 'date_modified_gmt': variation['date_modified_gmt']}
        except Exception as error:
            _logger.error(f'Error retrieving variation stock for product {product.id}: {error}')
        return None

    def woocommerce_product_fields(self, woocommerce_sync_config, woocommerce_product, woocommerce_currency=None, woocommerce_weight_unit=None, woocommerce_dimension_unit=None, woocommerce_tax_rates=None):
        # WooCommerce site URL field
        product_values = {
            'woocommerce_product_site_url': woocommerce_sync_config.settings_woocommerce_connection_url,
        }

        # WooCommerce REST API - Common fields for Products and Product Variants
        product_values.update({'woocommerce_product_type': woocommerce_product['type']})

        # WooCommerce REST API - Product properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#product-properties
        product_values.update(
            {
                'woocommerce_product_id': woocommerce_product['id'],
                'woocommerce_product_name': woocommerce_product['name'],
                'woocommerce_product_slug': woocommerce_product['slug'],
                'woocommerce_product_permalink': woocommerce_product['permalink'],
                'woocommerce_product_date_created': woocommerce_product['date_created'],
                'woocommerce_product_date_created_gmt': woocommerce_product['date_created_gmt'],
                'woocommerce_product_date_modified': woocommerce_product['date_modified'],
                'woocommerce_product_date_modified_gmt': woocommerce_product['date_modified_gmt'],
                'woocommerce_product_status': woocommerce_product['status'],
                'woocommerce_product_featured': woocommerce_product['featured'],
                'woocommerce_product_catalog_visibility': woocommerce_product['catalog_visibility'],
                'woocommerce_product_description': woocommerce_product['description'],
                'woocommerce_product_short_description': woocommerce_product['short_description'],
                'woocommerce_product_sku': woocommerce_product['sku'],
                'woocommerce_product_price': woocommerce_product['price'],
                'woocommerce_product_regular_price': woocommerce_product['regular_price'],
                'woocommerce_product_sale_price': woocommerce_product['sale_price'],
                'woocommerce_product_date_on_sale_from': woocommerce_product['date_on_sale_from'],
                'woocommerce_product_date_on_sale_from_gmt': woocommerce_product['date_on_sale_from_gmt'],
                'woocommerce_product_date_on_sale_to': woocommerce_product['date_on_sale_to'],
                'woocommerce_product_date_on_sale_to_gmt': woocommerce_product['date_on_sale_to_gmt'],
                'woocommerce_product_price_html': woocommerce_product['price_html'],
                'woocommerce_product_on_sale': woocommerce_product['on_sale'],
                'woocommerce_product_purchasable': woocommerce_product['purchasable'],
                'woocommerce_product_total_sales': woocommerce_product['total_sales'],
                'woocommerce_product_virtual': woocommerce_product['virtual'],
                'woocommerce_product_downloadable': woocommerce_product['downloadable'],
                'woocommerce_product_downloads': woocommerce_product['downloads'],
                'woocommerce_product_download_limit': woocommerce_product['download_limit'],
                'woocommerce_product_download_expiry': woocommerce_product['download_expiry'],
                'woocommerce_product_external_url': woocommerce_product['external_url'],
                'woocommerce_product_button_text': woocommerce_product['button_text'],
                'woocommerce_product_tax_status': woocommerce_product['tax_status'],
                'woocommerce_product_tax_class': woocommerce_product['tax_class'],
                'woocommerce_product_manage_stock': woocommerce_product['manage_stock'],
                'woocommerce_product_stock_quantity': woocommerce_product['stock_quantity'],
                'woocommerce_product_stock_status': woocommerce_product['stock_status'],
                'woocommerce_product_backorders': woocommerce_product['backorders'],
                'woocommerce_product_backorders_allowed': woocommerce_product['backorders_allowed'],
                'woocommerce_product_backordered': woocommerce_product['backordered'],
                'woocommerce_product_sold_individually': woocommerce_product['sold_individually'],
                'woocommerce_product_weight': woocommerce_product['weight'],
                'woocommerce_product_dimensions': woocommerce_product['dimensions'],
                'woocommerce_product_shipping_required': woocommerce_product['shipping_required'],
                'woocommerce_product_shipping_taxable': woocommerce_product['shipping_taxable'],
                'woocommerce_product_shipping_class': woocommerce_product['shipping_class'],
                'woocommerce_product_shipping_class_id': woocommerce_product['shipping_class_id'],
                'woocommerce_product_reviews_allowed': woocommerce_product['reviews_allowed'],
                'woocommerce_product_average_rating': woocommerce_product['average_rating'],
                'woocommerce_product_rating_count': woocommerce_product['rating_count'],
                'woocommerce_product_related_ids': woocommerce_product['related_ids'],
                'woocommerce_product_upsell_ids': woocommerce_product['upsell_ids'],
                'woocommerce_product_cross_sell_ids': woocommerce_product['cross_sell_ids'],
                'woocommerce_product_parent_id': woocommerce_product['parent_id'],
                'woocommerce_product_purchase_note': woocommerce_product['purchase_note'],
                'woocommerce_product_categories': woocommerce_product['categories'],
                'woocommerce_product_tags': woocommerce_product['tags'],
                'woocommerce_product_images': woocommerce_product['images'],
                'woocommerce_product_attributes': woocommerce_product['attributes'],
                'woocommerce_product_default_attributes': woocommerce_product['default_attributes'],
                'woocommerce_product_variations': woocommerce_product['variations'],
                'woocommerce_product_grouped_products': woocommerce_product['grouped_products'],
                'woocommerce_product_menu_order': woocommerce_product['menu_order'],
                'woocommerce_product_meta_data': woocommerce_product['meta_data'],
            },
        )

        # WooCommerce REST API - Fields not mentioned in the documentation
        product_values.update(
            {
                'woocommerce_product_brands': woocommerce_product['brands'],
            },
        )

        # Additional fields
        product_values.update(
            {
                'woocommerce_product_currency': woocommerce_currency if woocommerce_currency else None,
                'woocommerce_product_weight_unit': woocommerce_weight_unit if woocommerce_weight_unit else None,
                'woocommerce_product_dimension_unit': woocommerce_dimension_unit if woocommerce_dimension_unit else None,
                'woocommerce_product_tax_rate': woocommerce_tax_rates.get(woocommerce_product['tax_class'] if woocommerce_product['tax_class'] else 'standard') if woocommerce_tax_rates else None,
            },
        )

        # Custom fields
        product_values.update(
            {
                'product_sync_to_woocommerce': True,
                'product_source': 'WooCommerce',
                'product_language_code': woocommerce_product.get('lang', None),  # Polylang field
                'woocommerce_product_service': False,  # woocommerce_product.get('service', False), # Germanized field - https://vendidero.de/doc/woocommerce-germanized/products-rest-api
            },
        )

        # Loop through the explicitly defined columns
        for column in [
            'woocommerce_product_date_created',
            'woocommerce_product_date_created_gmt',
            'woocommerce_product_date_modified',
            'woocommerce_product_date_modified_gmt',
            'woocommerce_product_date_on_sale_from',
            'woocommerce_product_date_on_sale_from_gmt',
            'woocommerce_product_date_on_sale_to',
            'woocommerce_product_date_on_sale_to_gmt',
        ]:
            if column in product_values and product_values[column]:
                product_values[column] = self.datetime_convert(product_values[column])

        return product_values

    def woocommerce_to_odoo_products_sync(
        self,
        woocommerce_sync_config,
        woocommerce_api,
        woocommerce_currency,
        woocommerce_tax_rates,
        woocommerce_product_prices_include_tax,
        woocommerce_weight_unit,
        woocommerce_dimension_unit,
    ):
        # WooCommerce REST API parameters
        search_parameters = {'status': 'publish'}

        if woocommerce_sync_config.settings_woocommerce_modified_records_import:
            woocommerce_last_execution_datetime = self.woocommerce_last_execution_datetime()
            if woocommerce_last_execution_datetime:
                search_parameters['modified_after'] = woocommerce_last_execution_datetime.strftime('%Y-%m-%dT%H:%M:%S')  # ISO 8601 date format

        if woocommerce_sync_config.settings_woocommerce_to_odoo_products_language_code:
            search_parameters['lang'] = woocommerce_sync_config.settings_woocommerce_to_odoo_products_language_code

        # WooCommerce woocommerce_products
        woocommerce_products = self.woocommerce_api_get_all_items(woocommerce_api, endpoint='products', search_parameters=search_parameters)

        # Filter for WooCommerce products that have SKU
        woocommerce_products = [product for product in woocommerce_products if product['sku']]

        for product in woocommerce_products:
            try:
                # Search for existing product in Odoo
                odoo_product = self.env['product.template'].search(
                    [('woocommerce_product_site_url', '=', woocommerce_sync_config.settings_woocommerce_connection_url), ('active', '=', True), ('woocommerce_product_id', '=', product['id'])],
                    limit=1,
                )

                # If the product exists, check the 'manage_stock' field
                if odoo_product:
                    if odoo_product.woocommerce_product_manage_stock != product['manage_stock']:
                        # Remove the product from Odoo so it can be re-imported fresh
                        odoo_product.unlink()
                        odoo_product = False

                # Create new product in Odoo if it does not yet exist or update product in Odoo only if WooCommerce version is newer
                if not odoo_product or (odoo_product and self.datetime_convert(product['date_modified_gmt']) > odoo_product.write_date):
                    product_values = self.woocommerce_product_fields(woocommerce_sync_config, product, woocommerce_currency, woocommerce_weight_unit, woocommerce_dimension_unit, woocommerce_tax_rates)

                    # Currency
                    if product_values['woocommerce_product_currency']:
                        odoo_product_currency = self.odoo_currency_retrieve(product_values['woocommerce_product_currency'])

                    # Tax
                    if product_values['woocommerce_product_tax_rate']:
                        odoo_product_tax = self.odoo_tax_create_or_retrieve(product_values['woocommerce_product_tax_rate'], woocommerce_product_prices_include_tax)
                        if odoo_product_tax:
                            odoo_product_tax_id = [(6, 0, [odoo_product_tax.id])]
                        else:
                            odoo_product_tax_id = []

                    # Brand (requires 'product_brand' add-on)
                    if self.env['ir.module.module'].search([('name', '=', 'product_brand'), ('state', '=', 'installed')], limit=1):
                        odoo_product_brands_ids = []
                        for brand in product_values['woocommerce_product_brands']:
                            odoo_brand = self.odoo_brand_create_or_retrieve(brand['name'])
                            if odoo_brand:
                                odoo_product_brands_ids.append(odoo_brand.id)

                        product_values.update({'product_brand_id': odoo_product_brands_ids[0] if odoo_product_brands_ids else False})

                    # Category
                    odoo_product_categories_ids = []
                    for category in product_values['woocommerce_product_categories']:
                        odoo_category = self.odoo_category_create_or_retrieve(category['name'])
                        if odoo_category:
                            odoo_product_categories_ids.append(odoo_category.id)

                    # Categories (requires 'product_multi_category' add-on)
                    if self.env['ir.module.module'].search([('name', '=', 'product_multi_category'), ('state', '=', 'installed')], limit=1):
                        product_values.update({'categ_ids': [(6, 0, odoo_product_categories_ids)]})

                    # Tags
                    odoo_product_tags_ids = []
                    for tag in product_values['woocommerce_product_tags']:
                        odoo_tag = self.odoo_tag_create_or_retrieve(tag['name'])
                        if odoo_tag:
                            odoo_product_tags_ids.append(odoo_tag.id)

                    # Unit of measure
                    if product_values['woocommerce_product_weight_unit']:
                        odoo_product_unit_of_measure = self.odoo_unit_of_measure_create_or_retrieve(product_values['woocommerce_product_weight_unit'])

                    # Dimensions (requires 'product_dimension' add-on)
                    if self.env['ir.module.module'].search([('name', '=', 'product_dimension'), ('state', '=', 'installed')], limit=1):
                        odoo_product_unit_of_measure_dimension = self.odoo_unit_of_measure_dimension_retrieve(product_values['woocommerce_product_dimension_unit'])

                        if odoo_product_unit_of_measure_dimension:
                            odoo_product_unit_of_measure_dimension_id = odoo_product_unit_of_measure_dimension.id

                        product_values.update(
                            {
                                'dimensional_uom_id': odoo_product_unit_of_measure_dimension_id,
                                'product_length': product_values['woocommerce_product_dimensions']['length'],
                                'product_width': product_values['woocommerce_product_dimensions']['width'],
                                'product_height': product_values['woocommerce_product_dimensions']['height'],
                            },
                        )

                    # Image featured
                    if woocommerce_sync_config.settings_woocommerce_images_sync and len(product_values['woocommerce_product_images']) > 0:
                        odoo_product_image_featured = self.image_download_file_to_base64(product_values['woocommerce_product_images'][0])

                    else:
                        odoo_product_image_featured = None

                    # Odoo 'product.template' model fields
                    product_values.update(
                        {
                            # General information
                            'name': product_values['woocommerce_product_name'],
                            'image_1920': odoo_product_image_featured,
                            'default_code': product_values['woocommerce_product_sku'],
                            'create_date': product_values['woocommerce_product_date_created_gmt'],
                            # 'type': 'service' if product_values['woocommerce_product_service'] else 'product' if product_values['woocommerce_product_manage_stock'] else 'consu',
                            'detailed_type': 'service' if product_values['woocommerce_product_service'] else 'product' if product_values['woocommerce_product_manage_stock'] else 'consu',
                            'description': 'Imported via Odoo-WooCommerce Sync',
                            'description_sale': product_values['woocommerce_product_description'],
                            'responsible_id': woocommerce_sync_config.settings_woocommerce_user_responsible.id,
                            # Product status
                            'active': True if product_values['woocommerce_product_status'] == 'publish' else False,
                            'sale_ok': product_values['woocommerce_product_purchasable'],
                            # Pricing
                            'currency_id': odoo_product_currency.id,
                            'taxes_id': odoo_product_tax_id,
                            'invoice_policy': 'order',
                            'list_price': product_values['woocommerce_product_price'],
                            # Category and tags
                            'categ_id': odoo_product_categories_ids[0] if odoo_product_categories_ids else False,
                            'product_tag_ids': [(6, 0, odoo_product_tags_ids)],
                            # Variations and attributes
                            'is_product_variant': True if product_values['woocommerce_product_type'] == 'variation' else False,
                            'has_configurable_attributes': True if product_values['woocommerce_product_type'] == 'variation' and len(attribute_value_ids or []) > 0 else False,
                            # Dimensions
                            'weight': product_values['woocommerce_product_weight'],
                            'uom_id': odoo_product_unit_of_measure.id if odoo_product_unit_of_measure else False,
                            'uom_po_id': odoo_product_unit_of_measure.id if odoo_product_unit_of_measure else False,
                            'volume': (
                                float(product_values['woocommerce_product_dimensions']['length'])
                                * float(product_values['woocommerce_product_dimensions']['width'])
                                * float(product_values['woocommerce_product_dimensions']['height'])
                                if (product_values['woocommerce_product_dimensions']['length'] and product_values['woocommerce_product_dimensions']['width'] and product_values['woocommerce_product_dimensions']['height'])
                                else False
                            ),
                        },
                    )

                    # Update product in Odoo only if WooCommerce version is newer
                    if odoo_product:
                        odoo_product.write(product_values)

                    # Create new product in Odoo if it does not yet exist
                    else:
                        odoo_product = self.env['product.template'].create(product_values)

                    # Product gallery
                    if odoo_product and woocommerce_sync_config.settings_woocommerce_images_sync and len(product_values['woocommerce_product_images']) > 0:
                        product_images_ids = self.image_process_attachments(product_values['woocommerce_product_images'], odoo_product)

                        if product_images_ids:
                            odoo_product.write({'product_images_ids': [(6, 0, product_images_ids)]})

                    # Commit changes
                    self.env.cr.commit()

            except Exception as error:
                # Roll back changes
                self.env.cr.rollback()
                _logger.exception(f'Error syncing product {product["id"]}: {error}')

    def woocommerce_to_odoo_product_related_ids(self, woocommerce_sync_config):
        # Retrieve all Odoo products
        odoo_products = self.env['product.template'].search([('woocommerce_product_site_url', '=', woocommerce_sync_config.settings_woocommerce_connection_url), ('active', '=', True)])

        for odoo_product in odoo_products:
            if len(odoo_product['woocommerce_product_related_ids'] or []) > 0:
                odoo_products_related_ids = []
                for product_related_id in odoo_product['woocommerce_product_related_ids']:
                    odoo_product_related = self.env['product.template'].search(
                        [('woocommerce_product_site_url', '=', woocommerce_sync_config.settings_woocommerce_connection_url), ('active', '=', True), ('woocommerce_product_id', '=', product_related_id)],
                        limit=1,
                    )

                    if odoo_product_related:
                        odoo_products_related_ids.append(odoo_product_related.id)

                # Update the optional_product_ids field for the current Odoo product
                if odoo_products_related_ids:
                    odoo_product.write({'optional_product_ids': [(6, 0, odoo_products_related_ids)]})

    def woocommerce_product_variation_fields(
        self,
        woocommerce_sync_config,
        woocommerce_product_variation,
        woocommerce_currency=None,
        woocommerce_weight_unit=None,
        woocommerce_dimension_unit=None,
        woocommerce_tax_rates=None,
    ):
        # WooCommerce site URL field
        product_variation_values = {
            'woocommerce_product_variation_site_url': woocommerce_sync_config.settings_woocommerce_connection_url,
        }

        # WooCommerce REST API - Common fields for Products and Product Variants
        product_variation_values.update(
            {
                'woocommerce_product_type': woocommerce_product_variation['type'],
            },
        )

        # WooCommerce REST API - Product variation properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#product-variation-properties
        product_variation_values.update(
            {
                'woocommerce_product_variation_id': woocommerce_product_variation['id'],
                'woocommerce_product_variation_name': woocommerce_product_variation['name'],
                'woocommerce_product_variation_permalink': woocommerce_product_variation['permalink'],
                'woocommerce_product_variation_date_created': woocommerce_product_variation['date_created'],
                'woocommerce_product_variation_date_created_gmt': woocommerce_product_variation['date_created_gmt'],
                'woocommerce_product_variation_date_modified': woocommerce_product_variation['date_modified'],
                'woocommerce_product_variation_date_modified_gmt': woocommerce_product_variation['date_modified_gmt'],
                'woocommerce_product_variation_status': woocommerce_product_variation['status'],
                'woocommerce_product_variation_description': woocommerce_product_variation['description'],
                'woocommerce_product_variation_sku': woocommerce_product_variation['sku'],
                'woocommerce_product_variation_price': woocommerce_product_variation['price'],
                'woocommerce_product_variation_regular_price': woocommerce_product_variation['regular_price'],
                'woocommerce_product_variation_sale_price': woocommerce_product_variation['sale_price'],
                'woocommerce_product_variation_date_on_sale_from': woocommerce_product_variation['date_on_sale_from'],
                'woocommerce_product_variation_date_on_sale_from_gmt': woocommerce_product_variation['date_on_sale_from_gmt'],
                'woocommerce_product_variation_date_on_sale_to': woocommerce_product_variation['date_on_sale_to'],
                'woocommerce_product_variation_date_on_sale_to_gmt': woocommerce_product_variation['date_on_sale_to_gmt'],
                'woocommerce_product_variation_on_sale': woocommerce_product_variation['on_sale'],
                'woocommerce_product_variation_purchasable': woocommerce_product_variation['purchasable'],
                'woocommerce_product_variation_virtual': woocommerce_product_variation['virtual'],
                'woocommerce_product_variation_downloadable': woocommerce_product_variation['downloadable'],
                'woocommerce_product_variation_downloads': woocommerce_product_variation['downloads'],
                'woocommerce_product_variation_download_limit': woocommerce_product_variation['download_limit'],
                'woocommerce_product_variation_download_expiry': woocommerce_product_variation['download_expiry'],
                'woocommerce_product_variation_tax_status': woocommerce_product_variation['tax_status'],
                'woocommerce_product_variation_tax_class': woocommerce_product_variation['tax_class'],
                'woocommerce_product_variation_manage_stock': woocommerce_product_variation['manage_stock'],
                'woocommerce_product_variation_stock_quantity': woocommerce_product_variation['stock_quantity'],
                'woocommerce_product_variation_stock_status': woocommerce_product_variation['stock_status'],
                'woocommerce_product_variation_backorders': woocommerce_product_variation['backorders'],
                'woocommerce_product_variation_backorders_allowed': woocommerce_product_variation['backorders_allowed'],
                'woocommerce_product_variation_backordered': woocommerce_product_variation['backordered'],
                'woocommerce_product_variation_weight': woocommerce_product_variation['weight'],
                'woocommerce_product_variation_dimensions': woocommerce_product_variation['dimensions'],
                'woocommerce_product_variation_shipping_class': woocommerce_product_variation['shipping_class'],
                'woocommerce_product_variation_shipping_class_id': woocommerce_product_variation['shipping_class_id'],
                'woocommerce_product_variation_image': woocommerce_product_variation['image'],
                'woocommerce_product_variation_attributes': woocommerce_product_variation['attributes'],
                'woocommerce_product_variation_menu_order': woocommerce_product_variation['menu_order'],
                'woocommerce_product_variation_meta_data': woocommerce_product_variation['meta_data'],
            },
        )

        # WooCommerce REST API - Fields not mentioned in the documentation
        product_variation_values.update(
            {
                'woocommerce_product_variation_parent_id': woocommerce_product_variation['parent_id'],
            },
        )

        # Additional fields
        product_variation_values.update(
            {
                'woocommerce_product_variation_currency': woocommerce_currency if woocommerce_currency else None,
                'woocommerce_product_variation_weight_unit': woocommerce_weight_unit if woocommerce_weight_unit else None,
                'woocommerce_product_variation_dimension_unit': woocommerce_dimension_unit if woocommerce_dimension_unit else None,
                'woocommerce_product_variation_tax_rate': woocommerce_tax_rates.get(woocommerce_product_variation['tax_class'] if woocommerce_product_variation['tax_class'] else 'standard')
                if woocommerce_tax_rates
                else None,
            },
        )

        # Custom fields
        product_variation_values.update(
            {
                'woocommerce_product_variation_service': False,  # product.get('service', False), # Germanized field - https://vendidero.de/doc/woocommerce-germanized/products-rest-api
            },
        )

        # Loop through the explicitly defined columns
        for column in [
            'woocommerce_product_variation_date_created',
            'woocommerce_product_variation_date_created_gmt',
            'woocommerce_product_variation_date_modified',
            'woocommerce_product_variation_date_modified_gmt',
            'woocommerce_product_variation_date_on_sale_from',
            'woocommerce_product_variation_date_on_sale_from_gmt',
            'woocommerce_product_variation_date_on_sale_to',
            'woocommerce_product_variation_date_on_sale_to_gmt',
        ]:
            if column in product_variation_values and product_variation_values[column]:
                product_variation_values[column] = self.datetime_convert(product_variation_values[column])

        return product_variation_values

    def woocommerce_to_odoo_products_variations_sync(
        self,
        woocommerce_sync_config,
        woocommerce_api,
        woocommerce_currency,
        woocommerce_tax_rates,
        woocommerce_product_prices_include_tax,
        woocommerce_weight_unit,
        woocommerce_dimension_unit,
    ):
        # WooCommerce REST API parameters
        search_parameters = {'status': 'publish', 'fields': 'id,variations', 'type': 'variable'}

        if woocommerce_sync_config.settings_woocommerce_modified_records_import:
            woocommerce_last_execution_datetime = self.woocommerce_last_execution_datetime()
            if woocommerce_last_execution_datetime:
                search_parameters['modified_after'] = woocommerce_last_execution_datetime.strftime('%Y-%m-%dT%H:%M:%S')  # ISO 8601 date format

        if woocommerce_sync_config.settings_woocommerce_to_odoo_products_language_code:
            search_parameters['lang'] = woocommerce_sync_config.settings_woocommerce_to_odoo_products_language_code

        # WooCommerce products
        woocommerce_products = self.woocommerce_api_get_all_items(woocommerce_api, endpoint='products', search_parameters=search_parameters)

        # Filter for WooCommerce products that have SKU
        woocommerce_products = [product for product in woocommerce_products if product['sku']]

        for product in woocommerce_products:
            try:
                # Search for existing product in Odoo
                odoo_product = self.env['product.template'].search(
                    [('woocommerce_product_site_url', '=', woocommerce_sync_config.settings_woocommerce_connection_url), ('active', '=', True), ('woocommerce_product_id', '=', product['id'])],
                    limit=1,
                )
                if not odoo_product:
                    _logger.warning(f"Product template for WooCommerce product '{product['name']}' not found.")
                    continue

                if odoo_product:
                    # Store 'product.template' SKU
                    odoo_product_sku = odoo_product.default_code

                    # WooCommerce REST API parameters
                    search_parameters = {'status': 'publish'}

                    if woocommerce_sync_config.settings_woocommerce_modified_records_import:
                        if woocommerce_last_execution_datetime:
                            search_parameters['modified_after'] = woocommerce_last_execution_datetime.strftime('%Y-%m-%dT%H:%M:%S')  # ISO 8601 date format

                    # WooCommerce product variations for the product
                    woocommerce_product_variations = self.woocommerce_api_get_all_items(woocommerce_api, endpoint=f'products/{product["id"]}/variations', search_parameters=search_parameters)

                    for product_variation in woocommerce_product_variations:
                        product_variation_values = self.woocommerce_product_variation_fields(
                            woocommerce_sync_config,
                            product_variation,
                            woocommerce_currency,
                            woocommerce_weight_unit,
                            woocommerce_dimension_unit,
                            woocommerce_tax_rates,
                        )

                        # Currency
                        if product_variation_values['woocommerce_product_variation_currency']:
                            odoo_product_variation_currency = self.odoo_currency_retrieve(product_variation_values['woocommerce_product_variation_currency'])

                        # Tax
                        if product_variation_values['woocommerce_product_variation_tax_rate']:
                            odoo_product_variation_tax = self.odoo_tax_create_or_retrieve(product_variation_values['woocommerce_product_variation_tax_rate'], woocommerce_product_prices_include_tax)
                            if odoo_product_variation_tax:
                                odoo_product_variation_tax_id = [(6, 0, [odoo_product_variation_tax.id])]
                            else:
                                odoo_product_variation_tax_id = []

                        # Unit of measure
                        if product_variation_values['woocommerce_product_variation_weight_unit']:
                            odoo_product_variation_unit_of_measure = self.odoo_unit_of_measure_create_or_retrieve(product_variation_values['woocommerce_product_variation_weight_unit'])

                        # Image featured
                        if woocommerce_sync_config.settings_woocommerce_images_sync and product_variation_values['woocommerce_product_variation_image'] is not None:
                            odoo_product_variation_image_featured = self.image_download_file_to_base64(product_variation_values['woocommerce_product_variation_image'])
                        else:
                            odoo_product_variation_image_featured = None

                        # Build a list of Odoo attribute value IDs from the WooCommerce variation attributes
                        attribute_value_ids = []
                        for attribute in product_variation_values['woocommerce_product_variation_attributes']:
                            if not attribute.get('name') or not attribute.get('option'):
                                continue

                            # Search for the product attribute (case-insensitive)
                            product_attribute = self.env['product.attribute'].search([('name', '=ilike', attribute.get('name'))], limit=1)
                            if not product_attribute:
                                # Create the attribute if it doesn't exist
                                product_attribute = self.env['product.attribute'].create({'name': attribute.get('name'), 'create_variant': 'always'})

                            # Search for the attribute value for this attribute
                            product_attr_value = self.env['product.attribute.value'].search([('name', '=ilike', attribute.get('option')), ('attribute_id', '=', product_attribute.id)], limit=1)

                            if not product_attr_value:
                                product_attr_value = self.env['product.attribute.value'].create({'name': attribute.get('option'), 'attribute_id': product_attribute.id})

                            attribute_value_ids.append(product_attr_value.id)

                            # Ensure the product template has an attribute line for this attribute
                            attribute_line = odoo_product.attribute_line_ids.filtered(lambda line: line.attribute_id.id == product_attribute.id)
                            if attribute_line:
                                # If the attribute value is not already in the line, add it
                                if product_attr_value not in attribute_line.value_ids:
                                    attribute_line.write({'value_ids': [(4, product_attr_value.id)]})

                            else:
                                # Create a new attribute line with this attribute value
                                self.env['product.template.attribute.line'].create({'product_tmpl_id': odoo_product.id, 'attribute_id': product_attribute.id, 'value_ids': [(6, 0, [product_attr_value.id])]})

                        # Odoo 'product.product' model fields
                        product_variation_values.update(
                            {
                                # General information
                                # 'name': product_variation_values['woocommerce_product_variation_name'], # Warning: Adding 'name' to a product variation will also affect its parent product
                                'image_1920': odoo_product_variation_image_featured,
                                'default_code': product_variation_values['woocommerce_product_variation_sku'],
                                'create_date': product_variation_values['woocommerce_product_variation_date_created_gmt'],
                                # 'type': ('service' if product_variation_values['woocommerce_product_variation_service'] else 'product' if product_variation_values['woocommerce_product_variation_manage_stock'] else 'consu'),
                                'detailed_type': (
                                    'service' if product_variation_values['woocommerce_product_variation_service'] else 'product' if product_variation_values['woocommerce_product_variation_manage_stock'] else 'consu'
                                ),
                                'description': 'Imported via Odoo-WooCommerce Sync',
                                'description_sale': product_variation_values['woocommerce_product_variation_description'],
                                # Product status
                                'active': True if product_variation_values['woocommerce_product_variation_status'] == 'publish' else False,
                                'sale_ok': product_variation_values['woocommerce_product_variation_purchasable'],
                                # Pricing
                                'currency_id': odoo_product_variation_currency.id,
                                'taxes_id': odoo_product_variation_tax_id,
                                'invoice_policy': 'order',
                                'list_price': product_variation_values['woocommerce_product_variation_price'],
                                # Variations and attributes
                                'is_product_variant': True if product_variation_values['woocommerce_product_type'] == 'variation' else False,
                                'has_configurable_attributes': True if product_variation_values['woocommerce_product_type'] == 'variation' and len(attribute_value_ids or []) > 0 else False,
                                # Dimensions
                                'weight': product_variation_values['woocommerce_product_variation_weight'],
                                'uom_id': odoo_product_variation_unit_of_measure.id if odoo_product_variation_unit_of_measure else False,
                                'volume': (
                                    float(product_variation_values['woocommerce_product_variation_dimensions']['length'])
                                    * float(product_variation_values['woocommerce_product_variation_dimensions']['width'])
                                    * float(product_variation_values['woocommerce_product_variation_dimensions']['height'])
                                    if (
                                        product_variation_values['woocommerce_product_variation_dimensions']['length']
                                        and product_variation_values['woocommerce_product_variation_dimensions']['width']
                                        and product_variation_values['woocommerce_product_variation_dimensions']['height']
                                    )
                                    else False
                                ),
                            },
                        )

                        # Update the product template so that all attribute lines are considered and variants are created
                        odoo_product._create_variant_ids()

                        # Locate the variant corresponding to this combination of attribute values
                        odoo_variant = False
                        for variant in odoo_product.product_variant_ids:
                            # Get the 'product.attribute.value' IDs associated with the variant's template attribute values
                            variant_attribute_value_ids = variant.product_template_attribute_value_ids.product_attribute_value_id.ids
                            # Compare the set of attribute values in the variant with the WooCommerce variation's attribute values
                            if set(variant_attribute_value_ids) == set(attribute_value_ids):
                                odoo_variant = variant
                                break

                        if not odoo_variant:
                            continue

                        # Update the variant with the WooCommerce values
                        odoo_variant.write(product_variation_values)

                        # Commit changes
                        self.env.cr.commit()

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
                _logger.exception(f'Error syncing product {product["id"]}: {error}')

    def woocommerce_to_odoo_customers_sync(self, woocommerce_sync_config, woocommerce_api):
        # WooCommerce REST API parameters
        search_parameters = {}

        if woocommerce_sync_config.settings_woocommerce_modified_records_import:
            woocommerce_last_execution_datetime = self.woocommerce_last_execution_datetime()
            if woocommerce_last_execution_datetime:
                search_parameters['modified_after'] = woocommerce_last_execution_datetime.strftime('%Y-%m-%dT%H:%M:%S')  # ISO 8601 date format

        # WooCommerce customers
        woocommerce_customers = self.woocommerce_api_get_all_items(woocommerce_api, endpoint='customers', search_parameters=search_parameters)

        for customer in woocommerce_customers:
            try:
                # Search for existing customer in Odoo
                odoo_customer = self.env['res.partner'].search(
                    [('woocommerce_customer_site_url', '=', woocommerce_sync_config.settings_woocommerce_connection_url), ('active', '=', True), ('woocommerce_customer_id', '=', customer['id'])],
                    limit=1,
                )

                # Create new customer in Odoo if it does not yet exist or update customer in Odoo only if WooCommerce version is newer
                if not odoo_customer or (odoo_customer and self.datetime_convert(customer['date_modified_gmt']) > odoo_customer.write_date):
                    # WooCommerce site URL field
                    customer_values = {
                        'woocommerce_customer_site_url': woocommerce_sync_config.settings_woocommerce_connection_url,
                    }

                    # WooCommerce REST API - Customer properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#customer-properties
                    customer_values.update(
                        {
                            'woocommerce_customer_id': customer['id'],
                            'woocommerce_customer_date_created': customer['date_created'],
                            'woocommerce_customer_date_created_gmt': customer['date_created_gmt'],
                            'woocommerce_customer_date_modified': customer['date_modified'],
                            'woocommerce_customer_date_modified_gmt': customer['date_modified_gmt'],
                            'woocommerce_customer_email': customer['email'],
                            'woocommerce_customer_first_name': customer['first_name'],
                            'woocommerce_customer_last_name': customer['last_name'],
                            'woocommerce_customer_role': customer['role'],
                            'woocommerce_customer_username': customer['username'],
                            'woocommerce_customer_is_paying_customer': customer['is_paying_customer'],
                            'woocommerce_customer_avatar_url': customer['avatar_url'],
                            'woocommerce_customer_meta_data': customer['meta_data'],
                        },
                    )

                    # WooCommerce REST API - Customer billing properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#customer-billing-properties
                    customer_values.update(
                        {
                            'woocommerce_customer_billing_first_name': customer['billing']['first_name'],
                            'woocommerce_customer_billing_last_name': customer['billing']['last_name'],
                            'woocommerce_customer_billing_company': customer['billing']['company'],
                            'woocommerce_customer_billing_address_1': customer['billing']['address_1'],
                            'woocommerce_customer_billing_address_2': customer['billing']['address_2'],
                            'woocommerce_customer_billing_city': customer['billing']['city'],
                            'woocommerce_customer_billing_state': customer['billing']['state'],
                            'woocommerce_customer_billing_postcode': customer['billing']['postcode'],
                            'woocommerce_customer_billing_country': customer['billing']['country'],
                            'woocommerce_customer_billing_email': customer['billing']['email'],
                            'woocommerce_customer_billing_phone': customer['billing']['phone'],
                        },
                    )

                    # WooCommerce REST API - Customer shipping properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#customer-shipping-properties
                    customer_values.update(
                        {
                            'woocommerce_customer_shipping_first_name': customer['shipping']['first_name'],
                            'woocommerce_customer_shipping_last_name': customer['shipping']['last_name'],
                            'woocommerce_customer_shipping_company': customer['shipping']['company'],
                            'woocommerce_customer_shipping_address_1': customer['shipping']['address_1'],
                            'woocommerce_customer_shipping_address_2': customer['shipping']['address_2'],
                            'woocommerce_customer_shipping_city': customer['shipping']['city'],
                            'woocommerce_customer_shipping_state': customer['shipping']['state'],
                            'woocommerce_customer_shipping_postcode': customer['shipping']['postcode'],
                            'woocommerce_customer_shipping_country': customer['shipping']['country'],
                        },
                    )

                    # Custom fields
                    customer_values.update(
                        {
                            'woocommerce_customer_date_last_login': datetime.fromtimestamp(int(meta['value']))
                            if (meta := next((meta for meta in customer['meta_data'] if meta.get('key') == 'wfls-last-login'), None))
                            else None,  # Wordfence Security field
                        },
                    )

                    # Loop through the explicitly defined date columns for conversion
                    for column in [
                        'woocommerce_customer_date_created',
                        'woocommerce_customer_date_created_gmt',
                        'woocommerce_customer_date_modified',
                        'woocommerce_customer_date_modified_gmt',
                    ]:
                        if column in customer_values and customer_values[column]:
                            customer_values[column] = self.datetime_convert(customer_values[column])

                    # Customer avatar
                    if woocommerce_sync_config.settings_woocommerce_images_sync and customer_values['woocommerce_customer_avatar_url'] != '':
                        odoo_avatar_url = self.image_download_file_to_base64({'src': customer_values['woocommerce_customer_avatar_url']})
                    else:
                        odoo_avatar_url = None

                    # Odoo 'res.partner' model fields
                    customer_values.update(
                        {
                            # General information
                            'name': f'{customer_values["woocommerce_customer_first_name"]} {customer_values["woocommerce_customer_last_name"]}',
                            'image_1920': odoo_avatar_url,
                            'ref': customer_values['woocommerce_customer_id'],
                            'create_date': customer_values['woocommerce_customer_date_created_gmt'],
                            'company_type': 'person',
                            'customer_rank': 1 if customer_values['woocommerce_customer_is_paying_customer'] else 0,
                            'email': customer_values['woocommerce_customer_email'],
                            'mobile': customer_values['woocommerce_customer_billing_phone'],
                            'user_id': woocommerce_sync_config.settings_woocommerce_user_responsible.id,
                            # Customer status
                            'active': True,
                            # Address
                            'street': customer_values['woocommerce_customer_billing_address_1'],
                            'street2': customer_values['woocommerce_customer_billing_address_2'],
                            'city': customer_values['woocommerce_customer_billing_city'],
                            'zip': customer_values['woocommerce_customer_billing_postcode'],
                            'country_id': self.env['res.country'].search([('code', '=', customer_values['woocommerce_customer_billing_country'])], limit=1).id,
                        },
                    )

                    # Update customer in Odoo only if WooCommerce version is newer
                    if odoo_customer:
                        if customer_values['woocommerce_customer_date_modified_gmt'] > odoo_customer.write_date:
                            odoo_customer.write(customer_values)

                    # Create new customer in Odoo if it does not yet exist
                    else:
                        odoo_customer = self.env['res.partner'].create(customer_values)

                    # Commit changes
                    self.env.cr.commit()

            except Exception as error:
                # Roll back changes
                self.env.cr.rollback()
                _logger.exception(f'Error syncing customer {customer["id"]}: {error}')

    def woocommerce_to_odoo_orders_sync(self, woocommerce_sync_config, woocommerce_api, woocommerce_tax_rates, woocommerce_weight_unit):
        # WooCommerce REST API parameters
        search_parameters = {}

        if woocommerce_sync_config.settings_woocommerce_modified_records_import:
            woocommerce_last_execution_datetime = self.woocommerce_last_execution_datetime()
            if woocommerce_last_execution_datetime:
                search_parameters['modified_after'] = woocommerce_last_execution_datetime.strftime('%Y-%m-%dT%H:%M:%S')  # ISO 8601 date format

        # WooCommerce orders
        woocommerce_orders = self.woocommerce_api_get_all_items(woocommerce_api, endpoint='orders', search_parameters=search_parameters)

        for order in woocommerce_orders:
            try:
                # Search for existing sale order in Odoo
                odoo_sale_order = self.env['sale.order'].search(
                    [('woocommerce_order_site_url', '=', woocommerce_sync_config.settings_woocommerce_connection_url), ('woocommerce_order_id', '=', order['id'])],
                    limit=1,
                )

                # Create new sale order in Odoo if it does not yet exist or update sale order in Odoo only if WooCommerce version is newer
                if not odoo_sale_order or (odoo_sale_order and self.datetime_convert(order['date_modified_gmt']) > odoo_sale_order.write_date):
                    # WooCommerce site URL field
                    order_values = {
                        'woocommerce_order_site_url': woocommerce_sync_config.settings_woocommerce_connection_url,
                    }

                    # WooCommerce REST API - Order properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#order-properties
                    order_values.update(
                        {
                            'woocommerce_order_id': order['id'],
                            'woocommerce_order_parent_id': order['parent_id'],
                            'woocommerce_order_number': order['number'],
                            'woocommerce_order_order_key': order['order_key'],
                            'woocommerce_order_created_via': order['created_via'],
                            'woocommerce_order_version': order['version'],
                            'woocommerce_order_status': order['status'],
                            'woocommerce_order_currency': order['currency'],
                            'woocommerce_order_date_created': order['date_created'],
                            'woocommerce_order_date_created_gmt': order['date_created_gmt'],
                            'woocommerce_order_date_modified': order['date_modified'],
                            'woocommerce_order_date_modified_gmt': order['date_modified_gmt'],
                            'woocommerce_order_discount_total': order['discount_total'],
                            'woocommerce_order_discount_tax': order['discount_tax'],
                            'woocommerce_order_shipping_total': order['shipping_total'],
                            'woocommerce_order_shipping_tax': order['shipping_tax'],
                            'woocommerce_order_cart_tax': order['cart_tax'],
                            'woocommerce_order_total': order['total'],
                            'woocommerce_order_total_tax': order['total_tax'],
                            'woocommerce_order_prices_include_tax': order['prices_include_tax'],
                            'woocommerce_order_customer_id': order['customer_id'],
                            'woocommerce_order_customer_ip_address': order['customer_ip_address'],
                            'woocommerce_order_customer_user_agent': order['customer_user_agent'],
                            'woocommerce_order_customer_note': order['customer_note'],
                            'woocommerce_order_payment_method': order['payment_method'],
                            'woocommerce_order_payment_method_title': order['payment_method_title'],
                            'woocommerce_order_transaction_id': order['transaction_id'],
                            'woocommerce_order_date_paid': order['date_paid'],
                            'woocommerce_order_date_paid_gmt': order['date_paid_gmt'],
                            'woocommerce_order_date_completed': order['date_completed'],
                            'woocommerce_order_date_completed_gmt': order['date_completed_gmt'],
                            'woocommerce_order_cart_hash': order['cart_hash'],
                            'woocommerce_order_meta_data': order['meta_data'],
                            'woocommerce_order_line_items': order['line_items'],
                            'woocommerce_order_tax_lines': order['tax_lines'],
                            'woocommerce_order_shipping_lines': order['shipping_lines'],
                            'woocommerce_order_fee_lines': order['fee_lines'],
                            'woocommerce_order_coupon_lines': order['coupon_lines'],
                            'woocommerce_order_refunds': order['refunds'],
                            # 'woocommerce_order_set_paid': order['set_paid'],
                        },
                    )

                    # WooCommerce REST API - Order billing properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#order-billing-properties
                    order_values.update(
                        {
                            'woocommerce_order_billing_first_name': order['billing']['first_name'],
                            'woocommerce_order_billing_last_name': order['billing']['last_name'],
                            'woocommerce_order_billing_company': order['billing']['company'],
                            'woocommerce_order_billing_address_1': order['billing']['address_1'],
                            'woocommerce_order_billing_address_2': order['billing']['address_2'],
                            'woocommerce_order_billing_city': order['billing']['city'],
                            'woocommerce_order_billing_state': order['billing']['state'],
                            'woocommerce_order_billing_postcode': order['billing']['postcode'],
                            'woocommerce_order_billing_country': order['billing']['country'],
                            'woocommerce_order_billing_email': order['billing']['email'],
                            'woocommerce_order_billing_phone': order['billing']['phone'],
                        },
                    )

                    # WooCommerce REST API - Order shipping properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#order-shipping-properties
                    order_values.update(
                        {
                            'woocommerce_order_shipping_first_name': order['shipping']['first_name'],
                            'woocommerce_order_shipping_last_name': order['shipping']['last_name'],
                            'woocommerce_order_shipping_company': order['shipping']['company'],
                            'woocommerce_order_shipping_address_1': order['shipping']['address_1'],
                            'woocommerce_order_shipping_address_2': order['shipping']['address_2'],
                            'woocommerce_order_shipping_city': order['shipping']['city'],
                            'woocommerce_order_shipping_state': order['shipping']['state'],
                            'woocommerce_order_shipping_postcode': order['shipping']['postcode'],
                            'woocommerce_order_shipping_country': order['shipping']['country'],
                        },
                    )

                    # Fees
                    woocommerce_order_transaction_fee = None

                    ## PayPal
                    woocommerce_order_transaction_fee_paypal = next((item['value'] for item in order_values['woocommerce_order_meta_data'] if item.get('key') == 'PayPal Transaction Fee'), None)

                    ## Stripe
                    woocommerce_order_transaction_fee_stripe = next((item['value'] for item in order_values['woocommerce_order_meta_data'] if item.get('key') == '_stripe_fee'), None)

                    woocommerce_order_transaction_fee = woocommerce_order_transaction_fee_paypal or woocommerce_order_transaction_fee_stripe

                    # Custom fields
                    if woocommerce_order_transaction_fee:
                        order_values.update(
                            {
                                'woocommerce_order_transaction_fee': woocommerce_order_transaction_fee,
                            },
                        )
                    order_values.update(
                        {
                            'order_language_code': order.get('lang', None),  # Polylang field
                        },
                    )

                    # Loop through the explicitly defined date columns for conversion
                    for column in [
                        'woocommerce_order_date_created',
                        'woocommerce_order_date_created_gmt',
                        'woocommerce_order_date_modified',
                        'woocommerce_order_date_modified_gmt',
                        'woocommerce_order_date_paid',
                        'woocommerce_order_date_paid_gmt',
                        'woocommerce_order_date_completed',
                        'woocommerce_order_date_completed_gmt',
                    ]:
                        if column in order_values and order_values[column]:
                            order_values[column] = self.datetime_convert(order_values[column])

                    # Currency
                    if order_values['woocommerce_order_currency']:
                        odoo_order_currency = self.odoo_currency_retrieve(order_values['woocommerce_order_currency'])

                    # Odoo Customer ID
                    odoo_customer = self.env['res.partner'].search(
                        [
                            ('woocommerce_customer_site_url', '=', woocommerce_sync_config.settings_woocommerce_connection_url),
                            ('active', '=', True),
                            ('woocommerce_customer_id', '=', order_values['woocommerce_order_customer_id']),
                        ],
                        limit=1,
                    )

                    if not odoo_customer:
                        if woocommerce_sync_config.settings_woocommerce_orders_customers_map:
                            customer_values = {
                                'woocommerce_customer_id': order_values['woocommerce_order_customer_id'],
                            }

                            # WooCommerce REST API - Customer billing properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#customer-billing-properties
                            customer_values.update(
                                {
                                    'woocommerce_customer_billing_first_name': order_values['woocommerce_order_billing_first_name'],
                                    'woocommerce_customer_billing_last_name': order_values['woocommerce_order_billing_last_name'],
                                    'woocommerce_customer_billing_company': order_values['woocommerce_order_billing_company'],
                                    'woocommerce_customer_billing_address_1': order_values['woocommerce_order_billing_address_1'],
                                    'woocommerce_customer_billing_address_2': order_values['woocommerce_order_billing_address_2'],
                                    'woocommerce_customer_billing_city': order_values['woocommerce_order_billing_city'],
                                    'woocommerce_customer_billing_state': order_values['woocommerce_order_billing_state'],
                                    'woocommerce_customer_billing_postcode': order_values['woocommerce_order_billing_postcode'],
                                    'woocommerce_customer_billing_country': order_values['woocommerce_order_billing_country'],
                                    'woocommerce_customer_billing_email': order_values['woocommerce_order_billing_email'],
                                    'woocommerce_customer_billing_phone': order_values['woocommerce_order_billing_phone'],
                                },
                            )

                            # WooCommerce REST API - Customer shipping properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#customer-shipping-properties
                            customer_values.update(
                                {
                                    'woocommerce_customer_shipping_first_name': order_values['woocommerce_order_shipping_first_name'],
                                    'woocommerce_customer_shipping_last_name': order_values['woocommerce_order_shipping_last_name'],
                                    'woocommerce_customer_shipping_company': order_values['woocommerce_order_shipping_company'],
                                    'woocommerce_customer_shipping_address_1': order_values['woocommerce_order_shipping_address_1'],
                                    'woocommerce_customer_shipping_address_2': order_values['woocommerce_order_shipping_address_2'],
                                    'woocommerce_customer_shipping_city': order_values['woocommerce_order_shipping_city'],
                                    'woocommerce_customer_shipping_state': order_values['woocommerce_order_shipping_state'],
                                    'woocommerce_customer_shipping_postcode': order_values['woocommerce_order_shipping_postcode'],
                                    'woocommerce_customer_shipping_country': order_values['woocommerce_order_shipping_country'],
                                },
                            )

                            # Odoo 'res.partner' model fields
                            customer_values.update(
                                {
                                    # General information
                                    'name': f'{customer_values["woocommerce_customer_billing_first_name"]} {customer_values["woocommerce_customer_billing_last_name"]}',
                                    'ref': customer_values['woocommerce_customer_id'],
                                    'company_type': 'person',
                                    'email': customer_values['woocommerce_customer_billing_email'],
                                    'mobile': customer_values['woocommerce_customer_billing_phone'],
                                    'user_id': woocommerce_sync_config.settings_woocommerce_user_responsible.id,
                                    # Customer status
                                    'active': True,
                                    # Address
                                    'street': customer_values['woocommerce_customer_billing_address_1'],
                                    'street2': customer_values['woocommerce_customer_billing_address_2'],
                                    'city': customer_values['woocommerce_customer_billing_city'],
                                    'zip': customer_values['woocommerce_customer_billing_postcode'],
                                    'country_id': self.env['res.country'].search([('code', '=', customer_values['woocommerce_customer_billing_country'])], limit=1).id,
                                },
                            )

                            # Check for duplicate email
                            if customer_values['email']:
                                odoo_customer = self.env['res.partner'].search(
                                    [('woocommerce_customer_site_url', '=', woocommerce_sync_config.settings_woocommerce_connection_url), ('active', '=', True), ('email', '=', customer_values['email'])],
                                    limit=1,
                                )

                                if not odoo_customer:
                                    odoo_customer = self.env['res.partner'].create(customer_values)

                        else:
                            # Create/retrieve customer placeholder
                            odoo_customer = self.odoo_customer_placeholder_create_or_retrieve()

                    # Odoo 'sale.order' model fields
                    order_values.update(
                        {
                            # General information
                            'name': f'#{order_values["woocommerce_order_number"]} {order_values["woocommerce_order_billing_first_name"]} {order_values["woocommerce_order_billing_last_name"]}',
                            'country_code': order_values['woocommerce_order_billing_country'],
                            'client_order_ref': order_values['woocommerce_order_number'],
                            'origin': order_values['woocommerce_order_created_via'],
                            'type_name': 'Sales Order',
                            'date_order': order_values['woocommerce_order_date_created_gmt'],
                            'state': (
                                'draft'
                                if order_values['woocommerce_order_status'] == 'pending'
                                else 'sale'
                                if order_values['woocommerce_order_status'] in ('processing', 'on-hold')
                                else 'done'
                                if order_values['woocommerce_order_status'] == 'completed'
                                else 'cancel'
                                if order_values['woocommerce_order_status'] in ('cancelled', 'refunded', 'failed', 'trash')
                                else 'draft'
                            ),
                            'note': order_values['woocommerce_order_customer_note'],
                            'user_id': woocommerce_sync_config.settings_woocommerce_user_responsible.id,
                            # Customer
                            'partner_id': odoo_customer.id,
                            'partner_invoice_id': odoo_customer.id,
                            'partner_shipping_id': odoo_customer.id,
                            # Shipping and stock
                            'picking_policy': 'direct',
                            # 'warehouse_id': woocommerce_sync_config.settings_woocommerce_products_warehouse_location.id,
                            # Payment
                            # 'currency_id': odoo_order_currency.id,
                            # 'tax_country_id': self.env['res.country'].search([('code', '=', order_values['woocommerce_customer_billing_country'])], limit=1).id,
                            # 'amount_tax': order_values['woocommerce_order_total_tax'],
                            # 'amount_total': order_values['woocommerce_order_total'],
                        },
                    )

                    # Create new sale order in Odoo if it does not yet exist
                    if not odoo_sale_order:
                        odoo_sale_order = self.env['sale.order'].create(order_values)

                    # Order line items
                    for line_item in order_values['woocommerce_order_line_items']:
                        # WooCommerce site URL field
                        order_line_values = {
                            'woocommerce_order_line_site_url': woocommerce_sync_config.settings_woocommerce_connection_url,
                        }

                        # WooCommerce REST API - Order line items properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#order-line-items-properties
                        order_line_values.update(
                            {
                                'woocommerce_order_line_item_id': line_item['id'],
                                'woocommerce_order_line_item_name': line_item['name'],
                                'woocommerce_order_line_item_product_id': line_item['product_id'],
                                'woocommerce_order_line_item_variation_id': line_item['variation_id'],
                                'woocommerce_order_line_item_quantity': line_item['quantity'],
                                'woocommerce_order_line_item_tax_class': woocommerce_tax_rates.get(line_item['tax_class'] if line_item['tax_class'] else 'standard'),
                                'woocommerce_order_line_item_subtotal': line_item['subtotal'],
                                'woocommerce_order_line_item_subtotal_tax': line_item['subtotal_tax'],
                                'woocommerce_order_line_item_total': line_item['total'],
                                'woocommerce_order_line_item_total_tax': line_item['total_tax'],
                                'woocommerce_order_line_item_taxes': line_item['taxes'],
                                'woocommerce_order_line_item_meta_data': line_item['meta_data'],
                                'woocommerce_order_line_item_sku': line_item['sku'],
                                'woocommerce_order_line_item_price': line_item['price'],
                            },
                        )

                        # Additional fields
                        order_line_values.update(
                            {
                                'woocommerce_order_line_item_weight_unit': woocommerce_weight_unit if woocommerce_weight_unit else None,
                            },
                        )

                        # Odoo Product ID
                        odoo_product_variation = None

                        if woocommerce_sync_config.settings_woocommerce_order_line_items_product_map:
                            # Product variation (for 'simple' products, Odoo still creates a single default variant under 'product.product' model)
                            odoo_product_variation = self.env['product.product'].search(
                                [
                                    ('woocommerce_product_variation_site_url', '=', woocommerce_sync_config.settings_woocommerce_connection_url),
                                    ('active', '=', True),
                                    ('woocommerce_product_id', '=', order_line_values['woocommerce_order_line_item_product_id']),
                                ],
                                limit=1,
                            )

                        if not odoo_product_variation:
                            # Create/retrieve product placeholder
                            odoo_product = self.odoo_product_placeholder_create_or_retrieve()

                        # Tax
                        if order_line_values['woocommerce_order_line_item_tax_class']:
                            odoo_order_line_item_tax = self.odoo_tax_create_or_retrieve(order_line_values['woocommerce_order_line_item_tax_class'], order_values['woocommerce_order_prices_include_tax'])
                            if odoo_order_line_item_tax:
                                odoo_order_line_item_tax_id = [(6, 0, [odoo_order_line_item_tax.id])]

                        # Unit of measure
                        if order_line_values['woocommerce_order_line_item_weight_unit']:
                            odoo_order_line_item_unit_of_measure = self.odoo_unit_of_measure_create_or_retrieve(order_line_values['woocommerce_order_line_item_weight_unit'])

                        # Odoo 'sale.order.line' model fields
                        order_line_values.update(
                            {
                                # General information
                                'order_id': odoo_sale_order.id,
                                'name': order_line_values['woocommerce_order_line_item_name'],
                                'product_id': odoo_product.product_variant_ids[:1].id,
                                # Shipping and stock
                                'warehouse_id': woocommerce_sync_config.settings_woocommerce_products_warehouse_location.id,
                                # Dimensions
                                'product_uom': odoo_order_line_item_unit_of_measure.id if odoo_order_line_item_unit_of_measure else False,
                                # Payment
                                'currency_id': odoo_order_currency.id,
                                'tax_id': odoo_order_line_item_tax_id,
                                'product_uom_qty': order_line_values['woocommerce_order_line_item_quantity'],
                                'price_unit': (
                                    order_line_values['woocommerce_order_line_item_price']
                                    + (float(order_line_values['woocommerce_order_line_item_subtotal_tax']) / order_line_values['woocommerce_order_line_item_quantity'])
                                    if order_values['woocommerce_order_prices_include_tax']
                                    else order_line_values['woocommerce_order_line_item_price']
                                ),
                                # 'discount'
                            },
                        )

                        odoo_sale_order_line = self.env['sale.order.line'].search(
                            [
                                ('woocommerce_order_line_site_url', '=', woocommerce_sync_config.settings_woocommerce_connection_url),
                                ('order_id', '=', odoo_sale_order.id),
                                ('woocommerce_order_line_item_id', '=', order_line_values['woocommerce_order_line_item_id']),
                            ],
                            limit=1,
                        )

                        if odoo_sale_order_line:
                            # Update the sale order line
                            odoo_sale_order_line.write(order_line_values)

                            # Update sale order
                            if odoo_sale_order:
                                odoo_sale_order.write(order_values)

                        else:
                            self.env['sale.order.line'].create(order_line_values)

                    # Commit changes
                    self.env.cr.commit()

            except Exception as error:
                # Roll back changes
                self.env.cr.rollback()
                _logger.exception(f'Error syncing order {order["id"]}: {error}')

    def woocommerce_attribute_create_or_retrieve(self, woocommerce_api, attribute_type, attribute_name, language_code=None):
        """Create or retrieve a WooCommerce attribute, brand, category or tag."""
        if not attribute_type and not attribute_name:
            return False

        search_parameters = {'search': attribute_name}
        if language_code is not None:
            search_parameters['lang'] = language_code

        woocommerce_attribute_values = self.woocommerce_api_get_all_items(woocommerce_api, endpoint=f'products/{attribute_type}', search_parameters=search_parameters)
        if woocommerce_attribute_values:
            return woocommerce_attribute_values[0]
        else:
            data = {'name': attribute_name}
            if language_code is not None:
                data['lang'] = language_code

            return woocommerce_api.post(f'products/{attribute_type}', data=data).json()

    def odoo_to_woocommerce_products_sync(
        self,
        woocommerce_sync_config,
        woocommerce_api,
        woocommerce_currency,
        woocommerce_tax_rates,
        woocommerce_product_prices_include_tax,
        woocommerce_weight_unit,
        woocommerce_dimension_unit,
    ):
        # Odoo search conditions
        search_conditions = [('active', '=', True), ('product_sync_to_woocommerce', '=', True), ('default_code', '!=', False)]

        if woocommerce_sync_config.settings_woocommerce_odoo_to_woocommerce_products_language_code:
            search_conditions.append(('product_language_code', '=', woocommerce_sync_config.settings_woocommerce_odoo_to_woocommerce_products_language_code))

        # Odoo products
        odoo_products = self.env['product.template'].search(search_conditions) | self.env['product.product'].search(search_conditions + [('product_tmpl_id.default_code', '!=', False)]).mapped('product_tmpl_id')

        for product in odoo_products:
            try:
                # Search for existing product in WooCommerce
                search_parameters = {'status': 'publish', 'sku': product.default_code}

                if woocommerce_sync_config.settings_woocommerce_to_odoo_products_language_code:
                    search_parameters['lang'] = woocommerce_sync_config.settings_woocommerce_to_odoo_products_language_code

                woocommerce_products = self.woocommerce_api_get_all_items(woocommerce_api, endpoint='products', search_parameters=search_parameters)
                woocommerce_product = woocommerce_products[0] if woocommerce_products else None

                # Create new product in WooCommerce if it does not yet exist or update product in WooCommerce only if Odoo version is newer
                if not woocommerce_product or (woocommerce_product and product.write_date > self.datetime_convert(woocommerce_product['date_modified_gmt'])):
                    product_values = {
                        'name': product.name,
                        'sku': product.default_code or '',
                        'date_created_gmt': product.create_date.strftime('%Y-%m-%dT%H:%M:%S') if product.create_date else None,
                        'description': product.description_sale if product.description_sale else None,
                        'status': 'publish' if product.active else 'draft',
                        'purchasable': product.sale_ok,
                        'tax_class': next((tax_class for tax_class, tax_amount in woocommerce_tax_rates.items() if product.taxes_id and product.taxes_id[0].amount == tax_amount), 'standard'),
                        'regular_price': f'{product.list_price:.2f}',
                        'manage_stock': True if product.detailed_type == 'product' else False,
                        'type': 'simple',
                        'weight': product.weight if product.weight != 0.0 else '',
                        'dimensions': {
                            'length': product.product_length if product.product_length != 0.0 else '',
                            'width': product.product_width if product.product_width != 0.0 else '',
                            'height': product.product_height if product.product_height != 0.0 else '',
                        },
                    }

                    # Check if product has multiple variants
                    if len(product.product_variant_ids) > 1:
                        product_values['type'] = 'variable'

                        woocommerce_attributes = []
                        for line in product.attribute_line_ids:
                            odoo_attributes = [value.name for value in line.value_ids]

                            for attribute in odoo_attributes:
                                woocommerce_attribute = self.woocommerce_attribute_create_or_retrieve(
                                    woocommerce_api,
                                    'attributes',
                                    attribute,
                                    product.product_language_code if product.product_language_code else None,
                                )
                                if woocommerce_attribute:
                                    woocommerce_attributes.append({'id': woocommerce_attribute['id'], 'name': line.attribute_id.name, 'variation': True, 'visible': True, 'options': odoo_attributes})

                            if woocommerce_attributes:
                                product_values['attributes'] = woocommerce_attributes

                    # Brand (requires 'product_brand' add-on)
                    if self.env['ir.module.module'].search([('name', '=', 'product_brand'), ('state', '=', 'installed')], limit=1) and len(product.product_brand_id) > 0:
                        woocommerce_brands = []
                        woocommerce_brand = self.woocommerce_attribute_create_or_retrieve(
                            woocommerce_api,
                            'brands',
                            product.product_brand_id.name,
                            product.product_language_code if product.product_language_code else None,
                        )
                        if woocommerce_brand:
                            woocommerce_brands.append({'id': woocommerce_brand['id']})

                        if len(woocommerce_brands) > 0:
                            product_values.update({'brands': woocommerce_brands})

                    # Categories
                    woocommerce_categories = []

                    ## 'categ_ids' (requires 'product_multi_category' add-on)
                    if self.env['ir.module.module'].search([('name', '=', 'product_multi_category'), ('state', '=', 'installed')], limit=1) and len(product.categ_ids) > 0:
                        for odoo_category in product.categ_ids:
                            woocommerce_category = self.woocommerce_attribute_create_or_retrieve(
                                woocommerce_api,
                                'categories',
                                odoo_category.name,
                                product.product_language_code if product.product_language_code else None,
                            )
                            if woocommerce_category:
                                woocommerce_categories.append({'id': woocommerce_category['id']})

                    ## 'categ_id'
                    if product.categ_id:
                        woocommerce_category = self.woocommerce_attribute_create_or_retrieve(
                            woocommerce_api,
                            'categories',
                            product.categ_id.name,
                            product.product_language_code if product.product_language_code else None,
                        )
                        if woocommerce_category:
                            woocommerce_categories.append({'id': woocommerce_category['id']})

                    woocommerce_categories = sorted({category['id'] for category in woocommerce_categories})

                    if len(woocommerce_categories) > 0:
                        product_values.update({'categories': [{'id': category_id} for category_id in woocommerce_categories]})

                    # Tags
                    woocommerce_tags = []

                    if len(product.product_tag_ids) > 0:
                        for odoo_tag in product.product_tag_ids:
                            woocommerce_tag = self.woocommerce_attribute_create_or_retrieve(woocommerce_api, 'tags', odoo_tag.name, product.product_language_code if product.product_language_code else None)
                            if woocommerce_tag:
                                woocommerce_tags.append({'id': woocommerce_tag['id']})

                        if len(woocommerce_tags) > 0:
                            product_values.update({'tags': woocommerce_tags})

                    # Language (requires Polylang)
                    if product.product_language_code:
                        product_values.update({'lang': product.product_language_code})

                    # Update product in WooCommerce only if Odoo version is newer
                    if woocommerce_product:
                        woocommerce_product = woocommerce_api.put(f'products/{woocommerce_product["id"]}', data=product_values).json()

                    # Create new product in WooCommerce if it does not yet exist
                    else:
                        woocommerce_product = woocommerce_api.post('products', data=product_values).json()

                        if woocommerce_product['id']:
                            product.write(
                                self.woocommerce_product_fields(woocommerce_sync_config, woocommerce_product, woocommerce_currency, woocommerce_weight_unit, woocommerce_dimension_unit, woocommerce_tax_rates),
                            )

                    if woocommerce_product:
                        _logger.info(f'WooCommerce response: {woocommerce_product}')

                    # For variable products, handle variations
                    if product_values.get('type') == 'variable':
                        # Retrieve existing variations from WooCommerce
                        woocommerce_product_variations = self.woocommerce_api_get_all_items(woocommerce_api, endpoint=f'products/{woocommerce_product["id"]}/variations', search_parameters={'status': 'publish'})

                        # Build a mapping by SKU for easier lookup
                        variations_by_sku = {variation.get('sku'): variation for variation in woocommerce_product_variations if variation.get('sku')}

                        for odoo_product_variant in product.product_variant_ids:
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
                                'manage_stock': True if product.detailed_type == 'product' else False,
                                'attributes': variation_attributes,
                            }

                            # Check if a variation with this SKU already exists
                            variation_existing = variations_by_sku.get(odoo_product_variant.default_code)
                            if variation_existing:
                                if odoo_product_variant.write_date > self.datetime_convert(variation_existing['date_modified_gmt']):
                                    # Update product variation in WooCommerce only if Odoo version is newer
                                    woocommerce_product_variant = woocommerce_api.put(f'products/{woocommerce_product["id"]}/variations/{variation_existing["id"]}', data=variation_data).json()

                                else:
                                    _logger.info(f'Variation {odoo_product_variant.default_code} for product {product.name} is up-to-date')

                            else:
                                # Create the product variation if it doesn't exist
                                woocommerce_product_variant = woocommerce_api.post(f'products/{woocommerce_product["id"]}/variations', data=variation_data).json()

                                if woocommerce_product_variant['id']:
                                    odoo_product_variant.write(
                                        self.woocommerce_product_variation_fields(
                                            woocommerce_sync_config,
                                            woocommerce_product_variant,
                                            woocommerce_currency,
                                            woocommerce_weight_unit,
                                            woocommerce_dimension_unit,
                                            woocommerce_tax_rates,
                                        ),
                                    )

                            if woocommerce_product_variant:
                                _logger.info(f'WooCommerce response: {woocommerce_product_variant}')

            except Exception as error:
                _logger.exception(f'Error syncing product {product.id} to WooCommerce: {error}')
