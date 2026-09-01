import os
import subprocess
import sys
import tempfile
import types
import unittest
import configparser
import zipfile
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

if "openai" not in sys.modules:
    openai_stub = types.ModuleType("openai")
    openai_stub.AsyncOpenAI = object
    sys.modules["openai"] = openai_stub

import wp4ai_generate
import wp4ai_categories
import wp4ai_media_title
import wp4ai_fix_schema
import wp4ai_gui
import build as build_script


class FakeResponse:
    def __init__(
        self,
        *,
        status_code=200,
        url="https://example.com/wp-json/wp/v2/media",
        headers=None,
        text="",
        payload=None,
        history=None,
    ):
        self.status_code = status_code
        self.url = url
        self.headers = headers or {}
        self.text = text
        self._payload = payload
        self.history = history or []

    def raise_for_status(self):
        if self.status_code >= 400:
            error = wp4ai_generate.requests.exceptions.HTTPError(
                f"{self.status_code} Server Error"
            )
            error.response = self
            raise error
        return None

    def json(self):
        return self._payload


class FakeAIError(Exception):
    def __init__(self, message="Authentication failed", status_code=401):
        super().__init__(message)
        self.status_code = status_code


class UploadMediaDiagnosticsTest(unittest.TestCase):
    def test_normalize_wp_domain_strips_trailing_slashes(self):
        self.assertEqual(
            wp4ai_generate._normalize_wp_domain(" https://example.com/// "),
            "https://example.com",
        )

    def test_upload_wp_media_logs_response_shape_when_media_api_returns_list(self):
        response = FakeResponse(
            status_code=200,
            url="https://example.com/wp-json/wp/v2/media",
            headers={"Content-Type": "application/json"},
            text='[{"id": 123}]',
            payload=[{"id": 123}],
            history=[
                FakeResponse(status_code=302, url="http://example.com//wp-json/wp/v2/media")
            ],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = os.path.join(tmpdir, "image.jpg")
            with open(image_path, "wb") as image:
                image.write(b"fake-image")

            with patch.object(wp4ai_generate.requests, "post", return_value=response) as post:
                with patch.object(wp4ai_generate.time, "sleep"):
                    with self.assertLogs(wp4ai_generate.logger, level="WARNING") as logs:
                        result = wp4ai_generate.upload_wp_media(image_path)

        self.assertEqual(result, (None, None))
        self.assertEqual(post.call_count, 1)

        log_text = "\n".join(logs.output)
        self.assertIn("expected=dict, actual=list", log_text)
        self.assertIn("HTTP 200", log_text)
        self.assertIn("url=https://example.com/wp-json/wp/v2/media", log_text)
        self.assertIn("Content-Type=application/json", log_text)
        self.assertIn("history=302 http://example.com//wp-json/wp/v2/media", log_text)

    def test_upload_wp_media_logs_http_error_response_details(self):
        response = FakeResponse(
            status_code=503,
            url="https://example.com/wp-json/wp/v2/media",
            headers={"Content-Type": "text/html; charset=UTF-8"},
            text="<html>Service Temporarily Unavailable</html>",
            history=[
                FakeResponse(status_code=301, url="http://example.com//wp-json/wp/v2/media")
            ],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = os.path.join(tmpdir, "image.jpg")
            with open(image_path, "wb") as image:
                image.write(b"fake-image")

            with patch.object(wp4ai_generate.requests, "post", return_value=response):
                with patch.object(wp4ai_generate.time, "sleep"):
                    with self.assertLogs(wp4ai_generate.logger, level="WARNING") as logs:
                        result = wp4ai_generate.upload_wp_media(image_path)

        self.assertEqual(result, (None, None))

        log_text = "\n".join(logs.output)
        self.assertIn("HTTP 503", log_text)
        self.assertIn("url=https://example.com/wp-json/wp/v2/media", log_text)
        self.assertIn("Content-Type=text/html; charset=UTF-8", log_text)
        self.assertIn("history=301 http://example.com//wp-json/wp/v2/media", log_text)
        self.assertIn("Service Temporarily Unavailable", log_text)

    def test_upload_wp_media_uses_webp_content_type(self):
        response = FakeResponse(
            status_code=200,
            payload={"id": 456, "source_url": "https://example.com/uploads/image.webp"},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = os.path.join(tmpdir, "image.webp")
            with open(image_path, "wb") as image:
                image.write(b"fake-webp")

            with patch.object(wp4ai_generate.requests, "post", return_value=response) as post:
                result = wp4ai_generate.upload_wp_media(image_path)

        self.assertEqual(result, (456, "https://example.com/uploads/image.webp"))
        self.assertEqual(post.call_args.kwargs["headers"]["Content-Type"], "image/webp")

    def test_upload_wp_media_falls_back_to_legacy_jpeg_mime_for_webp_rejection(self):
        rejected = FakeResponse(
            status_code=500,
            url="https://example.com/wp-json/wp/v2/media",
            headers={"Content-Type": "application/json"},
            text='{"code":"rest_upload_sideload_error","message":"Sorry, you are not allowed to upload this file type."}',
            payload={
                "code": "rest_upload_sideload_error",
                "message": "Sorry, you are not allowed to upload this file type.",
            },
        )
        accepted = FakeResponse(
            status_code=201,
            payload={"id": 457, "source_url": "https://example.com/uploads/image.webp"},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = os.path.join(tmpdir, "image.webp")
            with open(image_path, "wb") as image:
                image.write(b"fake-webp")

            with patch.object(
                wp4ai_generate.requests,
                "post",
                side_effect=[rejected, accepted],
            ) as post:
                with patch.object(wp4ai_generate.time, "sleep"):
                    result = wp4ai_generate.upload_wp_media(image_path)

        self.assertEqual(result, (457, "https://example.com/uploads/image.webp"))
        self.assertEqual(post.call_count, 2)
        self.assertEqual(
            post.call_args_list[0].kwargs["headers"]["Content-Type"],
            "image/webp",
        )
        self.assertEqual(
            post.call_args_list[1].kwargs["headers"]["Content-Type"],
            "image/jpeg",
        )


class SeoPromptCustomizationTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_prompt_targets_yoast_seo(self):
        create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"data": []}'))]
            )
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        await wp4ai_generate.generate_seo_data_by_keywords(
            client,
            ["leather handbag"],
        )

        system_prompt = create.await_args.kwargs["messages"][0]["content"]
        self.assertIn("Yoast SEO", system_prompt)
        self.assertNotIn("Rank Math", system_prompt)
        self.assertNotIn("RankMath", system_prompt)

    async def test_custom_prompt_replaces_only_system_message(self):
        create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"data": []}'))]
            )
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        result = await wp4ai_generate.generate_seo_data_by_keywords(
            client,
            ["leather handbag"],
            custom_prompt="Return only the requested JSON.",
        )

        self.assertEqual(result, '{"data": []}')
        messages = create.await_args.kwargs["messages"]
        self.assertEqual(messages[0], {"role": "system", "content": "Return only the requested JSON."})
        self.assertEqual(messages[1], {"role": "user", "content": "leather handbag"})

    async def test_score_retry_forwards_custom_prompt(self):
        generated = [{
            "Url": "leather-handbag",
            "keyword": "leather handbag",
            "seoTitle": "Leather handbag 2026",
            "seoDescription": "Leather handbag description",
            "productDescription": "<h2>Leather handbag</h2><p>" + ("content " * 600) + "</p>",
        }]

        with patch.object(
            wp4ai_generate,
            "generate_seo_data_by_keywords",
            new=AsyncMock(return_value='{"data": []}'),
        ) as generate:
            with patch.object(wp4ai_generate, "_parse_ai_json", return_value=generated):
                with patch.object(wp4ai_generate, "calculate_seo_score_locally", return_value=100):
                    result = await wp4ai_generate.generate_with_score_retry(
                        client=object(),
                        keywords=["leather handbag"],
                        min_score=65,
                        max_retries=1,
                        bak_client=None,
                        custom_prompt="Use my custom system instructions.",
                    )

        self.assertEqual(result, generated)
        self.assertEqual(generate.await_args.kwargs["custom_prompt"], "Use my custom system instructions.")

    async def test_authentication_error_stops_main_retries_and_uses_backup(self):
        generated = [{"keyword": "leather handbag", "seoTitle": "Leather handbag"}]
        main_create = AsyncMock(side_effect=FakeAIError())
        backup_create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"data": []}'))]
            )
        )
        main_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=main_create))
        )
        backup_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=backup_create))
        )

        with patch.object(wp4ai_generate, "_parse_ai_json", return_value=generated):
            with patch.object(wp4ai_generate, "calculate_seo_score_locally", return_value=100):
                result = await wp4ai_generate.generate_with_score_retry(
                    client=main_client,
                    keywords=["leather handbag"],
                    min_score=65,
                    max_retries=5,
                    bak_client=backup_client,
                )

        self.assertEqual(result, generated)
        self.assertEqual(main_create.await_count, 1)
        self.assertEqual(backup_create.await_count, 1)

    async def test_authentication_error_without_backup_is_not_repeated(self):
        main_create = AsyncMock(side_effect=FakeAIError())
        main_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=main_create))
        )

        with self.assertRaises(FakeAIError):
            await wp4ai_generate.generate_with_score_retry(
                client=main_client,
                keywords=["leather handbag"],
                min_score=65,
                max_retries=5,
                bak_client=None,
            )

        self.assertEqual(main_create.await_count, 1)

    async def test_authentication_error_does_not_return_to_main_after_backup_failure(self):
        main_create = AsyncMock(side_effect=FakeAIError())
        backup_create = AsyncMock(side_effect=RuntimeError("backup temporarily unavailable"))
        main_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=main_create))
        )
        backup_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=backup_create))
        )

        with patch.object(wp4ai_generate, "DEFAULT_MODEL", "main-model"):
            with patch.object(wp4ai_generate, "BAK_DEFAULT_MODEL", "backup-model"):
                result = await wp4ai_generate.generate_with_score_retry(
                    client=main_client,
                    keywords=["leather handbag"],
                    min_score=65,
                    max_retries=5,
                    bak_client=backup_client,
                )

        self.assertEqual(result, [])
        self.assertEqual(main_create.await_count, 1)
        self.assertEqual(backup_create.await_count, 5)


