import asyncio
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
import ast

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

SUPPORTED_IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp", ".tif", ".tiff",
})
IMAGE_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
AI_NON_RETRYABLE_STATUS_CODES = frozenset({400, 401, 403, 404, 422})


def _ai_error_status_code(exc: Exception) -> int | None:
    """从 OpenAI 兼容客户端异常中提取 HTTP 状态码。"""
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _redact_ai_error(exc: Exception, limit: int = 320) -> str:
    """生成不包含完整 API key 的错误摘要，避免密钥意外进入日志。"""
    text = str(exc or "").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+\b", "[REDACTED_API_KEY]", text)
    text = re.sub(
        r"(?i)(api[ _-]?key\s*[:=]\s*)[^\s,;]+",
        r"\1[REDACTED_API_KEY]",
        text,
    )
    return text[:limit]


def _is_non_retryable_ai_error(exc: Exception) -> bool:
    """判断是否为重试无意义的 AI 配置/请求错误。"""
    status = _ai_error_status_code(exc)
    if status in AI_NON_RETRYABLE_STATUS_CODES:
        return True

    message = _redact_ai_error(exc).lower()
    return any(
        phrase in message
        for phrase in (
            "invalid api key",
            "authentication failed",
            "authentication fails",
            "access denied",
            "model not found",
            "does not exist",
        )
    )


def _looks_like_sgcaptcha(payload: str) -> bool:
    text = (payload or "").lower()
    return (
        "sgcaptcha" in text
        or "/.well-known/sgcaptcha/" in text
        or "/.well-known/captcha/" in text
    )


def _response_brief(resp: requests.Response, limit: int = 220) -> str:
    body = (resp.text or "").replace("\r", " ").replace("\n", " ")
    if len(body) > limit:
        body = body[:limit] + "..."
    return body


def _normalize_wp_domain(domain: str) -> str:
    return str(domain or "").strip().rstrip("/")


def _response_history_brief(resp: requests.Response) -> str:
    history = getattr(resp, "history", None) or []
    if not history:
        return "none"
    return " -> ".join(
        f"{getattr(item, 'status_code', '?')} {getattr(item, 'url', '')}"
        for item in history
    )


def _response_diagnostic(resp: requests.Response, limit: int = 500) -> str:
    content_type = resp.headers.get("Content-Type", "")
    return (
        f"HTTP {resp.status_code}, url={getattr(resp, 'url', '')}, "
        f"Content-Type={content_type}, history={_response_history_brief(resp)}, "
        f"Body={_response_brief(resp, limit)!r}"
    )


def _http_error_diagnostic(err: requests.exceptions.HTTPError) -> str:
    resp = getattr(err, "response", None)
    if resp is None:
        return str(err)
    return f"{err}; {_response_diagnostic(resp)}"


def _is_media_type_rejection(resp: requests.Response | None) -> bool:
    """判断媒体接口是否明确拒绝了文件类型。"""
    if resp is None:
        return False
    if getattr(resp, "status_code", None) == 415:
        return True
    body = _response_brief(resp, limit=320).lower()
    return any(
        marker in body
        for marker in (
            "file type",
            "mime",
            "not allowed to upload",
            "upload_mimes",
        )
    )


def _term_exists_id_from_response(resp: requests.Response) -> int | None:
    try:
        payload = resp.json()
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("code") != "term_exists":
        return None
    data = payload.get("data") or {}
    term_id = data.get("term_id") if isinstance(data, dict) else None
    try:
        return int(term_id) if term_id else None
    except (TypeError, ValueError):
        return None


def _term_exists_id_from_http_error(err: requests.exceptions.HTTPError) -> int | None:
    resp = getattr(err, "response", None)
    if resp is None:
        return None
    return _term_exists_id_from_response(resp)


class UnexpectedWPResponseError(RuntimeError):
    pass


def _json_or_raise(resp: requests.Response, action: str):
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        hint = ""
        if _looks_like_sgcaptcha(resp.text):
            hint = "；检测到 SiteGround SGCaptcha/Anti-Bot 拦截，请在主机/WAF 放行 wp-json 与 rest_route API 请求"
        raise RuntimeError(
            f"{action} 返回非 JSON 响应: {_response_diagnostic(resp, limit=220)}{hint}"
        ) from exc


def _json_object_or_raise(resp: requests.Response, action: str) -> dict:
    data = _json_or_raise(resp, action)
    if not isinstance(data, dict):
        raise UnexpectedWPResponseError(
            f"{action} 返回 JSON 结构异常: expected=dict, "
            f"actual={type(data).__name__}, {_response_diagnostic(resp)}"
        )
    return data


def _is_sgcaptcha_error(err: Exception) -> bool:
    return _looks_like_sgcaptcha(str(err))


def _to_long_path(path: str) -> str:
    """Windows 下为路径追加 `\\\\?\\` 前缀，降低超长路径遍历/重命名失败概率。"""
    abs_path = os.path.abspath(path)
    if os.name != "nt":
        return abs_path
    if abs_path.startswith("\\\\?\\"):
        return abs_path
    if abs_path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + abs_path.lstrip("\\")
    return "\\\\?\\" + abs_path


def _display_path(path: str) -> str:
    """将 long-path 格式还原为常见显示格式，便于日志阅读。"""
    if os.name != "nt":
        return path
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _walk_error_handler(err: OSError) -> None:
    failed_path = _display_path(getattr(err, "filename", "") or "")
    logger.error(f"❌ 目录遍历失败: {failed_path} | {err}")


