"""Tests for setups beyond a single-company, single-currency, metric store (connector.py):
- Managers can add custom WooCommerce order statuses to the import filter.
- WooCommerce weights/dimensions are converted to Odoo's units, and the WooCommerce weight unit is no longer used as sales unit of measure.
- WooCommerce stock changes are applied as validated inventory adjustment moves, without requiring Stock Manager rights.
- Orders in another currency use a pricelist in that currency when one exists.
- Sync jobs run in the connector's company, and imported orders use the connector's warehouse.
- Webhook store settings are cached, the order status push only dispatches orders to push, and stock variations are retrieved per chunk of products.
- Brazilian checkout plugin fields are mapped to the fields available in the database.
"""

import time
from typing import ClassVar
from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.tests.common import tagged

from .common import (
    WoocommerceSyncCommon,
    make_woocommerce_customer_payload,
    make_woocommerce_order_payload,
    make_woocommerce_product_payload,
    storable_product_values,
)


def make_line_item(line_id: int) -> dict:
    return {
        'id': line_id,
        'name': 'Item',
        'product_id': 0,
        'variation_id': 0,
        'quantity': 1,
        'tax_class': '',
        'subtotal': '100.00',
        'subtotal_tax': '0.00',
        'total': '100.00',
        'total_tax': '0.00',
        'taxes': [],
        'meta_data': [],
        'sku': '',
        'price': 100.0,
    }


def create_user(env, login: str, group_xmlids: list[str]):
    groups_field = 'group_ids' if 'group_ids' in env['res.users']._fields else 'groups_id'
    return env['res.users'].create({'name': login, 'login': login, groups_field: [(6, 0, [env.ref(xmlid).id for xmlid in group_xmlids])]})


