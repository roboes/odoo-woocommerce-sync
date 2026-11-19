# Odoo-WooCommerce Sync - Brazilian NF-e

Optional bridge between `woocommerce_sync` and OCA's Brazilian NF-e localization.

- Automatically queues a private WooCommerce order note after a company-issued NF-e model 55 becomes authorized.
- Provides an **Action** menu item on an invoice to send or retry the access key.
- Sends `NF-e: <44-digit access key>` as a private note. The installed Melhor Envio WordPress plugin can extract the key from that note.
- Records whether the note is queued, sent to WooCommerce, or failed. "Sent" confirms the WooCommerce note API accepted the note; it does not confirm that Melhor Envio completed shipment processing.
- Assumes one authorized NF-e per WooCommerce order.

Install this addon alongside `woocommerce_sync` and the OCA `l10n_br_account_nfe` localization. The base WooCommerce connector does not depend on Brazilian modules.
