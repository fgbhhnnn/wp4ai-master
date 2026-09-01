import asyncio
import html
import json
import logging
import os
import time
import requests
import configparser
import sys
import re
from urllib.parse import quote
from requests.auth import HTTPBasicAuth
from openai import AsyncOpenAI

# === 配置区 ===
# 日志
log_dir = os.path.join(os.path.dirname(__file__), 'log')
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, f'wp4ai_{time.strftime("%Y%m%d")}.log')

# 修复：强制将 stdout/stderr 重配置为 UTF-8，防止在 latin-1 等非 UTF-8 终端
# 环境下（如 cron、supervisor、部分 macOS locale 未设置场景）写出中文日志时崩溃。
# errors='backslashreplace' 保证即便遇到极端不可编码字符也只做降级输出，不抛异常。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
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
    # AI 模型相关的常量配置
    AI_API_KEY = config.get('AI', 'AI_API_KEY')
    AI_BASE_URL = config.get('AI', 'AI_BASE_URL')
    DEFAULT_MODEL = config.get('AI', 'DEFAULT_MODEL')

    # 备用 AI 模型配置（可选，读不到则为 None，自动回退主模型）
    BAK_AI_API_KEY = config.get('AI', 'BAK_AI_API_KEY', fallback=None) or None
    BAK_AI_BASE_URL = config.get('AI', 'BAK_AI_BASE_URL', fallback=None) or None
    BAK_DEFAULT_MODEL = config.get('AI', 'BAK_DEFAULT_MODEL', fallback=None) or None

    # WordPress 接口相关的常量配置
    WP_DOMAIN = config.get('WordPress', 'WP_DOMAIN')
    WP_PRODUCT_URL = WP_DOMAIN+"/products/"
    WP_ABOUT_URL = WP_DOMAIN+"/junhao-clothing-streetwear/"
    WP_CONTACT_URL = WP_DOMAIN+"/contact/"
    WP_BLOG_URL = WP_DOMAIN+"/category/blog/"

    WP_URL = WP_DOMAIN+"/wp-json/wp/v2"
    WC_URL = WP_DOMAIN+"/wp-json/wc/v3"
    WP_YOAST_BULK_URL = WP_DOMAIN+"/wp-json/yoast/v1/bulk_editor/update_search"
    WP_YOAST_HEAD_URL = WP_DOMAIN+"/wp-json/yoast/v1/get_head"
    WP_USERNAME = config.get('WordPress', 'WP_USERNAME')  
    WP_PASSWORD = config.get('WordPress', 'WP_PASSWORD') 
except (configparser.NoSectionError, configparser.NoOptionError) as e:
    logger.error(f"❌ 配置文件 config.ini 缺少必要的键或者分区块内容: {e}，停止执行！请核对。")
    sys.exit(1)

# SEO 质量门控配置（可按需修改）
# SEO_MIN_SCORE  : 本地预估分低于此值则打回 AI 重新生成
# SEO_MAX_RETRIES: 单次产品最多重新生成次数（含第 1 次）
SEO_MIN_SCORE: int = int(config.get('SEO', 'SEO_MIN_SCORE', fallback='65'))
SEO_MAX_RETRIES: int = int(config.get('SEO', 'SEO_MAX_RETRIES', fallback='5'))

# 站点名称（main() 启动时从 WP 拉取，供分类 AI Prompt 使用）
SITENAME: str = ""

# === 核心处理函数 ===

def ensure_wp_category(category_name: str) -> int:
    """确认 WordPress 中存在该分类，不存在则新建，返回分类 ID"""
    for attempt in range(5):
        try:
            search_url = f"{WP_URL}/product_cat"
            search_term = category_name.split('&')[0].strip() if '&' in category_name else category_name
            params = {"search": search_term, "hide_empty": False}
            res = requests.get(search_url, params=params, auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD), timeout=15)
            res.raise_for_status()
            cats = res.json()
            
            # 精确匹配（按 & 前缀对比以兼容 API 返回时的转义问题）
            for cat in cats:
                cat_n = cat.get('name', '').split('&')[0].strip().lower()
                local_n = category_name.split('&')[0].strip().lower()
                if cat_n == local_n:
                    return cat.get('id')
                    
            # 如果没有找到，新建分类
            logger.info(f"WordPress 中未找到分类【{category_name}】，正在尝试新建 (尝试 {attempt+1}/5)...")
            payload = {"name": category_name}
            res_post = requests.post(search_url, json=payload, auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD), timeout=15)
            res_post.raise_for_status()
            new_cat = res_post.json()
            return new_cat.get('id')
        except Exception as e:
            logger.warning(f"获取/创建分类【{category_name}】时网络异常 (尝试 {attempt+1}/5): {e}")
            if attempt < 4:
                time.sleep(2)
            else:
                logger.error(f"❌ 经过 5 次尝试获取/建分类依然失败。")
                return None


def _extract_id_and_name(raw_name: str) -> tuple:
    """从目录名提取 [ID] 前缀和真实名称。
    '[42]Men Wear' -> (42, 'Men Wear') | 'Men Wear' -> (None, 'Men Wear')
    """
    if raw_name.startswith('['):
        try:
            id_str = raw_name.split(']')[0].lstrip('[')
            if id_str.isdigit():
                return int(id_str), raw_name[len(id_str) + 2:]
        except Exception:
            pass
    return None, raw_name


