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

    def test_customer_import_preserves_existing_name_when_payload_has_no_name(self):
        existing_partner = self.env['res.partner'].create(
            {
                'name': 'Manually maintained name',
                'email': 'customer@example.test',
                'woocommerce_site_url': self.connector.settings_woocommerce_connection_url,
            }
        )
        payload = make_woocommerce_customer_payload(id=3105, first_name='', last_name='')

        self.connector.woocommerce_to_odoo_customer_sync(payload, {})

        self.assertEqual(existing_partner.name, 'Manually maintained name')

    def test_customer_without_profile_name_uses_billing_name(self):
        payload = make_woocommerce_customer_payload(id=3101, first_name='', last_name='')
        payload['billing'].update({'first_name': 'Billing', 'last_name': 'Name'})

        self.connector.woocommerce_to_odoo_customer_sync(payload, {})

        partner = self.env['res.partner'].search([('woocommerce_id', '=', '3101')])
        self.assertEqual(partner.name, 'Billing Name')

    def test_customer_without_any_name_uses_email(self):
        self.connector.woocommerce_to_odoo_customer_sync(make_woocommerce_customer_payload(id=3102, first_name='', last_name='', email='nameless@example.test'), {})

        partner = self.env['res.partner'].search([('woocommerce_id', '=', '3102')])
        self.assertEqual(partner.name, 'nameless@example.test')

    def test_customer_without_profile_name_matches_by_billing_name_and_address(self):
        existing_partner = self.env['res.partner'].create(
            {
                'name': 'Billing Name',
                'email': 'old@example.test',
                'city': 'Brest',
                'zip': '29200',
                'woocommerce_site_url': self.connector.settings_woocommerce_connection_url,
            }
        )
        payload = make_woocommerce_customer_payload(id=3106, email='new@example.test', first_name='', last_name='')
        payload['billing'].update({'first_name': 'Billing', 'last_name': 'Name', 'city': 'Brest', 'postcode': '29200'})

        status = self.connector.woocommerce_to_odoo_customer_sync(payload, {})

        self.assertEqual(status, 'updated')
        self.assertEqual(existing_partner.woocommerce_id, '3106')
        self.assertEqual(existing_partner.email, 'new@example.test')
        self.assertEqual(
            self.env['res.partner'].search_count([('parent_id', '=', False), ('woocommerce_site_url', '=', self.connector.settings_woocommerce_connection_url), ('woocommerce_id', '=', '3106')]),
            1,
        )

    def test_customer_namesake_linked_to_another_account_gets_own_contact(self):
        address = {'city': 'Brest', 'postcode': '29200'}
        first_payload = make_woocommerce_customer_payload(id=3103, email='first@example.test', first_name='Same', last_name='Person')
        first_payload['billing'].update(address)
        self.connector.woocommerce_to_odoo_customer_sync(first_payload, {})
        second_payload = make_woocommerce_customer_payload(id=3104, email='second@example.test', first_name='Same', last_name='Person')
        second_payload['billing'].update(address)

        status = self.connector.woocommerce_to_odoo_customer_sync(second_payload, {})

        self.assertEqual(status, 'created')
        first_partner = self.env['res.partner'].search([('woocommerce_id', '=', '3103')])
        second_partner = self.env['res.partner'].search([('woocommerce_id', '=', '3104')])
        self.assertTrue(second_partner)
        self.assertNotEqual(first_partner, second_partner)
        self.assertEqual(first_partner.email, 'first@example.test')
