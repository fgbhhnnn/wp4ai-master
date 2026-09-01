# Yoast SEO Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace every Rank Math SEO write path with tested Yoast SEO metadata synchronization and produce updated Windows packages.

**Architecture:** Both Python entry points use a small `sync_yoast_seo()` boundary that maps product and product-category IDs to standard WordPress REST endpoints and writes the three Yoast meta keys. Schema remains on the existing independent `saswp_custom_schema_field` path. A small WordPress plugin registers protected Yoast meta for REST writes, while Python reports a precise setup error when that registration is missing.

**Tech Stack:** Python 3, `requests`, `unittest`, WordPress REST API, PHP WordPress plugin API, PyInstaller.

**Spec:** `docs/superpowers/specs/2026-09-01-yoast-seo-migration.md`

## Global Constraints

- Preserve all existing dirty-worktree changes.
- Do not use or expose WordPress credentials, application passwords, or AI keys in tests, logs, docs, or packages.
- Do not create Git commits because the user requested edits and packaging only.
- Keep custom product prompt behavior limited to replacing `messages[0]["content"]` in `generate_seo_data_by_keywords()`.
- Keep Schema independent from Yoast and do not represent the local score as an official Yoast score.

---

### Task 1: Specify Yoast Metadata Synchronization

**Files:**
- Modify: `tests/test_wp4ai_generate_media.py`
- Modify: `wp4ai_generate.py`

**Interfaces:**
- Produces: `sync_yoast_seo(object_id: int, object_type: str, focus_keyword, title: str, description: str, retries: int = 5) -> bool`
- Produces: `YOAST_META_KEYS: dict[str, str]`

- [ ] **Step 1: Write failing tests**

```python
def test_sync_yoast_product_uses_standard_rest_meta(self):
    # Assert endpoint `/product/987` and exact `_yoast_wpseo_*` keys.

def test_sync_yoast_term_uses_product_cat_endpoint(self):
    # Assert endpoint `/product_cat/42` and a list keyword becomes CSV.

def test_sync_yoast_protected_meta_error_is_not_retried(self):
    # Return a REST 403/400 protected-meta response and assert one request.
```

- [ ] **Step 2: Run the focused tests and verify they fail because `sync_yoast_seo` does not exist**

Run: `python -m unittest tests.test_wp4ai_generate_media.YoastSeoSyncTest -v`

- [ ] **Step 3: Implement the minimal writer**

```python
YOAST_META_KEYS = {
    "focus_keyword": "_yoast_wpseo_focuskw",
    "title": "_yoast_wpseo_title",
    "description": "_yoast_wpseo_metadesc",
}

def sync_yoast_seo(object_id, object_type, focus_keyword, title, description, retries=5):
    endpoint = "product" if object_type == "post" else "product_cat"
    payload = {"meta": {
        YOAST_META_KEYS["focus_keyword"]: _normalize_focus_keyword(focus_keyword),
        YOAST_META_KEYS["title"]: title or "",
        YOAST_META_KEYS["description"]: description or "",
    }}
    # POST with existing Basic Auth, stop immediately on authorization/meta-registration failures.
```

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `python -m unittest tests.test_wp4ai_generate_media.YoastSeoSyncTest -v`

### Task 2: Migrate Product Publishing and Generic Local Scoring

**Files:**
- Modify: `tests/test_wp4ai_generate_media.py`
- Modify: `wp4ai_generate.py`

**Interfaces:**
- Consumes: `sync_yoast_seo(...) -> bool`
- Produces: `calculate_seo_score_locally(post_data: dict) -> int`
- Produces: generic score payload meta keys `focus_keyword` and `seo_description`

- [ ] **Step 1: Change product publishing tests to expect the Yoast synchronization call and no Rank Math request**

```python
with patch.object(wp4ai_generate, "sync_yoast_seo", return_value=True) as sync:
    product_id = wp4ai_generate.publish_product_to_wordpress(ai_data, product_title="Directory Product Name")
sync.assert_called_once_with(987, "post", "focus keyword", "SEO Title", "SEO description")
```

- [ ] **Step 2: Run the product tests and verify the old implementation fails them**

