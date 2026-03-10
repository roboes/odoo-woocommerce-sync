from datetime import datetime
from typing import Any

from odoo import api, fields, models
from odoo.release import version_info


# Account Move Line
class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    # Override the existing 'product_id' field to add the ondelete cascade
    product_id = fields.Many2one(
        comodel_name='product.product',
        string='Product',
        inverse='_inverse_product_id',
        ondelete='cascade',
    )


# Delivery Carrier
class DeliveryCarrier(models.Model):
    _inherit = 'delivery.carrier'

    # Override the existing 'product_id' field to add the ondelete cascade
    product_id = fields.Many2one(comodel_name='product.product', string='Delivery Product', required=True, ondelete='cascade')

    woocommerce_site_url = fields.Char(string='WooCommerce Site URL', readonly=True, index=True)


# Stock
class StockQuant(models.Model):
    _inherit = 'stock.quant'

    # Override the existing 'product_id' field to add the ondelete cascade
    product_id = fields.Many2one(comodel_name='product.product', string='Product', domain=lambda self: self._domain_product_id(), required=True, index=True, check_company=True, ondelete='cascade')

    woocommerce_site_url = fields.Char(string='WooCommerce Site URL', readonly=True, index=True)

    stock_quantity_last_update = fields.Datetime(string='Stock Quantity Last Update', readonly=True, copy=False)

    def write(self, values: dict[str, Any]) -> bool:
        """Overrides the standard write method to update a timestamp only when the stock quantity is changed manually by a user."""
        if 'quantity' in values and not self.env.context.get('from_stock_move') and not self.env.context.get('from_external_sync'):
            values['stock_quantity_last_update'] = fields.Datetime.now()

        return super().write(values)


class StockMove(models.Model):
    _inherit = 'stock.move'

    # Override the existing 'product_id' field to add the ondelete cascade
    if version_info[0] == 16:
        product_id = fields.Many2one(
            comodel_name='product.product',
            string='Product',
            check_company=True,
            domain="[('type', 'in', ['product', 'consu']), '|', ('company_id', '=', False), ('company_id', '=', company_id)]",
            required=True,
            index=True,
            states={'done': [('readonly', True)]},
            ondelete='cascade',
        )

    elif version_info[0] in [18, 19]:
        product_id = fields.Many2one(
            comodel_name='product.product',
            string='Product',
            check_company=True,
            domain="[('type', '=', 'consu')]",
            required=True,
            index=True,
            ondelete='cascade',
        )


if version_info[0] in [16, 18]:

    class StockValuationLayer(models.Model):
        _inherit = 'stock.valuation.layer'

        # Override the existing 'product_id' field to add the ondelete cascade
        product_id = fields.Many2one(comodel_name='product.product', string='Product', index=True, domain=lambda self: self._domain_product_id(), required=True, check_company=True, ondelete='cascade')


