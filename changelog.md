# Changelog

## v16.0.5.2 / v18.0.5.2 / v19.0.5.2 - 2026-08-30

### Improvements

- Products exported to WooCommerce now have an explicit connector assignment, preventing one connector from exporting another store's products while retaining existing URL-based mappings.
- Incremental import watermarks are tracked separately for products, variations, customers, and orders, so enabling a direction later cannot skip records covered only by another direction's prior run.
- Stock synchronization now computes quantities in the configured warehouse, groups all matching quants, rejects unsafe automatic adjustments for lot/serial-tracked products, and advances its watermark only after every stock chunk completes.
- Webhook registration refreshes existing hooks and only enables webhook processing after every required topic succeeds. Delivery IDs now distinguish successive webhook jobs for the same resource.
- Full variation imports archive mapped variants that are no longer published remotely. Order resync removes stale integration-owned lines unless invoicing or delivery history makes removal unsafe.
- The test runner now executes the add-on suite with explicit Odoo test flags against a configurable database, without deleting global queue jobs or truncating container logs.
- Full sync runs now use an idempotent per-chunk event ledger and completion barrier. The incremental-import watermark advances only after every inbound chunk finishes without record errors, and uses the run's start time so changes made in WooCommerce during a run remain eligible for the next sync.
- Overlapping full sync runs are deferred instead of mixing their events, and inbound queue-job identity keys now include the run timestamp so an older failed job cannot suppress a later retry run.
- Sync summaries now report skipped records alongside new, updated, and errored records; legacy event rows without a sync direction are included under `Legacy/Unknown`.
- Standard WooCommerce REST API page size was increased from 10 to 100 records to reduce API round trips; Test mode remains limited to the first 10 records.
- Connector configuration, credentials, temporary sync data, logs, and summary events are now restricted to the new `WooCommerce Sync Manager` group, which is automatically inherited by Odoo Settings administrators.
- Odoo's standard sale-order deletion safeguards are restored instead of being globally bypassed for all sale orders.

### Fixes

- Fixed WooCommerce batch calls targeting collection endpoints instead of the required `/batch` endpoints. Ambiguous create failures are no longer retried automatically, avoiding duplicate remote products.
- All non-success WooCommerce responses and malformed pagination payloads now raise errors instead of being interpreted as an empty catalog, preventing failed requests from triggering mass product deletion or cursor advancement.
- Fixed sync-summary event collisions between product, variation, customer, and order chunks that happened to contain identical remote ID sequences.
- Escaped all WooCommerce-controlled JSON keys and values in the backend table widget, preventing stored HTML/script injection in authenticated Odoo sessions.
- Tax lookup, creation, and caching are now company-scoped.
- Inbound product, customer, and order freshness checks now compare the last imported WooCommerce timestamp instead of generic Odoo `write_date` changes.
- Delivery lines are synchronized before completed orders are confirmed and locked; existing completed orders are temporarily unlocked for safe resynchronization.
- Restored Python 3.10 compatibility for Odoo 16 by replacing `datetime.UTC` with `timezone.utc`, and added the missing Odoo 16 webhook controls and module-button visibility condition.
- Removed WooCommerce credentials from v16/v18 connector list views, corrected the active gallery SCSS selector, and relaxed the Pillow requirement so it can follow each Odoo release's supported dependency version.
- Variation batch jobs now wait until all product chunks finish before dispatching variation work, fixing the queue-ordering race that could report a missing parent; a genuinely missing parent still produces a deterministic sync-summary error.
- Product deletion detection now requests IDs for all WooCommerce statuses, preventing draft, pending, or private products from being mistaken for deleted products merely because they are not published.
- Archived WooCommerce-linked products, variations, and customers are now found and updated/reactivated instead of being treated as missing and conflicting with their unique WooCommerce ID.
- Per-record rollback now clears shared chunk caches so IDs of records rolled back with a failed product or variation cannot be reused by later records in the same chunk.
- First-import variations are now counted as new even when Odoo automatically materializes their variant rows while attribute lines are created; only variations that already had a WooCommerce ID mapping before the sync are counted as updated.
- Unchanged variations now compare WooCommerce's incoming modification timestamp with the last imported WooCommerce timestamp instead of Odoo's transaction-level `write_date`, preventing unnecessary updates during long-running transactions.
- Dedicated customer import now links an existing unbound top-level contact with the same site/email instead of creating a duplicate. Guest contacts receive a normalized per-site email key with a database uniqueness constraint to prevent concurrent order chunks from creating duplicates.
- WooCommerce `modified_after` and outbound `date_created_gmt` values now include an explicit UTC `Z` suffix instead of relying on interpretation of a timezone-less timestamp.
- Webhook requests now reject unsupported topics and payloads larger than 2 MB while retaining HMAC-SHA256 validation over the exact request body.
- WooCommerce-provided image URLs now reject credentials and non-HTTP(S) schemes, restrict access to non-public network addresses, revalidate redirects, and enforce a 25 MB download limit.
- SQL identifiers used for partial unique indexes are now composed with `psycopg2.sql.Identifier`; sync-log rows and per-run chunk events also have database uniqueness constraints.
- Fixed test discovery so all existing add-on test modules are loaded, and added regression coverage for customer identity linking, UTC serialization, REST page sizing, variation status aggregation, and non-destructive product deletion in Test mode.