async def _generate_cat_seo(client: AsyncOpenAI, cat_name: str) -> dict:
    """为分类名生成描述；分类 Yoast 元数据由人工在后台维护。"""
    prompt = f"- 公司名称:{SITENAME}\n- 网站网址:{WP_DOMAIN}\n- 产品类目:{cat_name}"
    try:
        resp = await client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": """你是专业 B2B 跨境 SEO 文案专家。请只返回 JSON 对象 {\"description\":\"\"}，不要输出解释文字。分类描述必须为纯英文、简洁、面向采购商，突出材质、定制能力和中国工厂优势；禁止虚构价格、MOQ、交期、认证或产能。"""},
                {"role": "user", "content": prompt}
            ],
            stream=False,
            temperature=0.7, max_tokens=4096,
            response_format={"type": "json_object"}
        )
        raw = resp.choices[0].message.content
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"分类 [{cat_name}] SEO 生成失败: {e}")
        return {}


async def _ensure_cat_with_parent(
    client: AsyncOpenAI,
    name: str,
    parent_id: int,
    cat_cache: dict,
):
    """查找或新建指定父分类下的子分类，返回 cat_id，同步写 cat_cache。"""
    cache_key = (parent_id, name.lower())
    if cache_key in cat_cache:
        return cat_cache[cache_key]

    cat_url = f"{WP_URL}/product_cat"
    search_term = name.split('&')[0].strip() if '&' in name else name
    # 先查询是否已存在
    for attempt in range(5):
        try:
            res = await asyncio.to_thread(
                requests.get, cat_url,
                params={"search": search_term, "hide_empty": False, "parent": parent_id},
                auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD), timeout=15,
            )
            res.raise_for_status()
            for cat in res.json():
                cat_n = cat.get('name', '').split('&')[0].strip().lower()
                local_n = name.split('&')[0].strip().lower()
                if cat_n == local_n and cat.get("parent", -1) == parent_id:
                    cat_cache[cache_key] = cat["id"]
                    logger.info(f"  ✅ 分类【{name}】已存在 (parent:{parent_id}) ID:{cat['id']}")
                    return cat["id"]
            break
        except Exception as e:
            logger.warning(f"  查询分类【{name}】异常 (尝试 {attempt+1}/5): {e}")
            if attempt < 4:
                await asyncio.sleep(2)
            else:
                return None

    # 不存在 → AI 生成描述并新建
    logger.info(f"  -> 新建分类【{name}】(parent:{parent_id})...")
    ai = await _generate_cat_seo(client, name)
    description = ai.get("description", ai.get("seoDescription", ""))

    cat_id = None
    for attempt in range(5):
        try:
            payload = {
                "name": name, "parent": parent_id,
                "description": description,
            }
            res_post = await asyncio.to_thread(
                requests.post, cat_url, json=payload,
                auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD), timeout=15,
            )
            res_post.raise_for_status()
            cat_id = res_post.json().get("id")
            logger.info(f"  ✅ 分类【{name}】新建成功 ID:{cat_id}")
            break
        except Exception as e:
            logger.warning(f"  新建分类【{name}】异常 (尝试 {attempt+1}/5): {e}")
            if attempt < 4:
                await asyncio.sleep(2)
            else:
                logger.error(f"  ❌ 新建分类【{name}】失败。")
                return None

    if cat_id:
        cat_cache[cache_key] = cat_id
    return cat_id


async def scan_and_build_cat_cache(
    client: AsyncOpenAI,
    base_dir: str,
    cat_cache: dict,
    actual_names: dict,
) -> None:
    """递归扫描 base_dir，逐层建立/复用 WordPress 分类，写入 cat_cache。

    目录规则：最深一层无子目录的目录为产品目录，其余各层为分类。
    cat_cache key: (parent_id, name.lower()) -> cat_id
    actual_names key: path_tuple -> 当前真实目录名（可能带 [ID] 前缀）
    """
    # 收集所有分类路径（保留原始目录名含 [ID] 前缀）
    cat_paths: set[tuple] = set()
    for dirpath, dirnames, filenames in os.walk(base_dir):
        rel = os.path.relpath(dirpath, base_dir)
        parts = tuple(rel.split(os.sep)) if rel != '.' else ()
        if not parts:
            continue
        has_sub = any(os.path.isdir(os.path.join(dirpath, d)) for d in os.listdir(dirpath))
        cat_parts = parts[:-1] if not has_sub else parts
        for i in range(1, len(cat_parts) + 1):
            cat_paths.add(cat_parts[:i])

    if not cat_paths:
        return

    # 初始化 actual_names
    for p in cat_paths:
        if p not in actual_names:
            actual_names[p] = p[-1]

    for path_tuple in sorted(cat_paths, key=lambda p: len(p)):
        raw_name = path_tuple[-1]
        prefilled_id, cat_name = _extract_id_and_name(raw_name)

        # 计算父 ID
        p_id = 0
        for i, rp in enumerate(path_tuple[:-1]):
            grand = 0 if i == 0 else p_id
            _, rn = _extract_id_and_name(rp)
            p_id = cat_cache.get((grand, rn.lower()), 0)
        parent_id = p_id if path_tuple[:-1] else 0

        cache_key = (parent_id, cat_name.lower())
        if prefilled_id is not None:
            if cache_key not in cat_cache:
                cat_cache[cache_key] = prefilled_id
            logger.info(f"📁 【{cat_name}】已标注 ID={prefilled_id}，跳过新建。")
            continue

        display = ' > '.join(_extract_id_and_name(p)[1] for p in path_tuple)
        logger.info(f"\n>>> 处理分类【{display}】(parent={parent_id})")
        new_id = await _ensure_cat_with_parent(client, cat_name, parent_id, cat_cache)

        # 新建成功 → 重命名本地目录打 [ID] 标记
        if new_id:
            try:
                cur = base_dir
                for i in range(1, len(path_tuple) + 1):
                    cur = os.path.join(cur, actual_names.get(path_tuple[:i], path_tuple[i-1]))
                new_raw = f"[{new_id}]{cat_name}"
                new_abs = os.path.join(os.path.dirname(cur), new_raw)
                if os.path.exists(cur) and not os.path.exists(new_abs):
                    os.rename(cur, new_abs)
                    actual_names[path_tuple] = new_raw
                    logger.info(f"  🌟 本地目录已标记: {new_raw}")
                elif os.path.exists(new_abs):
                    actual_names[path_tuple] = new_raw
            except Exception as e:
                logger.warning(f"  ⚠️ 重命名失败（不影响分类建立）: {e}")


