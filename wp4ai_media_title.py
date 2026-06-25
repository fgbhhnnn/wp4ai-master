import asyncio
import configparser
import logging
import os
import sys
import time
import argparse
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth

# === 配置区 ===
# 日志
log_dir = os.path.join(os.path.dirname(__file__), 'log')
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, f'wp4ai_media_{time.strftime("%Y%m%d")}.log')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# 读取配置文件
config = configparser.ConfigParser()
config_path = os.path.join(os.path.dirname(__file__), 'config.ini')

if not os.path.exists(config_path):
    config_path = 'config.ini'

if not os.path.exists(config_path):
    logger.error("❌ 未找到配置文件 config.ini，停止执行！请确保脚本目录下存在 config.ini 文件。")
    sys.exit(1)

config.read(config_path, encoding='utf-8')

try:
    WP_DOMAIN = config.get('WordPress', 'WP_DOMAIN')
    WP_URL = WP_DOMAIN + "/wp-json/wp/v2"
    WP_USERNAME = config.get('WordPress', 'WP_USERNAME')
    WP_PASSWORD = config.get('WordPress', 'WP_PASSWORD')
except (configparser.NoSectionError, configparser.NoOptionError) as e:
    logger.error(f"❌ 配置文件 config.ini 缺少必要的键或者分区块内容: {e}，停止执行！请核对。")
    sys.exit(1)

# 全局 HTTP 认证对象
AUTH = HTTPBasicAuth(WP_USERNAME, WP_PASSWORD)


# =========================================================
# 辅助函数
# =========================================================

def parse_attachment_ids(thumbnail_id: str, gallery_str: str) -> list[int]:
    """解析文章 meta 中的附件 ID 列表，合并去重并过滤无效值。

    Args:
        thumbnail_id: _thumbnail_id 字段值，单个 ID 字符串，如 "123"。
        gallery_str:  _product_image_gallery 字段值，逗号分隔多个 ID，如 "124,125,126"。

    Returns:
        去重后的有效附件 ID 整数列表。
    """
    ids: set[int] = set()

    # 解析缩略图 ID
    if thumbnail_id:
        try:
            ids.add(int(str(thumbnail_id).strip()))
        except ValueError:
            logger.warning(f"  ⚠️ _thumbnail_id 无效值: '{thumbnail_id}'，跳过。")

    # 解析图库 ID 列表
    if gallery_str:
        for raw in str(gallery_str).split(','):
            raw = raw.strip()
            if not raw:
                continue
            try:
                ids.add(int(raw))
            except ValueError:
                logger.warning(f"  ⚠️ _product_image_gallery 中含无效 ID: '{raw}'，跳过。")

    return sorted(ids)


def update_media_title_alt(attachment_id: int, new_title: str, retries: int = 3) -> bool:
    """通过 WP REST API 更新单个附件的标题（title）和替代文本（alt_text）。

    Args:
        attachment_id: 媒体附件的 WordPress ID。
        new_title:     要写入的新标题及替代文本内容。
        retries:       请求失败后的最大重试次数，默认 3 次。

    Returns:
        更新成功返回 True，否则返回 False。
    """
    url = f"{WP_URL}/media/{attachment_id}"
    payload = {
        "title": new_title,
        "alt_text": new_title,
    }

    for attempt in range(1, retries + 1):
        try:
            resp = requests.patch(
                url,
                json=payload,
                auth=AUTH,
                timeout=20,
            )
            resp.raise_for_status()
            logger.info(f"    ✅ 附件 [{attachment_id}] title/alt 已更新为: 「{new_title}」")
            return True
        except requests.exceptions.HTTPError as he:
            err_text = he.response.text if hasattr(he, 'response') else ''
            logger.warning(
                f"    ❌ HTTP 错误 (尝试 {attempt}/{retries}) 附件 [{attachment_id}]: "
                f"{he} | {err_text[:200]}"
            )
        except requests.exceptions.RequestException as re_err:
            logger.warning(
                f"    ❌ 网络异常 (尝试 {attempt}/{retries}) 附件 [{attachment_id}]: {re_err}"
            )

        if attempt < retries:
            time.sleep(2)

    logger.error(f"    ❌ 附件 [{attachment_id}] 经过 {retries} 次重试仍然失败，放弃更新。")
    return False


