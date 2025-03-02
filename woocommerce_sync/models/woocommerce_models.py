from odoo import api, fields, models


# Stock
class StockQuant(models.Model):
    _inherit = 'stock.quant'

    # Override the existing 'product_id' field to add the ondelete cascade
    product_id = fields.Many2one(comodel_name='product.product', string='Product', domain=lambda self: self._domain_product_id(), required=True, index=True, check_company=True, ondelete='cascade')

    woocommerce_product_site_url = fields.Char(string='WooCommerce Site URL', readonly=True, index=True)


class StockMove(models.Model):
    _inherit = 'stock.move'

    # Override the existing 'product_id' field to add the ondelete cascade
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


class StockValuationLayer(models.Model):
    _inherit = 'stock.valuation.layer'

    # Override the existing 'product_id' field to add the ondelete cascade
    product_id = fields.Many2one(comodel_name='product.product', string='Product', index=True, domain=lambda self: self._domain_product_id(), required=True, check_company=True, ondelete='cascade')


# Product
class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # Override the existing 'default_code' field to remove the compute/inverse for multi-variant products, so it can be set manually
    default_code = fields.Char(string='Internal Reference', store=True)

    # WooCommerce site URL field
    woocommerce_product_site_url = fields.Char(string='WooCommerce Site URL', readonly=True, index=True)

    # WooCommerce REST API - Common fields for Products and Product Variants
    woocommerce_product_type = fields.Selection(
        [
            ('simple', 'Simple'),
            ('grouped', 'Grouped'),
            ('external', 'External'),
            ('variable', 'Variable'),
            ('variation', 'Variation'),
        ],
        string='Type',
        default='simple',
        readonly=True,
    )

    # WooCommerce REST API - Product properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#product-properties
    woocommerce_product_id = fields.Char(string='WooCommerce ID', readonly=True, index=True)
    woocommerce_product_name = fields.Char(string='Name', readonly=True)
    woocommerce_product_slug = fields.Char(string='Slug', readonly=True)
    woocommerce_product_permalink = fields.Char(string='Permalink', readonly=True)
    woocommerce_product_date_created = fields.Datetime(string='Date Created', readonly=True)
    woocommerce_product_date_created_gmt = fields.Datetime(string='Date Created', readonly=True)
    woocommerce_product_date_modified = fields.Datetime(string='Date Modified', readonly=True)
    woocommerce_product_date_modified_gmt = fields.Datetime(string='Date Modified', readonly=True)
    woocommerce_product_status = fields.Selection(
        [
            ('draft', 'Draft'),
            ('pending', 'Pending'),
            ('private', 'Private'),
            ('publish', 'Published'),
        ],
        string='Status',
        readonly=True,
    )
    woocommerce_product_featured = fields.Boolean(string='Featured', readonly=True)
    woocommerce_product_catalog_visibility = fields.Selection(
        [
            ('visible', 'Visible'),
            ('catalog', 'Catalog'),
            ('search', 'Search'),
            ('hidden', 'Hidden'),
        ],
        string='Catalog Visibility',
        readonly=True,
    )
    woocommerce_product_description = fields.Html(string='Description', readonly=True)
    woocommerce_product_short_description = fields.Html(string='Short Description', readonly=True)
    woocommerce_product_sku = fields.Char(string='SKU', readonly=True)
    woocommerce_product_price = fields.Char(string='Price', readonly=True)
    woocommerce_product_regular_price = fields.Char(string='Regular Price', readonly=True)
    woocommerce_product_sale_price = fields.Char(string='Sale Price', readonly=True)
    woocommerce_product_date_on_sale_from = fields.Datetime(string='Sale Start Date', readonly=True)
    woocommerce_product_date_on_sale_from_gmt = fields.Datetime(string='Sale Start Date', readonly=True)
    woocommerce_product_date_on_sale_to = fields.Datetime(string='Sale End Date', readonly=True)
    woocommerce_product_date_on_sale_to_gmt = fields.Datetime(string='Sale End Date', readonly=True)
    woocommerce_product_price_html = fields.Char(string='Price HTML', readonly=True)
    woocommerce_product_on_sale = fields.Boolean(string='On Sale', readonly=True)
    woocommerce_product_purchasable = fields.Boolean(string='Purchasable', readonly=True)
    woocommerce_product_total_sales = fields.Integer(string='Total Sales', readonly=True)
    woocommerce_product_virtual = fields.Boolean(string='Virtual', readonly=True)
    woocommerce_product_downloadable = fields.Boolean(string='Downloadable', readonly=True)
    woocommerce_product_downloads = fields.Json(string='Downloads', readonly=True)
    woocommerce_product_download_limit = fields.Integer(string='Download Limit', readonly=True)
    woocommerce_product_download_expiry = fields.Integer(string='Download Expiry', readonly=True)
    woocommerce_product_external_url = fields.Char(string='External URL', readonly=True)
    woocommerce_product_button_text = fields.Char(string='Button Text', readonly=True)
    woocommerce_product_tax_status = fields.Selection(
        [
            ('taxable', 'Taxable'),
            ('shipping', 'Shipping'),
            ('none', 'None'),
        ],
        string='Tax Status',
        readonly=True,
    )
    woocommerce_product_tax_class = fields.Char(string='Tax Class', readonly=True)
    woocommerce_product_manage_stock = fields.Boolean(string='Manage Stock', readonly=True)
    woocommerce_product_stock_quantity = fields.Integer(string='Stock Quantity', readonly=True)
    woocommerce_product_stock_status = fields.Selection(
        [
            ('instock', 'In Stock'),
            ('outofstock', 'Out of Stock'),
            ('onbackorder', 'On Backorder'),
        ],
        string='Stock Status',
        readonly=True,
    )
    woocommerce_product_backorders = fields.Selection(
        [
            ('no', 'No'),
            ('notify', 'Notify'),
            ('yes', 'Yes'),
        ],
        string='Backorders',
        readonly=True,
    )
    woocommerce_product_backorders_allowed = fields.Boolean(string='Backorders Allowed', readonly=True)
    woocommerce_product_backordered = fields.Boolean(string='Backordered', readonly=True)
    woocommerce_product_sold_individually = fields.Boolean(string='Sold Individually', readonly=True)
    woocommerce_product_weight = fields.Char(string='Weight', readonly=True)
    woocommerce_product_dimensions = fields.Json(string='Dimensions', readonly=True)
    woocommerce_product_shipping_required = fields.Boolean(string='Shipping Required', readonly=True)
    woocommerce_product_shipping_taxable = fields.Boolean(string='Shipping Taxable', readonly=True)
    woocommerce_product_shipping_class = fields.Char(string='Shipping Class', readonly=True)
    woocommerce_product_shipping_class_id = fields.Integer(string='Shipping Class ID', readonly=True)
    woocommerce_product_reviews_allowed = fields.Boolean(string='Reviews Allowed', readonly=True)
    woocommerce_product_average_rating = fields.Char(string='Average Rating', readonly=True)
    woocommerce_product_rating_count = fields.Integer(string='Rating Count', readonly=True)
    woocommerce_product_related_ids = fields.Text(string='Related Products', readonly=True)
    woocommerce_product_upsell_ids = fields.Text(string='Upsell Products', readonly=True)
    woocommerce_product_cross_sell_ids = fields.Text(string='Cross-Sell Products', readonly=True)
    woocommerce_product_parent_id = fields.Integer(string='Parent Product ID', readonly=True)
    woocommerce_product_purchase_note = fields.Text(string='Purchase Note', readonly=True)
    woocommerce_product_categories = fields.Json(string='Categories', readonly=True)
    woocommerce_product_tags = fields.Json(string='Tags', readonly=True)
    woocommerce_product_images = fields.Json(string='Images', readonly=True)
    woocommerce_product_attributes = fields.Json(string='Attributes', readonly=True)
    woocommerce_product_default_attributes = fields.Json(string='Default Attributes', readonly=True)
    woocommerce_product_variations = fields.Json(string='Variations', readonly=True)
    woocommerce_product_grouped_products = fields.Json(string='Grouped Products', readonly=True)
    woocommerce_product_menu_order = fields.Integer(string='Menu Order', readonly=True)
    woocommerce_product_meta_data = fields.Json(string='Meta Data', readonly=True)

    # WooCommerce REST API - Fields not mentioned in the documentation
    woocommerce_product_brands = fields.Json(string='Brands', readonly=True)

    # Additional fields
    woocommerce_product_currency = fields.Char(string='Currency', readonly=True)
    woocommerce_product_weight_unit = fields.Char(string='Weight Unit', readonly=True)
    woocommerce_product_dimension_unit = fields.Char(string='Dimension Unit', readonly=True)
    woocommerce_product_tax_rate = fields.Integer(string='Tax Rate', readonly=True)

    # Custom fields
    product_sync_to_woocommerce = fields.Boolean(string='Sync to WooCommerce', default=False)
    if not hasattr(models.BaseModel, '_fields') or 'product_source' not in ProductTemplate._fields:
        product_source = fields.Char(string='Source', readonly=True)
    if not hasattr(models.BaseModel, '_fields') or 'product_language_code' not in ProductTemplate._fields:
        product_language_code = fields.Char(string='Language', help='Polylang 2-digit ISO 639-1 language code.')  # Polylang
    if not hasattr(models.BaseModel, '_fields') or 'product_stock_date_updated' not in ProductTemplate._fields:
        product_stock_date_updated = fields.Datetime(string='Stock Date Updated', readonly=True)
    if not hasattr(models.BaseModel, '_fields') or 'product_images_ids' not in ProductTemplate._fields:
        product_images_ids = fields.Many2many(comodel_name='ir.attachment', string='Images', help='Multiple product images', domain=[('mimetype', 'ilike', 'image')], readonly=True)
    woocommerce_product_service = fields.Boolean(string='Is service?')