# Product
class ProductTemplate(models.Model):
    _name = 'product.template'
    _inherit = ['product.template', 'base_multi_image.owner']
    # _inherit = 'product.template' # TODO Odoo v19

    # Override the existing 'default_code' field to remove the compute/inverse for multi-variant products, so it can be set manually
    default_code = fields.Char(string='Internal Reference', store=True)

    # WooCommerce REST API - Common fields for Products and Product Variants
    woocommerce_type = fields.Selection(
        selection=[('simple', 'Simple'), ('grouped', 'Grouped'), ('external', 'External'), ('variable', 'Variable'), ('variation', 'Variation')], string='Type', default='simple', readonly=True
    )

    # WooCommerce REST API - Product properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#product-properties
    woocommerce_id = fields.Char(string='WooCommerce Product ID', readonly=True, index=True)
    woocommerce_name = fields.Char(string='Name', readonly=True)
    woocommerce_slug = fields.Char(string='Slug', readonly=True)
    woocommerce_permalink = fields.Char(string='Permalink', readonly=True)
    woocommerce_date_created = fields.Datetime(string='Date Created', readonly=True)
    woocommerce_date_created_gmt = fields.Datetime(string='Date Created', readonly=True)
    woocommerce_date_modified = fields.Datetime(string='Date Modified', readonly=True)
    woocommerce_date_modified_gmt = fields.Datetime(string='Date Modified', readonly=True)
    woocommerce_status = fields.Selection(selection=[('draft', 'Draft'), ('pending', 'Pending'), ('private', 'Private'), ('publish', 'Published')], string='Status', readonly=True)
    woocommerce_featured = fields.Boolean(string='Featured', readonly=True)
    woocommerce_catalog_visibility = fields.Selection(selection=[('visible', 'Visible'), ('catalog', 'Catalog'), ('search', 'Search'), ('hidden', 'Hidden')], string='Catalog Visibility', readonly=True)
    woocommerce_description = fields.Html(string='Description', readonly=True)
    woocommerce_short_description = fields.Html(string='Short Description', readonly=True)
    woocommerce_sku = fields.Char(string='SKU', readonly=True)
    woocommerce_price = fields.Char(string='Price', readonly=True)
    woocommerce_regular_price = fields.Char(string='Regular Price', readonly=True)
    woocommerce_sale_price = fields.Char(string='Sale Price', readonly=True)
    woocommerce_date_on_sale_from = fields.Datetime(string='Sale Start Date', readonly=True)
    woocommerce_date_on_sale_from_gmt = fields.Datetime(string='Sale Start Date', readonly=True)
    woocommerce_date_on_sale_to = fields.Datetime(string='Sale End Date', readonly=True)
    woocommerce_date_on_sale_to_gmt = fields.Datetime(string='Sale End Date', readonly=True)
    woocommerce_price_html = fields.Char(string='Price HTML', readonly=True)
    woocommerce_on_sale = fields.Boolean(string='On Sale', readonly=True)
    woocommerce_purchasable = fields.Boolean(string='Purchasable', readonly=True)
    woocommerce_total_sales = fields.Integer(string='Total Sales', readonly=True)
    woocommerce_virtual = fields.Boolean(string='Virtual', readonly=True)
    woocommerce_downloadable = fields.Boolean(string='Downloadable', readonly=True)
    woocommerce_downloads = fields.Json(string='Downloads', readonly=True)
    woocommerce_download_limit = fields.Integer(string='Download Limit', readonly=True)
    woocommerce_download_expiry = fields.Integer(string='Download Expiry', readonly=True)
    woocommerce_external_url = fields.Char(string='External URL', readonly=True)
    woocommerce_button_text = fields.Char(string='Button Text', readonly=True)
    woocommerce_tax_status = fields.Selection(selection=[('taxable', 'Taxable'), ('shipping', 'Shipping'), ('none', 'None')], string='Tax Status', readonly=True)
    woocommerce_tax_class = fields.Char(string='Tax Class', readonly=True)
    woocommerce_manage_stock = fields.Boolean(string='Manage Stock', readonly=True)
    woocommerce_stock_quantity = fields.Integer(string='Stock Quantity', readonly=True)
    woocommerce_stock_status = fields.Selection(selection=[('instock', 'In Stock'), ('outofstock', 'Out of Stock'), ('onbackorder', 'On Backorder')], string='Stock Status', readonly=True)
    woocommerce_backorders = fields.Selection(selection=[('no', 'No'), ('notify', 'Notify'), ('yes', 'Yes')], string='Backorders', readonly=True)
    woocommerce_backorders_allowed = fields.Boolean(string='Backorders Allowed', readonly=True)
    woocommerce_backordered = fields.Boolean(string='Backordered', readonly=True)
    woocommerce_sold_individually = fields.Boolean(string='Sold Individually', readonly=True)
    woocommerce_weight = fields.Char(string='Weight', readonly=True)
    woocommerce_dimensions = fields.Json(string='Dimensions', readonly=True)
    woocommerce_shipping_required = fields.Boolean(string='Shipping Required', readonly=True)
    woocommerce_shipping_taxable = fields.Boolean(string='Shipping Taxable', readonly=True)
    woocommerce_shipping_class = fields.Char(string='Shipping Class', readonly=True)
    woocommerce_shipping_class_id = fields.Integer(string='Shipping Class ID', readonly=True)
    woocommerce_reviews_allowed = fields.Boolean(string='Reviews Allowed', readonly=True)
    woocommerce_average_rating = fields.Char(string='Average Rating', readonly=True)
    woocommerce_rating_count = fields.Integer(string='Rating Count', readonly=True)
    woocommerce_related_ids = fields.Text(string='Related Products', readonly=True)
    woocommerce_upsell_ids = fields.Text(string='Upsell Products', readonly=True)
    woocommerce_cross_sell_ids = fields.Text(string='Cross-Sell Products', readonly=True)
    woocommerce_parent_id = fields.Integer(string='Parent Product ID', readonly=True)
    woocommerce_purchase_note = fields.Text(string='Purchase Note', readonly=True)
    woocommerce_categories = fields.Json(string='Categories', readonly=True)
    woocommerce_tags = fields.Json(string='Tags', readonly=True)
    woocommerce_images = fields.Json(string='Images', readonly=True)
    woocommerce_attributes = fields.Json(string='Attributes', readonly=True)
    woocommerce_default_attributes = fields.Json(string='Default Attributes', readonly=True)
    woocommerce_variations = fields.Json(string='Variations', readonly=True)
    woocommerce_grouped_products = fields.Json(string='Grouped Products', readonly=True)
    woocommerce_menu_order = fields.Integer(string='Menu Order', readonly=True)
    woocommerce_meta_data = fields.Json(string='Meta Data', readonly=True)

    # WooCommerce REST API - Fields not mentioned in the documentation
    woocommerce_brands = fields.Json(string='Brands', readonly=True)

    # Additional fields
    woocommerce_currency = fields.Char(string='Currency', readonly=True)
    woocommerce_weight_unit = fields.Char(string='Weight Unit', readonly=True)
    woocommerce_dimension_unit = fields.Char(string='Dimension Unit', readonly=True)
    woocommerce_tax_rate = fields.Integer(string='Tax Rate', readonly=True)

    # Custom fields
    sync_to_woocommerce = fields.Boolean(string='Sync to WooCommerce', default=False)
    woocommerce_to_odoo_last_sync = fields.Datetime(string='WooCommerce to Odoo Last Sync', readonly=True)
    odoo_to_woocommerce_last_sync = fields.Datetime(string='Odoo to WooCommerce Last Sync', readonly=True)
    woocommerce_stock_last_sync = fields.Datetime(string='Stock Date Updated', readonly=True)
    source = fields.Char(string='Source', readonly=True)
    language_code = fields.Char(string='Language', help='2-digit ISO 639-1 language code.')

    woocommerce_service = fields.Boolean(string='Is service?')

    # Custom fields
    woocommerce_site_url = fields.Char(string='WooCommerce Site URL', readonly=True, index=True)

    def get_gallery_images(self):
        """Returns list of base64 binary images for the product gallery (excluding the main image_1920)."""
        self.ensure_one()
        if 'base_multi_image.image' in self.env:
            return [img.image_1920 for img in self.env['base_multi_image.image'].search([('owner_model', '=', 'product.template'), ('owner_id', '=', self.id)]).sorted('sequence') if img.image_1920]
        else:
            return [att.datas for att in self.env['ir.attachment'].search([('res_model', '=', 'product.template'), ('res_id', '=', self.id), ('mimetype', 'ilike', 'image')]) if att.datas]


