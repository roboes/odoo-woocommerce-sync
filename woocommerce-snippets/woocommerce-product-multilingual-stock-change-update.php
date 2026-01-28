<?php

// WooCommerce - Update multilingual translation (Polylang/WPML) modified dates on product stock change
// Last update: 2026-01-28


if (function_exists('WC')) {

    add_action(hook_name: 'woocommerce_product_set_stock', callback: 'multilingual_update_modified_date_on_stock_change', priority: 10, accepted_args: 1);
    add_action(hook_name: 'woocommerce_variation_set_stock', callback: 'multilingual_update_modified_date_on_stock_change', priority: 10, accepted_args: 1);

    function multilingual_update_modified_date_on_stock_change(mixed $product): void
    {
        if (!$product instanceof WC_Product) {
            return;
        }

        $product_id = (int) $product->get_id();

        // Get the modified date of the product that was just updated.
        $source_post = get_post($product_id);
        if (!$source_post instanceof WP_Post) {
            return;
        }

        $modified_date_mysql = $source_post->post_modified;
        $modified_date_gmt = $source_post->post_modified_gmt;

        // If Polylang/WPML is active, update translations
        $translations = apply_filters('wpml_get_element_translations', null, $product_id, 'post_product');

        if (!empty($translations) && is_array($translations)) {

            foreach ($translations as $translation) {
                // Extract element_id whether translation is array or object
                $translated_id = is_object($translation) ? (int) ($translation->element_id ?? 0) : (int) ($translation['element_id'] ?? 0);

                // Update if it's a valid ID and NOT the current product
                if ($translated_id && $translated_id !== (int) $product_id) {
                    wp_update_post([
                        'ID' => $translated_id,
                        'post_modified' => $modified_date_mysql,
                        'post_modified_gmt' => $modified_date_gmt,
                    ]);
                }
            }
        }
    }

}
