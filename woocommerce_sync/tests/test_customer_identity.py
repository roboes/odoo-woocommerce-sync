"""Regression tests for customer identity shared by order and customer imports."""

from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon, make_woocommerce_customer_payload


@tagged('post_install', '-at_install')
class TestCustomerIdentity(WoocommerceSyncCommon):
    def test_customer_import_links_unbound_partner_with_same_email(self):
        existing_partner = self.env['res.partner'].create(
            {
                'name': 'Order-created customer',
                'email': 'customer@example.test',
                'woocommerce_site_url': self.connector.settings_woocommerce_connection_url,
            }
        )

        status = self.connector.woocommerce_to_odoo_customer_sync(make_woocommerce_customer_payload(id=3100), {})

        self.assertEqual(status, 'updated')
        self.assertEqual(existing_partner.woocommerce_id, '3100')
        self.assertEqual(
            self.env['res.partner'].search_count([('parent_id', '=', False), ('woocommerce_site_url', '=', self.connector.settings_woocommerce_connection_url), ('email', '=ilike', 'customer@example.test')]),
            1,
        )
