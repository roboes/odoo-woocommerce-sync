# Changelog

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

### Fix

- Resolved issue where product mapping in WooCommerce order line items only worked for variable products. Mapping logic has been updated to correctly handle simple products as well.

## 2025-04-30

### Fix

- Fixes for the Guest Customer Mapping and Line Item Product Mapping.

## 2025-04-06

### Features

- Added support for WooCommerce Shipping Methods: WooCommerce shipping methods are now imported into Odoo under `Home Menu` > `Sales` > `Configuration` > `Sales Orders` > `Shipping Methods`. Imported Sales Orders from WooCommerce will include the respective `carrier_id` for accurate delivery method assignment.

### Fix

- Fixed minor bugs.

## 2025-03-21

### Fix

- Removed the `required=True` attribute from the `woocommerce_customer_email` field in the `woocommerce_models`.
- The fields `woocommerce_order_transaction_fee` and `woocommerce_order_payout` were incorrectly displayed on non-WooCommerce orders.

## 2025-03-16

### Features

- Added initial support for Brazilian localization.

### Fix

- Fixed minor bugs.

## 2025-03-03

- Initial release.
