# Yoast SEO Migration Specification

## Problem

The application hard-codes Rank Math REST endpoints for product and product-category SEO metadata. Sites using Yoast SEO do not expose those endpoints, so the product is created but SEO synchronization retries five times and ends with a 404 failure.

## Approved Scope

- Replace Rank Math product and product-category metadata synchronization with Yoast SEO metadata.
- Write `_yoast_wpseo_focuskw`, `_yoast_wpseo_title`, and `_yoast_wpseo_metadesc` through the standard WordPress REST endpoints.
- Remove runtime calls to `/rankmath/v1/updateMeta` and `/rankmath/v1/updateSchemas`.
- Keep the existing `saswp_custom_schema_field` and JSON-LD behavior independent from Yoast.
- Rename the local score implementation and its logs as a generic SEO estimate. It is not a Yoast official score.
- Change built-in prompt wording from Rank Math to Yoast without changing the GUI rule that a custom prompt replaces only the system message in `generate_seo_data_by_keywords()`.
- Cover the product and all product-category workflows in both `wp4ai_generate.py` and `wp4ai_categories.py`.
- Provide a WordPress plugin helper that registers Yoast's protected post and term meta as REST-writable.
- Rebuild the GUI executable, CLI executable, and `dist.zip`.

## Success Criteria

- Publishing a product sends Yoast metadata to `/wp-json/wp/v2/product/<id>` and never requests a Rank Math endpoint.
- Category create/update flows send Yoast metadata to `/wp-json/wp/v2/product_cat/<id>` and never request a Rank Math endpoint.
- A protected-meta REST error stops immediately with a deployment-oriented diagnostic instead of five identical retries.
- Existing media upload, product title, URL normalization, custom prompt, and AI error behavior remain covered by tests.
- Source code and tests contain no active Rank Math endpoint, metadata key, scoring name, or user-facing Rank Math wording.