Run: `python -m unittest tests.test_wp4ai_generate_media.PublishProductTitleTest -v`

- [ ] **Step 3: Replace the post-create Rank Math loop with `sync_yoast_seo` and rename the local score symbols and payload keys**

```python
sync_yoast_seo(new_product_id, "post", keyword, title, description)
```

- [ ] **Step 4: Change built-in product/category prompt wording to Yoast SEO and remove Rank Math score meta writes**

- [ ] **Step 5: Run all current tests**

Run: `python -m unittest discover -s tests -v`

### Task 3: Migrate Every Product Category Workflow

**Files:**
- Modify: `tests/test_wp4ai_generate_media.py`
- Modify: `wp4ai_generate.py`
- Modify: `wp4ai_categories.py`

**Interfaces:**
- Consumes: module-local `sync_yoast_seo(...) -> bool`
- Retains: term `description` and `saswp_custom_schema_field` updates through `/product_cat/<id>`

- [ ] **Step 1: Add a focused category test asserting the standard term endpoint and Yoast keys**

```python
def test_category_yoast_sync_never_calls_rankmath_endpoint(self):
    # Exercise category synchronization and assert every URL excludes `/rankmath/`.
```

- [ ] **Step 2: Run the category test and verify it fails against the Rank Math implementation**

Run: `python -m unittest tests.test_wp4ai_generate_media.CategoryYoastSyncTest -v`

- [ ] **Step 3: Replace category Rank Math metadata blocks with `sync_yoast_seo` in both entry points and remove Rank Math schema endpoint helpers**

- [ ] **Step 4: Preserve category Schema only through `saswp_custom_schema_field` and run all tests**

Run: `python -m unittest discover -s tests -v`

### Task 4: Register Protected Yoast Meta in WordPress

**Files:**
- Create: `wordpress-plugin/wp4ai-yoast-rest-meta/wp4ai-yoast-rest-meta.php`
- Create: `wordpress-plugin/wp4ai-yoast-rest-meta/README.md`

**Interfaces:**
- Exposes post meta on REST type `product`: `_yoast_wpseo_focuskw`, `_yoast_wpseo_title`, `_yoast_wpseo_metadesc`
- Exposes term meta on taxonomy `product_cat`: the same three keys

- [ ] **Step 1: Add source-level tests for the plugin header, hook, post type, taxonomy, and exact Yoast keys**

- [ ] **Step 2: Run the plugin contract tests and verify they fail because the plugin does not exist**

- [ ] **Step 3: Implement registration using `register_post_meta()` and `register_term_meta()` with string/single/show-in-rest settings and capability callbacks**

```php
add_action( 'init', 'wp4ai_register_yoast_rest_meta' );
register_post_meta( 'product', $meta_key, $post_args );
register_term_meta( 'product_cat', $meta_key, $term_args );
```

- [ ] **Step 4: Run the plugin contract tests and PHP syntax check when `php` is available**

Run: `python -m unittest tests.test_wp4ai_generate_media.YoastPluginContractTest -v`

### Task 5: Verify and Package

**Files:**
- Modify: `dist/wp4ai_gui.exe` (generated)
- Modify: `dist/wp4ai_generate_cli.exe` (generated)
- Modify: `dist.zip` (generated)

**Interfaces:**
- Produces: two Windows executables plus a distribution archive containing executables and the WordPress helper plugin.

- [ ] **Step 1: Scan source for prohibited Rank Math endpoints, metadata keys, and active wording**

Run: `rg -n -i "rank[ _-]?math|rankmath|WP_RM|updateSchemas|updateMeta" -g "!dist/**" -g "!build/**" -g "!docs/superpowers/**"`

- [ ] **Step 2: Run syntax, test, and whitespace validation**

Run: `python -m py_compile wp4ai_generate.py wp4ai_categories.py wp4ai_gui.py`

Run: `python -m unittest discover -s tests -v`

Run: `git diff --check`

- [ ] **Step 3: Build both executables**

Run: `python build.py --skip-deps`

- [ ] **Step 4: Create `dist.zip` containing both executables and `wordpress-plugin/wp4ai-yoast-rest-meta/`**

- [ ] **Step 5: Inspect archive members, executable sizes, and checksums before reporting completion**