class GuiSeoPromptConfigTest(unittest.TestCase):
    def test_site_prompt_is_written_to_worker_config(self):
        site = wp4ai_gui.SiteConfig.new_default()
        site.seo_system_prompt = "Line one\nLine two with 100% precision"
        app = object.__new__(wp4ai_gui.WP4AIGui)

        temp_path = app.write_temp_config(site)
        try:
            worker_config = configparser.ConfigParser(interpolation=None)
            worker_config.read(temp_path, encoding="utf-8")
            self.assertEqual(
                worker_config.get("SEO", "SEO_SYSTEM_PROMPT"),
                site.seo_system_prompt,
            )
        finally:
            os.unlink(temp_path)


class GuiAiConnectionTest(unittest.TestCase):
    def test_ai_connection_redacts_api_key_from_error_body(self):
        response = FakeResponse(
            status_code=401,
            text="invalid api key: sk-test-secret-123",
        )
        app = object.__new__(wp4ai_gui.WP4AIGui)

        with patch.object(wp4ai_gui.requests, "get", return_value=response):
            ok, message = app._test_ai_connection(
                "主AI",
                "https://api.example.com/v1",
                "sk-configured-key",
                model="example-model",
            )

        self.assertFalse(ok)
        self.assertNotIn("sk-test-secret-123", message)
        self.assertIn("[REDACTED_API_KEY]", message)

    def test_ai_connection_reports_model_not_provided_by_endpoint(self):
        response = FakeResponse(payload={"data": [{"id": "deepseek-chat"}]})
        app = object.__new__(wp4ai_gui.WP4AIGui)

        with patch.object(wp4ai_gui.requests, "get", return_value=response):
            ok, message = app._test_ai_connection(
                "备用AI",
                "https://api.deepseek.com/v1",
                "test-key",
                model="qwen3.5-flash-2026-02-23",
            )

        self.assertFalse(ok)
        self.assertIn("qwen3.5-flash-2026-02-23", message)
        self.assertIn("模型", message)

    def test_validate_site_rejects_partial_backup_configuration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            site = wp4ai_gui.SiteConfig.new_default()
            site.products_dir = tmpdir
            site.wp_username = "wp-user"
            site.wp_password = "wp-pass"
            site.ai_api_key = "test-key"
            site.bak_default_model = "qwen3.5-flash-2026-02-23"
            app = object.__new__(wp4ai_gui.WP4AIGui)

            ok, errors = app.validate_site(site)

        self.assertFalse(ok)
        self.assertTrue(any("备用AI" in error for error in errors))