## v16.0.5.1 / v18.0.5.1 / v19.0.5.1 - 2026-08-29

### Improvements

- Queue job chunk size must now be greater than zero, preventing invalid settings from breaking chunk generation.
- Retryable queue job and PostgreSQL serialization/deadlock errors are now re-raised by all product, variation, customer, and order chunk jobs so OCA `queue_job` can retry them instead of recording them as ordinary per-record errors.
- Existing core Odoo deletion policies are preserved for accounting lines, delivery carriers, stock quants, stock moves, and stock valuation layers; the previous custom `ondelete='cascade'` overrides have been disabled to protect accounting and stock history.

### Fixes

- Fixed WooCommerce variation IDs being compared as integers against Odoo `Char` values, which caused unchanged variations to be treated as updates on every sync. Variation summary counts now include all processed variations while correctly distinguishing new, updated, and skipped records.
- Missing parent products now produce a variation sync-summary error instead of silently reporting zero processed variations or exhausting queue job retries.
- Test mode continues to import the first 10 records from each WooCommerce endpoint, but now skips the product deletion check because its intentionally incomplete catalog response cannot safely identify deleted products.
- Test-mode variation import now uses variable products from the same first 10-product sample as product import, preventing variation jobs from targeting parent products outside the test sample.
- Sync cursors and product deletion searches are now scoped to the current WooCommerce connector, preventing one configured store from affecting another store's imports or products.
- Products are no longer deleted and recreated when WooCommerce stock-management mode changes.
- Legacy sync-summary rows with no direction are excluded from the per-direction aggregation.
- Customers created while importing orders are now assigned to the current WooCommerce site; guest orders without a usable mapped customer fall back to the placeholder customer.
- Fixed a possible division by zero while allocating Brazilian freight values across zero-total order lines.
- Fixed Odoo-to-WooCommerce product attribute export creating/searching global attributes by option name instead of by the actual attribute name.
- Added regression tests for variation status aggregation across repeated syncs and for non-destructive product deletion behavior in test mode.

## v16.0.5.0 / v18.0.5.0 / v19.0.5.0 - 2026-08-27

### Features

- Performance: Odoo↔WooCommerce sync (products, product variations, customers, orders, stock quantities) now dispatches chunked queue jobs and uses the WooCommerce REST API batch endpoints (`products/batch`, `products/{id}/variations/batch`) instead of one job/request per record. Lookups (brands, categories, tags, tax rates, units of measure, countries) are cached per batch/chunk to avoid repeated searches.
- WooCommerce webhooks: optional near-real-time sync via `order.created`/`order.updated`/`product.created`/`product.updated`/`customer.created`/`customer.updated` webhooks, validated with an HMAC-SHA256 signature, in addition to the regular polling sync.
- WooCommerce refunds are now converted into real Odoo credit notes (full refunds against a single posted invoice only; partial/ambiguous refunds are left for manual handling).
- WooCommerce order fee lines and coupon lines are now synced as real `sale.order.line` rows instead of only raw JSON.
- Customer/order shipping addresses are now synced to a dedicated `res.partner` delivery contact instead of reusing the billing address.
- Orders with multiple WooCommerce shipping lines now get an order line for each additional shipping method instead of only the first one.
- The queue job chunk size (previously a fixed constant) is now configurable via the `Queue Job Chunk Size` setting (default: 10).
- The HTTP `User-Agent` header sent with WooCommerce REST API requests is now configurable via the `User Agent` setting (default: `Odoo-Woocommerce Sync`).
- After a full sync run finishes, a summary message is posted to the WooCommerce Configuration record's chatter (and optionally also as a Discuss chat - see below), showing a per-direction breakdown (products, product variations, customers, orders) of records processed/new/updated/errors, plus when the run started and, if "Only Sync Modified/New Records" is enabled, the previous sync's timestamp ("since last sync"). Controlled by the `Post Sync Summary to Chatter` setting (enabled by default); shows a Test Mode warning when Test mode is active. Not triggered by webhook-based syncs.
- The sync summary is now also sent as a direct-message Discuss chat from a dedicated "WooCommerce Sync" contact (like the built-in OdooBot conversation), in addition to the chatter message. Controlled by the new `Post Sync Summary to Discuss Chat` setting (enabled by default).
- Sale orders are now locked in Odoo when the WooCommerce order status is `completed` (unlocked again if it moves back to an earlier status).
- Added a `Tax Calculation` setting (`Match Odoo Company Settings` (default), `Tax Included`, `Tax Excluded`) to control whether Odoo taxes/prices created by the sync are tax-included or tax-excluded, independent of WooCommerce's own `Prices entered with tax` setting.
- The WooCommerce Configuration form now opens at a friendly URL (`/odoo/woocommerce-sync/<id>`) instead of `/odoo/action-<id>/<id>` (v18/v19 only; not supported in v16).
- Added automated tests covering per-record savepoint isolation, order fee/coupon lines, order shipping lines, and webhook queue job dispatch.

