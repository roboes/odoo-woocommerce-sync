"""Automated tests for the 'woocommerce_sync' Odoo add-on.

These are standard Odoo tests (odoo.tests.common.TransactionCase / unittest), auto-discovered by Odoo's test runner - they need a real Odoo instance + database to run, they are NOT plain pytest (except test_woocommerce_client.py, which has no Odoo dependency and can also run standalone).

Run against the currently installed Odoo version (16/18/19) - version-specific behavior (e.g. 'detailed_type' vs 'is_storable') is exercised automatically via 'odoo.release.version_info', matching the branching used in the add-on itself. No need to run the suite once per version unless you maintain multiple Odoo versions; each run only exercises the branch for the Odoo version it is actually running against.

To run (adapt the database/module name to your setup, e.g. see tests/odoo-tests.sh):
    odoo-bin -d <database> -i woocommerce_sync --test-enable --test-tags /woocommerce_sync --stop-after-init

Or, inside the docker-based setup already used in tests/odoo-tests.sh:
    docker exec -it <container> odoo --database <database> -u woocommerce_sync \
        --test-enable --test-tags /woocommerce_sync --stop-after-init --no-http

To run only one file/class:
    --test-tags /woocommerce_sync:TestSavepointIsolation
"""

from . import (
    test_customer_identity,
    test_customer_shipping_address,
    test_odoo_to_woocommerce_values,
    test_order_fee_coupon_lines,
    test_order_shipping_lines,
    test_refund_credit_note,
    test_savepoint_isolation,
    test_variation_sync_status,
    test_webhook_queue_job_dispatch,
    test_webhook_signature,
    test_woocommerce_client,
    test_woocommerce_to_odoo_product_sync,
)

__all__ = [
    'test_customer_identity',
    'test_customer_shipping_address',
    'test_odoo_to_woocommerce_values',
    'test_order_fee_coupon_lines',
    'test_order_shipping_lines',
    'test_refund_credit_note',
    'test_savepoint_isolation',
    'test_variation_sync_status',
    'test_webhook_queue_job_dispatch',
    'test_webhook_signature',
    'test_woocommerce_client',
    'test_woocommerce_to_odoo_product_sync',
]
