## Odoo-WooCommerce Sync Tests
# Last update: 2026-03-10


# Logs:
# ?debug=1
# ?debug=assets


# Settings
domain="website.com"
domain_root_path="/home/$domain"
subdomain="erp"
system_user="website"
odoo_version="19.0"
database_name="${system_user}_odoo"
database_username="${system_user}_odoo_user"
odoo_addon_name="woocommerce_sync"

# Change current directory
cd $domain_root_path/domains/$subdomain.$domain/odoo

# Delete all queue jobs
docker exec -i odoo_postgres_${system_user} psql -U $database_username -d $database_name <<EOF
DELETE FROM queue_job;
EOF

# Restart Odoo container
docker restart odoo_server_${system_user}

# Update WooCommerce Sync module
docker exec -it odoo_server_${system_user} \
    odoo \
    --database $database_name \
    --update $odoo_addon_name \
    --no-http \
    --stop-after-init

# Restart again Odoo container
docker restart odoo_server_${system_user}

# Clean logs
truncate -s 0 $(docker inspect --format='{{.LogPath}}' odoo_server_${system_user})

# View logs
# docker logs odoo_server_${system_user}
# docker logs odoo_postgres_${system_user}

# Access Odoo shell
# docker exec -it odoo_server_${system_user} odoo shell --no-http -d $database_name

# Retrieve all field information for product templates
# fields = self.env['product.template'].fields_get()
# print(fields)
# print(fields.keys())

# Retrieve default values for product template fields
# print(self.env['product.template'].default_get(self.env['product.template']._fields.keys()))

# Retrieve required fields
# fields = {field: data for field, data in fields.items() if data.get('required')}
# print(fields.keys())

# Retrieve read-only fields
# fields = {field: data for field, data in fields.items() if data.get('readonly')}
# print(fields.keys())