async def update_categories_description(client: AsyncOpenAI) -> None:
    """拉取所有 product_cat，对无描述的分类逐一 AI 生成并更新。"""
    cat_url = f"{WP_URL}/product_cat"
    all_cats, page = [], 1
    while True:
        try:
            res = await asyncio.to_thread(
                requests.get, cat_url,
                params={"per_page": 100, "page": page, "hide_empty": False},
                auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD), timeout=15,
            )
            res.raise_for_status()
            batch = res.json()
            if not batch:
                break
            all_cats.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        except Exception as e:
            logger.error(f"❌ 获取分类列表失败 (第 {page} 页): {e}")
            break

    need = [c for c in all_cats if not (c.get("description") or "").strip()]
    logger.info(f"📄 共 {len(all_cats)} 个分类，其中 {len(need)} 个无描述。")

    for cat in need:
        cat_id, cat_name = cat.get("id"), cat.get("name", "")
        logger.info(f"\n>>> 补全分类【{cat_name}】(ID:{cat_id})...")
        ai = await _generate_cat_seo(client, cat_name)
        if not ai:
            continue
        description = ai.get("description", ai.get("seoDescription", ""))
        # PATCH
        for attempt in range(5):
            try:
                res_p = await asyncio.to_thread(
                    requests.post, f"{cat_url}/{cat_id}",
                    json={"description": description},
                    auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD), timeout=15,
                )
                res_p.raise_for_status()
                logger.info(f"  ✅ 描述已更新（{len(description)} 字符）")
                break
            except Exception as e:
                logger.warning(f"  PATCH 失败 (尝试 {attempt+1}/5): {e}")
                if attempt < 4:
                    await asyncio.sleep(2)
    logger.info(f"\n🎉 --update 完成！")


def upload_wp_media(file_path: str):
    """上传本地图片到 WordPress 媒体库并返回 ID 和 URL"""
    url = f"{WP_URL}/media"
    filename = os.path.basename(file_path)
    ext = filename.split('.')[-1].lower()
    content_type = "image/png" if ext == "png" else "image/jpeg"
    
    # RFC 5987：HTTP 头部只允许 latin-1，中文/特殊字符必须用 filename*=UTF-8''<url_encoded>
    # 同时保留 ASCII 安全的 filename= 作为旧版服务端/客户端兼容降级。
    filename_ascii = filename.encode("ascii", errors="replace").decode("ascii")
    filename_encoded = quote(filename, safe="")
    headers = {
        "Content-Disposition": (
            f'attachment; filename="{filename_ascii}"; '
            f"filename*=UTF-8''{filename_encoded}"
        ),
        "Content-Type": content_type,
    }
    
    for attempt in range(5):
        try:
            logger.info(f"    -> 正在上传第三层图片文件: {filename} (尝试 {attempt+1}/5) ...")
            with open(file_path, "rb") as f:
                res = requests.post(url, headers=headers, data=f, auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD), timeout=60)
            res.raise_for_status()
            data = res.json()
            return data.get("id"), data.get("source_url")
        except Exception as e:
            logger.warning(f"❌ 上传图片 {filename} 失败 (尝试 {attempt+1}/5): {e}")
            if attempt < 4:
                time.sleep(2)
            else:
                logger.error(f"❌ 图片 {filename} 经过 5 次尝试依然上传失败。")
                return None, None