class WordPressDomainNormalizationTest(unittest.TestCase):
    def test_all_wordpress_modules_strip_trailing_domain_slashes(self):
        modules = (
            wp4ai_generate,
            wp4ai_categories,
            wp4ai_media_title,
            wp4ai_fix_schema,
        )

        for module in modules:
            with self.subTest(module=module.__name__):
                self.assertEqual(
                    module._normalize_wp_domain(" https://example.com/// "),
                    "https://example.com",
                )


class CategoryCreationFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_existing_category_create_error_reuses_term_id(self):
        get_response = FakeResponse(
            status_code=200,
            payload=[],
            text="[]",
        )
        term_exists_response = FakeResponse(
            status_code=400,
            url="https://example.com/wp-json/wp/v2/product_cat",
            headers={"Content-Type": "application/json"},
            text='{"code":"term_exists","data":{"term_id":321}}',
            payload={"code": "term_exists", "data": {"term_id": 321}},
        )
        cat_cache = {}

        with patch.object(wp4ai_generate.requests, "get", return_value=get_response):
            with patch.object(wp4ai_generate.requests, "post", return_value=term_exists_response) as post:
                with patch.object(wp4ai_generate, "_generate_cat_seo", new=AsyncMock(return_value={})):
                    with patch.object(wp4ai_generate.asyncio, "sleep", new=AsyncMock()):
                        with self.assertLogs(wp4ai_generate.logger, level="WARNING") as logs:
                            cat_id = await wp4ai_generate._ensure_cat_with_parent(
                                client=None,
                                name="Leather Bags",
                                parent_id=0,
                                cat_cache=cat_cache,
                            )

        self.assertEqual(cat_id, 321)
        self.assertEqual(cat_cache[(0, "leather bags")], 321)
        self.assertEqual(post.call_count, 1)

        log_text = "\n".join(logs.output)
        self.assertIn("已存在", log_text)
        self.assertIn("ID:321", log_text)