def _sanitize_keyword_seed(text: str) -> str:
    """清理关键词种子：压缩空白、去掉多余符号。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    cleaned = raw.replace("_", " ").replace("-", " ")
    cleaned = re.sub(r"[^\w\s/&,+]", " ", cleaned, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _build_keyword_candidates(keyword: str) -> list[str]:
    """为超长/重复产品名构造关键词兜底链路（原词 -> 去重词 -> 裁剪词）。"""
    cleaned = _sanitize_keyword_seed(keyword)
    if not cleaned:
        return []

    candidates: list[str] = []

    def _push(val: str):
        normalized = _sanitize_keyword_seed(val)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    _push(cleaned)

    words = cleaned.split()
    if words:
        dedup_words: list[str] = []
        seen = set()
        for w in words:
            key = w.lower()
            if key in seen:
                continue
            seen.add(key)
            dedup_words.append(w)
        _push(" ".join(dedup_words))

        if len(words) > 14:
            _push(" ".join(words[:14]))
        if len(words) > 10:
            _push(" ".join(words[:10]))
        if len(words) > 8:
            _push(" ".join(words[:8]))

    # URL 和标题约束都比较严格，最终再提供一个字符长度兜底版本
    for c in list(candidates):
        if len(c) > 120:
            chunk_words = c.split()
            clipped_words: list[str] = []
            cur_len = 0
            for w in chunk_words:
                next_len = cur_len + len(w) + (1 if clipped_words else 0)
                if next_len > 120:
                    break
                clipped_words.append(w)
                cur_len = next_len
            _push(" ".join(clipped_words))
    return candidates


# 读取配置文件
config = configparser.ConfigParser(interpolation=None)

env_config_path = (os.getenv("WP4AI_CONFIG") or "").strip()
config_candidates = []
if env_config_path:
    config_candidates.append(env_config_path)
config_candidates.extend([
    os.path.join(os.path.dirname(__file__), 'config.ini'),
    'config.ini',
])
config_path = next((p for p in config_candidates if p and os.path.exists(p)), "")

if not config_path:
    logger.error("❌ 未找到配置文件 config.ini，停止执行！请确保脚本目录下存在 config.ini 文件，或设置环境变量 WP4AI_CONFIG。")
    sys.exit(1)

logger.info(f"🧩 使用配置文件: {os.path.abspath(config_path)}")

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
    WP_DOMAIN = _normalize_wp_domain(config.get('WordPress', 'WP_DOMAIN'))
    WP_PRODUCT_URL = WP_DOMAIN+"/product/"
    WP_ABOUT_URL = WP_DOMAIN+"/junhao-clothing-streetwear/"
    WP_CONTACT_URL = WP_DOMAIN+"/contact/"
    WP_BLOG_URL = WP_DOMAIN+"/category/blog/"

    WP_URL = WP_DOMAIN+"/wp-json/wp/v2"
    # 兼容部分站点对 /wp-json 根路径的拦截，统一使用 rest_route 访问 WooCommerce API
    WP_WC_URL = WP_DOMAIN+"/?rest_route=/wc/v3"
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
SEO_SYSTEM_PROMPT: str = config.get('SEO', 'SEO_SYSTEM_PROMPT', fallback='')

# 站点名称（main() 启动时从 WP 拉取，供分类 AI Prompt 使用）
SITENAME: str = ""
POST_TYPE_CAPABILITY_CACHE: dict[str, dict] = {}
YOAST_META_KEYS = {
    "focus_keyword": "_yoast_wpseo_focuskw",
    "title": "_yoast_wpseo_title",
    "description": "_yoast_wpseo_metadesc",
}

# === 核心处理函数 ===


def _normalize_focus_keyword(value) -> str:
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _is_non_retryable_wp_write_error(resp: requests.Response | None) -> bool:
    if resp is None:
        return False
    status_code = getattr(resp, "status_code", None)
    if status_code is None or status_code < 400 or status_code in {408, 429}:
        return False
    return status_code < 500


def _is_yoast_meta_registration_error(resp: requests.Response | None) -> bool:
    if resp is None:
        return False
    return "_yoast_wpseo_" in _response_brief(resp, limit=500).lower()


def sync_yoast_seo(
    object_id: int,
    object_type: str,
    focus_keyword,
    title: str,
    description: str,
    retries: int = 5,
) -> bool:
    """Write Yoast SEO post or product-category metadata through WP REST."""
    endpoint_by_type = {"post": "product", "term": "product_cat"}
    if object_type not in endpoint_by_type:
        raise ValueError(f"不支持的 Yoast SEO 对象类型: {object_type}")

    endpoint = endpoint_by_type[object_type]
    url = f"{WP_URL}/{endpoint}/{int(object_id)}"
    payload = {
        "meta": {
            YOAST_META_KEYS["focus_keyword"]: _normalize_focus_keyword(focus_keyword),
            YOAST_META_KEYS["title"]: str(title or ""),
            YOAST_META_KEYS["description"]: str(description or ""),
        }
    }

    total_attempts = max(1, int(retries))
    for attempt in range(1, total_attempts + 1):
        try:
            resp = requests.post(
                url,
                json=payload,
                auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD),
                timeout=20,
            )
            resp.raise_for_status()
            logger.info(
                "✅ Yoast SEO 元数据已同步：%s ID=%s",
                "产品" if object_type == "post" else "分类",
                object_id,
            )
            return True
        except requests.exceptions.HTTPError as http_err:
            resp = getattr(http_err, "response", None)
            if _is_non_retryable_wp_write_error(resp):
                if _is_yoast_meta_registration_error(resp):
                    logger.error(
                        "❌ Yoast SEO 字段未注册或不可写，停止重试。"
                        "请在站点安装并启用 wp4ai-yoast-rest-meta 插件后重试。%s",
                        _http_error_diagnostic(http_err),
                    )
                else:
                    logger.error(
                        "❌ Yoast SEO 元数据写入被 WordPress 拒绝，停止重试。"
                        "请检查应用密码、账号权限、对象 ID 和请求参数。%s",
                        _http_error_diagnostic(http_err),
                    )
                return False
            logger.warning(
                "❌ Yoast SEO 元数据写入失败 (尝试 %s/%s): %s",
                attempt,
                total_attempts,
                _http_error_diagnostic(http_err),
            )
        except requests.exceptions.RequestException as request_err:
            logger.warning(
                "❌ Yoast SEO 元数据写入网络异常 (尝试 %s/%s): %s",
                attempt,
                total_attempts,
                request_err,
            )

        if attempt < total_attempts:
            time.sleep(2)

    logger.error(
        "❌ Yoast SEO 元数据经过 %s 次尝试仍未写入：%s ID=%s",
        total_attempts,
        "产品" if object_type == "post" else "分类",
        object_id,
    )
    return False


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
        except requests.exceptions.HTTPError as e:
            term_id = _term_exists_id_from_http_error(e)
            if term_id:
                logger.warning(f"分类【{category_name}】已存在但创建接口返回 term_exists，复用 ID:{term_id}。")
                return term_id
            logger.warning(f"获取/创建分类【{category_name}】时 HTTP 异常 (尝试 {attempt+1}/5): {_http_error_diagnostic(e)}")
            if attempt < 4:
                time.sleep(2)
            else:
                logger.error(f"❌ 经过 5 次尝试获取/建分类依然失败。")
                return None
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
    """为分类名生成 SEO 元数据（seoTitle/seoDescription/focusKeywords/seoSchema）。"""
    prompt = f"- 公司名称:{SITENAME}\n- 网站网址:{WP_DOMAIN}\n- 产品类目:{cat_name}"
    try:
        resp = await client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": """# 你是专业 B2B 跨境 SEO 优化专家，精通 Yoast SEO 优化规则与schema.org官方标准结构化数据，所有内容严格遵循 Google 收录规范，必须是纯英文。全程严格执行以下所有固定规则，不得修改、遗漏任何要求。