# Product variations
class ProductProduct(models.Model):
    _inherit = 'product.product'

    # WooCommerce REST API - Product variation properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#product-variation-properties
    woocommerce_id = fields.Char(string='WooCommerce Product Variation ID', readonly=True, index=True)
    woocommerce_name = fields.Char(string='Name', readonly=True)
    woocommerce_permalink = fields.Char(string='Permalink', readonly=True)
    woocommerce_date_created = fields.Datetime(string='Date Created', readonly=True)
    woocommerce_date_created_gmt = fields.Datetime(string='Date Created', readonly=True)
    woocommerce_date_modified = fields.Datetime(string='Date Modified', readonly=True)
    woocommerce_date_modified_gmt = fields.Datetime(string='Date Modified', readonly=True)
    woocommerce_status = fields.Selection(selection=[('draft', 'Draft'), ('pending', 'Pending'), ('private', 'Private'), ('publish', 'Published')], string='Status', readonly=True)
    woocommerce_description = fields.Html(string='Description', readonly=True)
    woocommerce_sku = fields.Char(string='SKU', readonly=True)
    woocommerce_price = fields.Char(string='Price', readonly=True)
    woocommerce_regular_price = fields.Char(string='Regular Price', readonly=True)
    woocommerce_sale_price = fields.Char(string='Sale Price', readonly=True)
    woocommerce_date_on_sale_from = fields.Datetime(string='Sale Start Date', readonly=True)
    woocommerce_date_on_sale_from_gmt = fields.Datetime(string='Sale Start Date', readonly=True)
    woocommerce_date_on_sale_to = fields.Datetime(string='Sale End Date', readonly=True)
    woocommerce_date_on_sale_to_gmt = fields.Datetime(string='Sale End Date', readonly=True)
    woocommerce_on_sale = fields.Boolean(string='On Sale', readonly=True)
    woocommerce_purchasable = fields.Boolean(string='Purchasable', readonly=True)
    woocommerce_virtual = fields.Boolean(string='Virtual', readonly=True)
    woocommerce_downloadable = fields.Boolean(string='Downloadable', readonly=True)
    woocommerce_downloads = fields.Json(string='Downloads', readonly=True)
    woocommerce_download_limit = fields.Integer(string='Download Limit', readonly=True)
    woocommerce_download_expiry = fields.Integer(string='Download Expiry', readonly=True)
    woocommerce_tax_status = fields.Selection(selection=[('taxable', 'Taxable'), ('shipping', 'Shipping'), ('none', 'None')], string='Tax Status', readonly=True)
    woocommerce_tax_class = fields.Char(string='Tax Class', readonly=True)
    woocommerce_manage_stock = fields.Boolean(string='Manage Stock', readonly=True)
    woocommerce_stock_quantity = fields.Integer(string='Stock Quantity', readonly=True)
    woocommerce_stock_status = fields.Selection(selection=[('instock', 'In Stock'), ('outofstock', 'Out of Stock'), ('onbackorder', 'On Backorder')], string='Stock Status', readonly=True)
    woocommerce_backorders = fields.Selection(selection=[('no', 'No'), ('notify', 'Notify'), ('yes', 'Yes')], string='Backorders', readonly=True)
    woocommerce_backorders_allowed = fields.Boolean(string='Backorders Allowed', readonly=True)
    woocommerce_backordered = fields.Boolean(string='Backordered', readonly=True)
    woocommerce_weight = fields.Char(string='Weight', readonly=True)
    woocommerce_dimensions = fields.Json(string='Dimensions', readonly=True)
    woocommerce_shipping_class = fields.Char(string='Shipping Class', readonly=True)
    woocommerce_shipping_class_id = fields.Integer(string='Shipping Class ID', readonly=True)
    woocommerce_image = fields.Json(string='Images', readonly=True)
    woocommerce_attributes = fields.Json(string='Attributes', readonly=True)
    woocommerce_menu_order = fields.Integer(string='Menu Order', readonly=True)
    woocommerce_meta_data = fields.Json(string='Meta Data', readonly=True)

    # WooCommerce REST API - Fields not mentioned in the documentation
    woocommerce_parent_id = fields.Integer(string='Parent Product ID', readonly=True)

    # Additional fields
    woocommerce_currency = fields.Char(string='Currency', readonly=True)
    woocommerce_weight_unit = fields.Char(string='Weight Unit', readonly=True)
    woocommerce_dimension_unit = fields.Char(string='Dimension Unit', readonly=True)
    woocommerce_tax_rate = fields.Integer(string='Tax Rate', readonly=True)

    # Custom fields
    woocommerce_site_url = fields.Char(string='WooCommerce Site URL', readonly=True, index=True)
    woocommerce_to_odoo_last_sync = fields.Datetime(string='WooCommerce to Odoo Last Sync', readonly=True)
    odoo_to_woocommerce_last_sync = fields.Datetime(string='Odoo to WooCommerce Last Sync', readonly=True)
    woocommerce_stock_last_sync = fields.Datetime(string='Stock Date Updated', readonly=True)
    woocommerce_service = fields.Boolean(string='Is service?')

    def woocommerce_stock_last_sync_update(self: models.Model, timestamp: datetime) -> None:
        """Updates the 'woocommerce_stock_last_sync' field for both the product and its template directly via SQL to avoid updating the 'write_date'."""

        self.ensure_one()

        # Update the product.product record using a parameterized query
        self.env.cr.execute(query='UPDATE product_product SET woocommerce_stock_last_sync = %s WHERE id = %s', params=(timestamp, self.id))

        # Update the product.template record using a parameterized query
        self.env.cr.execute(query='UPDATE product_template SET woocommerce_stock_last_sync = %s WHERE id = %s', params=(timestamp, self.product_tmpl_id.id))

        # Invalidate the cache for the modified records to ensure consistency
        self.env['product.product']._invalidate_cache(ids=[self.id])
        self.env['product.template']._invalidate_cache(ids=[self.product_tmpl_id.id])