async def generate_seo_data_by_keywords(
    client: AsyncOpenAI,
    keywords: list[str],
    main_img_url: str = "",
    model: str = DEFAULT_MODEL,
):
    """根据产品关键词生成可供 Yoast 和 SASWP 使用的英文 SEO 数据。"""
    product_input = "、".join(keywords)
    image_instruction = ""
    if main_img_url:
        logger.info("存在主图:%s", main_img_url)
        image_instruction = (
            "Insert the product image once in the HTML where relevant: "
            f'<img src="{main_img_url}" alt="focus keyphrase" />. '
        )

    # 压缩长版行业提示词，只保留影响字段正确性和发布安全的强约束。
    prompt = (
       
    )

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a Yoast SEO Premium cannabis packaging B2B expert.",
                },
                {"role": "user", "content": prompt},
            ],
            stream=False,
            temperature=0.7,
            max_tokens=65536,
            response_format={"type": "json_object"},
            extra_body={"enable_thinking": True, "thinking_budget": 8000},
        )
        choice = response.choices[0]
        if choice.finish_reason == "length":
            logger.warning("AI 输出达到 max_tokens，丢弃截断的 JSON 并进入重试")
            return None

        content = choice.message.content or ""
        if not content.strip():
            reasoning = getattr(choice.message, "reasoning_content", "") or ""
            logger.error(
                "AI 思考完成但未返回最终 content（reasoning_content 长度: %s）",
                len(reasoning),
            )
            return None
        return content
    except Exception as exc:
        logger.error("调用 AI 接口时发生异常: %s", exc, exc_info=True)
        return None


# === Yoast SEO Premium 发布实现 ===

def _parse_ai_json(raw_str: str) -> list[dict]:
    """解析 AI JSON，统一返回产品对象列表。"""
    if not isinstance(raw_str, str):
        return None
    cleaned = raw_str.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        result = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        # 某些兼容 OpenAI 的模型会在 JSON 前后附带一句说明，提取首个完整对象。
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            logger.error("模型返回的内容无法反序列化为合法 JSON")
            return None
        try:
            result = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            logger.error("模型返回的内容无法反序列化为合法 JSON")
            return None
    if isinstance(result, dict) and isinstance(result.get("data"), list):
        return result["data"]
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return [result]
    logger.error("模型返回的 JSON 顶层类型无效: %s", type(result).__name__)
    return None

def normalize_ai_item(item: dict) -> dict:
    """将新旧 AI 字段统一为 Yoast 产品发布结构。"""
    if not isinstance(item, dict):
        raise ValueError("AI 产品数据必须是对象")

    raw_keywords = item.get("keyphraseSynonyms", [])
    if isinstance(raw_keywords, str):
        synonyms = re.split(r"[,，、;；|]+", raw_keywords)
    elif isinstance(raw_keywords, (list, tuple)):
        synonyms = [str(value) for value in raw_keywords]
    else:
        synonyms = []

    legacy_keywords = item.get("keyword", "")
    if not synonyms and legacy_keywords:
        values = re.split(r"[,，、;；|]+", str(legacy_keywords))
        focus = str(item.get("focusKeyphrase") or values[0] or "")
        synonyms = values[1:] if not item.get("focusKeyphrase") else values
    else:
        focus = str(item.get("focusKeyphrase") or "").strip()

    if not focus and legacy_keywords:
        focus = str(legacy_keywords).split(",")[0].strip()
    focus = re.sub(r"\s+", " ", focus).strip()

    clean_synonyms = []
    seen = {focus.casefold()} if focus else set()
    for value in synonyms:
        value = re.sub(r"\s+", " ", value).strip()
        if value and value.casefold() not in seen:
            clean_synonyms.append(value)
            seen.add(value.casefold())

    if not focus:
        raise ValueError("AI 未返回 focusKeyphrase/keyword")
    if len(clean_synonyms) > 5:
        clean_synonyms = clean_synonyms[:5]

    slug = item.get("slug", item.get("Url", item.get("afterUrl", "")))
    slug = re.sub(r"[^a-z0-9-]+", "-", str(slug).lower()).strip("-")
    description = item.get("metaDescription", item.get("seoDescription", ""))
    return {
        **item,
        "slug": slug,
        "focusKeyphrase": focus,
        "keyphraseSynonyms": clean_synonyms,
        "seoTitle": str(item.get("seoTitle", "")).strip(),
        "metaDescription": str(description).strip(),
    }


def build_yoast_synonyms_value(synonyms: list[str]) -> str:
    """按 Yoast Premium 的存储约定生成同义词 JSON 字符串。"""
    values = [str(value).strip() for value in synonyms if str(value).strip()]
    return json.dumps([", ".join(values)], ensure_ascii=False)


def _replace_schema_images(value, image_url: str):
    """递归替换 Schema 的图片字段和约定占位图片。"""
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if key == "image" and isinstance(child, str):
                result[key] = image_url
            else:
                result[key] = _replace_schema_images(child, image_url)
        return result
    if isinstance(value, list):
        return [_replace_schema_images(child, image_url) for child in value]
    if value == "https://example.com/image.jpg":
        return image_url
    return value


