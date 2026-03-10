# Odoo-WooCommerce Sync Odoo Settings Configuration

> [!NOTE]  
> Last update: 2026-03-09

## Settings

```.sh
domain="website.com"
domain_root_path="/home/$domain"
subdomain="erp"
system_user="website"
database_name="${system_user}_odoo"
```

## Start Odoo shell

```.sh
# Access Odoo shell
docker exec -it odoo_server_${system_user} odoo shell --no-http -d $database_name
```

## Settings

```.py
settings_username = 'admin'
settings_account_fiscal_localization_country = 'de'
settings_account_fiscal_localization_module = 'l10n_de'
settings_account_fiscal_localization_chart_template = 'de_skr04'
```

```.py
from odoo.release import version_info
print(version_info[0])
```

```.py
# List all available chart templates for the selected fiscal localization country
options = [option for option in env['res.config.settings']._fields['chart_template'].selection(env['res.config.settings']) if settings_account_fiscal_localization_country in option[0]]
for value, label in options:
    print(f'{value}: {label}')
```

## Install modules

```.py
# Install "delivery" module if not installed
delivery_module = env['ir.module.module'].search([('name', '=', 'delivery')], limit=1)
if delivery_module:
    if delivery_module.state != 'installed':
        delivery_module.button_immediate_install()
        print('Installed: "delivery" module')
    else:
        print('Already installed: "delivery" module')
else:
    print('Error: Module "delivery" not found in module list')

stock_module = env['ir.module.module'].search([('name', '=', 'stock')], limit=1)
if stock_module:
    if stock_module.state != 'installed':
        stock_module.button_immediate_install()
        print('Installed: "stock" module')
    else:
        print('Already installed: "stock" module')
else:
    print('Error: Module "stock" not found in module list')

# Install fiscal localization module if not installed
fiscal_module = env['ir.module.module'].search([('name', '=', settings_account_fiscal_localization_module)], limit=1)
if fiscal_module:
    if fiscal_module.state != 'installed':
        fiscal_module.button_immediate_install()
        print(f'Installed: "{settings_account_fiscal_localization_module}" module')
    else:
        print(f'Already installed: "{settings_account_fiscal_localization_module}" module')
else:
    print(f'Error: Module "{settings_account_fiscal_localization_module}" not found in module list')
```

## Odoo settings configuration

```.py
odoo_user = env['res.users'].search([('login', '=', settings_username)], limit=1)

if odoo_user:
    def assign_group(xml_id: str) -> None:
        group = env.ref(xml_id)
        if version_info[0] == 18:
            if group not in odoo_user.groups_id:
                odoo_user.write({'groups_id': [(4, group.id)]})
        elif version_info[0] == 19:
            if group not in odoo_user.group_ids:
                odoo_user.write({'group_ids': [(4, group.id)]})
            print(f'Assigned group: {xml_id}')
    # Group assignments
    assign_group('sales_team.group_sale_manager')  # Sales Administrator
    assign_group('account.group_account_manager')  # Billing Administrator
    assign_group('account.group_account_user')  # Full Accounting Features
    env['res.config.settings'].create({'group_product_variant': True}).execute()  # Product Variants
    if version_info[0] == 18:
        env['res.config.settings'].create({'group_stock_packaging': True}).execute() # Product Packagings
    elif version_info[0] == 19:
        env['res.config.settings'].create({'group_uom': True}).execute() # Product Packagings
    env['res.config.settings'].create({'group_uom': True}).execute()  # Units of Measure
    env.cr.commit()  # Commit changes to database
    assign_group('stock.group_stock_multi_locations')  # Storage Locations
    # Delivery Methods
    delivery_module = env['ir.module.module'].search([('name', '=', 'delivery')], limit=1)
    if delivery_module.state != 'installed':
        delivery_module.button_immediate_install()
    # Fiscal Localization
    fiscal_module = env['ir.module.module'].search([('name', '=', settings_account_fiscal_localization_module)], limit=1)
    if fiscal_module and fiscal_module.state == 'installed':
        env['res.config.settings'].create({'chart_template': settings_account_fiscal_localization_chart_template, 'account_fiscal_country_id': env['res.country'].search([('code', '=', settings_account_fiscal_localization_country.upper())]).id}).execute()
        print(f'Loaded chart template: {settings_account_fiscal_localization_chart_template}')
    else:
        print('Fiscal localization module not installed')
else:
    print(f'User not found: {settings_username}')
```

```.py
exit()
```

```.sh
# Rebuild Docker image
cd $domain_root_path/domains/$subdomain.$domain/odoo
docker compose build
docker compose up -d
```
