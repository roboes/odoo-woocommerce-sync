"""Customers with a WooCommerce billing company become Odoo companies, with the person kept as a contact."""

from datetime import datetime, timedelta, timezone

from odoo.tests.common import tagged

from .common import WoocommerceSyncCommon, make_woocommerce_customer_payload


def _company_payload(**overrides):
    payload = make_woocommerce_customer_payload(**overrides)
    payload['billing'].update({'company': 'Grand Hotel', 'phone': '0102030405', 'city': 'Paris', 'postcode': '75116', 'country': 'FR'})
    return payload


def _later(payload, minutes=5):
    later = (datetime.now(tz=timezone.utc) + timedelta(minutes=minutes)).strftime('%Y-%m-%dT%H:%M:%S')
    payload.update({'date_modified': later, 'date_modified_gmt': later})
    return payload


@tagged('post_install', '-at_install')
class TestCustomerCompany(WoocommerceSyncCommon):
    def _partner(self, woocommerce_id):
        return self.env['res.partner'].search([('woocommerce_id', '=', str(woocommerce_id))])

    def _contacts(self, partner):
        return self.env['res.partner'].with_context(active_test=False).search([('parent_id', '=', partner.id), ('type', '=', 'contact')])

    def test_customer_with_billing_company_is_imported_as_company(self):
        self.connector.woocommerce_to_odoo_customer_sync(_company_payload(id=3200, first_name='Jane', last_name='Doe'), {})

        partner = self._partner(3200)
        self.assertTrue(partner.is_company)
        self.assertEqual(partner.name, 'Grand Hotel')
        contact = self._contacts(partner)
        self.assertEqual(len(contact), 1)
        self.assertEqual(contact.name, 'Jane Doe')
        self.assertEqual(contact.email, 'customer@example.test')
        self.assertEqual(contact.phone, '0102030405')
        self.assertFalse(contact.woocommerce_id)

    def test_customer_without_billing_company_stays_person(self):
        self.connector.woocommerce_to_odoo_customer_sync(make_woocommerce_customer_payload(id=3201), {})

        partner = self._partner(3201)
        self.assertFalse(partner.is_company)
        self.assertEqual(partner.name, 'Test Customer')
        self.assertFalse(self._contacts(partner))

    def test_existing_person_becomes_company_when_billing_company_is_added(self):
        self.connector.woocommerce_to_odoo_customer_sync(make_woocommerce_customer_payload(id=3202), {})
        partner = self._partner(3202)
        self.assertFalse(partner.is_company)

        self.connector.woocommerce_to_odoo_customer_sync(_later(_company_payload(id=3202)), {'3202': partner.read(['id', 'name', 'active', 'write_date', 'woocommerce_id'])[0]})

        self.assertTrue(partner.is_company)
        self.assertEqual(partner.name, 'Grand Hotel')
        self.assertEqual(self._contacts(partner).name, 'Test Customer')

    def test_resync_updates_the_same_contact(self):
        self.connector.woocommerce_to_odoo_customer_sync(_company_payload(id=3203, first_name='Jane', last_name='Doe'), {})
        partner = self._partner(3203)

        payload = _later(_company_payload(id=3203, first_name='Janet', last_name='Doe', email='janet@example.test'))
        self.connector.woocommerce_to_odoo_customer_sync(payload, {'3203': partner.read(['id', 'name', 'active', 'write_date', 'woocommerce_id'])[0]})

        contact = self._contacts(partner)
        self.assertEqual(len(contact), 1)
        self.assertEqual(contact.name, 'Janet Doe')
        self.assertEqual(contact.email, 'janet@example.test')

    def test_company_set_in_odoo_is_kept_when_woocommerce_has_no_company(self):
        self.connector.woocommerce_to_odoo_customer_sync(make_woocommerce_customer_payload(id=3204), {})
        partner = self._partner(3204)
        partner.company_type = 'company'

        self.connector.woocommerce_to_odoo_customer_sync(_later(make_woocommerce_customer_payload(id=3204, last_name='Renamed')), {'3204': partner.read(['id', 'name', 'active', 'write_date', 'woocommerce_id'])[0]})

        self.assertTrue(partner.is_company)
        self.assertEqual(partner.name, 'Test Renamed')

    def test_company_without_person_name_gets_no_contact(self):
        payload = _company_payload(id=3205, first_name='', last_name='')

        self.connector.woocommerce_to_odoo_customer_sync(payload, {})

        partner = self._partner(3205)
        self.assertTrue(partner.is_company)
        self.assertFalse(self._contacts(partner))