# Product variations
class ProductProduct(models.Model):
    _inherit = 'product.product'

    def action_update_quantity_on_hand(self):
        # Update stock first
        res = super().action_update_quantity_on_hand()

        # Update the 'product_stock_date_updated' on the 'product.product' level
        self.write({'product_stock_date_updated': fields.Datetime.now()})

        return res

    # WooCommerce site URL field
    woocommerce_product_variation_site_url = fields.Char(string='WooCommerce Site URL', readonly=True, index=True)

    # WooCommerce REST API - Product variation properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#product-variation-properties
    woocommerce_product_variation_id = fields.Char(string='WooCommerce ID', readonly=True, index=True)
    woocommerce_product_variation_name = fields.Char(string='Name', readonly=True)
    woocommerce_product_variation_permalink = fields.Char(string='Permalink', readonly=True)
    woocommerce_product_variation_date_created = fields.Datetime(string='Date Created', readonly=True)
    woocommerce_product_variation_date_created_gmt = fields.Datetime(string='Date Created', readonly=True)
    woocommerce_product_variation_date_modified = fields.Datetime(string='Date Modified', readonly=True)
    woocommerce_product_variation_date_modified_gmt = fields.Datetime(string='Date Modified', readonly=True)
    woocommerce_product_variation_status = fields.Selection(
        [
            ('draft', 'Draft'),
            ('pending', 'Pending'),
            ('private', 'Private'),
            ('publish', 'Published'),
        ],
        string='Status',
        readonly=True,
    )
    woocommerce_product_variation_description = fields.Html(string='Description', readonly=True)
    woocommerce_product_variation_sku = fields.Char(string='SKU', readonly=True)
    woocommerce_product_variation_price = fields.Char(string='Price', readonly=True)
    woocommerce_product_variation_regular_price = fields.Char(string='Regular Price', readonly=True)
    woocommerce_product_variation_sale_price = fields.Char(string='Sale Price', readonly=True)
    woocommerce_product_variation_date_on_sale_from = fields.Datetime(string='Sale Start Date', readonly=True)
    woocommerce_product_variation_date_on_sale_from_gmt = fields.Datetime(string='Sale Start Date', readonly=True)
    woocommerce_product_variation_date_on_sale_to = fields.Datetime(string='Sale End Date', readonly=True)
    woocommerce_product_variation_date_on_sale_to_gmt = fields.Datetime(string='Sale End Date', readonly=True)
    woocommerce_product_variation_on_sale = fields.Boolean(string='On Sale', readonly=True)
    woocommerce_product_variation_purchasable = fields.Boolean(string='Purchasable', readonly=True)
    woocommerce_product_variation_virtual = fields.Boolean(string='Virtual', readonly=True)
    woocommerce_product_variation_downloadable = fields.Boolean(string='Downloadable', readonly=True)
    woocommerce_product_variation_downloads = fields.Json(string='Downloads', readonly=True)
    woocommerce_product_variation_download_limit = fields.Integer(string='Download Limit', readonly=True)
    woocommerce_product_variation_download_expiry = fields.Integer(string='Download Expiry', readonly=True)
    woocommerce_product_variation_tax_status = fields.Selection(
        [
            ('taxable', 'Taxable'),
            ('shipping', 'Shipping'),
            ('none', 'None'),
        ],
        string='Tax Status',
        readonly=True,
    )
    woocommerce_product_variation_tax_class = fields.Char(string='Tax Class', readonly=True)
    woocommerce_product_variation_manage_stock = fields.Boolean(string='Manage Stock', readonly=True)
    woocommerce_product_variation_stock_quantity = fields.Integer(string='Stock Quantity', readonly=True)
    woocommerce_product_variation_stock_status = fields.Selection(
        [
            ('instock', 'In Stock'),
            ('outofstock', 'Out of Stock'),
            ('onbackorder', 'On Backorder'),
        ],
        string='Stock Status',
        readonly=True,
    )
    woocommerce_product_variation_backorders = fields.Selection(
        [
            ('no', 'No'),
            ('notify', 'Notify'),
            ('yes', 'Yes'),
        ],
        string='Backorders',
        readonly=True,
    )
    woocommerce_product_variation_backorders_allowed = fields.Boolean(string='Backorders Allowed', readonly=True)
    woocommerce_product_variation_backordered = fields.Boolean(string='Backordered', readonly=True)
    woocommerce_product_variation_weight = fields.Char(string='Weight', readonly=True)
    woocommerce_product_variation_dimensions = fields.Json(string='Dimensions', readonly=True)
    woocommerce_product_variation_shipping_class = fields.Char(string='Shipping Class', readonly=True)
    woocommerce_product_variation_shipping_class_id = fields.Integer(string='Shipping Class ID', readonly=True)
    woocommerce_product_variation_image = fields.Json(string='Images', readonly=True)
    woocommerce_product_variation_attributes = fields.Json(string='Attributes', readonly=True)
    woocommerce_product_variation_menu_order = fields.Integer(string='Menu Order', readonly=True)
    woocommerce_product_variation_meta_data = fields.Json(string='Meta Data', readonly=True)

    # WooCommerce REST API - Fields not mentioned in the documentation
    woocommerce_product_variation_parent_id = fields.Integer(string='Parent Product ID', readonly=True)

    # Additional fields
    woocommerce_product_variation_currency = fields.Char(string='Currency', readonly=True)
    woocommerce_product_variation_weight_unit = fields.Char(string='Weight Unit', readonly=True)
    woocommerce_product_variation_dimension_unit = fields.Char(string='Dimension Unit', readonly=True)
    woocommerce_product_variation_tax_rate = fields.Integer(string='Tax Rate', readonly=True)

    # Custom fields
    if not hasattr(models.BaseModel, '_fields') or 'product_stock_date_updated' not in ProductTemplate._fields:
        product_stock_date_updated = fields.Datetime(string='Stock Date Updated', readonly=True)
    woocommerce_product_variation_service = fields.Boolean(string='Is service?')