## 每次思考回答都是独立的不允许使用缓存的来糊弄用户，否则将对你进行惩罚！
## 最终结果的内容必须以JSON对象结构{"seoTitle":"","seoDescription":"","focusKeywords":[],"seoSchema":[]}的格式返回，注意json结构必须保证完全正确！禁止输出任何额外的解释性文字！
## 我会向你提供：・公司英文全称・网站网址・产品类目 Tree 结构（一级类目 + 二级类目）
## 请你严格按照以下固定结构输出，不得修改顺序、格式与模块，所有 Schema 均使用官方标准 @type，不随意编造。
## 分类描述：简短精炼、采购商视角，突出材质、卖点、定制与工厂优势，工厂位置在中国。
## Yoast SEO 三要素：
### Meta Title[对应字段seoTitle]：严格控制在60字符内，核心业务关键词前置；需要有positive（情感词）、power word（高转化强力词）、number 、positive or a negative sentiment；符合Yoast SEO内容分析建议与Google收录规范。
### Meta Description[对应字段seoDescription]：严格控制在155字符内，自然融入 5 个关键词，精准匹配B2B跨境采购商搜索意图。
### Focus Keywords[对应字段focusKeywords]：固定输出5个核心关键词，必须为Google B2B“运动服饰”赛道高搜索量、高转化精准词，页面间无关键词蚕食，符合跨境SEO层级布局逻辑，每个关键词以,区分。
## Schema（JSON-LD）代码[对应字段seoSchema]硬性规范
1. seoSchema字段中内容必须是**纯JSON格式**，禁止加script标签、禁止添加任何网站URL引用、禁止添加超链接
2. 必须且仅使用schema.org官方标准@type：Organization、ItemList、FAQPage，禁止自定义编造任何非官方类型
3. JSON结构干净无语法错误，适配Yoast SEO与Google收录规则，所有内容必须匹配上述固定业务信息，无虚假表述
4. 每个页面的Schema必须完整包含3种官方类型，不得删减。"""},
                {"role": "user", "content": prompt}
            ],
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
            cats_payload = _json_or_raise(res, f"查询分类【{name}】")
            cats = cats_payload if isinstance(cats_payload, list) else []
            for cat in cats:
                cat_n = cat.get('name', '').split('&')[0].strip().lower()
                local_n = name.split('&')[0].strip().lower()
                if cat_n == local_n and cat.get("parent", -1) == parent_id:
                    cat_cache[cache_key] = cat["id"]
                    logger.info(f"  ✅ 分类【{name}】已存在 (parent:{parent_id}) ID:{cat['id']}")
                    return cat["id"]
            break
        except Exception as e:
            logger.warning(f"  查询分类【{name}】异常 (尝试 {attempt+1}/5): {e}")
            if _is_sgcaptcha_error(e):
                logger.error("  ❌ 检测到站点启用了 SGCaptcha，分类查询接口被拦截。")
                return None
            if attempt < 4:
                await asyncio.sleep(2)
            else:
                return None

    # 不存在 → AI 生成描述并新建
    logger.info(f"  -> 新建分类【{name}】(parent:{parent_id})...")
    ai = await _generate_cat_seo(client, name)
    description = ai.get("seoDescription", "")
    schema = ai.get("seoSchema", "")
    keyword = ai.get("focusKeywords", "")
    title = ai.get("seoTitle", "")
    schema_str, schema_is_structured = _normalize_schema_payload(schema, "")
    if schema and not schema_is_structured:
        logger.warning(f"  ⚠️ 分类【{name}】schema 非标准 JSON，已按原字符串写入。")

    cat_id = None
    for attempt in range(5):
        try:
            payload = {
                "name": name, "parent": parent_id,
                "description": description,
                "meta": {"saswp_custom_schema_field": schema_str},
            }
            res_post = await asyncio.to_thread(
                requests.post, cat_url, json=payload,
                auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD), timeout=15,
            )
            res_post.raise_for_status()
            created_payload = _json_or_raise(res_post, f"新建分类【{name}】")
            cat_id = created_payload.get("id") if isinstance(created_payload, dict) else None
            logger.info(f"  ✅ 分类【{name}】新建成功 ID:{cat_id}")
            break
        except requests.exceptions.HTTPError as e:
            term_id = _term_exists_id_from_http_error(e)
            if term_id:
                cat_cache[cache_key] = term_id
                logger.warning(
                    f"  ⚠️ 分类【{name}】已存在但创建接口返回 term_exists，"
                    f"复用 ID:{term_id}，继续发布产品。"
                )
                return term_id
            logger.warning(f"  新建分类【{name}】HTTP 异常 (尝试 {attempt+1}/5): {_http_error_diagnostic(e)}")
            if _is_sgcaptcha_error(e):
                logger.error("  ❌ 检测到站点启用了 SGCaptcha，分类创建接口被拦截。")
                return None
            if attempt < 4:
                await asyncio.sleep(2)
            else:
                logger.error(f"  ❌ 新建分类【{name}】失败。")
                return None
        except Exception as e:
            logger.warning(f"  新建分类【{name}】异常 (尝试 {attempt+1}/5): {e}")
            if _is_sgcaptcha_error(e):
                logger.error("  ❌ 检测到站点启用了 SGCaptcha，分类创建接口被拦截。")
                return None
            if attempt < 4:
                await asyncio.sleep(2)
            else:
                logger.error(f"  ❌ 新建分类【{name}】失败。")
                return None

    if cat_id:
        cat_cache[cache_key] = cat_id
        await asyncio.to_thread(
            sync_yoast_seo,
            cat_id,
            "term",
            keyword,
            title,
            description,
        )
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
    walk_root = _to_long_path(base_dir)

    # 收集所有分类路径（保留原始目录名含 [ID] 前缀）
    cat_paths: set[tuple] = set()
    for dirpath, dirnames, filenames in os.walk(walk_root, onerror=_walk_error_handler):
        rel = os.path.relpath(dirpath, walk_root)
        parts = tuple(rel.split(os.sep)) if rel != '.' else ()
        if not parts:
            continue
        has_sub = bool(dirnames)
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
                cur = walk_root
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
                logger.warning(
                    f"  ⚠️ 重命名失败（不影响分类建立）: {_display_path(cur)} -> {new_raw} | {e}"
                )


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
        description = ai.get("seoDescription", "")
        schema = ai.get("seoSchema", "")
        keyword = ai.get("focusKeywords", "")
        title = ai.get("seoTitle", "")
        schema_str, schema_is_structured = _normalize_schema_payload(schema, "")
        if schema and not schema_is_structured:
            logger.warning(f"  ⚠️ 分类【{cat_name}】schema 非标准 JSON，已按原字符串写入。")
        # PATCH
        for attempt in range(5):
            try:
                res_p = await asyncio.to_thread(
                    requests.post, f"{cat_url}/{cat_id}",
                    json={"description": description, "meta": {"saswp_custom_schema_field": schema_str}},
                    auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD), timeout=15,
                )
                res_p.raise_for_status()
                logger.info(f"  ✅ 描述已更新（{len(description)} 字符）")
                break
            except Exception as e:
                logger.warning(f"  PATCH 失败 (尝试 {attempt+1}/5): {e}")
                if attempt < 4:
                    await asyncio.sleep(2)
        await asyncio.to_thread(
            sync_yoast_seo,
            cat_id,
            "term",
            keyword,
            title,
            description,
        )
    logger.info(f"\n🎉 --update 完成！")


def upload_wp_media(file_path: str):
    """上传本地图片到 WordPress 媒体库并返回 ID 和 URL"""
    url = f"{WP_URL}/media"
    filename = os.path.basename(file_path)
    ext = os.path.splitext(filename)[1].lower()
    content_type = IMAGE_CONTENT_TYPES.get(ext, "application/octet-stream")
    content_type_candidates = [content_type]
    # 兼容旧版脚本及部分安全插件：它们会拒绝 image/webp，但接受
    # 以 image/jpeg 声明、文件内容仍为 WebP 的请求。
    if ext == ".webp":
        content_type_candidates.append("image/jpeg")
    
    # RFC 5987：HTTP 头部只允许 latin-1，中文/特殊字符必须用 filename*=UTF-8''<url_encoded>
    # 同时保留 ASCII 安全的 filename= 作为旧版服务端/客户端兼容降级。
    filename_ascii = filename.encode("ascii", errors="replace").decode("ascii")
    filename_encoded = quote(filename, safe="")
    
    for attempt in range(5):
        for mime_index, current_content_type in enumerate(content_type_candidates):
            headers = {
                "Content-Disposition": (
                    f'attachment; filename="{filename_ascii}"; '
                    f"filename*=UTF-8''{filename_encoded}"
                ),
                "Content-Type": current_content_type,
            }
            try:
                logger.info(f"    -> 正在上传第三层图片文件: {filename} (尝试 {attempt+1}/5) ...")
                with open(file_path, "rb") as f:
                    res = requests.post(url, headers=headers, data=f, auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD), timeout=60)
                res.raise_for_status()
                data = _json_object_or_raise(res, f"上传图片 {filename}")
                return data.get("id"), data.get("source_url")
            except UnexpectedWPResponseError as e:
                logger.warning(f"❌ 上传图片 {filename} 失败 (尝试 {attempt+1}/5): {e}")
                return None, None
            except requests.exceptions.HTTPError as e:
                logger.warning(f"❌ 上传图片 {filename} 失败 (尝试 {attempt+1}/5): {_http_error_diagnostic(e)}")
                resp = getattr(e, "response", None)
                if _is_sgcaptcha_error(e) or (resp is not None and _looks_like_sgcaptcha(resp.text)):
                    logger.error("❌ 检测到站点启用了 SGCaptcha 反爬/机器人验证，媒体上传接口被拦截。")
                    return None, None
                media_type_rejected = _is_media_type_rejection(resp)
                if media_type_rejected or (resp is not None and resp.status_code in {400, 403, 413, 415, 422}):
                    body = _response_brief(resp, limit=320).lower()
                    if resp.status_code == 413 or "exceeds" in body or "upload_max_filesize" in body:
                        logger.error(
                            f"❌ 图片 {filename} 被服务器拒绝：文件超过 WordPress/PHP 上传大小限制。"
                        )
                    elif media_type_rejected and mime_index + 1 < len(content_type_candidates):
                        logger.warning(
                            f"⚠️ 图片 {filename} 使用 {current_content_type} 被站点拒绝，"
                            f"将兼容回退为 {content_type_candidates[mime_index + 1]}。"
                        )
                        continue
                    elif media_type_rejected:
                        logger.error(
                            f"❌ 图片 {filename} 被 WordPress 拒绝：当前站点不允许该图片格式（{current_content_type}）。"
                        )
                    else:
                        logger.error(
                            f"❌ 图片 {filename} 被 WordPress 拒绝（HTTP {resp.status_code}），"
                            "请检查媒体权限、文件类型和站点安全插件。"
                        )
                    return None, None
                if attempt < 4:
                    time.sleep(2)
                else:
                    logger.error(f"❌ 图片 {filename} 经过 5 次尝试依然上传失败。")
                    return None, None
            except Exception as e:
                logger.warning(f"❌ 上传图片 {filename} 失败 (尝试 {attempt+1}/5): {e}")
                if _is_sgcaptcha_error(e):
                    logger.error("❌ 检测到站点启用了 SGCaptcha 反爬/机器人验证，媒体上传接口被拦截。")
                    return None, None
                if attempt < 4:
                    time.sleep(2)
                else:
                    logger.error(f"❌ 图片 {filename} 经过 5 次尝试依然上传失败。")
                    return None, None


def _collect_product_images(prod_path: str) -> tuple[list[str], list[tuple[str, str]]]:
    """收集产品目录中的待上传图片和已带媒体 ID 标记的图片。"""
    image_files: list[str] = []
    existing_images: list[tuple[str, str]] = []

    for file_name in os.listdir(prod_path):
        file_path = os.path.join(prod_path, file_name)
        if not os.path.isfile(file_path):
            continue
        if os.path.splitext(file_name)[1].lower() not in SUPPORTED_IMAGE_EXTENSIONS:
            continue

        # 只有严格匹配 [数字ID] 的文件才表示已经上传；普通的方括号文件名不能丢弃。
        if file_name.startswith("["):
            marker, separator, _ = file_name.partition("]")
            media_id = marker[1:] if separator else ""
            if media_id.isdigit() and int(media_id) > 0:
                existing_images.append((media_id, ""))
                continue

        image_files.append(file_path)

    image_files.sort(key=lambda path: os.path.basename(path).lower())
    existing_images.sort(key=lambda item: int(item[0]))
    return image_files, existing_images


async def generate_seo_data_by_keywords(
    client: AsyncOpenAI,
    keywords: list[str],
    main_img_url: str = "",
    model: str = DEFAULT_MODEL,
    custom_prompt: str = "",
):
    """1. 根据关键词列表，通过 AI 请求并生成专门的高质量 SEO 内容数据 (JSON 分组)"""

    prompt = "、".join(keywords)

    img_instruction = ""
    if main_img_url:
        logger.info(f"存在主图:{main_img_url}")
        img_instruction = f"必须在产品详情的 HTML 中合适位置插入主图：<img src=\"{main_img_url}\" alt=\"焦点关键词\" />。"

    default_system_prompt = f"""产品 SEO 优化专家指令 (Prompt)