class ProductImageDiscoveryTest(unittest.IsolatedAsyncioTestCase):
    def test_bracketed_filename_without_numeric_media_id_is_not_dropped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = os.path.join(tmpdir, "[front].webp")
            marked_path = os.path.join(tmpdir, "[456]back.webp")
            for path in (image_path, marked_path):
                with open(path, "wb") as image:
                    image.write(b"fake-webp")

            image_files, existing_images = wp4ai_generate._collect_product_images(tmpdir)

        self.assertEqual(image_files, [image_path])
        self.assertEqual(existing_images, [("456", "")])

    async def test_product_pipeline_uploads_webp_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            product_dir = os.path.join(tmpdir, "Category", "Product")
            os.makedirs(product_dir)
            image_path = os.path.join(product_dir, "product.webp")
            with open(image_path, "wb") as image:
                image.write(b"fake-webp")

            with patch.dict(os.environ, {"WP4AI_PRODUCTS_DIR": tmpdir}, clear=False):
                with patch.object(wp4ai_generate, "AI_API_KEY", "test-key"):
                    with patch.object(wp4ai_generate, "AsyncOpenAI", return_value=object()):
                        with patch.object(
                            wp4ai_generate.requests,
                            "get",
                            return_value=FakeResponse(payload={"title": "Test Site"}),
                        ):
                            with patch.object(
                                wp4ai_generate,
                                "scan_and_build_cat_cache",
                                new=AsyncMock(),
                            ):
                                with patch.object(
                                    wp4ai_generate,
                                    "generate_with_score_retry",
                                    new=AsyncMock(return_value=[]),
                                ):
                                    with patch.object(
                                        wp4ai_generate,
                                        "upload_wp_media",
                                        return_value=(789, "https://example.com/product.webp"),
                                    ) as upload:
                                        await wp4ai_generate.main()

        upload.assert_called_once()
        uploaded_path = upload.call_args.args[0]
        self.assertEqual(uploaded_path.removeprefix("\\\\?\\"), image_path)

    async def test_product_pipeline_does_not_mark_directory_when_image_upload_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            product_dir = os.path.join(tmpdir, "Category", "Product")
            os.makedirs(product_dir)
            image_path = os.path.join(product_dir, "product.webp")
            with open(image_path, "wb") as image:
                image.write(b"fake-webp")

            generated = [{"keyword": "Product", "seoTitle": "Product"}]
            with patch.dict(os.environ, {"WP4AI_PRODUCTS_DIR": tmpdir}, clear=False):
                with patch.object(wp4ai_generate, "AI_API_KEY", "test-key"):
                    with patch.object(wp4ai_generate, "AsyncOpenAI", return_value=object()):
                        with patch.object(
                            wp4ai_generate.requests,
                            "get",
                            return_value=FakeResponse(payload={"title": "Test Site"}),
                        ):
                            with patch.object(
                                wp4ai_generate,
                                "scan_and_build_cat_cache",
                                new=AsyncMock(),
                            ):
                                with patch.object(
                                    wp4ai_generate,
                                    "generate_with_score_retry",
                                    new=AsyncMock(return_value=generated),
                                ):
                                    with patch.object(
                                        wp4ai_generate,
                                        "upload_wp_media",
                                        return_value=(None, None),
                                    ) as upload:
                                        with patch.object(
                                            wp4ai_generate,
                                            "publish_product_to_wordpress",
                                            return_value=987,
                                        ) as publish:
                                            await wp4ai_generate.main()

            upload.assert_called_once()
            publish.assert_not_called()
            self.assertTrue(os.path.isdir(product_dir))
            self.assertFalse(
                any(
                    path.is_dir() and path.name.startswith("!") and "@_" in path.name
                    for path in Path(tmpdir, "Category").iterdir()
                )
            )


