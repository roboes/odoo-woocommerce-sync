"""Tests for the optional barcode sync in connector.py ('settings_woocommerce_barcodes_sync'):
- Disabled by default: the WooCommerce GTIN ('global_unique_id') is ignored in both directions.
- When enabled, the GTIN becomes the Odoo product barcode, and the Odoo barcode is exported as 'global_unique_id'.
- A GTIN already used by another Odoo product is skipped instead of failing the product sync.
"""

from unittest.mock import Mock, patch

from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon, make_woocommerce_product_payload


@tagged('post_install', '-at_install')
class TestBarcodes(WoocommerceSyncCommon):
    def sync_woocommerce_product(self, **payload):
        existing_product = self.env['product.template'].search([('woocommerce_id', '=', str(payload['id']))])
        if existing_product:
            # A later modification date, otherwise the unchanged product is skipped
            payload.setdefault('date_modified_gmt', '2099-01-01T00:00:00')
        self.connector.woocommerce_to_odoo_product_sync(
            make_woocommerce_product_payload(**payload),
            woocommerce_currency=None,
            woocommerce_tax_rates={},
            woocommerce_prices_include_tax=False,
            woocommerce_weight_unit='kg',
            woocommerce_dimension_unit='cm',
            odoo_products={str(payload['id']): {'id': existing_product.id}} if existing_product else {},
        )
        return self.env['product.template'].search([('default_code', '=', payload['sku'])])

    def test_barcode_sync_is_disabled_by_default(self):
        self.assertFalse(self.connector.settings_woocommerce_barcodes_sync)

        odoo_product = self.sync_woocommerce_product(id=92001, sku='TEST-BARCODE-1', global_unique_id='4006381333931')

        self.assertTrue(odoo_product)
        self.assertFalse(odoo_product.barcode)

    def test_woocommerce_gtin_is_imported_as_barcode(self):
        self.connector.settings_woocommerce_barcodes_sync = True

        odoo_product = self.sync_woocommerce_product(id=92002, sku='TEST-BARCODE-2', global_unique_id=' 4006381333932 ')

        self.assertEqual(odoo_product.barcode, '4006381333932')

    def test_empty_gtin_keeps_odoo_barcode(self):
        self.connector.settings_woocommerce_barcodes_sync = True
        odoo_product = self.sync_woocommerce_product(id=92003, sku='TEST-BARCODE-3', global_unique_id='4006381333933')

        self.sync_woocommerce_product(id=92003, sku='TEST-BARCODE-3', global_unique_id='')

        self.assertEqual(odoo_product.barcode, '4006381333933')

    def test_gtin_used_by_another_product_is_skipped(self):
        self.connector.settings_woocommerce_barcodes_sync = True
        self.env['product.product'].create({'name': 'Existing barcode owner', 'barcode': '4006381333934'})

        odoo_product = self.sync_woocommerce_product(id=92004, sku='TEST-BARCODE-4', global_unique_id='4006381333934')

        self.assertTrue(odoo_product, 'The product should still be synced, only without barcode')
        self.assertFalse(odoo_product.barcode)

    def test_barcode_cache_tracks_products_written_in_the_same_chunk(self):
        self.connector.settings_woocommerce_barcodes_sync = True
        self.env['product.product'].create({'name': 'Existing cached barcode owner', 'barcode': '4006381333940'})
        barcode_cache = self.connector.odoo_barcode_cache_retrieve(['4006381333940'])
        barcode = self.connector.odoo_barcode_retrieve('4006381333940', self.env['product.product'], barcode_cache)

        self.assertIsNone(barcode)

        barcode_cache = self.connector.odoo_barcode_cache_retrieve([])
        barcode_owner = self.env['product.product'].create({'name': 'Chunk barcode owner'})

        self.connector.odoo_barcode_cache_update(barcode_cache, None, '4006381333941', barcode_owner)
        barcode = self.connector.odoo_barcode_retrieve('4006381333941', self.env['product.product'], barcode_cache)

        self.assertIsNone(barcode)

    def test_gtin_used_by_packaging_in_another_company_is_skipped(self):
        if 'product.packaging' not in self.env:
            self.skipTest('Odoo 19 no longer has product.packaging')

        other_company = self.env['res.company'].create({'name': 'Other barcode company'})
        packaging_product = self.env['product.product'].create({'name': 'Packaging barcode owner', 'company_id': other_company.id})
        self.env['product.packaging'].create(
            {
                'name': 'Packaging barcode owner',
                'product_id': packaging_product.id,
                'company_id': other_company.id,
                'barcode': '4006381333939',
            }
        )
        self.connector.settings_woocommerce_barcodes_sync = True

        barcode = self.connector.odoo_barcode_retrieve('4006381333939', self.env['product.product'])

        self.assertIsNone(barcode)

    def test_resync_keeps_own_barcode(self):
        self.connector.settings_woocommerce_barcodes_sync = True
        odoo_product = self.sync_woocommerce_product(id=92005, sku='TEST-BARCODE-5', global_unique_id='4006381333935')

        self.sync_woocommerce_product(id=92005, sku='TEST-BARCODE-5', global_unique_id='4006381333935')

        self.assertEqual(odoo_product.barcode, '4006381333935', "A product's own barcode must not count as a duplicate")

    def test_enabling_the_option_fills_unchanged_products(self):
        odoo_product = self.sync_woocommerce_product(id=92007, sku='TEST-BARCODE-7', global_unique_id='4006381333937', date_modified_gmt='2026-01-01T00:00:00')
        self.assertFalse(odoo_product.barcode)

        self.connector.settings_woocommerce_barcodes_sync = True
        self.sync_woocommerce_product(id=92007, sku='TEST-BARCODE-7', global_unique_id='4006381333937', date_modified_gmt='2026-01-01T00:00:00')

        self.assertEqual(odoo_product.barcode, '4006381333937', 'A product unchanged in WooCommerce should still get its barcode once the option is enabled')

    def test_enabling_the_option_fills_unchanged_variation_barcodes(self):
        self.connector.settings_woocommerce_modified_records_import = False
        parent_payload = make_woocommerce_product_payload(id=92008, sku='TEST-VARIABLE-BARCODE', type='variable', variations=[92009])
        self.connector.woocommerce_to_odoo_product_sync(
            parent_payload,
            woocommerce_currency=None,
            woocommerce_tax_rates={},
            woocommerce_prices_include_tax=False,
            woocommerce_weight_unit='kg',
            woocommerce_dimension_unit='cm',
            odoo_products={},
        )
        variation = make_woocommerce_product_payload(
            id=92009,
            parent_id=92008,
            sku='TEST-VARIABLE-BARCODE-RED',
            type='variation',
            image=None,
            attributes=[{'id': 1, 'name': 'Color', 'option': 'Red'}],
            global_unique_id='4006381333938',
            date_modified_gmt='2026-01-01T00:00:00',
        )

        with (
            patch.object(type(self.connector), 'woocommerce_api_get', return_value=Mock()),
            patch.object(type(self.connector), 'woocommerce_api_get_all_items', return_value=[variation]),
        ):
            self.connector.woocommerce_to_odoo_product_variations_sync(
                parent_payload,
                woocommerce_currency=None,
                woocommerce_tax_rates={},
                woocommerce_prices_include_tax=False,
                woocommerce_weight_unit='kg',
                woocommerce_dimension_unit='cm',
            )
            self.connector.settings_woocommerce_barcodes_sync = True
            counts = self.connector.woocommerce_to_odoo_product_variations_sync(
                parent_payload,
                woocommerce_currency=None,
                woocommerce_tax_rates={},
                woocommerce_prices_include_tax=False,
                woocommerce_weight_unit='kg',
                woocommerce_dimension_unit='cm',
            )

        synced_variant = self.env['product.product'].search([('woocommerce_id', '=', str(variation['id']))])
        self.assertEqual(counts['skipped'], 0)
        self.assertEqual(synced_variant.barcode, '4006381333938')

    def test_barcode_is_exported_as_gtin_only_when_enabled(self):
        odoo_product = self.env['product.template'].create({'name': 'Export barcode', 'default_code': 'TEST-BARCODE-6', 'barcode': '4006381333936'})

        with patch.object(type(self.connector), 'woocommerce_attribute_create_or_retrieve', return_value=None):
            disabled_values = self.connector.odoo_to_woocommerce_product_values(odoo_product, woocommerce_api=None, woocommerce_tax_rates={})
            self.connector.settings_woocommerce_barcodes_sync = True
            enabled_values = self.connector.odoo_to_woocommerce_product_values(odoo_product, woocommerce_api=None, woocommerce_tax_rates={})

        self.assertNotIn('global_unique_id', disabled_values)
        self.assertEqual(enabled_values['global_unique_id'], '4006381333936')