def normalize_saswp_schema(schema, image_url: str = "") -> tuple[dict, str]:
    """校验并序列化 SASWP 自定义 Schema。

    Args:
        schema: AI 返回的 JSON 对象或包含 JSON 对象的字符串。
        image_url: 已上传到 WordPress 的真实主图 URL。

    Returns:
        规范化后的 Schema 对象及其 JSON 字符串。

    Raises:
        ValueError: Schema 缺失、不是 JSON 对象或无法解析。
    """
    if isinstance(schema, str):
        try:
            normalized = json.loads(schema)
        except json.JSONDecodeError as exc:
            raise ValueError("Schema 字符串不是合法 JSON") from exc
    elif isinstance(schema, dict):
        # JSON 往返会隔离原对象，避免替换图片时修改 AI 原始结果。
        normalized = json.loads(json.dumps(schema, ensure_ascii=False))
    else:
        raise ValueError("Schema 必须是非空 JSON 对象")

    if not isinstance(normalized, dict) or not normalized:
        raise ValueError("Schema 必须是非空 JSON 对象")
    if image_url:
        normalized = _replace_schema_images(normalized, image_url)
    return normalized, json.dumps(normalized, ensure_ascii=False)


def calculate_yoast_score(item: dict) -> int:
    """执行轻量 Yoast 风格门控，不伪造或写入 Yoast 分数。"""
    data = normalize_ai_item(item)
    focus = data["focusKeyphrase"].casefold()
    title = data["seoTitle"].casefold()
    description = data["metaDescription"].casefold()
    body = re.sub(r"<[^>]+>", " ", data.get("productDescription", "")).casefold()
    score = 0
    score += 25 if focus in title else 0
    score += 25 if focus in description else 0
    score += 15 if focus in body else 0
    score += 15 if 50 <= len(data["seoTitle"]) <= 60 else 0
    score += 10 if 120 <= len(data["metaDescription"]) <= 170 else 0
    score += 10 if len(body.split()) >= 600 else 0
    return score


def _build_score_post_data(item: dict) -> dict:
    """兼容旧调用方，返回 Yoast 门控所需的标准数据。"""
    return normalize_ai_item(item)


async def generate_with_score_retry(
    client: AsyncOpenAI,
    keywords: list[str],
    main_img_url: str = "",
    *,
    min_score: int = SEO_MIN_SCORE,
    max_retries: int = SEO_MAX_RETRIES,
    bak_client: AsyncOpenAI = None,
) -> list[dict]:
    """生成并校验产品 SEO 数据，返回所有通过归一化的产品。"""
    best_result = []
    best_score = -1
    hint = ""
    for attempt in range(1, max_retries + 1):
        active_client = bak_client if attempt == max_retries and bak_client else client
        model = BAK_DEFAULT_MODEL if active_client is bak_client and bak_client else DEFAULT_MODEL
        request_keywords = keywords + ([hint] if hint else [])
        raw = await generate_seo_data_by_keywords(
            active_client, request_keywords, main_img_url, model=model
        )
        if not raw:
            continue
        items = _parse_ai_json(raw) or []
        if len(items) != 1:
            logger.warning("AI 必须返回恰好 1 个产品，实际返回 %s 个。", len(items))
            continue
        try:
            normalized = [normalize_ai_item(item) for item in items]
        except (TypeError, ValueError) as exc:
            logger.warning("第 %s 次 AI 结果字段无效: %s", attempt, exc)
            continue
        if not normalized:
            continue
        if len(normalized[0]["keyphraseSynonyms"]) != 5:
            logger.warning(
                "AI 必须返回恰好 5 个去重同义词，实际返回 %s 个。",
                len(normalized[0]["keyphraseSynonyms"]),
            )
            continue
        scores = [calculate_yoast_score(item) for item in normalized]
        batch_score = min(scores)
        content_lengths = [
            len(re.sub(r"<[^>]+>", " ", item.get("productDescription", "")).split())
            for item in normalized
        ]
        content_ok = all(600 <= length <= 700 for length in content_lengths)
        if batch_score > best_score:
            best_score, best_result = batch_score, normalized
        if batch_score >= min_score and content_ok:
            logger.info("Yoast SEO 门控通过，批次最低分 %s。", batch_score)
            return normalized
        if not content_ok:
            logger.warning(
                "Yoast 内容长度未达标（实际 %s 词，要求 600-700），进入重试。",
                content_lengths,
            )
        length_hint = (
            " The previous HTML body was outside 600-700 words; rewrite it to stay "
            "strictly within that range."
            if not content_ok
            else ""
        )
        hint = (
            f"Previous Yoast-style score was {batch_score}; ensure the focus keyphrase "
            "appears naturally in the SEO title, meta description, and opening content."
            + length_hint
        )
    best_lengths = [
        len(re.sub(r"<[^>]+>", " ", item.get("productDescription", "")).split())
        for item in best_result
    ]
    if best_result and best_score >= min_score and all(600 <= n <= 700 for n in best_lengths):
        logger.warning("Yoast 重试耗尽，返回满足内容长度的最高分批次 %s。", best_score)
        return best_result
    logger.error("Yoast 门控未通过，放弃发布（最高分 %s，正文长度 %s）。", best_score, best_lengths)
    return []


def _request_json(method: str, url: str, **kwargs) -> dict:
    """执行带认证的 JSON 请求并返回对象；错误包含响应正文便于诊断。"""
    response = requests.request(
        method, url, auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD), timeout=30, **kwargs
    )
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
    return response.json() if response.content else {}


