# Tests

Automated tests for the `woocommerce_sync` addon, using Odoo's standard `TransactionCase` test framework. They are auto-discovered by Odoo (via `tests/__init__.py`) - no `__manifest__.py` changes are required to enable them.

## What is covered

| File | Covers |
| --- | --- |
| `common.py` | Shared fixtures: `WoocommerceSyncCommon` base test class (creates the test connector), WooCommerce product/order payload builders, version-aware storable product values helper (not a test file itself). |
| `test_woocommerce_client.py` | `WooCommerceClient`: batching, chunking, retry/backoff. No Odoo database needed, but Odoo must be importable. |
| `test_webhook_signature.py` | Webhook HMAC signature validation (`WoocommerceSyncWebhookController._signature_valid`). |
| `test_webhook_queue_job_dispatch.py` | `woocommerce_webhook_process()`: dispatches the correct `with_delay()` job per topic, with a stable `identity_key`, and is a no-op for missing resource ids/unsupported topics. |
| `test_image_gallery_errors.py` | A database error while storing a gallery image only skips that image (the transaction stays usable), a failed pending product write reaches the product sync instead of being logged as an image error, and serialization failures, deadlocks and unavailable locks propagate through the gallery and scheduled product chunk handlers for queue_job retry. |
| `test_savepoint_isolation.py` | Regression test: one product failing during a sync must not roll back other, already-processed products in the same batch. |
| `test_refund_credit_note.py` | WooCommerce order refunds being converted into Odoo credit notes (full refund only; partial refunds are left untouched; idempotent on reprocessing). |
| `test_odoo_to_woocommerce_values.py` | Odoo -> WooCommerce product payload building, including Odoo-version-specific fields and bounded WordPress media lookup/upload requests. |
| `test_woocommerce_to_odoo_product_sync.py` | `woocommerce_service` mapping from `virtual`/`downloadable`, and tax only applied when WooCommerce `tax_status` is `taxable`. |
| `test_chunk_job_payload.py` | Chunk jobs only receive the slice of the Odoo lookup maps (products, customers, orders, stock) matching their records, instead of the whole map. |
| `test_customer_company.py` | Customers with a WooCommerce billing company imported as Odoo companies, person kept as a `type=contact` child and updated on resync, Brazilian individual/company precedence, Germany/US country mapping, and manually set Odoo company type preserved. |
| `test_customer_shipping_address.py` | Delivery child creation/resync, state differences and normalized Brazilian street numbers/neighborhoods. |
| `test_order_fee_coupon_lines.py` | Fee/coupon order lines, payment-method-specific fee selection, PagBank quotes versus buyer installment interest, quote currency validation, known-zero fees and clearing stale fees/payouts on resync. |
| `test_order_shipping_lines.py` | Multiple shipping lines, real-cost carriers, preserving manual lines and resync cleanup. With the Odoo Community Association (OCA) addon `l10n_br_sale` installed: freight is charged once, rounded to the currency precision, and reset on free-shipping resync. |
| `test_order_guest_customer.py` | Guest-order customer identity and idempotent reuse, including company billing details with the person retained as a child contact. |
| `test_barcodes.py` | Optional barcode sync: WooCommerce GTIN (`global_unique_id`) to Odoo barcode and back, disabled by default, duplicate barcodes skipped instead of failing the product. |
| `test_order_reimport_delivered.py` | Re-importing an order already delivered or invoiced in Odoo keeps its lines and only syncs its status (no failure on the delivered/invoiced delivery line); undelivered orders still get their lines updated. |
| `test_product_translations.py` | Optional update of product names and sales descriptions in the other active Odoo languages: off by default, only translations still equal to the previous source text follow WooCommerce, translations edited in Odoo are kept. |
| `test_products_delete.py` | Product deletion check: products no longer published in WooCommerce (draft, private...) are archived, even when the `product.updated` webhook was missed; products are checked by ID in chunks of one page, so products trashed or deleted during the check cannot hide other products (an offset-paginated listing would shift its pages). |
| `test_odoo_to_woocommerce_order_status_sync.py` | Odoo -> WooCommerce order status push: skipped when the WooCommerce status changed since the last import; with "Mark WooCommerce orders completed when delivered?", a fully delivered order is pushed as `completed` without being locked. |
| `test_stock_available_quantity.py` | Stock sync compares and pushes the quantity left for sale (on hand minus undelivered outgoing moves), so undelivered WooCommerce orders are not decremented twice; with "Odoo is the stock master?", newer WooCommerce stock never overwrites Odoo. |
| `test_tax_rates.py` | WooCommerce tax rates matched to existing localized Odoo sale taxes by percentage (scope, fiscal country, tax account, default sale tax, exact name), store location (country/state) rate selection per tax class, decimal rates kept. |
| `test_order_line_pricing_taxes.py` | Order line prices and taxes: coupon discounts carried by the line `discount` column (net price kept when the rounded percentage cannot reproduce the WooCommerce total), the tax rate actually charged (`tax_lines[].rate_percent`) instead of the store's base rate, equal-rate tax components kept distinct and compound taxes applied on top of the previous ones, no Odoo tax on untaxed lines, tax-included prices taken from the gross line amounts WooCommerce charged (no per-unit tax rounding drift). |
| `test_order_status_lifecycle.py` | Orders cancelled in WooCommerce are cancelled in Odoo without relying on a version-specific cancellation wizard, custom WooCommerce order statuses are imported, only WooCommerce orders bypass Odoo's confirmed order deletion restriction. |
| `test_product_pricing_identity.py` | Per-variation prices mapped to the template list price + attribute extra prices, regular price instead of sale price, tax conversion only for taxable products, export price conversion to the store's tax convention, first import linking an existing product by SKU, related product IDs parsing. |
| `test_sync_robustness.py` | Products deleted in WooCommerce but referenced in Odoo are archived instead of failing the sync, customer/shipping states mapped from WooCommerce state codes or names, internal connector writes do not rewrite the scheduled action. |
| `test_global_setups.py` | Managers adding custom order statuses, weight/volume unit conversion (kg/lb, m³), product unit of measure kept, stock changes as inventory moves without Stock Manager rights, foreign currency orders and pricelists, jobs in the connector's company and orders in its warehouse, webhook settings cache, order status push dispatch, chunked stock variation retrieval. Brazilian fields without localization and, when the official Odoo `l10n_br` localization or the Odoo Community Association (OCA) addon `l10n_br_base` is installed, individual/company tax identifiers, company classification, legal name, municipality errors and preserving existing tax identifiers. |