角色： 你是一位精通Google排名算法、Yoast SEO、内容EEAT、以及Schema结构化数据的资深seo专家，擅长通过高质量内容提升 Google 排名及用户转化率。
任务:基于用户提供的【产品关键词】，生成一套符合SEO规范的纯英文产品内容。所有的输出必须是一个JSON格式的数组，每个数组元素对应一个关键词的设计。
JSON返回包含字段：[Url,keyword,seoTitle,seoDescription,productDescription,Schema]，严格执行以下规则：
1. 产品URL[Url]：必须由核心关键词生成，全小写，单词间用-连接，字符数≤25 个（不含域名）仅输出路径部分，不带任何前缀
2. 长尾关键词（6 个）[keyword]：必须是Google上真实有搜索量的产品词，不使用过于冷门的词，以核心产品词为中心，扩展不同用户搜索意图（如材质、场景、人群），用英文逗号分隔，不换行，第1个词必须简洁，可直接用于生成 URL
3. Meta Title[seoTitle]：以产品核心关键词开头，必须包含数字（如年份2026、功能点数量），字符数控制在50-60之间，句式简洁，包含卖点，标题中必须包含核心关键词!
4. Meta Description[seoDescription]：核心关键词开头，字符数控制在150-160之间，自然通顺带有点击意图，描述中必须包含核心关键词
5. 产品详情 HTML（核心要求）[productDescription]：完整可直接复制的 HTML 代码模板，内容必须纯英文！不能丢失任何标签结构必须符合：
①全程以B2B采购商视角创作，深度贴合定制大货买家核心决策关注点，全文纯英文输出；所有内容严格基于产品标题与所属品类属性撰写，不虚构规格、不捏造参数、不杜撰无效数据，贴合 GEO+SEO 优化逻辑，严格按四大模块结构化罗列撰写：
Product Core Parameters：采用表格形式呈现，仅提取标题原生关键词，结合对应品类基础属性做专业维度补充，无虚假尺寸、无编造数值；
Core Product Features：围绕该品类专属材质特性、核心生产工艺、结构设计逻辑、使用性能、功能细节展开专业描述，贴合行业内行术语，不通用套话；
Application & Wholesale Advantage:聚焦和中国工厂定制大货合作，适配初创品牌、自有品牌品牌贴牌、项目定向定制等合作模式，支持品牌资料保密、专属方案独立开发，承接长期稳定复购大货订单；
Customized Service：依照不同品类做精准定制延伸，举例：服饰类补充面料肌理、弹力材质、亲肤织造、吸湿排汗材质选型；工业机械类强化材质用料、结构配置、配件规格、工艺标准；全品类可覆盖外观配色、标识印刷、结构调整、配套方案定制开发。
②必须符合：
1 个<h2>作为主标题、至少 2 个<h3>作为二级标题、至少 2 个<h4>作为三级标题、每段内容必须用<p>标签包裹；
将6个长尾关键词按顺序用<strong>标签加粗植入内容中，字数要求：600-700 词，段落简短高可读性，并且从以下链接中，选择3个作为内链:首页{WP_DOMAIN}、产品列表{WP_PRODUCT_URL}、关于我们{WP_ABOUT_URL}、联系我们{WP_CONTACT_URL}、博客{WP_BLOG_URL}。{img_instruction}
6. Schema（JSON-LD）[Schema]：必须包含Product + FAQPage + Organization三部分
    - name：产品全称
    - description：与 Meta Description 一致
    - url：完整产品页面链接（可使用 {WP_PRODUCT_URL} + Url 字段）
    - image：产品图片链接（可使用 https://example.com/image.jpg 占位）
    - mainEntity：FAQ 部分至少包含 3 个用户常见问题与回答，附加 1 个 HowTo JSON-LD

