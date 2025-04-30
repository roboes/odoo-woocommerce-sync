# Changelog

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