## Running the tests

Run against whichever Odoo version/database you're testing (16, 17, 18, 19 or 20 - the tests read `odoo.release.version_info` and field availability to adapt expectations):

Brazilian localization cases are conditional: use a database initialized with `woocommerce_sync,l10n_br` for official Odoo, or `woocommerce_sync,l10n_br_sale` for the Odoo Community Association (OCA) localization (with its addon/Python dependencies available). Test these stacks separately. Standard-delivery fixtures explicitly disable the localization's default fiscal operation; the fiscal-freight case opts into one and verifies paid/free shipping, discounts and a one-cent remainder.

### Disposable Docker run (no host Odoo installation)

Run from the repository root. The Docker runner defaults to the active manifest's major/minor version (for example, `16.0` for `16.0.6.6`); reading the manifest requires `python3` on the host:

```sh
bash tests/docker-tests.sh
```

`ODOO_VERSION=16.0 bash tests/docker-tests.sh` explicitly overrides the Docker image and OCA branch versions; it does not change the module manifest.

Each version-specific checkout should already have matching manifest version, dependency and view entries. To test another Odoo version from the `16.0` checkout, use a temporary copy and select the matching manifest version and views; for 17/19/20, also select the dependency list without `base_multi_image`. Do not switch the `16.0` checkout's manifest to another Odoo version just for testing. The Docker runner installs `openupgradelib`, declared as a Python dependency by OCA `queue_job` on Odoo 19.

### Existing Odoo installation

```sh
# Settings
system_user="website"
database_name="${system_user}_odoo"
```

```sh
odoo-bin --database ${database_name} --update woocommerce_sync --test-enable --test-tags /woocommerce_sync --stop-after-init
```

Or, inside an existing Odoo container (the deployment-specific `tests/odoo-tests.sh` does not enable tests as written):

```sh
docker exec -it odoo_server_${system_user} \
    odoo \
    --database ${database_name} \
    --update woocommerce_sync \
    --test-enable \
    --test-tags /woocommerce_sync \
    --no-http \
    --stop-after-init \
    --workers 0 \
    --http-port 8070
```

To run a single test file/class/method, narrow the `--test-tags` filter, e.g.:

```sh
--test-tags /woocommerce_sync:TestSavepointIsolation
--test-tags /woocommerce_sync:TestSavepointIsolation.test_failure_on_second_product_does_not_roll_back_first_product
```

Check the container logs (or stdout, with `--stop-after-init`) for `FAIL`/`ERROR` entries - Odoo does not otherwise change its exit code based on test results in older versions, so logs are the source of truth.

## Notes

- Tests never call the real WooCommerce REST API; HTTP calls are mocked via `unittest.mock.patch`.
- A fresh/disposable database is recommended, since `TransactionCase` tests run inside a transaction that is rolled back after each test, but the module installation itself still writes to the database.