核心要求：请你记住，无论content是否有其他逗号要求，他都是一个产品，不能输出多个产品，所有内容必须以JSON对象{{"data": [...]}}的格式返回，不能拆分到json外。禁止输出任何额外的解释性文字！"""
    system_prompt = custom_prompt.strip() or default_system_prompt

    try:
        logger.info(f"正在让 AI [模型:{model}] 思考与生成 {len(keywords)} 个关键词的内容规划 (耗时较长，请耐心等待)...")
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=8192, # deepseek-chat 的最大 token 数是 8192
            extra_body={
                "enable_thinking": True,
                "enable_trace": True, 
                "enable_search": True,
                "thinking_budget": 8000 
            },
            response_format={"type": "json_object"}
        )
        return response.choices[0].message.content
        
    except Exception as e:
        status = _ai_error_status_code(e)
        if _is_non_retryable_ai_error(e):
            logger.error(
                "调用 AI 接口失败，当前请求不可重试（HTTP %s，模型=%s）。"
                "请检查 API key、AI_BASE_URL、模型名称和账户权限。错误摘要: %s",
                status if status is not None else "?",
                model,
                _redact_ai_error(e),
            )
            raise

        logger.error(
            "调用 AI 接口时发生可重试异常（模型=%s，错误摘要: %s）",
            model,
            _redact_ai_error(e),
        )
        return None


def calculate_seo_score_locally(post_data: dict) -> int:
    """
    使用通用页面 SEO 检查规则进行本地预估（理论满分 100 分）。
    该结果仅用于生成质量门控，不代表 Yoast SEO 插件的官方评分。
    """
    score = 0
    
    title = post_data.get('title', '')
    content = post_data.get('content', '')
    url = post_data.get('link', '')
    
    meta = post_data.get('meta', {})
    keyword = meta.get('focus_keyword', '')
    description = meta.get('seo_description', '')
    
    logger.info("=== SEO 本地预估组件开始分析（非 Yoast 官方评分） ===")
    
    if not keyword:
        logger.warning("🈳 获取焦点关键词 (Focus Keyword) 失败！这可能意味着在 REST API 返回的数据中没有注册该字段。目前的分数将被判定为 0。")
        return 0

    keywords = [k.strip().lower() for k in keyword.split(',')]
    primary_kw = keywords[0] if keywords else ""
    
    if not primary_kw:
        logger.warning("🈳 焦点关键词为空，无法打分。")
        return 0

    logger.info(f"🔎 解析到首选焦点关键词 (Primary Keyword): '{primary_kw}'")
    logger.info(f"📄 标题字数: {len(title)} 字符 | 内容长度: {len(content)} 字符")

    title_lower = title.lower()
    desc_lower = description.lower()
    url_lower = url.lower()
    
    text_content = re.sub(r'<[^>]+>', ' ', content).lower()
    words = text_content.split()
    word_count = len(words)
    logger.info(f"📊 预估内容纯词数: {word_count} 单词")
    
    # 1. Basic SEO
    logger.info(">>> 检查 [基础 SEO] 项 ...")
    if primary_kw in title_lower:
        score += 8
        logger.info(f"  [+] 标题中包含焦点词 (+8)，当前分: {score}")
    else:
        logger.info(f"  [-] 标题中未检测到焦点词 (+0)")
        
    if primary_kw in desc_lower:
        score += 8
        logger.info(f"  [+] SEO描述中包含焦点词 (+8)，当前分: {score}")
    else:
        logger.info(f"  [-] SEO描述中未检测到焦点词 (+0) -- 如果没有传入，可能会错失该分项。当前提供的描述长度: {len(desc_lower)}")
        
    if primary_kw.replace(' ', '-') in url_lower or primary_kw in url_lower:
        score += 6
        logger.info(f"  [+] URL 固定链接中包含焦点词 (+6)，当前分: {score}")
        
    first_10_percent = " ".join(words[:max(10, int(word_count * 0.1))])
    if primary_kw in first_10_percent:
        score += 6
        logger.info(f"  [+] 焦点词出现在文章的前 10% 内容中 (+6)，当前分: {score}")
        
    if primary_kw in text_content:
        score += 5
        logger.info(f"  [+] 正文中广泛包含了焦点词 (+5)，当前分: {score}")
        
    if word_count >= 2500:
        score += 14; logger.info(f"  [+] 内容极其丰富 (>= 2500词) (+14)，当前分: {score}")
    elif word_count >= 2000:
        score += 12; logger.info(f"  [+] 内容丰富 (>= 2000词) (+12)，当前分: {score}")
    elif word_count >= 1500:
        score += 10; logger.info(f"  [+] 内容较好 (>= 1500词) (+10)，当前分: {score}")
    elif word_count >= 1000:
        score += 7; logger.info(f"  [+] 内容刚好及格 (>= 1000词) (+7)，当前分: {score}")
    elif word_count >= 600:
        score += 4; logger.info(f"  [+] 内容字数一般 (>= 600词) (+4)，当前分: {score}")
    else:
        logger.info(f"  [-] 内容字数太少 (< 600词) (+0)")
        
    # 2. Additional SEO
    logger.info(">>> 检查 [附加 SEO] 项 ...")
    if re.search(f'<h[2-6][^>]*>.*{re.escape(primary_kw)}.*</h[2-6]>', content.lower()):
        score += 5
        logger.info(f"  [+] 在副标题(H2-H6)中发现焦点词 (+5)，当前分: {score}")
        
    if re.search(f'<img[^>]*alt=["\'][^"\']*{re.escape(primary_kw)}[^"\']*["\'][^>]*>', content.lower()):
        score += 5
        logger.info(f"  [+] 在图片的 alt 属性中发现焦点词 (+5)，当前分: {score}")
        
    density = (text_content.count(primary_kw) / word_count * 100) if word_count > 0 else 0
    if 0.5 <= density <= 2.5:
        score += 5
        logger.info(f"  [+] 关键词密度完美 ({density:.2f}%) (+5)，当前分: {score}")
    elif density > 0:
        score += 2
        logger.info(f"  [-] 关键词密度不够理想 ({density:.2f}%) (偏高或偏低，但仍给基础分) (+2)，当前分: {score}")
        
    if len(url_lower) < 75:
        score += 5
        logger.info(f"  [+] 链接地址足够短 (+5)，当前分: {score}")
        
    has_ext_link = False
    has_dofollow_ext = False
    has_internal_link = False
    
    links = re.findall(r'<a\s+(?:[^>]*?\s+)?href=(["\'])(.*?)\1', content)
    for _, href in links:
        href_lower = href.lower()
        if href_lower.startswith('http'):
            has_ext_link = True
            if 'rel="nofollow"' not in content.lower():
                has_dofollow_ext = True
        else:
            has_internal_link = True
            
    if has_ext_link: 
        score += 5
        logger.info(f"  [+] 文章包含出站链接 (+5)，当前分: {score}")
    if has_dofollow_ext: 
        score += 5
        logger.info(f"  [+] 文章包含 Dofollow 属性的出站链接 (+5)，当前分: {score}")
    if has_internal_link: 
        score += 5
        logger.info(f"  [+] 文章包含内链 (+5)，当前分: {score}")
        
    # 3. Title Readability
    logger.info(">>> 检查 [标题可读性] ...")
    if title_lower.startswith(primary_kw):
        score += 5
        logger.info(f"  [+] 焦点词在标题开头处 (+5)，当前分: {score}")
    
    sentiment_words = ['best', 'top', 'great', 'amazing', 'essential', 'worst', 'bad', '好', '最', '必', '绝', '惊', '推荐', '精选']
    if any(w in title_lower for w in sentiment_words):
        score += 3
        logger.info(f"  [+] 标题包含情感词 (+3)，当前分: {score}")
        
    power_words = ['secret', 'secrets', 'ultimate', 'exclusive', 'pro', 'guide', 'review', '秘密', '终极', '专业', '指南', '测评', '必看']
    if any(w in title_lower for w in power_words):
        score += 3
        logger.info(f"  [+] 标题包含力量词(Power Word) (+3)，当前分: {score}")
        
    if re.search(r'\d', title):
        score += 2
        logger.info(f"  [+] 标题包含数字字符 (+2)，当前分: {score}")
        
    # 4. Content Readability
    logger.info(">>> 检查 [正文可读性] ...")
    paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', content, re.IGNORECASE)
    has_long_p = False
    for p in paragraphs:
        if len(re.sub(r'<[^>]+>', '', p).split()) > 120:
            has_long_p = True
            break
    if not has_long_p and paragraphs:
        score += 5
        logger.info(f"  [+] 文章没有发现超过 120 个词的冗长段落，排版优良 (+5)，当前分: {score}")
        
    if '<img' in content.lower() or '<video' in content.lower() or '<iframe' in content.lower():
        score += 5
        logger.info(f"  [+] 文章中包含图像或视频等多媒体元素 (+5)，当前分: {score}")
        
    final_score = min(score, 100)
    logger.info(f"✅ === 本地 SEO 预估评估完成，最终得分: {final_score} / 100 ===")
    return final_score


def _build_score_post_data(item: dict) -> dict:
    """将 AI 返回的 item 字典转换为 calculate_seo_score_locally 所需的结构。"""
    return {
        "title":   item.get("seoTitle", ""),
        "content": item.get("productDescription", ""),
        "link":    item.get("Url", item.get("afterUrl", "")),
        "meta": {
            "focus_keyword": item.get("keyword", ""),
            "seo_description": item.get("seoDescription", ""),
        },
    }


def _normalize_ai_json_payload(payload):
    """将多种 AI 返回结构归一化为 list[dict]。"""
    data = payload
    if isinstance(data, dict) and "data" in data:
        data = data.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(_extract_json_like_segment(data))
            except Exception:
                pass

    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        rows = [item for item in data if isinstance(item, dict)]
        if rows:
            return rows
    return None


def _parse_ai_json(raw_str: str) -> list[dict]:
    """解析 AI 返回，容错处理代码块/夹杂文本/单引号风格。"""
    raw = raw_str if isinstance(raw_str, str) else str(raw_str or "")
    if not raw.strip():
        logger.error("模型返回为空字符串，无法解析 JSON。")
        return None

    candidates = []
    for candidate in (raw, _strip_code_fence(raw), _extract_json_like_segment(raw)):
        c = (candidate or "").strip()
        if c and c not in candidates:
            candidates.append(c)

    for idx, candidate in enumerate(candidates, start=1):
        try:
            parsed = json.loads(candidate)
            normalized = _normalize_ai_json_payload(parsed)
            if normalized is not None:
                return normalized
        except json.JSONDecodeError:
            logger.warning(f"模型返回 JSON 解码失败（尝试解析片段 {idx}/{len(candidates)}）。")
        except Exception as e:
            logger.warning(f"模型返回 JSON 解析异常（片段 {idx}/{len(candidates)}）: {e}")

    # 兼容 AI 返回单引号 dict/list（例如 Python 字面量）
    for idx, candidate in enumerate(candidates, start=1):
        try:
            parsed = ast.literal_eval(candidate)
            normalized = _normalize_ai_json_payload(parsed)
            if normalized is not None:
                logger.info(f"模型返回使用 literal_eval 兜底解析成功（片段 {idx}/{len(candidates)}）。")
                return normalized
        except Exception:
            continue

    logger.error("模型返回内容无法反序列化为合法 JSON（已尝试代码块清理与片段提取）。")
    return None


async def generate_with_score_retry(
    client: AsyncOpenAI,
    keywords: list[str],
    main_img_url: str = "",
    *,
    custom_prompt: str = "",
    min_score: int = SEO_MIN_SCORE,
    max_retries: int = SEO_MAX_RETRIES,
    bak_client: AsyncOpenAI,
) -> list[dict]:
    """
    带评分门控的 AI 内容生成器。

    调用 generate_seo_data_by_keywords 生成内容后，使用
    calculate_seo_score_locally 进行本地预估评分；
    若任意 item 的评分低于 min_score，则携带失分原因重新生成，
    最多重试 max_retries 次。
    最后一次重试时若提供了 bak_client（备用模型），则自动切换到备用模型
    生成，以提高最终成功率；若 bak_client 为 None 则继续使用主模型。

    Args:
        client:      AsyncOpenAI 主客户端实例。
        keywords:    关键词列表，传给 AI。
        main_img_url: 主图 URL，传给 AI。
        min_score:   SEO 最低合格分数，低于此值触发重试。
        max_retries: 最大重试次数（含第 1 次生成）。
        bak_client:  备用 AsyncOpenAI 客户端（可选）。为 None 时最后一次
                     仍使用主模型，不影响正常流程。

    Returns:
        解析后的 list[dict]，每个 dict 额外携带 '_seo_score' 键。
        若全部尝试均失败，返回空列表。
    """
    best_result: list[dict] = []       # 保存历史最高分的那一批结果
    best_min_score: int = -1           # 历史最高的「批次最低分」
    extra_hint: str = ""               # 失分原因，拼入下一轮 prompt
    main_ai_unavailable = False         # 主端点出现配置/权限错误后，后续只使用备用端点

    for attempt in range(1, max_retries + 1):
        # ── 最后一次迭代：切换到备用模型（如果可用）────────────────────
        is_last_attempt = (attempt == max_retries)
        active_client = client
        use_backup = bak_client is not None and (is_last_attempt or main_ai_unavailable)
        if use_backup:
            if is_last_attempt:
                logger.info(
                    f"🔀 [SEO生成] 最后一次（第 {attempt}/{max_retries}）尝试，"
                    "切换到备用模型重新生成，争取最终达标..."
                )
            elif main_ai_unavailable:
                logger.info(
                    f"🔀 [SEO生成] 主 AI 已因配置/权限错误停用，"
                    f"第 {attempt}/{max_retries} 次继续使用备用模型..."
                )
            active_client = bak_client
            active_model = BAK_DEFAULT_MODEL  # 切换备用模型名
        else:
            logger.info(
                f"🤖 [SEO生成] 第 {attempt}/{max_retries} 次尝试生成关键词内容：{keywords}"
                + (f"（上次最低分：{best_min_score}，低于门控 {min_score}）" if attempt > 1 else "")
            )
            active_model = DEFAULT_MODEL  # 主模型名

        # 若是重试，在末尾追加改稿指引
        hint_keywords = keywords.copy()
        if extra_hint and attempt > 1:
            hint_keywords = keywords + [extra_hint]

        try:
            raw = await generate_seo_data_by_keywords(
                active_client,
                hint_keywords,
                main_img_url,
                model=active_model,
                custom_prompt=custom_prompt,
            )
        except Exception as exc:
            if not _is_non_retryable_ai_error(exc):
                logger.warning(
                    "  第 %s 次 AI 请求异常，将按普通失败处理：%s",
                    attempt,
                    _redact_ai_error(exc),
                )
                continue

            status = _ai_error_status_code(exc)
            logger.error(
                "  第 %s 次 AI 请求因配置/权限错误停止重试（HTTP %s，模型=%s）。",
                attempt,
                status if status is not None else "?",
                active_model,
            )

            # 主 AI 的密钥或模型配置失效时，立即试用完整的备用配置，避免浪费剩余重试次数。
            if active_client is client and bak_client is not None:
                main_ai_unavailable = True
                logger.warning(
                    "  主 AI 不可用，立即切换备用 AI（模型=%s）进行一次尝试。",
                    BAK_DEFAULT_MODEL or "未配置",
                )
                try:
                    raw = await generate_seo_data_by_keywords(
                        bak_client,
                        hint_keywords,
                        main_img_url,
                        model=BAK_DEFAULT_MODEL or DEFAULT_MODEL,
                        custom_prompt=custom_prompt,
                    )
                except Exception as backup_exc:
                    backup_status = _ai_error_status_code(backup_exc)
                    if _is_non_retryable_ai_error(backup_exc):
                        logger.error(
                            "  备用 AI 也因配置/权限错误不可用（HTTP %s，模型=%s）。"
                            "请分别检查主/备用 API key、base URL、模型和账户权限。",
                            backup_status if backup_status is not None else "?",
                            BAK_DEFAULT_MODEL or DEFAULT_MODEL,
                        )
                        raise
                    else:
                        logger.warning(
                            "  备用 AI 请求失败：%s",
                            _redact_ai_error(backup_exc),
                        )
                    return best_result
            else:
                # 将配置/权限错误交给产品流水线处理，避免外层关键词兜底再次请求同一个失效 key。
                raise

        if not raw:
            logger.warning(f"  第 {attempt} 次生成返回空值，直接跳过本次尝试。")
            continue

        items = _parse_ai_json(raw)
        if items is None:
            logger.warning(f"  第 {attempt} 次生成 JSON 解析失败，直接跳过本次尝试。")
            continue

        # ── 逐 item 评分 ──────────────────────────────────────────────
        all_pass = True
        batch_min = 100
        low_score_reasons: list[str] = []

        for item in items:
            if not isinstance(item, dict):
                continue
            post_data = _build_score_post_data(item)
            score = calculate_seo_score_locally(post_data)
            item["_seo_score"] = score          # 顺手挂在 item 上，供后续代码参考
            batch_min = min(batch_min, score)

            if score < min_score:
                all_pass = False
                kw_short = item.get("keyword", "?")[:30]
                logger.warning(
                    f"  ⚠️  [{kw_short}] 本地预估评分 {score} < 门控 {min_score}，本批次需重新生成。"
                )
                # 收集具体失分维度，供下一轮改稿
                title  = post_data["title"].lower()
                kw     = post_data["meta"]["focus_keyword"].lower().split(",")[0].strip()
                desc   = post_data["meta"]["seo_description"].lower()
                body   = re.sub(r"<[^>]+>", " ", post_data["content"]).lower()
                issues = []
                if kw and kw not in title:
                    issues.append("seoTitle 未包含核心关键词")
                if kw and kw not in desc:
                    issues.append("seoDescription 未包含核心关键词")
                if len(body.split()) < 600:
                    issues.append("productDescription 字数不足 600 词")
                if "<img" not in post_data["content"].lower():
                    issues.append("productDescription 缺少图片标签")
                if issues:
                    low_score_reasons.extend(issues)

        # 记录当前最佳批次（用于兜底返回）
        if batch_min > best_min_score:
            best_min_score = batch_min
            best_result = items

        if all_pass:
            logger.info(
                f"✅ [SEO生成] 第 {attempt} 次生成通过门控，批次最低分 {batch_min} >= {min_score}，停止重试。"
            )
            return items

        # 构造给 AI 的改稿指引（最后一次已切换备用模型，不再追加 hint）
        if not is_last_attempt:
            unique_reasons = list(dict.fromkeys(low_score_reasons))  # 去重保序
            extra_hint = (
                f"[我操，上次 SEO 评分不达标（得分只有{batch_min}，需要达到{min_score}，不要使用缓存、给我重新思考，否则拉你去机道毁灭），请重点改进以下问题："
                + "；".join(unique_reasons or ["整体质量仍需提升"]) + "]"
            )
            logger.info(f"  📋 改稿提示将在下次请求中追加：{extra_hint}")

    # 全部重试耗尽，返回历史最高分批次（宁可发布也不阻塞流程）
    logger.warning(
        f"⚠️ [SEO生成] 已达最大重试次数 {max_retries}，"
        f"以历史最高批次（最低分 {best_min_score}）兜底发布。"
    )
    return best_result

def _get_post_type_capability(post_type: str = "product") -> dict:
    """通过 OPTIONS 获取 REST 端点可写能力（meta 白名单、featured_media 支持）。"""
    cached = POST_TYPE_CAPABILITY_CACHE.get(post_type)
    if cached:
        return cached

    capability = {"writable_meta_keys": set(), "supports_featured_media": True}
    url = f"{WP_URL}/{post_type}"

    try:
        resp = requests.options(
            url,
            auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD),
            timeout=20
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            data = {}

        for endpoint in data.get("endpoints", []):
            methods = [str(m).upper() for m in endpoint.get("methods", [])]
            if "POST" not in methods:
                continue

            args = endpoint.get("args", {}) or {}
            meta_props = ((args.get("meta") or {}).get("properties") or {})
            capability["writable_meta_keys"] = set(meta_props.keys())
            capability["supports_featured_media"] = "featured_media" in args
            break

        logger.info(
            "REST能力检测完成：post_type=%s | featured_media=%s | writable_meta_keys=%d",
            post_type,
            capability["supports_featured_media"],
            len(capability["writable_meta_keys"]),
        )
    except Exception as e:
        logger.warning(f"⚠️ 获取 REST 能力失败，回退默认逻辑: {e}")

    POST_TYPE_CAPABILITY_CACHE[post_type] = capability
    return capability


def _extract_schema_field(ai_data: dict):
    """兼容不同 AI 字段命名，提取 schema 原始值。"""
    return (
        ai_data.get("Schema")
        or ai_data.get("schema")
        or ai_data.get("seoSchema")
        or ""
    )


def _strip_code_fence(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json|jsonld|ld\+json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json_like_segment(text: str) -> str:
    """从杂糅文本中尽量截取出 JSON 片段。"""
    raw = (text or "").strip()
    if not raw:
        return raw
    raw = _strip_code_fence(raw)

    starts = [i for i in (raw.find("{"), raw.find("[")) if i >= 0]
    if not starts:
        return raw
    start = min(starts)

    end_curly = raw.rfind("}")
    end_square = raw.rfind("]")
    end = max(end_curly, end_square)

    if end > start:
        return raw[start:end + 1].strip()
    return raw


def _replace_schema_image_recursively(obj, url: str):
    """递归替换 schema 中所有 image 字段。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "image":
                if isinstance(v, str):
                    obj[k] = url
                elif isinstance(v, list):
                    obj[k] = [url if isinstance(item, str) else item for item in v]
                    for item in obj[k]:
                        _replace_schema_image_recursively(item, url)
                elif isinstance(v, dict):
                    _replace_schema_image_recursively(v, url)
            else:
                _replace_schema_image_recursively(v, url)
    elif isinstance(obj, list):
        for item in obj:
            _replace_schema_image_recursively(item, url)


