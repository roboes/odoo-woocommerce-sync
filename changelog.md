# Changelog

## 2025-07-27

### Features

- Code improvements.

## 2025-07-24

### Features

- Added `external_dependencies` to the manifest to prevent module installation if the `woocommerce` Python library is not installed.

### Fixes

- Corrected UoM creation to use the `factor` field instead of the computed, read-only `factor_inv`.
- Fixed duplicate delivery methods creation by properly setting `woocommerce_product_site_url` during carrier creation.
- Updated product type handling for Odoo 18: product classification now uses a combination of `type = 'consu'` and `is_storable = True` instead of a single `type = 'product'` value.

## 2025-07-21

### Fixes

- Optimizations focused on reducing database and API calls.

## 2025-07-20

### Fixes

- Fixed an issue where the scheduled cron job was not executing in Odoo 18.

## 2025-07-15

### Features

- New view configuration setting to control the status (`active`/`archived`) of imported WooCommerce delivery methods. By default, new delivery methods imported from WooCommerce orders will now be created as archived (inactive) in Odoo to prevent clutter in the Delivery Methods list. Existing methods will be updated to match this setting during sync.

## 2025-06-24

### Features

- First fully compatible version for Odoo 18, now maintained in its own branch.
- Updated image storage logic for products with multiple images. The `Product Gallery` is now displayed under the `Sales` tab, following the same UX pattern as Odoo's `website_sale` module.

### Fixes

- Fixed minor bugs.

## 2025-06-23

### Features

- Updated codebase for initial Odoo 18 compatibility and easier future migration.

## 2025-06-21

### Features

- New [odoo-settings-configuration.md](./installation/odoo-settings-configuration.md) with instructions to automatically configure Odoo settings.

## 2025-06-17

### Features

- New [odoo-module-dependency-installer.md](./installation/odoo-module-dependency-installer.md) with instructions to automatically download and install the required and optional Odoo add-ons.
- Updated codebase for initial Odoo 18 compatibility and easier future migration.

## 2025-05-22

### Features

- Add the possibility to filter WooCommerce orders import by order statuses.

### Fixes

- Resolved issue where product mapping in WooCommerce order line items only worked for variable products. Mapping logic has been updated to correctly handle simple products as well.

## 2025-04-30

### Fixes

- Fixes for the Guest Customer Mapping and Line Item Product Mapping.

## 2025-04-06

### Features

- Added support for WooCommerce Shipping Methods: WooCommerce shipping methods are now imported into Odoo under `Home Menu` > `Sales` > `Configuration` > `Sales Orders` > `Shipping Methods`. Imported Sales Orders from WooCommerce will include the respective `carrier_id` for accurate delivery method assignment.

### Fixes

- Fixed minor bugs.

## 2025-03-21

### Fixes

- Removed the `required=True` attribute from the `woocommerce_customer_email` field in the `woocommerce_models`.
- The fields `woocommerce_order_transaction_fee` and `woocommerce_order_payout` were incorrectly displayed on non-WooCommerce orders.

## 2025-03-16

### Features

- Added initial support for Brazilian localization.

### Fixes

- Fixed minor bugs.

## 2025-03-03

- Initial release.
