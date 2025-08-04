# Odoo-WooCommerce Sync Odoo Settings Configuration

> [!NOTE]
> Last update: 2025-08-04

## Settings

```.sh
website="website.com"
website_root_path="/var/www/vhosts/$website/httpdocs"
odoo_conf="/etc/odoo.conf"
```

## Start Odoo command line

```.sh
$website_root_path/odoo/venv/bin/python3 $website_root_path/odoo/odoo-bin shell --config=$odoo_conf
```

## Settings

```.py
settings_username = 'admin'
settings_account_fiscal_localization_module = 'l10n_de_skr04'
settings_account_fiscal_localization_template = 'Germany SKR04 - Accounting'
```

## Install modules

```.py
# Install "delivery" module if not installed
delivery_module = env['ir.module.module'].search([('name', '=', 'delivery')], limit=1)
if delivery_module and delivery_module.state != 'installed':
    delivery_module.button_immediate_install()
    print('Installed: "delivery" module')
else:
    print('Already installed: "delivery" module')

# Install "stock" module if not installed
stock_module = env['ir.module.module'].search([('name', '=', 'stock')], limit=1)
if stock_module and stock_module.state != 'installed':
    stock_module.button_immediate_install()
    print('Installed: "stock" module')
else:
    print('Already installed: "stock" module')

# Install fiscal localization module if not installed
fiscal_module = env['ir.module.module'].search([('name', '=', settings_account_fiscal_localization_module)], limit=1)
if fiscal_module and fiscal_module.state != 'installed':
    fiscal_module.button_immediate_install()
    print('Installed: "{settings_account_fiscal_localization_module}" module')
else:
    print(f'Already installed: "{settings_account_fiscal_localization_module}" module')
```

## Odoo settings configuration

```.py
odoo_user = env['res.users'].search([('login', '=', settings_username)], limit=1)

if odoo_user:
    def assign_group(xml_id: str) -> None:
        group = env.ref(xml_id)
        if group not in odoo_user.groups_id:
            odoo_user.write({'groups_id': [(4, group.id)]})
            print(f'Assigned group: {xml_id}')
    # Group assignments
    assign_group('sales_team.group_sale_manager')  # Sales Administrator
    assign_group('account.group_account_manager')  # Billing Administrator
    assign_group('account.group_account_user')  # Full Accounting Features
    env['res.config.settings'].create({'group_product_variant': True}).execute()  # Product Variants
    env['res.config.settings'].create({'group_delivery_invoice_policy': True}).execute() # Delivery Methods
    env['res.config.settings'].create({'group_stock_packaging': True}).execute() # Product Packagings
    env['res.config.settings'].create({'group_uom': True}).execute()  # Units of Measure
    env.cr.commit()  # Commit changes to database
    assign_group('stock.group_stock_multi_locations')  # Storage Locations
    # Fiscal Localization
    module = env['ir.module.module'].search([('name', '=', settings_account_fiscal_localization_module)])
    if module and module.state == 'installed':
        template = env['account.chart.template'].search([('name', '=', settings_account_fiscal_localization_template)], limit=1)
        if template:
            company = env.user.company_id
            company.chart_template_id = template.id
            company._load_chart_template(template)
            print(f'Loaded chart template: {settings_account_fiscal_localization_template}')
        else:
            print('Chart template not found')
    else:
        print('Fiscal localization module not installed')
else:
    print(f'User not found: {settings_username}')
```

```.py
exit()
```