@tagged('post_install', '-at_install')
class TestGlobalSetups(WoocommerceSyncCommon):
    connector_values: ClassVar[dict] = {'settings_woocommerce_orders_customers_map': False, 'settings_woocommerce_line_items_product_map': False}

    def _sync_order(self, woocommerce_order):
        self.connector.woocommerce_to_odoo_order_sync(woocommerce_order, woocommerce_tax_rates={}, woocommerce_weight_unit='kg', woocommerce_shipping_methods=[], odoo_sale_orders={})
        return self.env['sale.order'].search([('woocommerce_site_url', '=', self.connector.settings_woocommerce_connection_url), ('woocommerce_id', '=', str(woocommerce_order['id']))], limit=1)

    # Order statuses

    def test_manager_can_add_custom_order_status(self):
        manager = create_user(self.env, 'woocommerce-status-manager', ['woocommerce_sync.group_woocommerce_sync_manager'])

        status = self.env['woocommerce.sync.order.status'].with_user(manager).create({'name': 'Shipped', 'status': 'shipped'})

        self.assertEqual(status.status, 'shipped')

    # Units

    def test_weight_and_volume_are_converted_to_odoo_units(self):
        self.assertAlmostEqual(self.connector.woocommerce_weight_to_odoo_weight('500', 'g'), 0.5)
        self.assertAlmostEqual(self.connector.woocommerce_weight_to_odoo_weight('2', 'lbs'), 0.90718474, places=4)
        self.assertAlmostEqual(self.connector.woocommerce_weight_to_odoo_weight('3', 'stone'), 3.0, msg='Unknown units are kept unchanged')
        self.assertAlmostEqual(self.connector.woocommerce_dimensions_to_odoo_volume({'length': '10', 'width': '20', 'height': '30'}, 'cm'), 0.006)
        self.assertFalse(self.connector.woocommerce_dimensions_to_odoo_volume({'length': '10', 'width': '', 'height': '30'}, 'cm'))
        self.assertAlmostEqual(self.connector.odoo_weight_to_woocommerce_weight(0.5, 'g'), 500.0)

    def test_weight_converted_to_pounds_when_odoo_uses_pounds(self):
        self.env['ir.config_parameter'].sudo().set_param('product.weight_in_lbs', '1')

        self.assertAlmostEqual(self.connector.woocommerce_weight_to_odoo_weight('1', 'kg'), 2.20462262, places=4)
        self.assertAlmostEqual(self.connector.odoo_weight_to_woocommerce_weight(2.20462262, 'kg'), 1.0, places=4)

    def test_product_import_converts_weight_and_keeps_its_unit_of_measure(self):
        self.connector.settings_woocommerce_products_package_size_unit_default = False

        self.connector.woocommerce_to_odoo_product_sync(
            make_woocommerce_product_payload(id=99601, sku='UNIT-SKU-1', weight='500', dimensions={'length': '100', 'width': '50', 'height': '40'}),
            woocommerce_currency=None,
            woocommerce_tax_rates={},
            woocommerce_prices_include_tax=False,
            woocommerce_weight_unit='g',
            woocommerce_dimension_unit='cm',
            odoo_products={},
        )

        odoo_product = self.env['product.template'].search([('default_code', '=', 'UNIT-SKU-1')])
        self.assertAlmostEqual(odoo_product.weight, 0.5)
        # 100 x 50 x 40 cm (Odoo stores volume with its 'Volume' precision, 2 decimals by default)
        self.assertAlmostEqual(odoo_product.volume, 0.2)
        self.assertEqual(odoo_product.uom_id, self.env.ref('uom.product_uom_unit'), 'The WooCommerce weight unit must not become the sales unit of measure')

    # Stock

    def test_stock_change_creates_inventory_move_without_stock_manager_rights(self):
        stock_user = create_user(self.env, 'woocommerce-stock-user', ['base.group_user', 'stock.group_stock_user', 'woocommerce_sync.group_woocommerce_sync_operator'])
        odoo_product = self.env['product.product'].create({'name': 'Inventory move product', **storable_product_values()})
        stock_location = self.connector.settings_woocommerce_products_warehouse_location.lot_stock_id

        self.connector.with_user(stock_user).odoo_stock_inventory_adjust(odoo_product, stock_location, 5.0)
        self.connector.with_user(stock_user).odoo_stock_inventory_adjust(odoo_product, stock_location, -2.0)

        self.assertEqual(odoo_product.with_context(location=stock_location.id).qty_available, 3.0)
        inventory_moves = self.env['stock.move'].search([('product_id', '=', odoo_product.id), ('is_inventory', '=', True), ('state', '=', 'done')])
        self.assertEqual(sorted(inventory_moves.mapped('product_uom_qty')), [2.0, 5.0])

    # Currency

    def _foreign_currencies(self):
        currencies = self.env['res.currency'].with_context(active_test=False).search([('name', 'in', ('CHF', 'SEK')), ('id', '!=', self.env.company.currency_id.id)], order='name')
        currencies.write({'active': True})
        return currencies

    def test_foreign_currency_order_uses_matching_pricelist(self):
        currency = self._foreign_currencies()[:1]
        pricelist = self.env['product.pricelist'].create({'name': f'WooCommerce {currency.name}', 'currency_id': currency.id})

        odoo_sale_order = self._sync_order(make_woocommerce_order_payload(id=99701, number='99701', currency=currency.name, line_items=[make_line_item(71)]))

        self.assertEqual(odoo_sale_order.pricelist_id, pricelist)
        self.assertEqual(odoo_sale_order.currency_id, currency)
        self.assertAlmostEqual(odoo_sale_order.amount_untaxed, 100.0)

    def test_foreign_currency_order_without_pricelist_is_imported_with_a_warning(self):
        currency = self._foreign_currencies()[1:2]
        self.env['product.pricelist'].with_context(active_test=False).search([('currency_id', '=', currency.id)]).write({'active': False})

        odoo_sale_order = self._sync_order(make_woocommerce_order_payload(id=99702, number='99702', currency=currency.name, line_items=[make_line_item(72)]))

        self.assertTrue(odoo_sale_order, 'The order must still be imported')
        self.assertNotEqual(odoo_sale_order.currency_id, currency)
        self.assertTrue(any(currency.name in (message.body or '') for message in odoo_sale_order.message_ids))

    # Company

    def test_company_warehouse_default_and_server_side_validation(self):
        company = self.env['res.company'].create({'name': 'Warehouse company'})
        connector = self.env['woocommerce.sync.connector'].create({'settings_woocommerce_connection_name': 'Company default', 'company_id': company.id})
        self.assertEqual(connector.settings_woocommerce_products_warehouse_location.company_id, company)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            connector.write({'settings_woocommerce_products_warehouse_location': self.connector.settings_woocommerce_products_warehouse_location.id})

    def test_company_can_change_before_sync_but_not_after(self):
        company = self.env['res.company'].create({'name': 'New connector company'})
        warehouse = self.env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
        connector = self.env['woocommerce.sync.connector'].create({'settings_woocommerce_connection_name': 'Not configured yet'})
        connector.write({'company_id': company.id, 'settings_woocommerce_products_warehouse_location': warehouse.id})
        self.assertEqual(connector.company_id, company)
        self.env['product.template'].create({'name': 'Linked product', 'woocommerce_site_url': self.connector.settings_woocommerce_connection_url, 'woocommerce_id': '99704'})
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.connector.write({'company_id': company.id, 'settings_woocommerce_products_warehouse_location': warehouse.id})

    def test_jobs_run_in_connector_company_and_orders_use_its_warehouse(self):
        company = self.env['res.company'].create({'name': 'WooCommerce second company'})
        warehouse = self.env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
        connector = self.env['woocommerce.sync.connector'].create(
            {
                'settings_woocommerce_connection_url': 'https://second-company.example.test',
                'settings_woocommerce_consumer_key': 'ck_test',
                'settings_woocommerce_consumer_secret': 'cs_test',
                'company_id': company.id,
                'settings_woocommerce_products_warehouse_location': warehouse.id,
                'settings_woocommerce_orders_customers_map': False,
                'settings_woocommerce_line_items_product_map': False,
            }
        )

        self.assertEqual(connector.delayable().recordset.env.company, company)

        company_connector = connector.woocommerce_company_env()
        company_connector.woocommerce_to_odoo_order_sync(
            make_woocommerce_order_payload(id=99801, number='99801', line_items=[make_line_item(81)]), woocommerce_tax_rates={}, woocommerce_weight_unit='kg', woocommerce_shipping_methods=[], odoo_sale_orders={}
        )
        odoo_sale_order = self.env['sale.order'].search([('woocommerce_site_url', '=', connector.settings_woocommerce_connection_url), ('woocommerce_id', '=', '99801')])
        self.assertEqual(odoo_sale_order.company_id, company)
        self.assertEqual(odoo_sale_order.warehouse_id, warehouse)

    # Efficiency

    def test_webhook_store_settings_are_cached(self):
        type(self.connector)._woocommerce_webhook_settings_cache.pop((self.connector.id, self.connector.settings_woocommerce_connection_url), None)
        woocommerce_api = MagicMock()
        woocommerce_api.get.return_value.json.return_value = {'value': 'no'}

        with patch.object(type(self.connector), 'woocommerce_tax_rates_retrieve', return_value={}) as mocked_tax_rates:
            first_settings = self.connector.woocommerce_webhook_settings_retrieve(woocommerce_api)
            second_settings = self.connector.woocommerce_webhook_settings_retrieve(woocommerce_api)

        self.assertEqual(first_settings, second_settings)
        self.assertEqual(woocommerce_api.get.call_count, 5)
        mocked_tax_rates.assert_called_once()

        expired = time.monotonic() + self.connector.WEBHOOK_SETTINGS_CACHE_SECONDS + 1
        with patch.object(time, 'monotonic', return_value=expired), patch.object(type(self.connector), 'woocommerce_tax_rates_retrieve', return_value={}):
            self.connector.woocommerce_webhook_settings_retrieve(woocommerce_api)
        self.assertEqual(woocommerce_api.get.call_count, 10, 'Expired settings must be retrieved again')

    def test_order_status_push_only_dispatches_orders_to_push(self):
        partner = self.env['res.partner'].create({'name': 'Status push customer'})
        product = self.env['product.product'].create({'name': 'Status push product', 'list_price': 10.0, 'taxes_id': [(6, 0, [])]})
        orders = self.env['sale.order']
        for woocommerce_id, woocommerce_status in (('99901', 'processing'), ('99902', 'pending')):
            order = self.env['sale.order'].create(
                {
                    'partner_id': partner.id,
                    'woocommerce_site_url': self.connector.settings_woocommerce_connection_url,
                    'woocommerce_id': woocommerce_id,
                    'woocommerce_status': woocommerce_status,
                    'order_line': [(0, 0, {'product_id': product.id, 'product_uom_qty': 1})],
                }
            )
            order.action_confirm()
            orders |= order

        with patch.object(type(self.connector), 'woocommerce_api_get', return_value=MagicMock()), patch.object(type(self.connector), 'with_delay', return_value=MagicMock()) as mocked_with_delay:
            self.connector.odoo_to_woocommerce_orders_status_sync()

        dispatched_ids = [order_id for call in mocked_with_delay.return_value.odoo_to_woocommerce_orders_status_chunk_sync.call_args_list for order_id in call.args[0]]
        self.assertEqual(dispatched_ids, orders.filtered(lambda order: order.woocommerce_status == 'pending').ids)

    def test_stock_variations_are_retrieved_per_chunk_of_products(self):
        temporary_sync_data_record = self.env['woocommerce.sync.data.temp'].create({'woocommerce_products_variations_data': {'1': {'id': 1}}})
        variations_by_endpoint = {'products/10/variations': [{'id': 11, 'stock_quantity': 1}], 'products/20/variations': [{'id': 21, 'stock_quantity': 2}]}

        with (
            patch.object(type(self.connector), 'woocommerce_api_get', return_value=MagicMock()),
            patch.object(type(self.connector), 'woocommerce_api_get_all_items', side_effect=lambda woocommerce_api, endpoint, params: variations_by_endpoint[endpoint]),
        ):
            self.connector.woocommerce_products_variations_stock_retrieve([10, 20], temporary_sync_data_record.id)

        self.assertEqual(set(temporary_sync_data_record.woocommerce_products_variations_data), {'1', '11', '21'})

    # Brazil

    def test_brazil_fields_without_brazilian_localization(self):
        partner_fields = self.env['res.partner']._fields
        if any(field_name in partner_fields for field_name in ('cnpj_cpf', 'l10n_br_ie_code', 'street_number', 'district')):
            self.skipTest('A Brazilian localization or extended address module is installed')

        billing = {'cpf': '529.982.247-25', 'ie': '123', 'number': '123', 'neighborhood': 'Centro', 'country': 'BR'}
        customer_values = {'street': 'Rua A', 'street2': 'Apto 1'}
        self.connector.odoo_customer_brazil_values_update(customer_values, billing)
        self.assertEqual(customer_values, {'street': 'Rua A, 123', 'street2': 'Apto 1, Centro'})

        partner = self.env['res.partner'].create({'name': 'No Brazilian localization'})
        self.connector.odoo_customer_brazil_vat_update(partner, billing)
        self.assertFalse(partner.vat)


