"""Tests for the optional product translations update in connector.py ('settings_woocommerce_to_odoo_products_translations_sync').

The connector writes product texts as the Odoo source text (en_US): without the option, users working in another language keep seeing the text from the product's creation.
"""

from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon, make_woocommerce_product_payload

WOOCOMMERCE_ID = 95001


@tagged('post_install', '-at_install')
class TestProductTranslations(WoocommerceSyncCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['res.lang']._activate_lang('fr_FR')

    def sync_woocommerce_product(self, **payload):
        existing_product = self.env['product.template'].search([('woocommerce_id', '=', str(WOOCOMMERCE_ID))])
        if existing_product:
            payload.setdefault('date_modified_gmt', '2099-01-01T00:00:00')
        self.connector.woocommerce_to_odoo_product_sync(
            make_woocommerce_product_payload(id=WOOCOMMERCE_ID, sku='TEST-TRANSLATIONS', **payload),
            woocommerce_currency=None,
            woocommerce_tax_rates={},
            woocommerce_prices_include_tax=False,
            woocommerce_weight_unit='kg',
            woocommerce_dimension_unit='cm',
            odoo_products={str(WOOCOMMERCE_ID): {'id': existing_product.id}} if existing_product else {},
        )
        return self.env['product.template'].search([('woocommerce_id', '=', str(WOOCOMMERCE_ID))])

    def setUp(self):
        super().setUp()
        self.product = self.sync_woocommerce_product(name='Hockey mask', description='Old description')
        # As in a French Odoo, where the translation is stored next to the source text at creation
        self.product.update_field_translations('name', {'fr_FR': 'Hockey mask'})
        self.product.with_context(lang='fr_FR').write({'description_sale': 'Old description'})

    def text(self, field_name, lang):
        return self.product.with_context(lang=lang)[field_name]

    def test_other_languages_unchanged_by_default(self):
        self.sync_woocommerce_product(name='Hockey mask TEST')

        self.assertEqual(self.text('name', 'en_US'), 'Hockey mask TEST')
        self.assertEqual(self.text('name', 'fr_FR'), 'Hockey mask')

    def test_untranslated_texts_follow_woocommerce_when_enabled(self):
        self.connector.settings_woocommerce_to_odoo_products_translations_sync = True

        self.sync_woocommerce_product(name='Hockey mask TEST', description='New description')

        self.assertEqual(self.text('name', 'fr_FR'), 'Hockey mask TEST')
        self.assertEqual(self.text('description_sale', 'fr_FR'), 'New description')

    def test_empty_source_description_clears_untranslated_translation(self):
        self.connector.settings_woocommerce_to_odoo_products_translations_sync = True

        self.sync_woocommerce_product(description='')

        self.assertEqual(self.text('description_sale', 'en_US'), '')
        self.assertEqual(self.text('description_sale', 'fr_FR'), '')

    def test_translations_edited_in_odoo_are_kept(self):
        self.connector.settings_woocommerce_to_odoo_products_translations_sync = True
        self.product.update_field_translations('name', {'fr_FR': 'Masque de hockey'})

        self.sync_woocommerce_product(name='Hockey mask TEST', description='New description')

        self.assertEqual(self.text('name', 'en_US'), 'Hockey mask TEST')
        self.assertEqual(self.text('name', 'fr_FR'), 'Masque de hockey')
        self.assertEqual(self.text('description_sale', 'fr_FR'), 'New description')