# Product attribute
class ProductAttribute(models.Model):
    _inherit = 'product.attribute'

    # Custom fields
    woocommerce_site_url = fields.Char(string='WooCommerce Site URL', readonly=True, index=True)


# Product attribute value
class ProductAttributeValue(models.Model):
    _inherit = 'product.attribute.value'

    # Custom fields
    woocommerce_site_url = fields.Char(string='WooCommerce Site URL', readonly=True, index=True)


# Product template attribute value
class ProductTemplateAttributeValue(models.Model):
    _inherit = 'product.template.attribute.value'

    # Custom fields
    woocommerce_site_url = fields.Char(string='WooCommerce Site URL', readonly=True, index=True)


# Product template attribute line
class ProductTemplateAttributeLine(models.Model):
    _inherit = 'product.template.attribute.line'

    # Custom fields
    woocommerce_site_url = fields.Char(string='WooCommerce Site URL', readonly=True, index=True)


# Customers
class ResPartner(models.Model):
    _inherit = 'res.partner'

    # WooCommerce REST API - Customer properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#customer-properties
    woocommerce_id = fields.Char(string='WooCommerce Customer ID', readonly=True, index=True)
    woocommerce_date_created = fields.Datetime(string='Created Date', readonly=True)
    woocommerce_date_created_gmt = fields.Datetime(string='Created Date', readonly=True)
    woocommerce_date_modified = fields.Datetime(string='Modified Date', readonly=True)
    woocommerce_date_modified_gmt = fields.Datetime(string='Modified Date', readonly=True)
    woocommerce_email = fields.Char(string='Email', readonly=True)
    woocommerce_first_name = fields.Char(string='First Name', readonly=True)
    woocommerce_last_name = fields.Char(string='Last Name', readonly=True)
    woocommerce_role = fields.Char(string='Role', readonly=True)
    woocommerce_username = fields.Char(string='Username', readonly=True)
    woocommerce_is_paying_customer = fields.Boolean(string='Is Paying Customer', readonly=True)
    woocommerce_avatar_url = fields.Char(string='Avatar URL', readonly=True)
    woocommerce_meta_data = fields.Json(string='Meta Data', readonly=True)

    # WooCommerce REST API - Customer billing properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#customer-billing-properties
    woocommerce_billing_first_name = fields.Char(string='Billing First Name', readonly=True)
    woocommerce_billing_last_name = fields.Char(string='Billing Last Name', readonly=True)
    woocommerce_billing_company = fields.Char(string='Billing Company', readonly=True)
    woocommerce_billing_address_1 = fields.Char(string='Billing Address 1', readonly=True)
    woocommerce_billing_address_2 = fields.Char(string='Billing Address 2', readonly=True)
    woocommerce_billing_city = fields.Char(string='Billing City', readonly=True)
    woocommerce_billing_state = fields.Char(string='Billing State', readonly=True)
    woocommerce_billing_postcode = fields.Char(string='Billing Postcode', readonly=True)
    woocommerce_billing_country = fields.Char(string='Billing Country', readonly=True)
    woocommerce_billing_email = fields.Char(string='Billing Email', readonly=True)
    woocommerce_billing_phone = fields.Char(string='Billing Phone', readonly=True)

    # WooCommerce REST API - Customer shipping properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#customer-shipping-properties
    woocommerce_shipping_first_name = fields.Char(string='Shipping First Name', readonly=True)
    woocommerce_shipping_last_name = fields.Char(string='Shipping Last Name', readonly=True)
    woocommerce_shipping_company = fields.Char(string='Shipping Company', readonly=True)
    woocommerce_shipping_address_1 = fields.Char(string='Shipping Address 1', readonly=True)
    woocommerce_shipping_address_2 = fields.Char(string='Shipping Address 2', readonly=True)
    woocommerce_shipping_city = fields.Char(string='Shipping City', readonly=True)
    woocommerce_shipping_state = fields.Char(string='Shipping State', readonly=True)
    woocommerce_shipping_postcode = fields.Char(string='Shipping Postcode', readonly=True)
    woocommerce_shipping_country = fields.Char(string='Shipping Country', readonly=True)

    # Custom fields
    woocommerce_site_url = fields.Char(string='WooCommerce Site URL', readonly=True, index=True)
    woocommerce_to_odoo_last_sync = fields.Datetime(string='WooCommerce to Odoo Last Sync', readonly=True)
    woocommerce_last_login_date = fields.Datetime(string='Last Login Date', readonly=True)  # Wordfence fields