def preflight_wordpress() -> bool:
    """验证目标站点路由、产品类型和 Yoast 管理权限。"""
    try:
        root = _request_json("GET", f"{WP_DOMAIN}/wp-json/")
        routes = root.get("routes", {})
        required = ["/wp/v2/product", "/yoast/v1/bulk_editor/update_search"]
        if any(route not in routes for route in required):
            logger.error("目标站点缺少必要 REST 路由: %s", required)
            return False
        schema = _request_json("OPTIONS", f"{WP_URL}/product")
        methods = schema.get("methods", [])
        if "POST" not in methods:
            logger.error("当前账号无法创建 WooCommerce product")
            return False
        current_user = _request_json("GET", f"{WP_URL}/users/me", params={"context": "edit"})
        if not current_user.get("id"):
            logger.error("WordPress 账号身份校验失败")
            return False
        # 空批次不会写入数据；有权限时 Yoast 返回 400 参数校验错误，无权限返回 401/403。
        permission_probe = requests.post(
            WP_YOAST_BULK_URL,
            json={"items": []},
            auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD),
            timeout=20,
        )
        if permission_probe.status_code not in (400,):
            logger.error(
                "账号缺少 Yoast 管理权限，权限探测返回 HTTP %s: %s",
                permission_probe.status_code,
                permission_probe.text[:300],
            )
            return False
        logger.info("WordPress/Yoast 路由预检通过。")
        return True
    except Exception as exc:
        logger.error("WordPress 预检失败: %s", exc)
        return False


def _yoast_update(product_id: int, data: dict) -> None:
    """通过 Yoast Premium Bulk Editor 写入三项搜索外观字段。"""
    payload = {"items": [{
        "id": int(product_id),
        "seo_title": data["seoTitle"],
        "meta_description": data["metaDescription"],
        "focus_keyphrase": data["focusKeyphrase"],
    }]}
    result = _request_json("POST", WP_YOAST_BULK_URL, json=payload)
    entries = result.get("results", [])
    if not entries or not entries[0].get("success"):
        raise RuntimeError(f"Yoast SEO 写入失败: {result}")


def _verify_yoast_head(product_url: str, data: dict) -> None:
    """回查公开 Yoast head，确认标题和描述已进入最终输出。"""
    last_error = ""
    for attempt in range(5):
        try:
            result = _request_json("GET", WP_YOAST_HEAD_URL, params={"url": product_url})
            output = result.get("json", {})
            actual_title = html.unescape(str(output.get("title", "")))
            actual_description = html.unescape(str(output.get("description", "")))
            if data["seoTitle"] in actual_title and actual_description == data["metaDescription"]:
                return
            last_error = f"title={actual_title!r}, description={actual_description!r}"
        except Exception as exc:
            last_error = str(exc)
        if attempt < 4:
            time.sleep(2)
    raise RuntimeError(f"Yoast get_head 回查失败: {last_error}")


def publish_product_to_wordpress(
    ai_data: dict,
    cat_id: int = None,
    thumbnail_id: str = "",
    gallery_ids: str = "",
    main_image_url: str = "",
    product_title: str = "",
):
    """以草稿为事务边界发布产品，并验证 Yoast 与 SASWP 字段。"""
    data = normalize_ai_item(ai_data)
    images = []
    if thumbnail_id:
        images.append({"id": int(thumbnail_id)})
    for value in (gallery_ids or "").split(","):
        if value.strip().isdigit():
            images.append({"id": int(value.strip())})
    payload = {
        "title": product_title or data["focusKeyphrase"],
        "slug": data["slug"],
        "content": data.get("productDescription", ""),
        "status": "draft",
    }
    if cat_id is not None:
        payload["product_cat"] = [int(cat_id)]
    if images:
        payload["featured_media"] = images[0]["id"]
    created = _request_json("POST", f"{WP_URL}/product", json=payload)
    product_id = created.get("id")
    if not product_id:
        raise RuntimeError(f"产品草稿创建响应缺少 ID: {created}")
    try:
        schema_object, schema_value = normalize_saswp_schema(
            data.get("Schema"), main_image_url
        )
        _request_json(
            "PUT",
            f"{WP_URL}/product/{product_id}",
            json={"meta": {"saswp_custom_schema_field": schema_value}},
        )
        wp_verified = _request_json(
            "GET",
            f"{WP_URL}/product/{product_id}",
            params={"context": "edit"},
        )
        stored_schema = wp_verified.get("meta", {}).get(
            "saswp_custom_schema_field"
        )
        stored_object, _ = normalize_saswp_schema(stored_schema)
        if stored_object != schema_object:
            raise RuntimeError("SASWP Schema 回查不一致")

        _yoast_update(product_id, data)
        wc_payload = {"meta_data": [{
            "key": "_yoast_wpseo_keywordsynonyms",
            "value": build_yoast_synonyms_value(data["keyphraseSynonyms"]),
        }]}
        if images:
            wc_payload["images"] = images
        _request_json("PUT", f"{WC_URL}/products/{product_id}", json=wc_payload)
        verified = _request_json("GET", f"{WC_URL}/products/{product_id}")
        actual = next((m.get("value") for m in verified.get("meta_data", [])
                       if m.get("key") == "_yoast_wpseo_keywordsynonyms"), None)
        expected = build_yoast_synonyms_value(data["keyphraseSynonyms"])
        if actual != expected:
            raise RuntimeError("Yoast keyphrase synonyms 回查不一致")
        published = _request_json("PUT", f"{WC_URL}/products/{product_id}",
                                  json={"status": "publish"})
        if published.get("status") != "publish":
            raise RuntimeError(f"产品发布失败: {published}")
        product_url = published.get("permalink") or created.get("link")
        if not product_url:
            raise RuntimeError("产品发布响应缺少 permalink")
        _verify_yoast_head(product_url, data)
        logger.info("产品 [%s] 已发布并完成 Yoast SEO 校验。", product_id)
        return product_id
    except Exception as exc:
        logger.error("产品 [%s] SEO/发布失败，保留草稿: %s", product_id, exc)
        try:
            _request_json("PUT", f"{WC_URL}/products/{product_id}",
                          json={"status": "draft"})
        except Exception as restore_exc:
            logger.error("恢复产品 [%s] 草稿状态失败: %s", product_id, restore_exc)
        return None


