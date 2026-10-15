# Tests

Automated tests for the `woocommerce_sync` addon, using Odoo's standard `TransactionCase` test framework. They are auto-discovered by Odoo (via `tests/__init__.py`) - no `__manifest__.py` changes are required to enable them.

## What is covered

| File | Covers |
| --- | --- |
| `common.py` | Shared fixtures: `WoocommerceSyncCommon` base test class (creates the test connector), WooCommerce product/order payload builders, version-aware storable product values helper (not a test file itself). |
| `test_woocommerce_client.py` | `WooCommerceClient`: batching, chunking, retry/backoff. No Odoo database needed, but Odoo must be importable. |
| `test_webhook_signature.py` | Webhook HMAC signature validation (`WoocommerceSyncWebhookController._signature_valid`). |
| `test_webhook_queue_job_dispatch.py` | `woocommerce_webhook_process()`: dispatches the correct `with_delay()` job per topic, with a stable `identity_key`, and is a no-op for missing resource ids/unsupported topics. |
| `test_savepoint_isolation.py` | Regression test: one product failing during a sync must not roll back other, already-processed products in the same batch. |
| `test_refund_credit_note.py` | WooCommerce order refunds being converted into Odoo credit notes (full refund only; partial refunds are left untouched; idempotent on reprocessing). |
| `test_odoo_to_woocommerce_values.py` | Odoo -> WooCommerce product payload building, including Odoo-version-specific fields and bounded WordPress media lookup/upload requests. |
| `test_woocommerce_to_odoo_product_sync.py` | `woocommerce_service` mapping from `virtual`/`downloadable`, and tax only applied when WooCommerce `tax_status` is `taxable`. |
| `test_chunk_job_payload.py` | Chunk jobs only receive the slice of the Odoo lookup maps (products, customers, orders, stock) matching their records, instead of the whole map. |
| `test_customer_shipping_address.py` | `odoo_customer_shipping_address_create_or_update()`: creates/updates a `type=delivery` child contact from WooCommerce shipping fields instead of duplicating it on resync. |
| `test_order_fee_coupon_lines.py` | WooCommerce order fee lines/coupon lines synced as real `sale.order.line` rows, idempotent on resync. |
| `test_order_shipping_lines.py` | Orders with multiple WooCommerce shipping lines: extra lines beyond the first are added as order lines, idempotent on resync. |
| `test_barcodes.py` | Optional barcode sync: WooCommerce GTIN (`global_unique_id`) to Odoo barcode and back, disabled by default, duplicate barcodes skipped instead of failing the product. |
| `test_order_reimport_delivered.py` | Re-importing an order already delivered or invoiced in Odoo keeps its lines and only syncs its status (no failure on the delivered/invoiced delivery line); undelivered orders still get their lines updated. |
| `test_product_translations.py` | Optional update of product names and sales descriptions in the other active Odoo languages: off by default, only translations still equal to the previous source text follow WooCommerce, translations edited in Odoo are kept. |
| `test_odoo_to_woocommerce_order_status_sync.py` | Odoo -> WooCommerce order status push: skipped when the WooCommerce status changed since the last import; with "Mark WooCommerce orders completed when delivered?", a fully delivered order is pushed as `completed` without being locked. |
| `test_stock_available_quantity.py` | Stock sync compares and pushes the quantity left for sale (on hand minus undelivered outgoing moves), so undelivered WooCommerce orders are not decremented twice; with "Odoo is the stock master?", newer WooCommerce stock never overwrites Odoo. |
| `test_tax_rates.py` | WooCommerce tax rates matched to existing localized Odoo sale taxes by percentage (scope, fiscal country, tax account, default sale tax, exact name), store location (country/state) rate selection per tax class, decimal rates kept. |

## Running the tests

Run against whichever Odoo version/database you're testing (16, 18 or 19 - the tests read `odoo.release.version_info` to adapt expectations automatically):

```sh
# Settings
system_user="website"
database_name="${system_user}_odoo"
```

```sh
odoo-bin --database ${database_name} --update woocommerce_sync --test-enable --test-tags /woocommerce_sync --stop-after-init
```

Or, inside the project's Docker container (see `tests/odoo-tests.sh` at the repo root, which already includes this as one of its steps):

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
