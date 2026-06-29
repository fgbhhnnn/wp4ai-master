import os
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

if "openai" not in sys.modules:
    openai_stub = types.ModuleType("openai")
    openai_stub.AsyncOpenAI = object
    sys.modules["openai"] = openai_stub

import wp4ai_generate


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


if __name__ == "__main__":
    unittest.main()