### Fixes

- Fixed a bug where a single failing record in a sync batch could roll back and discard other records already synced in the same batch; each record's sync is now isolated in its own savepoint.
- `woocommerce_service` is now derived from WooCommerce's `virtual`/`downloadable` flags instead of always being `False`.
- WooCommerce tax is no longer applied to products/variations whose `tax_status` is `none`/`shipping`.
- Added a partial unique index on WooCommerce ID fields (products, variations, customers, orders, refunds) to prevent duplicate records from concurrent sync jobs.
- Added retry/backoff to image downloads, matching the existing WooCommerce REST API resilience.
- Fixed a `NotNullViolation` on `product.template.categ_id` when a synced WooCommerce product has no assigned category; Odoo's own default category is now used instead of forcing an empty value.
- Queue jobs dispatched via `with_delay()`/`delayable()` now show the function name (e.g. `woocommerce.sync.connector.woocommerce_to_odoo_orders_chunk_sync`) in the Job Queue view instead of the function's docstring.
- Fixed `Datetime field expects a naive datetime` error on manual "Synchronize Now" sync (https://github.com/roboes/odoo-woocommerce-sync/issues/12).
- The Brazil CPF/CNPJ/RG/IE billing fields are now read via `.get()` instead of direct key access, preventing a `KeyError` when a plugin doesn't expose one of these fields; CPF vs. CNPJ is now also preferred based on the `_billing_persontype` billing field (pessoa física vs. pessoa jurídica) when available.
- Fixed newly-created tax rates being named e.g. `19.0%`/`7.0%` instead of `19%`/`7%`, causing duplicate taxes instead of reusing an existing, identically-rated tax; whole-number rates no longer get a trailing `.0`.
- WooCommerce product variations are now skipped when not modified since the last sync, matching the existing behavior for products; previously every variation was unconditionally re-written (and logged as updated) on every sync run even when nothing about it had changed.
- Fixed each record's isolated per-record savepoint (product, product variation, customer, order sync) never being released after use, leaking an open savepoint on every record for the rest of the chunk job's transaction; the savepoint is now always released, whether the record synced successfully or was rolled back.

## v16.0.4.2 / v18.0.4.2 / v19.0.4.2 - 2026-07-09

### Features

- Bug fixes and improvements.

## v16.0.4.1 / v18.0.4.1 / v19.0.4.1 - 2026-06-04

### Fixes

- Added `web_icon` to the root menu item across all versions (v16, v18, v19).
- Odoo 19: Commented out the `product_image_ids` (Product Image Gallery) view and model inheritance from `base_multi_image.owner`, as the OCA `base_multi_image` module is not yet ported to Odoo 19. The feature will be re-enabled once `base_multi_image` is available for v19.

## v16.0.4.0 / v18.0.4.0 / v19.0.4.0

### Features

- Added support for `base_multi_image.image` (OCA `base_multi_image` module) as the preferred gallery image backend. When `base_multi_image` is installed, product gallery images synced from WooCommerce are stored as `base_multi_image.image` records and uploaded to WooCommerce from the same source. Falls back to `ir.attachment` when `base_multi_image` is not installed.
- Delivery carriers are now scoped per WooCommerce site URL, preventing conflicts when multiple WooCommerce connections are configured. All delivery carriers now share a single `WooCommerce Shipping Fee` service product, keeping the product catalog clean.
- Bug fixes and improvements.

## v16.0.3.0 / v18.0.3.0 / v19.0.3.0

### Features

- First fully compatible version for Odoo 19, now maintained in its own branch.
- App Configuration Shortcut: Added a functional "Sync Settings" stat-button directly on the Odoo Module info page (Apps > WooCommerce Sync).

### Fixes

- Partner Field Mapping: Migrated `mobile` field mapping to `phone`. This ensures compatibility with Odoo 19 (where mobile was removed from base) while remaining backwards compatible with Odoo 16 and 18.

## v16.0.2.7 / v18.0.2.7 - 2025-11-20

### Features

- Updated codebase for initial Odoo 19 compatibility and easier future migration.

## v16.0.2.6 / v18.0.2.6 - 2025-09-29

### Features

- Bug fixes and improvements.

## v16.0.2.5 / v18.0.2.5 - 2025-09-24

### Features

- Bug fixes and improvements.

## v16.0.2.4 / v18.0.2.4 - 2025-08-18

### Features

- Added support for uploading product images from Odoo to WooCommerce.
- Product image gallery now leverage Odoo's native `ir.attachment` model.

### Improvements

- Optimized stock quantity synchronization for better performance and to prevent unnecessary updates to the product's `write_date` field.

## v16.0.2.3 / v18.0.2.3 - 2025-08-14

### Improvements

- Improved logic of product stock quantity sync.

### Fixes

- Fixed a bug caused by mixing dictionary and attribute access to retrieve variables from Odoo records during WooCommerce to Odoo customer and order synchronization (<https://github.com/roboes/odoo-woocommerce-sync/issues/7>).

## v16.0.2.2 / v18.0.2.2 - 2025-08-13

### Features

- Revamped the stock quantity sync logic.

### Improvements

- Renamed variable from `product_stock_date_updated` to `woocommerce_stock_last_sync`.

## v16.0.2.1 / v18.0.2.1 - 2025-08-12

### Fixes

- Removed unused `woocommerce_api` argument from `odoo_to_woocommerce_products_sync()` after migrating to internal API retrieval in sequential queue job workflow.

## v16.0.2.0 / v18.0.2.0 - 2025-08-08

### Features

- Implemented a new feature to automatically delete products in Odoo if they are no longer found in WooCommerce, which is enabled by default in the configuration setting.

### Improvements

- Improved performance for large WooCommerce stores by implementing a sequential workflow of queue jobs for the synchronization of each individual product, product variation, customer, and order. This prevents `CPU time limit exceeded` errors by ensuring tasks are processed one at a time.
- Renamed WooCommerce-related fields across multiple models to follow a consistent woocommerce\_\* naming convention, improving clarity and alignment with the WooCommerce REST API.

### Fixes

- Fixed logic that caused Odoo to create a variant for every possible attribute combination. The synchronization process now directly creates or updates only the variants that exist in WooCommerce.

## v16.0.1.5 / 18.0.1.5 - 2025-08-03

### Features

- Introduced individual "last sync" tracking fields: `woocommerce_product_woocommerce_to_odoo_last_sync`, `woocommerce_product_variation_woocommerce_to_odoo_last_sync`, `woocommerce_customer_woocommerce_to_odoo_last_sync`,`woocommerce_order_woocommerce_to_odoo_last_sync` and `woocommerce_order_line_woocommerce_to_odoo_last_sync`.

### Improvements

- Added type hints for improved code clarity and static analysis.
- General code optimizations for improved performance.

## v16.0.1.4 / v18.0.1.4 - 2025-07-27

### Features

- Code improvements.

## v16.0.1.3 / v18.0.1.3 - 2025-07-24

### Features

- Added `external_dependencies` to the manifest to prevent module installation if the `woocommerce` Python library is not installed.

### Fixes

- Corrected UoM creation to use the `factor` field instead of the computed, read-only `factor_inv`.
- Fixed duplicate delivery methods creation by properly setting `woocommerce_product_site_url` during carrier creation.
- Updated product type handling for Odoo 18: product classification now uses a combination of `type = 'consu'` and `is_storable = True` instead of a single `type = 'product'` value.

## v16.0.1.2 / v18.0.1.2 - 2025-07-21

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
- Updated image storage logic for products with multiple images. The `Product Image Gallery` is now displayed under the `Sales` tab, following the same UX pattern as Odoo's `website_sale` module.

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

- Added support for WooCommerce Shipping Methods: WooCommerce shipping methods are now imported into Odoo under `Home Menu` → `Sales` → `Configuration` → `Sales Orders` → `Shipping Methods`. Imported Sales Orders from WooCommerce will include the respective `carrier_id` for accurate delivery method assignment.

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

## v16.0.1.0 - 2025-03-03

- Initial release.
