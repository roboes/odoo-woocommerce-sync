# Odoo-WooCommerce Sync Dependencies Installation

> [!NOTE]
> Last update: 2025-06-17

## Settings

```.sh
website="website.com"
website_root_path="/home/$website/public_html"
system_user=""
system_group=""
odoo_version="16.0"
database_name=""
odoo_conf="/etc/odoo/$website.conf"
addons_path="$website_root_path/odoo/addons"
```

## Installation

### Clone repositories

```.sh
# Repositories to clone
declare -A repos=(
  # Required Odoo Community Association (OCA) add-ons
  ["queue"]="https://github.com/OCA/queue.git"
  # Optional Odoo Community Association (OCA) add-ons
  ["server-tools"]="https://github.com/OCA/server-tools.git"
  ["product-attribute"]="https://github.com/OCA/product-attribute.git"
  ["brand"]="https://github.com/OCA/brand.git"
  # Optional Localization add-ons
  ["l10n-brazil"]="https://github.com/OCA/l10n-brazil.git"
)

# Mapping for known modules to auto-install
declare -A modules_to_install=(
  # Required Odoo Community Association (OCA) add-ons
  ["queue"]="queue_job"
  # Optional Odoo Community Association (OCA) add-ons
  ["server-tools"]="module_auto_update"
  ["product-attribute"]="product_dimension product_multi_category"
  ["brand"]="product_brand"
  # Optional Localization add-ons
  ["l10n-brazil"]="l10n_br_fiscal"
)

# Create addons directory (if necessary)
if [ ! -d "$addons_path" ]; then
  mkdir -p "$addons_path"
  chown -R $system_user:$system_group "$addons_path"
fi

# Clone repositories directly into addons path
cd "$addons_path"
for repo in "${!repos[@]}"; do
  echo "Cloning $repo..."
  git clone --depth 1 -b "$odoo_version" "${repos[$repo]}" "$repo"

  # For each module in this repo, move it up one level
  for module in ${modules_to_install[$repo]}; do
    if [ -d "$repo/$module" ]; then
      echo "Moving $module to $addons_path"
      mv "$repo/$module" "$addons_path/$module"
    else
      echo "Warning: $repo/$module not found"
    fi
  done

  # Clean up
  echo "Removing $repo"
  rm -rf "$repo/.git" "$repo"
done
```

### Install Python dependencies

```.sh
# Change current directory
cd "$website_root_path/odoo"

# Create a virtual environment
python -m venv "./venv"

# Activate the virtual environment
source "./venv/bin/activate"

# Install Python dependencies (if any)
echo "Installing requirements..."
find "$addons_path" -name "requirements.txt" -exec python -m pip install -r {} \;
```

### Odoo Config

```.sh
# Update odoo.conf with addons path
# if grep -q "^addons_path" "$odoo_conf"; then
  # sed -i "s|^addons_path.*|addons_path = $addons_path|" "$odoo_conf"
# else
  # echo "addons_path = $addons_path" >> "$odoo_conf"
# fi

# Restart Odoo to apply config
# echo "Restarting Odoo..."
# systemctl restart odoo
```

### Update base modules

```.sh
echo "Updating base module list..."
$website_root_path/odoo/venv/bin/python3 $website_root_path/odoo/odoo-bin \
    --config=$odoo_conf \
    --database $database_name \
    --update base \
    --stop-after-init

# Build module list for installation
install_modules=""
for repo in "${!repos[@]}"; do
  if [[ -n "${modules_to_install[$repo]}" ]]; then
    install_modules+="${modules_to_install[$repo]},"
  fi
done

# Remove trailing comma
install_modules="${install_modules%,}"

if [[ -n "$install_modules" ]]; then
  echo "Installing modules: $install_modules"
  $website_root_path/odoo/venv/bin/python3 $website_root_path/odoo/odoo-bin --config="$odoo_conf" --database="$database_name" --init="$install_modules" --stop-after-init
else
  echo "No modules to install."
fi

# Exit the virtual environment
deactivate
```