def get_post_data(post_id: int, post_type: str = "product") -> Optional[dict]:
    """通过 WP REST API 获取单篇文章的完整数据（含 meta）。

    Args:
        post_id:   WordPress 文章 ID。
        post_type: 文章类型，默认为 'product'。

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
        logger.error(f"❌ 获取文章 [{post_id}] 失败 (HTTP {he.response.status_code}): {he.response.text[:200]}")
    except requests.exceptions.RequestException as re_err:
        logger.error(f"❌ 获取文章 [{post_id}] 时发生网络异常: {re_err}")
    return None


def get_all_products(post_type: str = "product") -> list[dict]:
    """分页拉取全站所有指定类型的文章列表（含 meta 数据）。

    Args:
        post_type: WordPress 文章类型，默认为 'product'。

    Returns:
        包含所有文章数据字典的列表。
    """
    products_url = f"{WP_URL}/{post_type}"
    logger.info(f"开始拉取所有 [{post_type}] 类型的文章: {products_url}")

    all_products: list[dict] = []
    page = 1
    per_page = 100

    while True:
        logger.info(f"正在拉取第 {page} 页数据...")
        try:
            resp = requests.get(
                products_url,
                params={"per_page": per_page, "page": page, "context": "edit"},
                auth=AUTH,
                timeout=30,
            )
            resp.raise_for_status()
            batch = resp.json()
        except requests.exceptions.HTTPError as he:
            logger.error(f"❌ 拉取第 {page} 页失败 (HTTP {he.response.status_code}): {he.response.text[:200]}")
            break
        except requests.exceptions.RequestException as re_err:
            logger.error(f"❌ 拉取第 {page} 页时发生网络异常: {re_err}")
            break

        if not batch:
            break

        all_products.extend(batch)

        if len(batch) < per_page:
            break

        page += 1

    logger.info(f"✅ 拉取完毕！全站共找到 {len(all_products)} 个 [{post_type}] 类型的文章。")
    return all_products


# =========================================================
# 核心处理函数
# =========================================================

def process_single_post(post_data: dict) -> tuple[int, int]:
    """处理单篇文章的媒体标题/alt 更新任务。

    读取文章 meta 中的 _thumbnail_id 和 _product_image_gallery，
    将所有关联附件的 title 和 alt_text 更新为该文章的标题。

    Args:
        post_data: 从 WP REST API 获取的文章完整数据字典。

    Returns:
        (处理的附件数量, 成功更新的附件数量) 元组。
    """
    post_id = post_data.get('id', '?')

    # 提取文章标题（优先使用 raw，其次 rendered）
    title_obj = post_data.get('title', {})
    post_title = (
        title_obj.get('raw', '').strip()
        or title_obj.get('rendered', '').strip()
    )

    if not post_title:
        logger.warning(f"  ⚠️ 文章 [{post_id}] 没有有效标题，跳过媒体更新。")
        return 0, 0

    logger.info(f"  📝 文章标题: 「{post_title}」")

    # 提取 meta 中的附件 ID
    meta = post_data.get('meta', {})
    thumbnail_id = meta.get('_thumbnail_id', '') or ''
    gallery_str = meta.get('_product_image_gallery', '') or ''

    logger.info(f"  🖼️ _thumbnail_id       = {thumbnail_id!r}")
    logger.info(f"  🖼️ _product_image_gallery = {gallery_str!r}")

    attachment_ids = parse_attachment_ids(str(thumbnail_id), str(gallery_str))

    if not attachment_ids:
        logger.info(f"  ℹ️ 文章 [{post_id}] 未找到任何关联附件，跳过。")
        return 0, 0

    logger.info(f"  🔗 解析到 {len(attachment_ids)} 个附件 ID: {attachment_ids}")

    processed = 0
    succeeded = 0
    for att_id in attachment_ids:
        processed += 1
        ok = update_media_title_alt(att_id, post_title)
        if ok:
            succeeded += 1

    return processed, succeeded


# =========================================================
# 主流程
# =========================================================

async def main():
    logger.info("🚀 [WP4AI] WordPress 媒体标题及 alt 批量优化任务已启动！")

    # 命令行参数解析
    parser = argparse.ArgumentParser(
        description='WordPress 媒体附件 title/alt 自动优化工具',
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

    total_attachment = 0
    total_succeeded = 0

    # ── 单篇模式 ──────────────────────────────────────
    if target_id is not None:
        logger.info(f"\n========== 单篇处理模式：文章 ID = {target_id} ==========")
        post_data = get_post_data(target_id, post_type)

        if not post_data:
            logger.error(f"❌ 无法获取文章 [{target_id}] 的数据，任务终止。")
            return

        att_count, ok_count = process_single_post(post_data)
        total_attachment += att_count
        total_succeeded += ok_count

    # ── 批量模式 ──────────────────────────────────────
    else:
        logger.info(f"\n========== 批量处理模式：类型 = {post_type} ==========")
        all_posts = get_all_products(post_type)

        if not all_posts:
            logger.error("❌ 未获取到任何文章数据，任务终止。")
            return

        total_posts = len(all_posts)
        for idx, post_data in enumerate(all_posts, start=1):
            post_id = post_data.get('id', '?')
            logger.info(f"\n========== [{idx}/{total_posts}] 开始处理 ID: {post_id} ==========")

            att_count, ok_count = process_single_post(post_data)
            total_attachment += att_count
            total_succeeded += ok_count

    # ── 汇总报告 ──────────────────────────────────────
    logger.info(
        f"🎉 任务完成！"
        f"共处理附件 {total_attachment} 个，"
        f"成功更新 {total_succeeded} 个，"
        f"失败 {total_attachment - total_succeeded} 个。"
    )


if __name__ == "__main__":
    asyncio.run(main())