class YoastSeoSyncTest(unittest.TestCase):
    def test_sync_yoast_product_uses_standard_rest_meta(self):
        response = FakeResponse(payload={"id": 987})

        with patch.object(wp4ai_generate.requests, "post", return_value=response) as post:
            result = wp4ai_generate.sync_yoast_seo(
                987,
                "post",
                "focus keyword",
                "SEO Title",
                "SEO description",
            )

        self.assertTrue(result)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0], f"{wp4ai_generate.WP_URL}/product/987")
        self.assertEqual(
            post.call_args.kwargs["json"],
            {
                "meta": {
                    "_yoast_wpseo_focuskw": "focus keyword",
                    "_yoast_wpseo_title": "SEO Title",
                    "_yoast_wpseo_metadesc": "SEO description",
                }
            },
        )

    def test_sync_yoast_term_uses_product_cat_endpoint(self):
        response = FakeResponse(payload={"id": 42})

        with patch.object(wp4ai_generate.requests, "post", return_value=response) as post:
            result = wp4ai_generate.sync_yoast_seo(
                42,
                "term",
                ["wholesale bags", "custom handbags"],
                "Wholesale Bags",
                "Wholesale bags from a custom manufacturer.",
            )

        self.assertTrue(result)
        self.assertEqual(post.call_args.args[0], f"{wp4ai_generate.WP_URL}/product_cat/42")
        self.assertEqual(
            post.call_args.kwargs["json"]["meta"]["_yoast_wpseo_focuskw"],
            "wholesale bags,custom handbags",
        )

    def test_sync_yoast_protected_meta_error_is_not_retried(self):
        response = FakeResponse(
            status_code=400,
            text=(
                '{"code":"rest_cannot_update","message":'
                '"Sorry, you are not allowed to edit the _yoast_wpseo_title custom field."}'
            ),
            payload={
                "code": "rest_cannot_update",
                "message": "Sorry, you are not allowed to edit the protected custom field.",
            },
        )

        with patch.object(wp4ai_generate.requests, "post", return_value=response) as post:
            with patch.object(wp4ai_generate.time, "sleep") as sleep:
                with self.assertLogs(wp4ai_generate.logger, level="ERROR") as logs:
                    result = wp4ai_generate.sync_yoast_seo(
                        987,
                        "post",
                        "focus keyword",
                        "SEO Title",
                        "SEO description",
                    )

        self.assertFalse(result)
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()
        self.assertIn("wp4ai-yoast-rest-meta", "\n".join(logs.output))

    def test_sync_yoast_auth_error_does_not_claim_plugin_is_missing(self):
        response = FakeResponse(
            status_code=401,
            text=(
                '{"code":"rest_cannot_create","message":'
                '"Sorry, you are not allowed to create posts as this user."}'
            ),
            payload={
                "code": "rest_cannot_create",
                "message": "Sorry, you are not allowed to create posts as this user.",
            },
        )

        with patch.object(wp4ai_generate.requests, "post", return_value=response) as post:
            with patch.object(wp4ai_generate.time, "sleep") as sleep:
                with self.assertLogs(wp4ai_generate.logger, level="ERROR") as logs:
                    result = wp4ai_generate.sync_yoast_seo(
                        987,
                        "post",
                        "focus keyword",
                        "SEO Title",
                        "SEO description",
                    )

        self.assertFalse(result)
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()
        log_text = "\n".join(logs.output)
        self.assertNotIn("wp4ai-yoast-rest-meta", log_text)
        self.assertIn("账号权限", log_text)


class CategoryYoastSyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_category_syncs_yoast_without_rankmath_request(self):
        post_urls = []

        def fake_post(url, **kwargs):
            post_urls.append(url)
            if url.endswith("/product_cat"):
                return FakeResponse(payload={"id": 42})
            return FakeResponse(payload={"ok": True})

        generated = {
            "focusKeywords": ["wholesale bags", "custom handbags"],
            "seoTitle": "Wholesale Bags",
            "seoDescription": "Wholesale bags from a custom manufacturer.",
            "seoSchema": {"@type": "ItemList"},
        }

        with patch.object(
            wp4ai_generate.requests,
            "get",
            return_value=FakeResponse(payload=[]),
        ):
            with patch.object(
                wp4ai_generate,
                "_generate_cat_seo",
                new=AsyncMock(return_value=generated),
            ):
                with patch.object(wp4ai_generate.requests, "post", side_effect=fake_post):
                    with patch.object(
                        wp4ai_generate,
                        "sync_yoast_seo",
                        return_value=True,
                    ) as sync:
                        result = await wp4ai_generate._ensure_cat_with_parent(
                            object(),
                            "Wholesale Bags",
                            0,
                            {},
                        )

        self.assertEqual(result, 42)
        sync.assert_called_once_with(
            42,
            "term",
            ["wholesale bags", "custom handbags"],
            "Wholesale Bags",
            "Wholesale bags from a custom manufacturer.",
        )
        self.assertFalse(any("rankmath" in url.lower() for url in post_urls))

    async def test_categories_module_exposes_yoast_term_writer(self):
        response = FakeResponse(payload={"id": 42})

        with patch.object(wp4ai_categories.requests, "post", return_value=response) as post:
            result = wp4ai_categories.sync_yoast_seo(
                42,
                ["wholesale bags", "custom handbags"],
                "Wholesale Bags",
                "Wholesale bags from a custom manufacturer.",
            )

        self.assertTrue(result)
        self.assertEqual(post.call_args.args[0], f"{wp4ai_categories.WP_URL}/product_cat/42")
        self.assertNotIn("rankmath", post.call_args.args[0].lower())
        self.assertEqual(
            post.call_args.kwargs["json"]["meta"],
            {
                "_yoast_wpseo_focuskw": "wholesale bags,custom handbags",
                "_yoast_wpseo_title": "Wholesale Bags",
                "_yoast_wpseo_metadesc": "Wholesale bags from a custom manufacturer.",
            },
        )

    async def test_existing_category_does_not_clear_yoast_with_empty_values(self):
        with patch.object(
            wp4ai_categories,
            "ensure_wp_category",
            new=AsyncMock(return_value=42),
        ):
            with patch.object(wp4ai_categories, "sync_yoast_seo", return_value=True) as sync:
                result = await wp4ai_categories.publish_tag_to_wordpress(
                    "Existing Category",
                    object(),
                )

        self.assertTrue(result)
        sync.assert_not_called()