@tagged('post_install', '-at_install')
class TestBrazilLocalization(WoocommerceSyncCommon):
    """Only runs in a database where Odoo's 'l10n_br' is installed."""

    def setUp(self):
        super().setUp()
        if self.env['ir.module.module'].search([('name', '=', 'l10n_br')]).state != 'installed':
            self.skipTest("Odoo 'l10n_br' is not installed")

    def _billing(self, **values):
        billing = {
            'first_name': 'Maria',
            'last_name': 'Silva',
            'company': '',
            'address_1': 'Rua Augusta',
            'address_2': '',
            'city': 'São Paulo',
            'state': 'SP',
            'postcode': '01305-000',
            'country': 'BR',
            'email': 'maria@example.test',
            'phone': '',
            'number': '1500',
            'neighborhood': 'Consolação',
            'ie': '110042490114',
        }
        billing.update(values)
        return billing

    def test_individual_customer(self):
        woocommerce_customer = make_woocommerce_customer_payload(id=99951, email='maria@example.test', first_name='Maria', last_name='Silva', billing=self._billing(persontype='1', cpf='529.982.247-25'))

        self.connector.woocommerce_to_odoo_customer_sync(woocommerce_customer, {})

        partner = self.env['res.partner'].search([('woocommerce_id', '=', '99951')])
        self.assertEqual(partner.l10n_br_ie_code, '110042490114')
        self.assertIn('1500', partner.street)
        if 'l10n_br_cpf_code' in partner._fields:
            self.assertEqual(partner.l10n_br_cpf_code, '529.982.247-25')
        else:
            self.assertEqual(partner.street_number, '1500')
            self.assertTrue(partner.vat)
            self.assertEqual(partner.l10n_latam_identification_type_id, self.env.ref('l10n_br.cpf'))

    def test_company_customer_and_invalid_cnpj(self):
        self.connector.woocommerce_to_odoo_customer_sync(
            make_woocommerce_customer_payload(id=99952, email='empresa@example.test', billing=self._billing(persontype='2', cnpj='11.222.333/0001-81', email='empresa@example.test')), {}
        )
        partner = self.env['res.partner'].search([('woocommerce_id', '=', '99952')])
        self.assertTrue(partner.vat, 'A valid CNPJ is stored as tax ID')

        self.connector.woocommerce_to_odoo_customer_sync(
            make_woocommerce_customer_payload(id=99953, email='invalida@example.test', billing=self._billing(persontype='2', cnpj='11.111.111/1111-11', email='invalida@example.test')), {}
        )
        invalid_partner = self.env['res.partner'].search([('woocommerce_id', '=', '99953')])
        self.assertTrue(invalid_partner, 'An invalid CNPJ must not prevent the customer import')