# Orders
class SaleOrder(models.Model):
    _inherit = 'sale.order'

    # WooCommerce REST API - Order properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#order-properties
    woocommerce_id = fields.Char(string='WooCommerce Order ID', readonly=True, index=True)
    woocommerce_parent_id = fields.Char(string='Parent ID', readonly=True)
    woocommerce_number = fields.Char(string='Number', readonly=True)
    woocommerce_order_key = fields.Char(string='Key', readonly=True)
    woocommerce_created_via = fields.Char(string='Created Via', readonly=True)
    woocommerce_version = fields.Char(string='WooCommerce Version', readonly=True)
    woocommerce_status = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('processing', 'Processing'),
            ('on-hold', 'On Hold'),
            ('completed', 'Completed'),
            ('cancelled', 'Cancelled'),
            ('refunded', 'Refunded'),
            ('failed', 'Failed'),
            ('trash', 'Trash'),
        ],
        string='Status',
        readonly=True,
    )
    woocommerce_currency = fields.Char(string='Currency', readonly=True)
    woocommerce_date_created = fields.Datetime(string='Date Created', readonly=True)
    woocommerce_date_created_gmt = fields.Datetime(string='Date Created', readonly=True)
    woocommerce_date_modified = fields.Datetime(string='Date Modified', readonly=True)
    woocommerce_date_modified_gmt = fields.Datetime(string='Date Modified', readonly=True)
    woocommerce_discount_total = fields.Float(string='Discount Total', readonly=True)
    woocommerce_discount_tax = fields.Float(string='Discount Tax', readonly=True)
    woocommerce_shipping_total = fields.Float(string='Shipping Total', readonly=True)
    woocommerce_shipping_tax = fields.Float(string='Shipping Tax', readonly=True)
    woocommerce_cart_tax = fields.Float(string='Cart Tax', readonly=True)
    woocommerce_total = fields.Float(string='Grand Total', readonly=True)
    woocommerce_total_tax = fields.Float(string='Total Tax', readonly=True)
    woocommerce_prices_include_tax = fields.Boolean(string='Include Tax', readonly=True)
    woocommerce_customer_id = fields.Char(string='WooCommerce Customer ID', readonly=True)
    woocommerce_customer_ip_address = fields.Char(string='Customer IP Address', readonly=True)
    woocommerce_customer_user_agent = fields.Char(string='Customer User Agent', readonly=True)
    woocommerce_customer_note = fields.Char(string='Customer Note', readonly=True)
    woocommerce_payment_method = fields.Char(string='Payment Method', readonly=True)
    woocommerce_payment_method_title = fields.Char(string='Payment Method Title', readonly=True)
    woocommerce_transaction_id = fields.Char(string='Transaction ID', readonly=True)
    woocommerce_date_paid = fields.Datetime(string='Date Paid', readonly=True)
    woocommerce_date_paid_gmt = fields.Datetime(string='Date Paid', readonly=True)
    woocommerce_date_completed = fields.Datetime(string='Date Completed', readonly=True)
    woocommerce_date_completed_gmt = fields.Datetime(string='Date Completed', readonly=True)
    woocommerce_cart_hash = fields.Char(string='Cart Hash', readonly=True)
    woocommerce_meta_data = fields.Json(string='Meta Data', readonly=True)
    woocommerce_line_items = fields.Json(string='Line Items', readonly=True)
    woocommerce_tax_lines = fields.Json(string='Tax Lines', readonly=True)
    woocommerce_shipping_lines = fields.Json(string='Shipping Lines', readonly=True)
    woocommerce_fee_lines = fields.Json(string='Fee Lines', readonly=True)
    woocommerce_coupon_lines = fields.Json(string='Coupon Lines', readonly=True)
    woocommerce_refunds = fields.Json(string='Refunds', readonly=True)
    # woocommerce_set_paid = fields.Boolean(string='Set Paid', readonly=True)

    # WooCommerce REST API - Order billing properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#order-billing-properties
    woocommerce_billing_first_name = fields.Char(string='Billing First Name', readonly=True)
    woocommerce_billing_last_name = fields.Char(string='Billing Last Name', readonly=True)
    woocommerce_billing_company = fields.Char(string='Billing Company', readonly=True)
    woocommerce_billing_address_1 = fields.Char(string='Billing Address 1', readonly=True)
    woocommerce_billing_address_2 = fields.Char(string='Billing Address 2', readonly=True)
    woocommerce_billing_city = fields.Char(string='Billing City', readonly=True)
    woocommerce_billing_state = fields.Char(string='Billing State', readonly=True)
    woocommerce_billing_postcode = fields.Char(string='Billing Postcode', readonly=True)
    woocommerce_billing_country = fields.Char(string='Billing Country', readonly=True)
    woocommerce_billing_email = fields.Char(string='Billing Email', readonly=True)
    woocommerce_billing_phone = fields.Char(string='Billing Phone', readonly=True)

    # WooCommerce REST API - Order shipping properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#order-shipping-properties
    woocommerce_shipping_first_name = fields.Char(string='Shipping First Name', readonly=True)
    woocommerce_shipping_last_name = fields.Char(string='Shipping Last Name', readonly=True)
    woocommerce_shipping_company = fields.Char(string='Shipping Company', readonly=True)
    woocommerce_shipping_address_1 = fields.Char(string='Shipping Address 1', readonly=True)
    woocommerce_shipping_address_2 = fields.Char(string='Shipping Address 2', readonly=True)
    woocommerce_shipping_city = fields.Char(string='Shipping City', readonly=True)
    woocommerce_shipping_state = fields.Char(string='Shipping State', readonly=True)
    woocommerce_shipping_postcode = fields.Char(string='Shipping Postcode', readonly=True)
    woocommerce_shipping_country = fields.Char(string='Shipping Country', readonly=True)

    # Custom fields
    woocommerce_site_url = fields.Char(string='WooCommerce Site URL', readonly=True, index=True)
    woocommerce_to_odoo_last_sync = fields.Datetime(string='WooCommerce to Odoo Last Sync', readonly=True)
    woocommerce_transaction_fee = fields.Float(string='Transaction Fee', help='Transaction fees incurred from PayPal or Stripe.', readonly=True, default=0.0)
    language_code = fields.Char(string='Language', help='2-digit ISO 639-1 language code.', readonly=True)

    # Computed fields
    woocommerce_payout = fields.Float(string='Payout', help='Total - Order Transaction Fee.', compute='payout_compute', store=True, readonly=True)

    @api.depends('woocommerce_total', 'woocommerce_transaction_fee')
    def payout_compute(self: models.Model) -> None:
        for order in self:
            order.woocommerce_payout = order.woocommerce_total - order.woocommerce_transaction_fee

    @api.ondelete(at_uninstall=False)
    def _unlink_except_draft_or_cancel(self: models.Model) -> None:
        """Remove Odoo's restriction on deletion (allow deleting any order)."""
        return

    def unlink(self: models.Model) -> bool:
        """Auto-cancel orders before deletion to maintain consistency."""
        for order in self:
            if order.state not in ('draft', 'cancel'):
                order.action_cancel()
        return super().unlink()


