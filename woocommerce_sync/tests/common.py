"""Shared test fixtures/helpers, kept in one place so every test builds WooCommerce-shaped payloads the same way."""

import base64
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar
from unittest.mock import Mock

from odoo.release import version_info
from odoo.tests.common import TransactionCase

# A minimal 1x1 transparent PNG, used to fake 'image_download()' responses so tests exercising image-download code paths (e.g. a WooCommerce payload with 'images') never make a real HTTP request to a test/fake URL
FAKE_IMAGE_BYTES = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')


def make_fake_image_download() -> Mock:
    """Returns a mock suitable for 'patch.object(..., "image_download", ...)', returning a fake HTTP response wrapping 'FAKE_IMAGE_BYTES' instead of performing a real request."""
    return Mock(return_value=Mock(content=FAKE_IMAGE_BYTES))


class WoocommerceSyncCommon(TransactionCase):
    """Base class for connector.py tests: creates a 'woocommerce.sync.connector' record in 'setUpClass' with fake but well-formed credentials, so test files only declare the settings they actually care about via the 'connector_values' class attribute instead of repeating the same boilerplate dict."""

    connector_values: ClassVar[dict[str, Any]] = {}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.connector = cls.env['woocommerce.sync.connector'].create(
            {
                'settings_woocommerce_connection_name': 'Test connection',
                'settings_woocommerce_connection_url': 'https://example.test',
                'settings_woocommerce_consumer_key': 'ck_test',
                'settings_woocommerce_consumer_secret': 'cs_test',
                **cls.connector_values,
            },
        )


def bump_iso_datetime(iso_string: str, seconds: int = 60) -> str:
    """Returns 'iso_string' shifted forward by 'seconds', to simulate a WooCommerce 'date_modified_gmt' newer than a previous sync's 'write_date'."""
    return (datetime.fromisoformat(iso_string) + timedelta(seconds=seconds)).strftime('%Y-%m-%dT%H:%M:%S')


def storable_product_values(is_storable: bool = True) -> dict[str, Any]:
    """Returns the version-specific field(s) that mark a 'product.template'/'product.product' as stock-managed, matching the branching in models.py/connector.py."""
    if version_info[0] == 16:
        return {'detailed_type': 'product' if is_storable else 'consu'}

    return {'is_storable': is_storable}


def make_woocommerce_product_payload(**overrides: Any) -> dict[str, Any]:
    """Builds a minimal-but-complete WooCommerce 'products' REST API payload (all keys read by 'woocommerce_product_fields()'/'woocommerce_to_odoo_product_sync()'), so tests don't have to know every field the connector reads."""
    now = datetime.now(tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

    payload = {
        'id': 1000,
        'name': 'Test Product',
        'slug': 'test-product',
        'permalink': 'https://example.test/product/test-product',
        'date_created': now,
        'date_created_gmt': now,
        'date_modified': now,
        'date_modified_gmt': now,
        'type': 'simple',
        'status': 'publish',
        'featured': False,
        'catalog_visibility': 'visible',
        'description': 'Test description',
        'short_description': '',
        'sku': 'TEST-SKU-1',
        'price': '10.00',
        'regular_price': '10.00',
        'sale_price': '',
        'date_on_sale_from': None,
        'date_on_sale_from_gmt': None,
        'date_on_sale_to': None,
        'date_on_sale_to_gmt': None,
        'price_html': '',
        'on_sale': False,
        'purchasable': True,
        'total_sales': 0,
        'virtual': False,
        'downloadable': False,
        'downloads': [],
        'download_limit': -1,
        'download_expiry': -1,
        'external_url': '',
        'button_text': '',
        'tax_status': 'taxable',
        'tax_class': '',
        'manage_stock': False,
        'stock_quantity': None,
        'stock_status': 'instock',
        'backorders': 'no',
        'backorders_allowed': False,
        'backordered': False,
        'sold_individually': False,
        'weight': '',
        'dimensions': {'length': '', 'width': '', 'height': ''},
        'shipping_required': True,
        'shipping_taxable': True,
        'shipping_class': '',
        'shipping_class_id': 0,
        'reviews_allowed': True,
        'average_rating': '0.00',
        'rating_count': 0,
        'related_ids': [],
        'upsell_ids': [],
        'cross_sell_ids': [],
        'parent_id': 0,
        'purchase_note': '',
        'categories': [],
        'tags': [],
        'images': [],
        'attributes': [],
        'default_attributes': [],
        'variations': [],
        'grouped_products': [],
        'menu_order': 0,
        'meta_data': [],
        'brands': [],
    }
    payload.update(overrides)

    return payload


def make_woocommerce_order_payload(**overrides: Any) -> dict[str, Any]:
    """Builds a minimal-but-complete WooCommerce 'orders' REST API payload (all keys read by 'woocommerce_to_odoo_order_sync()'), so tests don't have to know every field the connector reads."""
    now = datetime.now(tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

    empty_address = {
        'first_name': '',
        'last_name': '',
        'company': '',
        'address_1': '',
        'address_2': '',
        'city': '',
        'state': '',
        'postcode': '',
        'country': '',
        'email': '',
        'phone': '',
        'cpf': '',
        'cnpj': '',
        'rg': '',
        'ie': '',
    }

    payload = {
        'id': 2000,
        'parent_id': 0,
        'number': '2000',
        'order_key': 'wc_order_test',
        'created_via': 'checkout',
        'version': '9.0.0',
        'status': 'processing',
        'currency': 'USD',
        'date_created': now,
        'date_created_gmt': now,
        'date_modified': now,
        'date_modified_gmt': now,
        'discount_total': '0.00',
        'discount_tax': '0.00',
        'shipping_total': '0.00',
        'shipping_tax': '0.00',
        'cart_tax': '0.00',
        'total': '0.00',
        'total_tax': '0.00',
        'prices_include_tax': False,
        'customer_id': 0,
        'customer_ip_address': '',
        'customer_user_agent': '',
        'customer_note': '',
        'payment_method': '',
        'payment_method_title': '',
        'transaction_id': '',
        'date_paid': None,
        'date_paid_gmt': None,
        'date_completed': None,
        'date_completed_gmt': None,
        'cart_hash': '',
        'meta_data': [],
        'line_items': [],
        'tax_lines': [],
        'shipping_lines': [],
        'fee_lines': [],
        'coupon_lines': [],
        'refunds': [],
        'billing': dict(empty_address),
        'shipping': dict(empty_address),
    }
    payload.update(overrides)

    return payload


def make_woocommerce_customer_payload(**overrides: Any) -> dict[str, Any]:
    """Build a minimal complete WooCommerce customer payload for connector tests."""
    now = datetime.now(tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    empty_address = {
        'first_name': '',
        'last_name': '',
        'company': '',
        'address_1': '',
        'address_2': '',
        'city': '',
        'state': '',
        'postcode': '',
        'country': '',
        'email': '',
        'phone': '',
    }
    payload = {
        'id': 3000,
        'date_created': now,
        'date_created_gmt': now,
        'date_modified': now,
        'date_modified_gmt': now,
        'email': 'customer@example.test',
        'first_name': 'Test',
        'last_name': 'Customer',
        'role': 'customer',
        'username': 'test-customer',
        'is_paying_customer': True,
        'avatar_url': '',
        'meta_data': [],
        'billing': dict(empty_address),
        'shipping': dict(empty_address),
    }
    payload['billing']['email'] = payload['email']
    payload.update(overrides)
    return payload