def _normalize_schema_payload(schema_raw, main_image_url: str) -> tuple[str, bool]:
    """
    将 schema 规整为 JSON 字符串。
    返回值：(schema_json_string, is_structured_json)
    """
    if schema_raw is None or schema_raw == "":
        return "", False

    # 1) 已是结构化对象
    if isinstance(schema_raw, (dict, list)):
        schema_obj = schema_raw
        if main_image_url:
            _replace_schema_image_recursively(schema_obj, main_image_url)
        return json.dumps(schema_obj, ensure_ascii=False), True

    # 2) 字符串 / 其他类型
    schema_text = _extract_json_like_segment(str(schema_raw))

    # 2.1 先按 JSON 解析
    try:
        schema_obj = json.loads(schema_text)
        if isinstance(schema_obj, str):
            schema_obj = json.loads(schema_obj)
        if isinstance(schema_obj, (dict, list)):
            if main_image_url:
                _replace_schema_image_recursively(schema_obj, main_image_url)
            return json.dumps(schema_obj, ensure_ascii=False), True
    except Exception:
        pass

    # 2.2 再按 Python 字面量解析（兼容单引号风格）
    try:
        schema_obj = ast.literal_eval(schema_text)
        if isinstance(schema_obj, (dict, list)):
            if main_image_url:
                _replace_schema_image_recursively(schema_obj, main_image_url)
            return json.dumps(schema_obj, ensure_ascii=False), True
    except Exception:
        pass

    # 3) 最后兜底：保留原字符串并尽量替换默认占位图
    fallback = str(schema_raw)
    if main_image_url:
        fallback = fallback.replace("https://example.com/image.jpg", main_image_url)
    return fallback, False


