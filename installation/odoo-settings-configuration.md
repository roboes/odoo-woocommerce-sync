# Odoo-WooCommerce Sync Odoo Settings Configuration

> [!NOTE]  
> Last update: 2026-08-26

## Settings

```sh
domain="website.com"
domain_root_path="/home/$domain"
subdomain="erp"
system_user="website"
database_name="${system_user}_odoo"
odoo_account_fiscal_localization_country='de'
```

## Install Modules

```sh
# Install "delivery", "stock" and the fiscal localization module (installed modules are skipped automatically)
docker exec -it odoo_server_${system_user} odoo -d $database_name --init delivery,stock,l10n_${odoo_account_fiscal_localization_country} --stop-after-init --http-port 8070
```

## Odoo Settings Configuration

```sh
# Access Odoo shell
docker exec -it odoo_server_${system_user} odoo shell --no-http -d $database_name
```

```py
# Settings
odoo_username = 'admin'
odoo_account_fiscal_localization_country = 'de'
odoo_account_price_include = 'tax_included'
odoo_account_fiscal_localization_chart_template = env['account.chart.template']._guess_chart_template(env['res.country'].search([('code', '=', odoo_account_fiscal_localization_country.upper())], limit=1))

odoo_user = env['res.users'].search([('login', '=', odoo_username)], limit=1)
odoo_fiscal_module = env['ir.module.module'].search([('name', '=', f'l10n_{odoo_account_fiscal_localization_country}')], limit=1)

# Odoo version
from odoo.release import version_info

print(f'Odoo version: {version_info[0]}')

# List all available chart templates for the selected fiscal localization country
chart_template_options = [option for option in env['res.config.settings']._fields['chart_template'].selection(env['res.config.settings']) if odoo_account_fiscal_localization_country in option[0]]
for value, label in chart_template_options:
    print(f'{value}: {label}')


def assign_group(xml_id: str) -> None:
    group = env.ref(xml_id)
    # 'group_ids' replaced 'groups_id' on 'res.users' in Odoo v19
    field_name = 'group_ids' if 'group_ids' in odoo_user._fields else 'groups_id'
    if group not in odoo_user[field_name]:
        odoo_user.write({field_name: [(4, group.id)]})
    print(f'Assigned group: {xml_id}')


if odoo_user and odoo_fiscal_module and odoo_fiscal_module.state == 'installed':
    # Group assignments
    assign_group('sales_team.group_sale_manager')  # Sales Administrator
    assign_group('account.group_account_manager')  # Billing Administrator
    assign_group('account.group_account_user')  # Full Accounting Features
    assign_group('stock.group_stock_multi_locations')  # Storage Locations
    # Settings (Product Variants, Units of Measure, Product Packagings)
    config_values = {'group_product_variant': True, 'group_uom': True}
    if version_info[0] in [18, 19]:
        config_values['group_stock_packaging'] = True
    env['res.config.settings'].create(config_values).execute()
    # Fiscal Localization
    env['res.config.settings'].create(
        {'chart_template': odoo_account_fiscal_localization_chart_template, 'account_fiscal_country_id': env['res.country'].search([('code', '=', odoo_account_fiscal_localization_country.upper())]).id}
    ).execute()
    print(f'Current Purchase Tax Prices setting: {env.company.account_price_include}')
    if env.company.account_price_include != odoo_account_price_include:
        env['res.config.settings'].create({'account_price_include': odoo_account_price_include}).execute()
        print(f'Updated Purchase Tax Prices setting: {env.company.account_price_include}')
    env.cr.commit()  # Commit changes to database
else:
    print(f'User ({odoo_username}) and/or fiscal module ({f"l10n_{odoo_account_fiscal_localization_country}"}) not found')
```

```py
exit()
```

```sh
# Rebuild Docker image
cd $domain_root_path/domains/$subdomain.$domain/odoo
docker compose build
docker compose up -d
```
