# WP4AI Yoast REST Meta

This WordPress helper plugin registers the protected Yoast SEO metadata used by
WP4AI as authenticated REST fields for WooCommerce products and product
categories.

## Install

1. Upload the `wp4ai-yoast-rest-meta` directory to `wp-content/plugins/`.
2. In WordPress Admin, open **Plugins > Installed Plugins**.
3. Activate **WP4AI Yoast REST Meta**.
4. Keep Yoast SEO and WooCommerce active.

The WordPress application-password user configured in WP4AI must be allowed to
edit products and product categories. The plugin does not expose write access to
anonymous visitors.

## Registered Fields

- `_yoast_wpseo_focuskw`
- `_yoast_wpseo_title`
- `_yoast_wpseo_metadesc`

The existing `saswp_custom_schema_field` integration remains separate. Yoast
generates its own Schema graph, so this plugin only registers the three metadata
fields listed above and does not alter Schema output.