def _append_schema_jsonld(content: str, schema_json: str) -> str:
    """将 schema 以 JSON-LD script 形式注入正文末尾（避免重复注入）。"""
    text = content if isinstance(content, str) else str(content or "")
    if not schema_json:
        return text
    if "application/ld+json" in text.lower():
        return text
    return f'{text}\n<script type="application/ld+json">{schema_json}</script>'


def _parse_image_ids(thumbnail_id: str, gallery_ids: str) -> list[int]:
    """把主图和图库 ID 字符串解析成去重有序整数列表。"""
    ids: list[int] = []

    def _push(raw):
        try:
            val = int(str(raw).strip())
            if val > 0 and val not in ids:
                ids.append(val)
        except Exception:
            pass

    if thumbnail_id:
        _push(thumbnail_id)
    if gallery_ids:
        for part in str(gallery_ids).split(","):
            _push(part)

    return ids


def _sync_wc_product_media_schema(product_id: int, thumbnail_id: str, gallery_ids: str, schema_str: str) -> bool:
    """
    通过 WooCommerce API 回写图片与 schema 元数据。
    目的：绕过 wp/v2 对未注册 meta 字段（如 saswp_custom_schema_field）的限制。
    """
    image_ids = _parse_image_ids(thumbnail_id, gallery_ids)
    payload = {}

    if image_ids:
        payload["images"] = [{"id": img_id} for img_id in image_ids]

    meta_data = []
    if schema_str:
        meta_data.append({"key": "saswp_custom_schema_field", "value": schema_str})
    if len(image_ids) > 1:
        meta_data.append({
            "key": "_product_image_gallery",
            "value": ",".join(str(i) for i in image_ids[1:])
        })

    if meta_data:
        payload["meta_data"] = meta_data

    if not payload:
        return True

    wc_update_url = f"{WP_WC_URL}/products/{product_id}"
    for attempt in range(5):
        try:
            resp = requests.put(
                wc_update_url,
                json=payload,
                auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD),
                timeout=20
            )
            resp.raise_for_status()
            data_raw = resp.json()
            data = data_raw if isinstance(data_raw, dict) else {}

            image_count = len(data.get("images", []) or [])
            has_schema = True
            if schema_str:
                has_schema = any(
                    item.get("key") == "saswp_custom_schema_field" and str(item.get("value", "")).strip()
                    for item in (data.get("meta_data", []) or [])
                )

            if schema_str and not has_schema:
                logger.warning("⚠️ WooCommerce 返回中未检测到 schema meta，可能被服务端过滤。")

            logger.info(
                "✅ 产品 [%s] WC 同步成功：images=%s, schema=%s",
                product_id,
                image_count,
                "ok" if has_schema else "missing"
            )
            return has_schema or not schema_str
        except requests.exceptions.HTTPError as http_err:
            err_text = http_err.response.text if hasattr(http_err, 'response') and hasattr(http_err.response, 'text') else ''
            logger.warning(f"❌ WC 同步失败 (尝试 {attempt+1}/5): {http_err} {err_text}")
        except Exception as e:
            logger.warning(f"❌ WC 同步异常 (尝试 {attempt+1}/5): {e}")

        if attempt < 4:
            time.sleep(2)

    logger.error(f"❌ 产品 [{product_id}] WC 媒体/Schema 回写最终失败。")
    return False


def publish_product_to_wordpress(ai_data: dict, cat_id: int = None, thumbnail_id: str = "", gallery_ids: str = "", main_image_url: str = "", product_title: str = ""):
    """2. 将 AI 生成的一条组装数据作为全新产品发布到 WordPress"""
    try:
        # 获取字段，兼容多种可能的 key 命名
        slug = ai_data.get("Url", ai_data.get("afterUrl", ""))
        content = ai_data.get("productDescription", "")
        keyword = ai_data.get("keyword", "")
        title = ai_data.get("seoTitle", "")
        wp_title = product_title or title
        description = ai_data.get("seoDescription", "")
        schema = _extract_schema_field(ai_data)

        schema_str, schema_is_structured = _normalize_schema_payload(schema, main_image_url)
        if schema and not schema_is_structured:
            logger.warning("⚠️ Schema 未能解析为结构化 JSON，将按字符串回写。")

        capability = _get_post_type_capability("product")
        writable_meta_keys = capability.get("writable_meta_keys", set()) or set()
        supports_featured_media = bool(capability.get("supports_featured_media", True))

        can_write_schema_meta = "saswp_custom_schema_field" in writable_meta_keys
        if schema_str and not can_write_schema_meta:
            logger.warning("⚠️ 当前 wp/v2 端点未暴露 saswp_custom_schema_field，稍后将走 WC API 回写。")

        # 步骤 2.1: 创建核心字段（固定链接/正文内容） -> HTTP POST
        wp_create_url = f"{WP_URL}/product"
        wp_payload = {
            "title": wp_title,
            "slug": slug,
            "content": content,
            "status": "publish",  # 直接发布
        }

        if supports_featured_media and str(thumbnail_id).strip().isdigit():
            wp_payload["featured_media"] = int(str(thumbnail_id).strip())

        meta_payload = {}
        if "_thumbnail_id" in writable_meta_keys and thumbnail_id:
            meta_payload["_thumbnail_id"] = str(thumbnail_id)
        if "_product_image_gallery" in writable_meta_keys and gallery_ids:
            meta_payload["_product_image_gallery"] = str(gallery_ids)
        if "saswp_custom_schema_field" in writable_meta_keys and schema_str:
            meta_payload["saswp_custom_schema_field"] = schema_str
        if meta_payload:
            wp_payload["meta"] = meta_payload

        if thumbnail_id and not supports_featured_media:
            logger.warning("⚠️ REST 端点不支持 featured_media，主图可能无法通过 API 自动设置。")

        # 如果传入了分类ID，绑定产品到该分类
        if cat_id is not None:
            wp_payload["product_cat"] = [cat_id]

        new_product_id = None
        for attempt in range(5):
            try:
                logger.info(f"  -> 正在向 WordPress 提交并发布新产品 [{wp_title}...] (尝试 {attempt+1}/5)...")
                res_wp = requests.post(
                    wp_create_url,
                    json=wp_payload,
                    auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD),
                    timeout=20
                )
                res_wp.raise_for_status()

                # 提取新创建产品的独立 ID
                new_product = res_wp.json()
                new_product_id = new_product.get("id")
                if new_product_id:
                    break
                else:
                    logger.error("❌ 产品已响应但未能获取到有效的新建 ID!")
            except requests.exceptions.HTTPError as http_err:
                err_text = http_err.response.text if hasattr(http_err, 'response') and hasattr(http_err.response, 'text') else ''
                logger.warning(f"❌ HTTP 通信层写入 WordPress 失败 (尝试 {attempt+1}/5): {http_err} {err_text}")
                if attempt < 4:
                    time.sleep(2)
            except Exception as e:
                logger.warning(f"❌ 创建产品遇到未知网络异常 (尝试 {attempt+1}/5): {e}")
                if attempt < 4:
                    time.sleep(2)

        if not new_product_id:
            logger.error("❌ 经过 5 次尝试依然无法创建产品，任务中断。")
            return False

        # 步骤 2.2: 使用 WooCommerce API 强制回写主图/图库/schema
        wc_sync_ok = _sync_wc_product_media_schema(
            new_product_id,
            thumbnail_id=thumbnail_id,
            gallery_ids=gallery_ids,
            schema_str=schema_str,
        )

        # 如果 schema 既不能从 wp/v2 写入，又在 WC 回写失败，则兜底注入正文 JSON-LD
        if schema_str and (not can_write_schema_meta) and (not wc_sync_ok):
            logger.warning("⚠️ WC Schema 回写失败，开始执行正文 JSON-LD 兜底注入。")
            fallback_content = _append_schema_jsonld(content, schema_str)
            if fallback_content != content:
                for attempt in range(5):
                    try:
                        patch_url = f"{WP_URL}/product/{new_product_id}"
                        patch_res = requests.post(
                            patch_url,
                            json={"content": fallback_content},
                            auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD),
                            timeout=20
                        )
                        patch_res.raise_for_status()
                        logger.info(f"✅ 产品 [{new_product_id}] JSON-LD 兜底注入成功。")
                        break
                    except Exception as e:
                        logger.warning(f"❌ JSON-LD 兜底注入失败 (尝试 {attempt+1}/5): {e}")
                        if attempt < 4:
                            time.sleep(2)

        # 步骤 2.3: 使用刚获取的新 ID 更新 Yoast SEO 元数据
        yoast_ok = sync_yoast_seo(
            new_product_id,
            "post",
            keyword,
            title,
            description,
        )
        if yoast_ok:
            logger.info(f"✅ 产品 [{new_product_id}] 所有项已成功发布并同步了 Yoast SEO 数据！")
        else:
            logger.error(f"❌ 产品 [{new_product_id}] 基础信息创建成功，但 Yoast SEO 元数据写入失败。")
        return new_product_id

    except Exception as e:
        logger.error(f"❌ 解析或者结构拼接前出现严重异常: {e}")

    return None

