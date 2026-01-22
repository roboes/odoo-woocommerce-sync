<?php

// WooCommerce - Add Polylang language slug to WooCommerce Rest API
// Last update: 2026-01-18


if (function_exists('WC') && class_exists('Polylang')) {

    add_filter(hook_name: 'woocommerce_rest_product_object_query', callback: function (array $args, WP_REST_Request $request): array {
        $lang = $request->get_param(key: 'lang');
        if ($lang) {
            $args['lang'] = sanitize_text_field(str: $lang);
        }
        return $args;
    }, priority: 10, accepted_args: 2);

    add_action(hook_name: 'rest_api_init', callback: function (): void {
        $post_types = ['product', 'shop_order'];

        foreach ($post_types as $post_type) {
            register_rest_field(object_type: $post_type, attribute: 'lang', args: [
                'get_callback' => function (array $object): ?string {
                    if (!isset($object['id'])) {
                        return null;
                    }
                    return pll_get_post_language(post_id: (int) $object['id'], field: 'slug') ?: null;
                },
                'schema' => ['description' => __('Language of the post', 'polylang'), 'type' => 'string']
            ]);
        }
    }, priority: 10, accepted_args: 0);

}