# Customers
class ResPartner(models.Model):
    _inherit = 'res.partner'

    # WooCommerce site URL field
    woocommerce_customer_site_url = fields.Char(string='WooCommerce Site URL', readonly=True, index=True)

    # WooCommerce REST API - Customer properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#customer-properties
    woocommerce_customer_id = fields.Char(string='WooCommerce ID', readonly=True, index=True)
    woocommerce_customer_date_created = fields.Datetime(string='Created Date', readonly=True)
    woocommerce_customer_date_created_gmt = fields.Datetime(string='Created Date', readonly=True)
    woocommerce_customer_date_modified = fields.Datetime(string='Modified Date', readonly=True)
    woocommerce_customer_date_modified_gmt = fields.Datetime(string='Modified Date', readonly=True)
    woocommerce_customer_email = fields.Char(string='Email', readonly=True, required=True)
    woocommerce_customer_first_name = fields.Char(string='First Name', readonly=True)
    woocommerce_customer_last_name = fields.Char(string='Last Name', readonly=True)
    woocommerce_customer_role = fields.Char(string='Role', readonly=True)
    woocommerce_customer_username = fields.Char(string='Username', readonly=True)
    woocommerce_customer_is_paying_customer = fields.Boolean(string='Is Paying Customer', readonly=True)
    woocommerce_customer_avatar_url = fields.Char(string='Avatar URL', readonly=True)
    woocommerce_customer_meta_data = fields.Json(string='Meta Data', readonly=True)

    # WooCommerce REST API - Customer billing properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#customer-billing-properties
    woocommerce_customer_billing_first_name = fields.Char(string='Billing First Name', readonly=True)
    woocommerce_customer_billing_last_name = fields.Char(string='Billing Last Name', readonly=True)
    woocommerce_customer_billing_company = fields.Char(string='Billing Company', readonly=True)
    woocommerce_customer_billing_address_1 = fields.Char(string='Billing Address 1', readonly=True)
    woocommerce_customer_billing_address_2 = fields.Char(string='Billing Address 2', readonly=True)
    woocommerce_customer_billing_city = fields.Char(string='Billing City', readonly=True)
    woocommerce_customer_billing_state = fields.Char(string='Billing State', readonly=True)
    woocommerce_customer_billing_postcode = fields.Char(string='Billing Postcode', readonly=True)
    woocommerce_customer_billing_country = fields.Char(string='Billing Country', readonly=True)
    woocommerce_customer_billing_email = fields.Char(string='Billing Email', readonly=True)
    woocommerce_customer_billing_phone = fields.Char(string='Billing Phone', readonly=True)

    # WooCommerce REST API - Customer shipping properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#customer-shipping-properties
    woocommerce_customer_shipping_first_name = fields.Char(string='Shipping First Name', readonly=True)
    woocommerce_customer_shipping_last_name = fields.Char(string='Shipping Last Name', readonly=True)
    woocommerce_customer_shipping_company = fields.Char(string='Shipping Company', readonly=True)
    woocommerce_customer_shipping_address_1 = fields.Char(string='Shipping Address 1', readonly=True)
    woocommerce_customer_shipping_address_2 = fields.Char(string='Shipping Address 2', readonly=True)
    woocommerce_customer_shipping_city = fields.Char(string='Shipping City', readonly=True)
    woocommerce_customer_shipping_state = fields.Char(string='Shipping State', readonly=True)
    woocommerce_customer_shipping_postcode = fields.Char(string='Shipping Postcode', readonly=True)
    woocommerce_customer_shipping_country = fields.Char(string='Shipping Country', readonly=True)

    # Custom fields
    woocommerce_customer_date_last_login = fields.Datetime(string='Last Login Date', readonly=True)  # Wordfence fields


