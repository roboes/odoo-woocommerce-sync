<?php

// WooCommerce - Update Polylang translation modified dates on product stock change
// Last update: 2025-10-01


if (class_exists('WooCommerce') && WC() && class_exists('Polylang')) {

    add_action($hook_name = 'woocommerce_product_set_stock', $callback = 'polylang_update_modified_date_on_stock_change', $priority = 10, $accepted_args = 1);
    add_action($hook_name = 'woocommerce_variation_set_stock', $callback = 'polylang_update_modified_date_on_stock_change', $priority = 10, $accepted_args = 1);

    function polylang_update_modified_date_on_stock_change($product)
    {
        if (! $product instanceof WC_Product) {
            return;
        }

        $product_id = $product->get_id();

        // Get the modified date of the product that was just updated.
        $source_post = get_post($product_id);
        $modified_date_mysql = $source_post->post_modified;
        $modified_date_gmt = $source_post->post_modified_gmt;

        // If Polylang is active, update translations
        if (function_exists('pll_get_post_translations')) {
            $translations = pll_get_post_translations($product_id);

            if (! empty($translations) && is_array($translations)) {
                foreach ($translations as $lang => $translated_id) {
                    if ($translated_id && $translated_id !== $product_id) {
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

}