# Order line items
class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    # WooCommerce REST API - Order line items properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#order-line-items-properties
    woocommerce_id = fields.Char(string='WooCommerce Order Line Item ID', readonly=True, index=True)
    woocommerce_name = fields.Char(string='Product Name', readonly=True)
    woocommerce_product_id = fields.Char(string='Product ID', readonly=True)
    woocommerce_variation_id = fields.Char(string='Variation ID', readonly=True)
    woocommerce_quantity = fields.Integer(string='Quantity', readonly=True)
    woocommerce_tax_class = fields.Char(string='Tax Class', readonly=True)
    woocommerce_subtotal = fields.Float(string='Subtotal', readonly=True)
    woocommerce_subtotal_tax = fields.Float(string='Subtotal Tax', readonly=True)
    woocommerce_total = fields.Float(string='Total', readonly=True)
    woocommerce_total_tax = fields.Float(string='Total Tax', readonly=True)
    woocommerce_taxes = fields.Text(string='Taxes', readonly=True)
    woocommerce_meta_data = fields.Text(string='Meta Data', readonly=True)
    woocommerce_sku = fields.Char(string='SKU', readonly=True)
    woocommerce_price = fields.Float(string='Price', readonly=True)

    # Additional fields
    woocommerce_weight_unit = fields.Char(string='Weight Unit', readonly=True)

    # Custom fields
    woocommerce_site_url = fields.Char(string='WooCommerce Site URL', readonly=True, index=True)
    woocommerce_to_odoo_last_sync = fields.Datetime(string='WooCommerce to Odoo Last Sync', readonly=True)


# Order status
class WoocommerceSyncOrderStatus(models.Model):
    _name = 'woocommerce.sync.order.status'
    _description = 'WooCommerce Sync Order Status'

    name = fields.Char(string='WooCommerce Order Status', required=True)
    status = fields.Char(string='WooCommerce Order Status Code', required=True)


# Sync log
class WoocommerceSyncLog(models.Model):
    _name = 'woocommerce.sync.log'
    _description = 'WooCommerce Sync Log'

    woocommerce_connection_id = fields.Integer(string='Connection ID', required=True, index=True)
    odoo_woocommerce_last_sync = fields.Datetime(string='Sync Date', readonly=True)


class WoocommerceSyncStockLog(models.Model):
    _name = 'woocommerce.sync.stock.log'
    _description = 'WooCommerce Stock Sync Log'

    woocommerce_connection_id = fields.Integer(string='Connection ID', required=True, index=True)
    odoo_woocommerce_last_sync = fields.Datetime(string='Sync Date', readonly=True)


class WoocommerceSyncDataTemp(models.Model):
    _name = 'woocommerce.sync.data.temp'
    _description = 'Odoo-WooCommerce Sync temporary data'

    woocommerce_products_variations_data = fields.Json(string='WooCommerce Products Variations Data')