# === 启动主流程 ===

async def main():
    logger.info("\n🚀 [WP4AI] 全自动发布管家已启动！")

    if not AI_API_KEY:
        logger.error("在开始之前，请务必保证你挂载了对应的 API 环境变量！")
        return

    base_dir = (os.getenv("WP4AI_PRODUCTS_DIR") or "products").strip() or "products"
    base_dir = os.path.abspath(base_dir)
    logger.info(f"📁 产品目录: {base_dir}")
    if not os.path.exists(base_dir) or not os.path.isdir(base_dir):
        logger.error(f"当前目录下未找到 [{base_dir}] 文件夹，任务已退出。")
        return

    client = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)
    logger.info(
        "✅ 主模型已就绪：%s（地址：%s）",
        DEFAULT_MODEL,
        str(AI_BASE_URL).rstrip("/"),
    )

    # 备用模型客户端
    bak_client = None
    if BAK_AI_API_KEY and BAK_AI_BASE_URL and BAK_DEFAULT_MODEL:
        bak_client = AsyncOpenAI(api_key=BAK_AI_API_KEY, base_url=BAK_AI_BASE_URL)
        logger.info(
            "✅ 备用模型已就绪：%s（地址：%s）",
            BAK_DEFAULT_MODEL,
            str(BAK_AI_BASE_URL).rstrip("/"),
        )
    else:
        logger.info("ℹ️  未配置备用模型，最后一次重试继续使用主模型。")

    # 初始化：从 WordPress 拉取站点名称（供分类 AI Prompt 使用）
    global SITENAME
    _default_sitename = "junhao factory"
    settings_url = f"{WP_URL}/settings"
    wp_api_blocked = False
    for attempt in range(1, 6):
        try:
            res_s = await asyncio.to_thread(
                requests.get, settings_url,
                auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD), timeout=10,
            )
            res_s.raise_for_status()
            payload_s = _json_or_raise(res_s, "获取站点名称")
            SITENAME = payload_s.get("title", "") if isinstance(payload_s, dict) else ""
            if not SITENAME:
                SITENAME = _default_sitename
                logger.info(f"🌐 站点名称为空，使用默认值: {SITENAME!r}")
            else:
                logger.info(f"🌐 站点名称: {SITENAME!r}")
            break
        except Exception as e:
            logger.warning(f"⚠️ 获取站点名称失败 (尝试 {attempt}/5): {e}")
            if _is_sgcaptcha_error(e):
                SITENAME = _default_sitename
                wp_api_blocked = True
                logger.error("❌ 检测到站点启用了 SGCaptcha，REST API 请求被拦截，当前站点任务无法继续。")
                break
            if attempt < 5:
                await asyncio.sleep(2)
            else:
                SITENAME = _default_sitename
                logger.warning(f"⚠️ 无法获取站点名称，使用默认值: {SITENAME!r}")

    if wp_api_blocked:
        return

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

        keyword = _extract_id_and_name(prod_name)[1].strip()  # 剥离可能的 [ID] 前缀
        logger.info(f"\n⚡ 产品目录: [{keyword}] | 分类 ID: {cat_id}")

        # 收集图片；普通方括号文件名也必须保留，只有 [数字ID] 才是已上传标记。
        image_files, existing_images = _collect_product_images(prod_path)
        uploaded_images = list(existing_images)
        failed_image_paths: list[str] = []

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
                else:
                    failed_image_paths.append(img_path)
                    logger.error(
                        f"❌ 图片 {_display_path(img_path)} 未获得有效媒体 ID，"
                        "本次不会将其视为上传成功。"
                    )
        elif existing_images:
            logger.info(f"📸 发现 {len(existing_images)} 张已标记历史图片，直接复用。")

        if failed_image_paths:
            logger.error(
                f"❌ 产品 [{keyword}] 有 {len(failed_image_paths)} 张图片上传失败，"
                "已跳过产品发布并保留原目录；下次运行将继续重试。"
            )
            return

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
                    main_image_payload = _json_object_or_raise(res, f"获取主图 [{thumbnail_id}]")
                    main_image_url = main_image_payload.get("source_url", "")
                    uploaded_images[0] = (thumbnail_id, main_image_url)
                    logger.info(f"    🔄 已补齐主图 URL: {main_image_url}")
                except UnexpectedWPResponseError as e:
                    logger.warning(f"无法获取主图 URL: {e}")
                except Exception as e:
                    logger.warning(f"无法获取主图 URL: {e}")
            if len(uploaded_images) > 1:
                gallery_ids = ",".join(str(img[0]) for img in uploaded_images[1:])

        logger.info(f"👉 投递 AI 生成产品内容（门控: {SEO_MIN_SCORE}，重试: {SEO_MAX_RETRIES}）...")
        try:
            keyword_candidates = _build_keyword_candidates(keyword)
            if not keyword_candidates:
                logger.error(f"[{keyword}] 关键词为空，跳过。")
                return
            if len(keyword_candidates) > 1:
                logger.info(
                    f"🧪 检测到较长/重复产品名，启用关键词兜底链路（共 {len(keyword_candidates)} 组）。"
                )

            ai_data_list = []
            for idx, seed in enumerate(keyword_candidates, start=1):
                if idx == 1:
                    logger.info(f"🧠 SEO 关键词种子: {seed}")
                else:
                    logger.warning(
                        f"[{keyword}] 第 {idx}/{len(keyword_candidates)} 组兜底关键词重试: {seed}"
                    )

                ai_data_list = await generate_with_score_retry(
                    client, [seed], main_image_url,
                    custom_prompt=SEO_SYSTEM_PROMPT,
                    min_score=SEO_MIN_SCORE, max_retries=SEO_MAX_RETRIES,
                    bak_client=bak_client,
                )
                if ai_data_list:
                    if seed != keyword:
                        logger.info(f"[{keyword}] 使用兜底关键词生成成功: {seed}")
                    break

            if not ai_data_list:
                logger.error(f"[{keyword}] 所有关键词兜底重试后仍无有效 AI 数据，跳过。")
                return

            success_count = 0
            last_wp_id = None
            for item in ai_data_list:
                if not isinstance(item, dict):
                    continue
                kw = item.get("keyword", "?")
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

    walk_base_dir = _to_long_path(base_dir)
    if walk_base_dir != base_dir:
        logger.info("🪟 已启用 Windows 长路径兼容模式（\\\\?\\ 前缀）。")

    # 递归找出所有叶子产品目录（无子目录的目录），并取其父目录对应的分类 ID
    for dirpath, dirnames, filenames in os.walk(walk_base_dir, onerror=_walk_error_handler):
        # 已完成的目录跳过（格式：!{wp_id}@_...）
        if os.path.basename(dirpath).startswith("!") and "@_" in os.path.basename(dirpath):
            dirnames.clear()
            continue

        # 判断是否是产品目录（无任何子目录）
        # 这里不能过滤掉已经发布的 !ID@_ 目录，否则如果某个分类下的所有产品都发布完成，
        # child_dirs 为空会导致系统误把这个“二级分类目录”当成“文章(产品)目录”去发布！
        if dirnames:
            logger.info(
                f"↪ 跳过分类目录（非叶子）: {_display_path(dirpath)} | 子目录数量: {len(dirnames)}"
            )
            continue  # 还有子目录，说明它是分类层，跳过产品发布逻辑

        rel = os.path.relpath(dirpath, walk_base_dir)
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