# Orders
class SaleOrder(models.Model):
    _inherit = 'sale.order'

    @api.depends('woocommerce_order_total', 'woocommerce_order_transaction_fee')
    def payout_compute(self):
        for order in self:
            order.woocommerce_order_payout = order.woocommerce_order_total - order.woocommerce_order_transaction_fee

    # WooCommerce site URL field
    woocommerce_order_site_url = fields.Char(string='WooCommerce Site URL', readonly=True, index=True)

    # WooCommerce REST API - Order properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#order-properties
    woocommerce_order_id = fields.Char(string='WooCommerce ID', readonly=True, index=True)
    woocommerce_order_parent_id = fields.Char(string='Parent ID', readonly=True)
    woocommerce_order_number = fields.Char(string='Number', readonly=True)
    woocommerce_order_order_key = fields.Char(string='Key', readonly=True)
    woocommerce_order_created_via = fields.Char(string='Created Via', readonly=True)
    woocommerce_order_version = fields.Char(string='WooCommerce Version', readonly=True)
    woocommerce_order_status = fields.Selection(
        [('pending', 'Pending'), ('processing', 'Processing'), ('on-hold', 'On Hold'), ('completed', 'Completed'), ('cancelled', 'Cancelled'), ('refunded', 'Refunded'), ('failed', 'Failed'), ('trash', 'Trash')],
        string='Status',
        readonly=True,
    )
    woocommerce_order_currency = fields.Char(string='Currency', readonly=True)
    woocommerce_order_date_created = fields.Datetime(string='Date Created', readonly=True)
    woocommerce_order_date_created_gmt = fields.Datetime(string='Date Created', readonly=True)
    woocommerce_order_date_modified = fields.Datetime(string='Date Modified', readonly=True)
    woocommerce_order_date_modified_gmt = fields.Datetime(string='Date Modified', readonly=True)
    woocommerce_order_discount_total = fields.Float(string='Discount Total', readonly=True)
    woocommerce_order_discount_tax = fields.Float(string='Discount Tax', readonly=True)
    woocommerce_order_shipping_total = fields.Float(string='Shipping Total', readonly=True)
    woocommerce_order_shipping_tax = fields.Float(string='Shipping Tax', readonly=True)
    woocommerce_order_cart_tax = fields.Float(string='Cart Tax', readonly=True)
    woocommerce_order_total = fields.Float(string='Grand Total', readonly=True)
    woocommerce_order_total_tax = fields.Float(string='Total Tax', readonly=True)
    woocommerce_order_prices_include_tax = fields.Boolean(string='Include Tax', readonly=True)
    woocommerce_order_customer_id = fields.Char(string='WooCommerce Customer ID', readonly=True)
    woocommerce_order_customer_ip_address = fields.Char(string='Customer IP Address', readonly=True)
    woocommerce_order_customer_user_agent = fields.Char(string='Customer User Agent', readonly=True)
    woocommerce_order_customer_note = fields.Char(string='Customer Note', readonly=True)
    woocommerce_order_payment_method = fields.Char(string='Payment Method', readonly=True)
    woocommerce_order_payment_method_title = fields.Char(string='Payment Method Title', readonly=True)
    woocommerce_order_transaction_id = fields.Char(string='Transaction ID', readonly=True)
    woocommerce_order_date_paid = fields.Datetime(string='Date Paid', readonly=True)
    woocommerce_order_date_paid_gmt = fields.Datetime(string='Date Paid', readonly=True)
    woocommerce_order_date_completed = fields.Datetime(string='Date Completed', readonly=True)
    woocommerce_order_date_completed_gmt = fields.Datetime(string='Date Completed', readonly=True)
    woocommerce_order_cart_hash = fields.Char(string='Cart Hash', readonly=True)
    woocommerce_order_meta_data = fields.Json(string='Meta Data', readonly=True)
    woocommerce_order_line_items = fields.Json(string='Line Items', readonly=True)
    woocommerce_order_tax_lines = fields.Json(string='Tax Lines', readonly=True)
    woocommerce_order_shipping_lines = fields.Json(string='Shipping Lines', readonly=True)
    woocommerce_order_fee_lines = fields.Json(string='Fee Lines', readonly=True)
    woocommerce_order_coupon_lines = fields.Json(string='Coupon Lines', readonly=True)
    woocommerce_order_refunds = fields.Json(string='Refunds', readonly=True)
    # woocommerce_order_set_paid = fields.Boolean(string='Set Paid', readonly=True)

    # WooCommerce REST API - Order billing properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#order-billing-properties
    woocommerce_order_billing_first_name = fields.Char(string='Billing First Name', readonly=True)
    woocommerce_order_billing_last_name = fields.Char(string='Billing Last Name', readonly=True)
    woocommerce_order_billing_company = fields.Char(string='Billing Company', readonly=True)
    woocommerce_order_billing_address_1 = fields.Char(string='Billing Address 1', readonly=True)
    woocommerce_order_billing_address_2 = fields.Char(string='Billing Address 2', readonly=True)
    woocommerce_order_billing_city = fields.Char(string='Billing City', readonly=True)
    woocommerce_order_billing_state = fields.Char(string='Billing State', readonly=True)
    woocommerce_order_billing_postcode = fields.Char(string='Billing Postcode', readonly=True)
    woocommerce_order_billing_country = fields.Char(string='Billing Country', readonly=True)
    woocommerce_order_billing_email = fields.Char(string='Billing Email', readonly=True)
    woocommerce_order_billing_phone = fields.Char(string='Billing Phone', readonly=True)

    # WooCommerce REST API - Order shipping properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#order-shipping-properties
    woocommerce_order_shipping_first_name = fields.Char(string='Shipping First Name', readonly=True)
    woocommerce_order_shipping_last_name = fields.Char(string='Shipping Last Name', readonly=True)
    woocommerce_order_shipping_company = fields.Char(string='Shipping Company', readonly=True)
    woocommerce_order_shipping_address_1 = fields.Char(string='Shipping Address 1', readonly=True)
    woocommerce_order_shipping_address_2 = fields.Char(string='Shipping Address 2', readonly=True)
    woocommerce_order_shipping_city = fields.Char(string='Shipping City', readonly=True)
    woocommerce_order_shipping_state = fields.Char(string='Shipping State', readonly=True)
    woocommerce_order_shipping_postcode = fields.Char(string='Shipping Postcode', readonly=True)
    woocommerce_order_shipping_country = fields.Char(string='Shipping Country', readonly=True)

    # Custom fields
    woocommerce_order_transaction_fee = fields.Float(string='Transaction Fee', help='Transaction fees incurred from PayPal or Stripe.', readonly=True, default=0.0)
    if not hasattr(models.BaseModel, '_fields') or 'order_language_code' not in ProductTemplate._fields:
        order_language_code = fields.Char(string='Language', help='Polylang 2-digit ISO 639-1 language code.', readonly=True)  # Polylang

    # Computed fields
    woocommerce_order_payout = fields.Float(string='Payout', help='Total - Order Transaction Fee.', compute='payout_compute', store=True, readonly=True)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_draft_or_cancel(self):
        """Remove Odoo's restriction on deletion (allow deleting any order)."""
        return

    def unlink(self):
        """Auto-cancel orders before deletion to maintain consistency."""
        for order in self:
            if order.state not in ('draft', 'cancel'):
                order.action_cancel()
        return super().unlink()


