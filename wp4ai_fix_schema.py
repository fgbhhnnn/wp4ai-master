"""
wp4ai_fix_schema.py
====================
将 WordPress 文章 meta 中的 `saswp_custom_schema_field` 字段
从不规范字符串修复为合法的 JSON 格式化字符串，并写回 WP REST API。

支持模式:
  - 单篇处理: python wp4ai_fix_schema.py --post-id 12345
  - 全站处理: python wp4ai_fix_schema.py --all

容错策略:
  1. 优先尝试 json.loads -> json.dumps（标准 JSON）
  2. 失败时改用 ast.literal_eval -> json.dumps（Python 字面量）
  3. 两者均失败则跳过，记录错误日志
"""

import ast
import configparser
import json
import logging
import os
import sys
import time
import argparse
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth

# =========================================================
# 配置 & 日志
# =========================================================

log_dir = os.path.join(os.path.dirname(__file__), 'log')
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, f'wp4ai_fix_schema_{time.strftime("%Y%m%d")}.log')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def _normalize_wp_domain(domain: str) -> str:
    return str(domain or "").strip().rstrip("/")


# 读取配置文件
config = configparser.ConfigParser()
config_path = os.path.join(os.path.dirname(__file__), 'config.ini')

if not os.path.exists(config_path):
    config_path = 'config.ini'

if not os.path.exists(config_path):
    logger.error("❌ 未找到 config.ini，停止执行！")
    sys.exit(1)

config.read(config_path, encoding='utf-8')

try:
    WP_DOMAIN = _normalize_wp_domain(config.get('WordPress', 'WP_DOMAIN'))
    WP_URL = WP_DOMAIN + "/wp-json/wp/v2"
    WP_USERNAME = config.get('WordPress', 'WP_USERNAME')
    WP_PASSWORD = config.get('WordPress', 'WP_PASSWORD')
except (configparser.NoSectionError, configparser.NoOptionError) as e:
    logger.error(f"❌ 配置文件缺少必要字段: {e}")
    sys.exit(1)

AUTH = HTTPBasicAuth(WP_USERNAME, WP_PASSWORD)

# schema 字段名
SCHEMA_META_KEY = "saswp_custom_schema_field"


# =========================================================
# 辅助函数
# =========================================================

def normalize_schema_string(raw: str) -> Optional[str]:
    """将 saswp_custom_schema_field 的原始字符串规范化为合法的 JSON 字符串。

    解析策略（按优先级）：
      1. json.loads -> json.dumps（处理标准 JSON 字符串）
      2. ast.literal_eval -> json.dumps（处理 Python 字典字面量字符串）

    Args:
        raw: meta 字段中的原始字符串值。

    Returns:
        格式化后的 JSON 字符串；若无法解析则返回 None。
    """
    raw = raw.strip()
    logger.debug(f"  原始值 (前 200 字符): {raw[:200]}")
    # --- 策略 1: 标准 JSON 解析 ---
    try:
        parsed = json.loads(raw)
        return raw
    except (json.JSONDecodeError, ValueError):
        logger.info("  ❌ json.loads 失败，尝试 ast.literal_eval...")

    # --- 策略 2: Python 字面量回退 ---
    try:
        parsed = ast.literal_eval(raw)
        return json.dumps(parsed, ensure_ascii=False, indent=None)
    except (ValueError, SyntaxError) as e:
        logger.warning(f"  ⚠️ ast.literal_eval 也失败: {e}")

    return None


