# Odoo-WooCommerce Sync

<p align="center">
  <img src="./media/odoo-woocommerce-sync-logo.png" alt="Odoo-WooCommerce Sync" height="300">
</p>

<br>

[!["Buy Me A Coffee"](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/roboes)

## Description

The **Odoo-WooCommerce Sync** add-on enables synchronization between WooCommerce and Odoo. The main features are:

- **WooCommerce to Odoo:** Synchronize new and existing products (including variations), stock quantity levels, customers, and orders (including line items).
- **Odoo to WooCommerce:** Synchronize new and existing products (including variations) and stock quantity levels.
- **Extended Models and Views:** Enhance existing Odoo models and views for `product.template`, `product.product`, `res.partner`, `sale.order`, and `sale.order.line` to accommodate corresponding WooCommerce REST API fields.
- **Automated and Manual Synchronization:** A built-in cron job scheduler enables regular synchronization, complemented by a dedicated button for manually triggering updates.
- **Advanced Settings:** Support for multiple WooCommerce websites with specific configuration options for each instance (e.g. syncing only products from WooCommerce to Odoo).
- **Image Synchronization:** Optionally synchronize product images from WooCommerce to Odoo. For products imported from WooCommerce that include multiple images/product gallery, an additional photo gallery tab is added to the `product.template` view.
- **Language Filtering:** Synchronize products by language (*requires Polylang*).
- **Orders Transactions Fee Support:** Integrates additional fee fields into orders processed with PayPal and Stripe (*requires the respective plugins*).

Some features require additional setup, as detailed in the [Requirements](#requirements) section.

> [!WARNING]
> This add-on is provided without any warranty and may contain bugs as it is a recently developed solution. Testing in a controlled environment is recommended before deployment, and usage is at one's own risk.

## Limitations

- **Performance:** Updating product variations may take long, as each variable product is processed through a separate WooCommerce REST API call.
- **Stock Management:** If a WooCommerce product variation's "Manage Stock" setting is modified, the corresponding parent product in Odoo is removed. This requires a complete re-import of the parent product and its variations, which can be manually retriggered by pressing the `Sync Now` button.
- **Unique SKU Requirement:** Every product must have a unique SKU. For product variations, both the parent product and each individual variation must possess a SKU. In Odoo, the internal reference field (`default_code`) should be used to store this value.
- **Media Endpoints:** The WooCommerce REST API does not provide direct access to media endpoints; therefore, uploading images from Odoo to WooCommerce is not supported in this add-on.

## Requirements

### Odoo

#### Python Dependencies

Install the necessary Python packages by running:

```sh
python -m pip install woocommerce
```

#### Odoo Add-ons (Required)

- **Products & Pricelists** (`product`)
  - The user should have **Administrator** privileges:
    - `Home Menu` > `Settings` > `Users & Companies` > `Users` > Select the user > `Sales` > `Sales` > `Administrator`.
- **Invoicing** (`account`)
  - The user should have **Billing Administrator** privileges:
    - `Home Menu` > `Settings` > `Users & Companies` > `Users` > Select the user > `Accounting` > `Invoicing` > `Billing Administrator`.
  - Configure Fiscal Localization:
    - `Home Menu` > `Settings` > `Invoicing` > `Fiscal Localization` > `Package` > Set the `Fiscal Localization` package (e.g. `Germany SKR04 - Accounting`).
  - Enable full accounting features:
    - `Home Menu` > `Settings` > `Users & Company` > `Groups` > Select the `Technical / Show Full Accounting Features` group > `Add a line` > Select the user.
- **Sales** (`sale_management`)
  - Enable [Product Variants](https://www.odoo.com/documentation/16.0/applications/sales/sales/products_prices/products/variants.html):
    - `Home Menu` > `Settings` > `Sales` > `Product Catalog` > Enable `Variants`.
- **Inventory** (`stock`)
  - Enable Units of Measure:
    - `Home Menu` > `Settings` > `Inventory` > `Products` > Enable `Units of Measure`.
  - (Optional) Set up a dedicated warehouse for WooCommerce sales:
    - `Home Menu` > `Settings` > `Inventory` > `Warehouse` > Enable `Storage Locations` and configure under `Locations` the warehouse accordingly.
- **Contacts** (`contacts`)
- **Job Queue** (`queue_job`)
  - [GitHub](https://github.com/OCA/queue/tree/16.0/queue_job) | [Odoo Apps Store](https://apps.odoo.com/apps/modules/16.0/queue_job) (requires additional [configuration instructions](https://github.com/OCA/queue/tree/16.0/queue_job#configuration)).

#### Odoo Add-ons (Optional)

While not mandatory, the following Odoo Community Association (OCA) add-ons are recommended to enhance functionality:

- **Module Auto Update** (`module_auto_update`): Automatically updates installed modules to their latest versions, ensuring the system remains current with minimal manual intervention.
  - [GitHub](https://github.com/OCA/server-tools/tree/16.0/module_auto_update) | [Odoo Apps Store](https://apps.odoo.com/apps/modules/16.0/module_auto_update)
- **Product Dimension** (`product_dimension`): Adds fields for length, width, height, and unit of measure, enabling detailed management of product dimensions.
  - [GitHub](https://github.com/OCA/product-attribute/tree/16.0/product_dimension) | [Odoo Apps Store](https://apps.odoo.com/apps/modules/16.0/product_dimension)
- **Product - Many Categories** (`product_multi_category`): Enhances the standard single-category assignment (`categ_id`) by introducing a `categ_ids` field, allowing products to be organized into multiple categories.
  - [GitHub](https://github.com/OCA/product-attribute/tree/16.0/product_multi_category) | [Odoo Apps Store](https://apps.odoo.com/apps/modules/16.0/product_multi_category)
- **Product Brand Manager** (`product_brand`): Adds a `product_brand_id` field to facilitate the import and management of product brands from WooCommerce (requires WooCommerce 9.6+) (only one brand per product allowed).
  - [GitHub](https://github.com/OCA/brand/tree/16.0/product_brand) | [Odoo Apps Store](https://apps.odoo.com/apps/modules/16.0/product_brand)

### WordPress

#### WordPress Plugins (Optional)

- **WooCommerce Customer Last Login:** (`woocommerce_customer_date_last_login` field): Requires the [Wordfence Security](https://wordpress.org/plugins/wordfence/) plugin.
- **Product Language Code:** (`product_language_code` field): Requires [Polylang for WooCommerce](https://polylang.pro/downloads/polylang-for-woocommerce/) and either:
  - [Polylang Pro](https://polylang.pro/downloads/polylang-pro/) (which enables the `lang` argument in the WooCommerce REST API); or
  - The custom code snippet provided in [this file](./woocommerce-rest-api/woocommerce-rest-api-polylang-language-slug.php), saved either into the `functions.php` file or into a code snippet plugin (e.g. [WPCode](https://wordpress.org/plugins/insert-headers-and-footers/)).
- **Orders Transactions Fee** (`woocommerce_order_transaction_fee` field): Requires the [WooCommerce PayPal Payments](https://wordpress.org/plugins/woocommerce-paypal-payments/) and/or [Payment Plugins for Stripe WooCommerce](https://wordpress.org/plugins/woo-stripe-payment/) plugin.
  - For the [Payment Plugins for Stripe WooCommerce](https://wordpress.org/plugins/woo-stripe-payment/), the following setting needs to be changed in order to enable the Stripe transaction fee field: `WooCommerce` > `Stripe by Payment Plugins` > `Settings` > `Advanced Settings` > Enable `Display Stripe Fee`.

## Installation

Follow these steps to install the Odoo-WooCommerce Sync add-on:

1. **Install Python Dependencies:** Ensure the [Python dependencies](#python-dependencies) are installed on the Odoo instance.
2. **Enable Odoo Add-ons:** Install and activate all [required](#odoo-add-ons-required) and, if applicable, [optional](#odoo-add-ons-optional) Odoo add-ons.
3. **Configure WordPress (if applicable):** Install and set up the [optional plugins](#wordpress-plugins-optional) for WordPress. Retrieve the WooCommerce REST API `consumer key` and `consumer secret` from `WooCommerce` > `Settings` > `Advanced` > `REST API`.
4. **Add the Add-on:** Download and place the [`woocommerce_sync`](./woocommerce_sync) directory into the Odoo `addons` directory.
5. **Activate Debug Mode:** Log in to Odoo and enable [Debug Mode](https://www.odoo.com/documentation/16.0/applications/general/developer_mode.html).
6. **Update the Apps List:** Navigate to `Home Menu` > `Apps` and click **Update Apps List**.
7. **Activate the Add-on:** Use the filter to search for `woocommerce_sync` and activate the add-on.

## Configuration

The add-on is configured through the WooCommerce Sync configuration, accessible via `Home Menu` > `WooCommerce Sync`.

For orders import, two optional mapping logics can be activated. By default, a product and customer (for orders placed by guest customer) placeholder/dummy is created. The options are:

- **Guest Customers Mapping:** When enabled, orders placed by guest (unregistered) customers are matched to existing Odoo customers using their email addresses. If no matching customer exists, a new record is created automatically. When disabled, a customer placeholder (`ref = WooCommerce_Customer_Placeholder`) is assigned to the order.
- **Line Items Product Mapping:** When enabled, each line item is mapped to an existing Odoo product using the `woocommerce_product_id`. If no match is found, a product placeholder is used. When disabled, all order line items are assigned to a placeholder product (`default_code = WooCommerce_Product_Placeholder`) while still displaying the WooCommerce product name. This option is not recommended since product details in WooCommerce may change over time, complicating accurate mapping.

## Reference

- [WooCommerce REST API Documentation](https://woocommerce.github.io/woocommerce-rest-api-docs/)
