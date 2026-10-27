"""Robustness tests for connector.py:
- Products deleted from WooCommerce but still referenced in Odoo (orders, stock moves...) are archived instead of failing the whole sync chain.
- Customer and shipping address states are mapped from WooCommerce state codes ('CA', 'DE-BY'...) or names.
- Internal connector writes made by sync jobs do not rewrite the scheduled 'ir.cron' record.
"""

from unittest.mock import MagicMock, patch

from odoo.tests.common import tagged
from odoo.tools import mute_logger

from .common import WoocommerceSyncCommon, make_woocommerce_customer_payload


@tagged('post_install', '-at_install')
class TestSyncRobustness(WoocommerceSyncCommon):
    def test_deleted_products_are_deleted_or_archived_when_referenced(self):
        site_url = self.connector.settings_woocommerce_connection_url
        unreferenced_product = self.env['product.template'].create({'name': 'Deleted unreferenced', 'woocommerce_site_url': site_url, 'woocommerce_id': '99001'})
        referenced_product = self.env['product.template'].create({'name': 'Deleted referenced', 'woocommerce_site_url': site_url, 'woocommerce_id': '99002', 'taxes_id': [(6, 0, [])]})
        kept_product = self.env['product.template'].create({'name': 'Still in WooCommerce', 'woocommerce_site_url': site_url, 'woocommerce_id': '99003'})
        partner = self.env['res.partner'].create({'name': 'Deletion customer'})
        self.env['sale.order'].create({'partner_id': partner.id, 'order_line': [(0, 0, {'product_id': referenced_product.product_variant_id.id, 'product_uom_qty': 1})]})

        with (
            patch.object(type(self.connector), 'woocommerce_api_get', return_value=MagicMock()),
            patch.object(type(self.connector), 'woocommerce_api_get_all_items', return_value=[{'id': 99003}]),
            mute_logger('odoo.sql_db'),
        ):
            self.connector.woocommerce_to_odoo_products_delete()

        self.assertFalse(unreferenced_product.exists())
        self.assertTrue(referenced_product.exists())
        self.assertFalse(referenced_product.active)
        self.assertTrue(kept_product.active)

    def test_state_retrieved_by_code_prefixed_code_or_name(self):
        country = self.env.ref('base.de')
        state = self.env['res.country.state'].create({'country_id': country.id, 'code': 'ZZ', 'name': 'Testland'})

        self.assertEqual(self.connector.odoo_state_retrieve(country.id, 'ZZ'), state)
        self.assertEqual(self.connector.odoo_state_retrieve(country.id, 'DE-ZZ'), state)
        self.assertEqual(self.connector.odoo_state_retrieve(country.id, 'testland'), state)
        self.assertFalse(self.connector.odoo_state_retrieve(country.id, ''))
        self.assertFalse(self.connector.odoo_state_retrieve(False, 'ZZ'))

    def test_customer_billing_and_shipping_states_are_mapped(self):
        us_state = self.env['res.country.state'].search([('country_id', '=', self.env.ref('base.us').id), ('code', '=', 'CA')], limit=1)
        br_state = self.env['res.country.state'].search([('country_id', '=', self.env.ref('base.br').id), ('code', '=', 'SP')], limit=1)
        payload = make_woocommerce_customer_payload(id=99101, email='state@example.test')
        payload['billing'].update({'address_1': '1 Billing St', 'city': 'Los Angeles', 'postcode': '90001', 'country': 'US', 'state': 'CA', 'email': 'state@example.test'})
        payload['shipping'].update({'first_name': 'Ship', 'last_name': 'To', 'address_1': 'Rua 1', 'city': 'São Paulo', 'postcode': '01000-000', 'country': 'BR', 'state': 'SP'})

        self.connector.woocommerce_to_odoo_customer_sync(payload, {})

        customer = self.env['res.partner'].search([('woocommerce_site_url', '=', self.connector.settings_woocommerce_connection_url), ('woocommerce_id', '=', '99101')])
        self.assertEqual(customer.state_id, us_state)
        shipping_address = customer.child_ids.filtered(lambda partner: partner.type == 'delivery')
        self.assertEqual(shipping_address.state_id, br_state)

    def test_internal_writes_do_not_rewrite_cron(self):
        with patch.object(type(self.connector), 'cron_job_update') as mocked_cron_job_update:
            self.connector.write({'sync_summary_run_active': True})
            mocked_cron_job_update.assert_not_called()

            self.connector.write({'settings_woocommerce_sync_scheduled_interval_minutes': 15})
            mocked_cron_job_update.assert_called_once()
