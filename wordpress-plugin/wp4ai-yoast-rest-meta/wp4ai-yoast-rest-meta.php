<?php
/**
 * Plugin Name: WP4AI Yoast REST Meta
 * Description: Allows authenticated WP4AI requests to update Yoast SEO metadata for WooCommerce products and product categories.
 * Version: 1.0.0
 * Requires at least: 5.9
 * Requires PHP: 7.4
 * Author: WP4AI
 * License: GPL-2.0-or-later
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

/**
 * Allow a user to update Yoast metadata only when they can edit the product.
 *
 * @param bool   $allowed Current authorization result.
 * @param string $meta_key Meta key being updated.
 * @param int    $post_id Product ID.
 * @return bool
 */
function wp4ai_can_edit_yoast_product_meta( $allowed, $meta_key, $post_id ) {
	unset( $allowed, $meta_key );
	return current_user_can( 'edit_post', (int) $post_id );
}

/**
 * Allow a user to update Yoast metadata only when they can edit the category.
 *
 * @param bool   $allowed Current authorization result.
 * @param string $meta_key Meta key being updated.
 * @param int    $term_id Product category ID.
 * @return bool
 */
function wp4ai_can_edit_yoast_product_category_meta( $allowed, $meta_key, $term_id ) {
	unset( $allowed, $meta_key );
	return current_user_can( 'edit_term', (int) $term_id );
}

/**
 * Register Yoast protected metadata with the WordPress REST API.
 *
 * Yoast stores these values as protected metadata and does not expose them as
 * writable REST fields by default. WP4AI still relies on WordPress capability
 * checks; this plugin does not make the values writable to anonymous users.
 */
function wp4ai_register_yoast_rest_meta() {
	$meta_keys = array(
		'_yoast_wpseo_focuskw',
		'_yoast_wpseo_title',
		'_yoast_wpseo_metadesc',
	);

	foreach ( $meta_keys as $meta_key ) {
		register_post_meta(
			'product',
			$meta_key,
			array(
				'type'              => 'string',
				'single'            => true,
				'show_in_rest'      => true,
				'sanitize_callback' => 'sanitize_text_field',
				'auth_callback'     => 'wp4ai_can_edit_yoast_product_meta',
			)
		);

		register_term_meta(
			'product_cat',
			$meta_key,
			array(
				'type'              => 'string',
				'single'            => true,
				'show_in_rest'      => true,
				'sanitize_callback' => 'sanitize_text_field',
				'auth_callback'     => 'wp4ai_can_edit_yoast_product_category_meta',
			)
		);
	}
}
add_action( 'init', 'wp4ai_register_yoast_rest_meta', 20 );