# === 启动主流程 ===

async def main():
    logger.info("\n🚀 [WP4AI] 全自动发布管家已启动！")

    if not AI_API_KEY:
        logger.error("在开始之前，请务必保证你挂载了对应的 API 环境变量！")
        return

    base_dir = "products"
    if not os.path.exists(base_dir) or not os.path.isdir(base_dir):
        logger.error(f"当前目录下未找到 [{base_dir}] 文件夹，任务已退出。")
        return

    client = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)

    # 备用模型客户端
    bak_client = None
    if BAK_AI_API_KEY and BAK_AI_BASE_URL and BAK_DEFAULT_MODEL:
        bak_client = AsyncOpenAI(api_key=BAK_AI_API_KEY, base_url=BAK_AI_BASE_URL)
        logger.info(f"✅ 备用模型已就绪：{BAK_DEFAULT_MODEL}")
    else:
        logger.info("ℹ️  未配置备用模型，最后一次重试继续使用主模型。")

    # 初始化：从 WordPress 拉取站点名称（供分类 AI Prompt 使用）
    global SITENAME
    _default_sitename = "junhao factory"
    settings_url = f"{WP_URL}/settings"
    for attempt in range(1, 6):
        try:
            res_s = await asyncio.to_thread(
                requests.get, settings_url,
                auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD), timeout=10,
            )
            res_s.raise_for_status()
            SITENAME = res_s.json().get("title", "") or ""
            if not SITENAME:
                SITENAME = _default_sitename
                logger.info(f"🌐 站点名称为空，使用默认值: {SITENAME!r}")
            else:
                logger.info(f"🌐 站点名称: {SITENAME!r}")
            break
        except Exception as e:
            logger.warning(f"⚠️ 获取站点名称失败 (尝试 {attempt}/5): {e}")
            if attempt < 5:
                await asyncio.sleep(2)
            else:
                SITENAME = _default_sitename
                logger.warning(f"⚠️ 无法获取站点名称，使用默认值: {SITENAME!r}")

    cli_args = sys.argv[1:]

    # === 模式 1：--scan，仅递归建立分类，不发布产品 ===
    if "--scan" in cli_args:
        logger.info("🔍 --scan 模式：递归扫描目录树建立分类...")
        cat_cache: dict = {}
        actual_names: dict = {}
        await scan_and_build_cat_cache(client, base_dir, cat_cache, actual_names)
        logger.info("🎉 --scan 完成！")
        return

    # === 模式 2：--update，补全无描述分类 ===
    if "--update" in cli_args:
        logger.info("🔄 --update 模式：补全无描述分类...")
        await update_categories_description(client)
        return

    # === 模式 3（默认）：建分类 + 发布产品 ===
    if not preflight_wordpress():
        logger.error("WordPress/Yoast 预检未通过，未创建任何产品。")
        return

    logger.info("📂 全流程模式：先建分类，再发布产品...")

    # 阶段一：递归扫描目录树，建好所有分类，cat_cache 中记录每层分类的 ID
    cat_cache: dict = {}       # (parent_id, name.lower()) -> cat_id
    actual_names: dict = {}    # path_tuple -> 当前真实目录名（含 [ID] 前缀）
    logger.info("\n===== 阶段一：建立分类体系 =====")
    await scan_and_build_cat_cache(client, base_dir, cat_cache, actual_names)

    # 阶段二：遍历所有叶子产品目录（无子目录的目录）发布产品
    logger.info("\n===== 阶段二：发布产品 =====")

    async def _process_product_dir(prod_path: str, prod_name: str, cat_id: int):
        """处理单个产品目录：上传图片 → AI 生成内容 → 发布到 WP。"""
        if prod_name.startswith("!") and "@_" in prod_name:
            return  # 已完成（格式：!{wp_id}@_产品名），跳过

        keyword = _extract_id_and_name(prod_name)[1]  # 剥离可能的 [ID] 前缀
        logger.info(f"\n⚡ 产品目录: [{keyword}] | 分类 ID: {cat_id}")

        # 收集图片
        image_files, existing_images = [], []
        for file_name in os.listdir(prod_path):
            if not file_name.lower().endswith(('.png', '.jpg', '.jpeg','.webp')):
                continue
            if file_name.startswith('['):
                try:
                    img_id = file_name.split(']')[0].lstrip('[')
                    if img_id.isdigit():
                        existing_images.append((img_id, ""))
                except Exception:
                    pass
            else:
                image_files.append(os.path.join(prod_path, file_name))

        image_files.sort()
        existing_images.sort()
        uploaded_images = list(existing_images)

        if image_files:
            logger.info(f"📸 找到 {len(image_files)} 张图片，开始上传...")
            for img_path in image_files:
                img_id, img_url = await asyncio.to_thread(upload_wp_media, img_path)
                if img_id:
                    uploaded_images.append((img_id, img_url))
                    try:
                        new_name = f"[{img_id}]{os.path.basename(img_path)}"
                        os.rename(img_path, os.path.join(os.path.dirname(img_path), new_name))
                        logger.info(f"    🌟 图片已标记: {new_name}")
                    except Exception as e:
                        logger.error(f"图片重命名失败: {e}")
        elif existing_images:
            logger.info(f"📸 发现 {len(existing_images)} 张已标记历史图片，直接复用。")

        thumbnail_id = ""
        gallery_ids = ""
        main_image_url = ""

        if uploaded_images:
            thumbnail_id = str(uploaded_images[0][0])
            main_image_url = uploaded_images[0][1]
            if not main_image_url:
                try:
                    res = await asyncio.to_thread(
                        requests.get, f"{WP_URL}/media/{thumbnail_id}",
                        auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD), timeout=15
                    )
                    res.raise_for_status()
                    main_image_url = res.json().get("source_url", "")
                    uploaded_images[0] = (thumbnail_id, main_image_url)
                    logger.info(f"    🔄 已补齐主图 URL: {main_image_url}")
                except Exception as e:
                    logger.warning(f"无法获取主图 URL: {e}")
            if len(uploaded_images) > 1:
                gallery_ids = ",".join(str(img[0]) for img in uploaded_images[1:])

        logger.info(f"👉 投递 AI 生成产品内容（门控: {SEO_MIN_SCORE}，重试: {SEO_MAX_RETRIES}）...")
        try:
            ai_data_list = await generate_with_score_retry(
                client, [keyword], main_image_url,
                min_score=SEO_MIN_SCORE, max_retries=SEO_MAX_RETRIES,
                bak_client=bak_client,
            )
            if not ai_data_list:
                logger.error(f"[{keyword}] 重试后仍无有效 AI 数据，跳过。")
                return

            success_count = 0
            last_wp_id = None
            for item in ai_data_list:
                if not isinstance(item, dict):
                    continue
                kw = item.get("focusKeyphrase", item.get("keyword", "?"))
                logger.info(f"✨ 发布产品 ({kw[:30]}...) 分类 ID={cat_id}")
                wp_id = await asyncio.to_thread(
                    publish_product_to_wordpress, item, cat_id,
                    thumbnail_id, gallery_ids, main_image_url, prod_name
                )
                if wp_id:
                    success_count += 1
                    last_wp_id = wp_id  # 记录最后一个成功发布的产品 ID

            if success_count > 0 and success_count == len(ai_data_list) and last_wp_id:
                try:
                    # 格式：!{wp_product_id}@_原始产品目录名
                    marked_name = f"!{last_wp_id}@_{prod_name}"
                    new_prod_path = os.path.join(os.path.dirname(prod_path), marked_name)
                    os.rename(prod_path, new_prod_path)
                    logger.info(f"    ✅ 产品目录已标记完成: {marked_name}")
                except Exception as e:
                    logger.error(f"重命名产品完成标记失败: {e}")
        except Exception as e:
            logger.error(f"产品发布流水线异常: {e}")

    # 递归找出所有叶子产品目录（无子目录的目录），并取其父目录对应的分类 ID
    for dirpath, dirnames, filenames in os.walk(base_dir):
        # 已完成的目录跳过（格式：!{wp_id}@_...）
        if os.path.basename(dirpath).startswith("!") and "@_" in os.path.basename(dirpath):
            dirnames.clear()
            continue

        # 判断是否是产品目录（无任何子目录）
        # 这里不能过滤掉已经发布的 !ID@_ 目录，否则如果某个分类下的所有产品都发布完成，
        # child_dirs 为空会导致系统误把这个“二级分类目录”当成“文章(产品)目录”去发布！
        if dirnames:
            continue  # 还有子目录，说明它是分类层，跳过产品发布逻辑

        rel = os.path.relpath(dirpath, base_dir)
        if rel == '.':
            continue

        parts = tuple(rel.split(os.sep))
        prod_name = parts[-1]

        # 产品的分类 = 其父目录（倒数第二层）的 cat_id
        parent_parts = parts[:-1]
        cat_id = None
        if parent_parts:
            p_id = 0
            for i, raw_part in enumerate(parent_parts):
                grand = 0 if i == 0 else p_id
                _, real = _extract_id_and_name(raw_part)
                p_id = cat_cache.get((grand, real.lower()), 0)
            cat_id = p_id if p_id else None

        if cat_id is None:
            logger.warning(f"⚠️ 产品 [{prod_name}] 未找到对应分类 ID，将不绑定分类发布。")

        await _process_product_dir(dirpath, prod_name, cat_id)

    logger.info("\n🎉 本次全自动化目录扫描及发帖任务已成功跑完！")

if __name__ == "__main__":
    asyncio.run(main())
