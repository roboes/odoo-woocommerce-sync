## Odoo-WooCommerce Sync Tests
# Last update: 2025-03-02


# Settings
website="website.com"
website_root_directory="/var/www/vhosts/$website/httpdocs"
odoo_conf="/etc/odoo.conf"
database_name="database_name"
odoo_addon_name="woocommerce_sync"


# First Odoo run after fresh install
# $website_root_directory/odoo/venv/bin/python3 $website_root_directory/odoo/odoo-bin --config=$odoo_conf --database $database_name --load=base,web --without-demo=all --update=all



# Stop Odoo Process

## List all running Odoo processes
ps aux | grep odoo

## Kill all Odoo processes except the grep process itself
ps aux | grep odoo | grep -v grep | awk '{print $2}' | xargs -r kill -9



# Start Odoo command line
# $website_root_directory/odoo/venv/bin/python3 $website_root_directory/odoo/odoo-bin shell --config=$odoo_conf

# Retrieve all field information for product templates
# fields = self.env['product.template'].fields_get()
# print(fields)

# Retrieve default values for product template fields
# print(self.env['product.template'].default_get(self.env['product.template']._fields.keys()))

# Retrieve required fields
# fields = {field: data for field, data in fields.items() if data.get('required')}
# fields.keys()

# Retrieve read-only fields
# fields = {field: data for field, data in fields.items() if data.get('readonly')}
# fields.keys()


# Update the WooCommerce Sync module in Odoo and stop Odoo after initialization
$website_root_directory/odoo/venv/bin/python3 $website_root_directory/odoo/odoo-bin \
    --config=$odoo_conf \
    --database $database_name \
    --update $odoo_addon_name \
    --stop-after-init

# Overwrite the Odoo log file with an empty content
cat <<EOF > "/var/log/odoo.log"
EOF

# Update WooCommerce Sync module and load additional dependencies
$website_root_directory/odoo/venv/bin/python3 $website_root_directory/odoo/odoo-bin \
	--config=$odoo_conf \
	--database $database_name \
	--update $odoo_addon_name \
	--load=product