def get_post_data(post_id: int, post_type: str = "posts") -> Optional[dict]:
    """通过 WP REST API 获取单篇文章的完整数据（含 meta）。

    Args:
        post_id:   WordPress 文章 ID。
        post_type: 文章类型（REST 端点路径），默认 'posts'。

    Returns:
        成功返回文章数据字典，失败返回 None。
    """
    url = f"{WP_URL}/{post_type}/{post_id}"
    try:
        resp = requests.get(
            url,
            params={"context": "edit"},
            auth=AUTH,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as he:
        logger.error(
            f"❌ 获取文章 [{post_id}] 失败 (HTTP {he.response.status_code}): "
            f"{he.response.text[:200]}"
        )
    except requests.exceptions.RequestException as re_err:
        logger.error(f"❌ 获取文章 [{post_id}] 时发生网络异常: {re_err}")
    return None


def get_all_posts(post_type: str = "posts") -> list[dict]:
    """分页拉取全站所有指定类型的文章列表（含 meta 数据）。

    Args:
        post_type: WordPress 文章类型（REST 端点），默认 'posts'。

    Returns:
        包含所有文章数据字典的列表。
    """
    endpoint = f"{WP_URL}/{post_type}"
    logger.info(f"开始拉取所有 [{post_type}] 类型的文章: {endpoint}")

    all_posts: list[dict] = []
    page = 1
    per_page = 100

    while True:
        logger.info(f"正在拉取第 {page} 页数据...")
        try:
            resp = requests.get(
                endpoint,
                params={"per_page": per_page, "page": page, "context": "edit"},
                auth=AUTH,
                timeout=30,
            )
            resp.raise_for_status()
            batch = resp.json()
        except requests.exceptions.HTTPError as he:
            logger.error(
                f"❌ 拉取第 {page} 页失败 (HTTP {he.response.status_code}): "
                f"{he.response.text[:200]}"
            )
            break
        except requests.exceptions.RequestException as re_err:
            logger.error(f"❌ 拉取第 {page} 页时发生网络异常: {re_err}")
            break

        if not batch:
            break

        all_posts.extend(batch)

        if len(batch) < per_page:
            break

        page += 1

    logger.info(f"✅ 拉取完毕，共找到 {len(all_posts)} 篇 [{post_type}] 类型的文章。")
    return all_posts


def update_post_schema(
    post_id: int,
    post_type: str,
    new_schema_str: str,
    retries: int = 3,
) -> bool:
    """通过 WP REST API 将格式化后的 schema 字符串写回文章 meta。

    Args:
        post_id:        WordPress 文章 ID。
        post_type:      文章类型（REST 端点路径）。
        new_schema_str: 格式化后的 JSON 字符串。
        retries:        失败重试次数，默认 3 次。

    Returns:
        更新成功返回 True，否则返回 False。
    """
    url = f"{WP_URL}/{post_type}/{post_id}"
    payload = {
        "meta": {
            SCHEMA_META_KEY: new_schema_str,
        }
    }

    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                url,
                json=payload,
                auth=AUTH,
                timeout=20,
            )
            resp.raise_for_status()
            logger.info(f"    ✅ 文章 [{post_id}] schema 已成功更新。")
            return True
        except requests.exceptions.HTTPError as he:
            err_text = he.response.text if hasattr(he, 'response') else ''
            logger.warning(
                f"    ❌ HTTP 错误 (尝试 {attempt}/{retries}) 文章 [{post_id}]: "
                f"{he} | {err_text[:200]}"
            )
        except requests.exceptions.RequestException as re_err:
            logger.warning(
                f"    ❌ 网络异常 (尝试 {attempt}/{retries}) 文章 [{post_id}]: {re_err}"
            )

        if attempt < retries:
            time.sleep(2)

    logger.error(f"    ❌ 文章 [{post_id}] 经过 {retries} 次重试仍然失败，放弃更新。")
    return False


# =========================================================
# 核心处理函数
# =========================================================

