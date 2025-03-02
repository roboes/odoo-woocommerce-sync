<?php

// WooCommerce - Add Polylang language slug to WooCommerce Rest API
// Last update: 2025-02-26

if (class_exists('WooCommerce') && WC() && class_exists('Polylang')) {

    add_filter($hook_name = 'woocommerce_rest_product_object_query', $callback = function ($args, $request) {
        if ($request->get_param('lang')) {
            $args['lang'] = sanitize_text_field($request->get_param('lang'));
        }
        return $args;
    }, $priority = 10, $accepted_args = 2);

    add_action($hook_name = 'rest_api_init', $callback = function () {
        $post_types = ['product', 'shop_order'];

        foreach ($post_types as $post_type) {
            register_rest_field($post_type, 'lang', [
                'get_callback' => function ($object) {
                    return pll_get_post_language($object['id']);
                },
                'schema' => ['description' => __('Language of the post', 'polylang'), 'type' => 'string']
            ]);
        }
    }, $priority = 10, $accepted_args = 1);

}