class YoastPluginContractTest(unittest.TestCase):
    def test_plugin_registers_product_and_category_yoast_meta_for_rest(self):
        plugin_file = Path(
            ROOT,
            "wordpress-plugin",
            "wp4ai-yoast-rest-meta",
            "wp4ai-yoast-rest-meta.php",
        )

        self.assertTrue(plugin_file.is_file(), f"Missing WordPress plugin: {plugin_file}")
        source = plugin_file.read_text(encoding="utf-8")

        self.assertIn("Plugin Name: WP4AI Yoast REST Meta", source)
        self.assertIn("register_post_meta", source)
        self.assertIn("register_term_meta", source)
        self.assertIn("'product'", source)
        self.assertIn("'product_cat'", source)
        self.assertRegex(source, r"'show_in_rest'\s*=>\s*true")
        for meta_key in (
            "_yoast_wpseo_focuskw",
            "_yoast_wpseo_title",
            "_yoast_wpseo_metadesc",
        ):
            with self.subTest(meta_key=meta_key):
                self.assertIn(meta_key, source)


class ReleaseArchiveTest(unittest.TestCase):
    def test_build_log_handles_unicode_on_gbk_console(self):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "gbk"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import build; build.log(chr(0x2705) + ' build complete')",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            result.stdout.decode("ascii", errors="backslashreplace"),
        )
        self.assertIn(bytes.fromhex("e29c85"), result.stdout)

    def test_release_archive_includes_plugin_and_excludes_site_database(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_root = Path(tmpdir)
            dist_dir = temp_root / "dist"
            plugin_dir = temp_root / "wp4ai-yoast-rest-meta"
            release_zip = temp_root / "dist.zip"
            readme_path = temp_root / "README.md"
            dist_dir.mkdir()
            plugin_dir.mkdir()

            (dist_dir / "wp4ai_gui.exe").write_bytes(b"gui")
            (dist_dir / "wp4ai_generate_cli.exe").write_bytes(b"cli")
            (dist_dir / "wp4ai_sites.db").write_bytes(b"plaintext credentials")
            (plugin_dir / "wp4ai-yoast-rest-meta.php").write_text("<?php", encoding="utf-8")
            readme_path.write_text("release notes", encoding="utf-8")

            build_script.build_release_archives(
                dist_dir=dist_dir,
                plugin_dir=plugin_dir,
                release_zip=release_zip,
                readme_path=readme_path,
            )

            with zipfile.ZipFile(release_zip) as archive:
                release_members = set(archive.namelist())
            with zipfile.ZipFile(dist_dir / "wp4ai-yoast-rest-meta.zip") as archive:
                plugin_members = set(archive.namelist())

        self.assertEqual(
            release_members,
            {
                "wp4ai_gui.exe",
                "wp4ai_generate_cli.exe",
                "wp4ai-yoast-rest-meta.zip",
                "README.md",
            },
        )
        self.assertNotIn("wp4ai_sites.db", release_members)
        self.assertIn(
            "wp4ai-yoast-rest-meta/wp4ai-yoast-rest-meta.php",
            plugin_members,
        )


class PublishProductTitleTest(unittest.TestCase):
    def test_publish_product_uses_supplied_product_title_for_wordpress_title(self):
        posts = []

        def fake_post(url, json=None, **kwargs):
            posts.append((url, json))
            if url.endswith("/product"):
                return FakeResponse(payload={"id": 987})
            return FakeResponse(payload={"ok": True})

        ai_data = {
            "Url": "sample-product",
            "productDescription": "<p>Body</p>",
            "keyword": "focus keyword",
            "seoTitle": "SEO Title",
            "seoDescription": "SEO description",
        }

        with patch.object(wp4ai_generate, "_get_post_type_capability", return_value={}):
            with patch.object(wp4ai_generate, "_sync_wc_product_media_schema", return_value=True):
                with patch.object(wp4ai_generate, "sync_yoast_seo", return_value=True) as sync:
                    with patch.object(wp4ai_generate.requests, "post", side_effect=fake_post):
                        result = wp4ai_generate.publish_product_to_wordpress(
                            ai_data,
                            product_title="Directory Product Name",
                        )

        self.assertEqual(result, 987)
        self.assertEqual(posts[0][1]["title"], "Directory Product Name")
        self.assertEqual(len(posts), 1)
        sync.assert_called_once_with(
            987,
            "post",
            "focus keyword",
            "SEO Title",
            "SEO description",
        )


if __name__ == "__main__":
    unittest.main()
