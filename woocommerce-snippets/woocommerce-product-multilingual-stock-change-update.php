<?php

// WooCommerce - Update multilingual translation (Polylang/WPML) modified dates on product stock change
// Last update: 2026-08-24


if (function_exists('WC')) {

    add_action(hook_name: 'woocommerce_product_set_stock', callback: 'multilingual_update_modified_date_on_stock_change', priority: 10, accepted_args: 1);
    add_action(hook_name: 'woocommerce_variation_set_stock', callback: 'multilingual_update_modified_date_on_stock_change', priority: 10, accepted_args: 1);

    if (!function_exists('get_product_translation_ids')) {
        function get_product_translation_ids(int $product_id): array
        {
            $ids = [];

            $languages = apply_filters('wpml_active_languages', null, ['skip_missing' => 0]);

            if (is_array($languages)) {
                $type = get_post_type($product_id) ?: 'product';

                foreach (array_keys($languages) as $lang_code) {
                    $translated_id = apply_filters('wpml_object_id', $product_id, $type, false, $lang_code);
                    if ($translated_id) {
                        $ids[] = (int) $translated_id;
                    }
                }
            }

            if (empty($ids)) {
                $ids[] = $product_id;
            }

            return array_unique($ids);
        }
    }

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

        // Get product ID + all its translation IDs (Polylang/WPML compatible)
        $translated_ids = get_product_translation_ids($product_id);

        // Update if it's a valid ID and NOT the current product
        foreach ($translated_ids as $translated_id) {
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