def process_single_post(post_data: dict, post_type: str) -> tuple[bool, str]:
    """处理单篇文章的 saswp_custom_schema_field 格式化任务。

    Args:
        post_data: 从 WP REST API 获取的文章完整数据字典。
        post_type: 文章类型（REST 端点路径）。

    Returns:
        (是否已成功更新, 结果说明) 元组。
          - (True,  "updated")  -> 已写回
          - (False, "skipped")  -> 字段不存在或已是合法 JSON，无需更新
          - (False, "failed")   -> 解析失败，无法修复
          - (False, "error")    -> API 写回失败
    """
    post_id = post_data.get('id', '?')
    meta: dict = post_data.get('meta', {})

    raw_schema = meta.get(SCHEMA_META_KEY)

    # 字段不存在或为空 -> 跳过
    if not raw_schema or not isinstance(raw_schema, str) or not raw_schema.strip():
        logger.info(f"  ℹ️ 文章 [{post_id}] 的 {SCHEMA_META_KEY} 为空或不存在，跳过。")
        return False, "skipped"

    logger.debug(f"  原始值 (前 200 字符): {raw_schema[:200]}")

    # 格式化
    normalized = normalize_schema_string(raw_schema)

    if normalized is None:
        logger.error(f"  ❌ 文章 [{post_id}] 的 schema 无法解析（json + ast 均失败），跳过。")
        return False, "failed"

    # 若格式化前后完全一致 -> 已经是规范 JSON，无需写回
    if normalized == raw_schema:
        logger.info(f"  ✅ 文章 [{post_id}] schema 已经是规范 JSON，无需更新。")
        return False, "skipped"

    # 写回 API
    ok = update_post_schema(post_id, post_type, normalized)
    if ok:
        return True, "updated"
    return False, "error"


# =========================================================
# 主流程
# =========================================================

def main() -> None:
    logger.info("🚀 [WP4AI] saswp_custom_schema_field JSON 格式化工具已启动！")

    parser = argparse.ArgumentParser(
        description="将 WordPress 文章 meta 中的 saswp_custom_schema_field 格式化为合法 JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python wp4ai_media_title.py           # 批量处理全站所有产品
  python wp4ai_media_title.py 12345     # 只处理文章 ID = 12345
        """
    )
    parser.add_argument(
        'post_id',
        nargs='?',
        type=int,
        default=None,
        help='指定文章 ID 则只处理该篇文章；不指定则批量处理全站所有产品（默认）'
    )

    parser.add_argument(
        '--type',
        type=str,
        default='product',
        dest='post_type',
        help='WordPress 文章类型，默认: product'
    )

    args = parser.parse_args()

    target_id = args.post_id
    post_type = args.post_type

    stats = {"updated": 0, "skipped": 0, "failed": 0, "error": 0}

    # ── 单篇模式 ──────────────────────────────────────────
    if target_id is not None:
        logger.info(f"\n========== 单篇处理模式：文章 ID = {target_id} ==========")
        post_data = get_post_data(target_id, post_type)

        if not post_data:
            logger.error(f"❌ 无法获取文章 [{target_id}] 的数据，任务终止。")
            sys.exit(1)

        _, result = process_single_post(post_data, post_type)
        stats[result] = stats.get(result, 0) + 1

    # ── 全站批量模式 ──────────────────────────────────────
    else:
        logger.info(f"\n========== 全站批量模式：类型 = {post_type} ==========")
        all_posts = get_all_posts(post_type)

        if not all_posts:
            logger.error("❌ 未获取到任何文章，任务终止。")
            sys.exit(1)

        total = len(all_posts)
        for idx, post_data in enumerate(all_posts, start=1):
            post_id = post_data.get('id', '?')
            logger.info(f"\n========== [{idx}/{total}] 处理文章 ID: {post_id} ==========")
            _, result = process_single_post(post_data, post_type)
            stats[result] = stats.get(result, 0) + 1
            # 批量处理时稍作延迟，避免对 API 造成压力
            time.sleep(0.2)

    # ── 汇总报告 ──────────────────────────────────────────
    logger.info(
        f"\n🎉 任务完成！"
        f"已更新: {stats['updated']} 篇 | "
        f"已跳过(无需更新): {stats['skipped']} 篇 | "
        f"解析失败: {stats['failed']} 篇 | "
        f"API写回失败: {stats['error']} 篇"
    )


if __name__ == "__main__":
    main()
