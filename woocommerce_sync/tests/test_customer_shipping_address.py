"""Tests for 'odoo_customer_shipping_address_create_or_update()' in connector.py: creates/updates a 'type=delivery' child contact from WooCommerce shipping address fields, used so 'partner_shipping_id' on synced customers/orders points to the real shipping address instead of the billing partner."""

from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon


@tagged('post_install', '-at_install')
class TestOdooCustomerShippingAddress(WoocommerceSyncCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.odoo_customer = cls.env['res.partner'].create({'name': 'Test Billing Customer', 'woocommerce_site_url': 'https://example.test'})

    def test_no_shipping_data_returns_billing_customer_unchanged(self):
        odoo_shipping_partner = self.connector.odoo_customer_shipping_address_create_or_update(self.odoo_customer, {})
        self.assertEqual(odoo_shipping_partner, self.odoo_customer)

    def test_shipping_data_creates_delivery_child_contact(self):
        shipping_values = {
            'woocommerce_shipping_first_name': 'Jane',
            'woocommerce_shipping_last_name': 'Doe',
            'woocommerce_shipping_address_1': '123 Shipping Lane',
            'woocommerce_shipping_address_2': '',
            'woocommerce_shipping_city': 'Shipville',
            'woocommerce_shipping_postcode': '12345',
            'woocommerce_shipping_country': None,
        }

        odoo_shipping_partner = self.connector.odoo_customer_shipping_address_create_or_update(self.odoo_customer, shipping_values)

        self.assertNotEqual(odoo_shipping_partner, self.odoo_customer)
        self.assertEqual(odoo_shipping_partner.type, 'delivery')
        self.assertEqual(odoo_shipping_partner.parent_id, self.odoo_customer)
        self.assertEqual(odoo_shipping_partner.name, 'Jane Doe')
        self.assertEqual(odoo_shipping_partner.street, '123 Shipping Lane')
        self.assertEqual(odoo_shipping_partner.city, 'Shipville')

    def test_calling_twice_updates_instead_of_duplicating(self):
        shipping_values = {
            'woocommerce_shipping_first_name': 'Jane',
            'woocommerce_shipping_last_name': 'Doe',
            'woocommerce_shipping_address_1': '123 Shipping Lane',
            'woocommerce_shipping_city': 'Shipville',
            'woocommerce_shipping_postcode': '12345',
        }

        first_call_partner = self.connector.odoo_customer_shipping_address_create_or_update(self.odoo_customer, shipping_values)

        updated_shipping_values = dict(shipping_values, woocommerce_shipping_city='New City')
        second_call_partner = self.connector.odoo_customer_shipping_address_create_or_update(self.odoo_customer, updated_shipping_values)

        self.assertEqual(first_call_partner, second_call_partner)
        self.assertEqual(second_call_partner.city, 'New City')

        delivery_children = self.env['res.partner'].search([('parent_id', '=', self.odoo_customer.id), ('type', '=', 'delivery')])
        self.assertEqual(len(delivery_children), 1)