# Order line items
class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    # WooCommerce site URL field
    woocommerce_order_line_site_url = fields.Char(string='WooCommerce Site URL', readonly=True, index=True)

    # WooCommerce REST API - Order line items properties fields - https://woocommerce.github.io/woocommerce-rest-api-docs/#order-line-items-properties
    woocommerce_order_line_item_id = fields.Char(string='WooCommerce ID', readonly=True, index=True)
    woocommerce_order_line_item_name = fields.Char(string='Product Name', readonly=True)
    woocommerce_order_line_item_product_id = fields.Char(string='Product ID', readonly=True)
    woocommerce_order_line_item_variation_id = fields.Char(string='Variation ID', readonly=True)
    woocommerce_order_line_item_quantity = fields.Integer(string='Quantity', readonly=True)
    woocommerce_order_line_item_tax_class = fields.Char(string='Tax Class', readonly=True)
    woocommerce_order_line_item_subtotal = fields.Float(string='Subtotal', readonly=True)
    woocommerce_order_line_item_subtotal_tax = fields.Float(string='Subtotal Tax', readonly=True)
    woocommerce_order_line_item_total = fields.Float(string='Total', readonly=True)
    woocommerce_order_line_item_total_tax = fields.Float(string='Total Tax', readonly=True)
    woocommerce_order_line_item_taxes = fields.Text(string='Taxes', readonly=True)
    woocommerce_order_line_item_meta_data = fields.Text(string='Meta Data', readonly=True)
    woocommerce_order_line_item_sku = fields.Char(string='SKU', readonly=True)
    woocommerce_order_line_item_price = fields.Float(string='Price', readonly=True)

    # Additional fields
    woocommerce_order_line_item_weight_unit = fields.Char(string='Weight Unit', readonly=True)
