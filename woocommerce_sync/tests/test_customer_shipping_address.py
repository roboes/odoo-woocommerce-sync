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

    def test_brazilian_extensions_compare_normalized_addresses(self):
        country = self.env.ref('base.br')
        state = self.env['res.country.state'].search([('country_id', '=', country.id), ('code', '=', 'SP')], limit=1)
        shipping = {'address_1': 'Rua A', 'address_2': '', 'number': '10', 'neighborhood': 'Centro', 'country': 'BR', 'state': 'SP', 'city': 'S\u00e3o Paulo', 'postcode': '12345-000'}
        values = {'name': 'Brazilian Billing', 'street': 'Rua A', 'street2': '', 'city': shipping['city'], 'zip': shipping['postcode'], 'country_id': country.id, 'state_id': state.id}
        self.connector.odoo_customer_brazil_values_update(values, shipping)
        billing = self.env['res.partner'].create(values)
        shipping_values = {f'woocommerce_shipping_{key}': value for key, value in shipping.items()}

        result = self.connector.odoo_customer_shipping_address_create_or_update(billing, shipping_values, shipping=shipping)
        self.assertEqual(result, billing, 'Composed Brazilian street/district must not create a spurious delivery child')

        shipping['number'] = '20'
        delivery = self.connector.odoo_customer_shipping_address_create_or_update(billing, shipping_values, shipping=shipping)
        self.assertNotEqual(delivery, billing, 'A different street number requires a delivery child')
        self.assertIn('20', delivery.street)
        self.assertEqual(delivery.district if 'district' in delivery._fields else delivery.street2, 'Centro')

    def test_different_state_requires_delivery_child(self):
        country = self.env.ref('base.us')
        states = self.env['res.country.state'].search([('country_id', '=', country.id)], limit=2)
        self.odoo_customer.write({'street': 'Test Street', 'country_id': country.id, 'state_id': states[0].id})
        shipping_values = {'woocommerce_shipping_address_1': 'Test Street', 'woocommerce_shipping_country': 'US', 'woocommerce_shipping_state': states[1].code}

        delivery = self.connector.odoo_customer_shipping_address_create_or_update(self.odoo_customer, shipping_values)

        self.assertNotEqual(delivery, self.odoo_customer)
        self.assertEqual(delivery.state_id, states[1])
