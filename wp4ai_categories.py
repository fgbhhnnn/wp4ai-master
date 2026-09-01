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
log_file = os.path.join(log_dir, f'wp4ai_categories_{time.strftime("%Y%m%d")}.log')

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


def _normalize_wp_domain(domain: str) -> str:
    return str(domain or "").strip().rstrip("/")


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
    WP_DOMAIN = _normalize_wp_domain(config.get('WordPress', 'WP_DOMAIN'))
    WP_PRODUCT_URL = WP_DOMAIN+"/product/"
    WP_ABOUT_URL = WP_DOMAIN+"/junhao-clothing-streetwear/"
    WP_CONTACT_URL = WP_DOMAIN+"/contact/"
    WP_BLOG_URL = WP_DOMAIN+"/category/blog/"

    WP_URL = WP_DOMAIN+"/wp-json/wp/v2"
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

# 站点名称（在 main() 启动时从 WordPress 拉取后写入，供全局 AI Prompt 使用）
SITENAME: str = ""
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


def sync_yoast_seo(
    term_id: int,
    focus_keyword,
    title: str,
    description: str,
    retries: int = 5,
) -> bool:
    """Write Yoast SEO metadata for a WooCommerce product category."""
    url = f"{WP_URL}/product_cat/{int(term_id)}"
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
            response = requests.post(
                url,
                json=payload,
                auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD),
                timeout=20,
            )
            response.raise_for_status()
            logger.info(f"✅ 分类【ID:{term_id}】Yoast SEO 元数据已同步。")
            return True
        except requests.exceptions.HTTPError as http_err:
            response = getattr(http_err, "response", None)
            status_code = getattr(response, "status_code", None)
            body = (getattr(response, "text", "") or "").replace("\r", " ").replace("\n", " ")[:320]
            if status_code is not None and 400 <= status_code < 500 and status_code not in {408, 429}:
                if "_yoast_wpseo_" in body.lower():
                    logger.error(
                        "❌ Yoast SEO 字段未注册或不可写，停止重试。"
                        "请在站点安装并启用 wp4ai-yoast-rest-meta 插件后重试。"
                        "HTTP %s: %s",
                        status_code,
                        body,
                    )
                else:
                    logger.error(
                        "❌ Yoast SEO 元数据写入被 WordPress 拒绝，停止重试。"
                        "请检查应用密码、账号权限、分类 ID 和请求参数。"
                        "HTTP %s: %s",
                        status_code,
                        body,
                    )
                return False
            logger.warning(
                "❌ Yoast SEO 元数据写入失败 (尝试 %s/%s): %s",
                attempt,
                total_attempts,
                http_err,
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

    logger.error(f"❌ 分类【ID:{term_id}】Yoast SEO 元数据写入最终失败。")
    return False


async def ensure_wp_category(category_name: str) -> int:
    """1. 确认 WordPress 中存在该分类，存在则返回 ID，不存在则返回 None。

    Args:
        category_name: WordPress 产品分类名称。

    Returns:
        找到时返回分类 ID（int），未找到或失败时返回 None。
    """
    for attempt in range(5):
        try:
            search_url = f"{WP_URL}/product_cat"
            params = {"search": category_name, "hide_empty": False}
            res = await asyncio.to_thread(
                requests.get, search_url,
                params=params,
                auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD),
                timeout=15,
            )
            res.raise_for_status()
            cats = res.json()

            # 精确匹配（防止 search 搜出包含子串的其他分类）
            for cat in cats:
                if cat.get('name', '').lower() == category_name.lower():
                    logger.info(f"✅ 已匹配到分类【{category_name}】ID: {cat.get('id')}")
                    return cat.get('id')

            # 未找到，返回 None（由调用方决定是否新建）
            logger.info(f"WordPress 中未找到分类【{category_name}】，返回 None。")
            return None
        except Exception as e:
            logger.warning(f"查询分类【{category_name}】时网络异常 (尝试 {attempt+1}/5): {e}")
            if attempt < 4:
                await asyncio.sleep(2)
            else:
                logger.error(f"❌ 经过 5 次尝试查询分类依然失败。")
                return None



async def generate_seo_data_by_keywords(
    client: AsyncOpenAI,
    keywords: str,
    model: str = DEFAULT_MODEL,
):
    """1. 根据关键词，通过 AI 请求并生成专门的高质量 SEO 内容数据 (JSON 分组)"""
    prompt = f"""- 公司名称:{SITENAME}
- 网站网址:{WP_DOMAIN}
- 产品类目:{keywords}"""
    logger.info(f"系统正在让 AI [模型:{model}] 思考与生成 {prompt} 的 SEO 内容，请耐心等待...")
    try:
        response = await client.chat.completions.create(
            model=model,
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
        logger.error(f"调用 AI 接口时发生异常: {e}", exc_info=True)
        return None


def _parse_ai_json(raw_str: str) -> dict:
    """解析 AI 返回的 JSON 字符串，统一返回 dict，失败返回 None。"""
    try:
        res = json.loads(raw_str)
        if isinstance(res, dict):
            return res
        logger.error(f"返回格式有误，原始对象：{str(res)[:200]}")
        return None
    except json.JSONDecodeError:
        logger.error("模型返回的内容无法反序列化为合法的 JSON！")
        return None


async def generate_with_score_retry(
    client: AsyncOpenAI,
    keywords: str,
    max_retries: int = SEO_MAX_RETRIES,
):
    """
    带评分门控的 AI 内容生成器。

    调用 generate_seo_data_by_keywords 生成内容后，使用
    通用 SEO 内容检查规则进行本地预估评分；
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

    items = None
    for attempt in range(1, max_retries + 1):
        raw = await generate_seo_data_by_keywords(client, keywords, model=DEFAULT_MODEL)
        if not raw:
            logger.warning(f"  第 {attempt} 次生成返回空值，直接跳过本次尝试。")
            continue

        items = _parse_ai_json(raw)
        if items is None:
            logger.warning(f"  第 {attempt} 次生成 JSON 解析失败，直接跳过本次尝试。")
            continue
        break
    return items

async def update_categories_description(client: AsyncOpenAI) -> None:
    """获取所有 product_cat 分类，对没有 description 的分类调用
    generate_seo_data_by_keywords 生成并更新到 WordPress。

    Args:
        client: AsyncOpenAI 客户端实例。
    """
    cat_url = f"{WP_URL}/product_cat"

    # 分页拉取所有分类
    all_cats: list[dict] = []
    page = 1
    while True:
        try:
            res = await asyncio.to_thread(
                requests.get, cat_url,
                params={"per_page": 100, "page": page, "hide_empty": False},
                auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD),
                timeout=15,
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

    logger.info(f"📄 共获取到 {len(all_cats)} 个分类，开始过滤无描述项...")

    # 过滤出没有 description 的分类（空字符串或仅有空白）
    need_update = [
        cat for cat in all_cats
        if not (cat.get("description") or "").strip()
    ]
    logger.info(f"🚨 其中 {len(need_update)} 个分类无描述，将逐一生成并更新。")

    for cat in need_update:
        cat_id   = cat.get("id")
        cat_name = cat.get("name", "")
        logger.info(f"\n======================================")
        logger.info(f"📁 正在处理分类【{cat_name}】(ID:{cat_id})...")

        # 调用 AI 生成 SEO 内容
        raw = await generate_seo_data_by_keywords(client, cat_name)
        if not raw:
            logger.warning(f"  ⚠️ AI 返回为空，跳过分类【{cat_name}】")
            continue

        ai_data = _parse_ai_json(raw)
        if not ai_data:
            logger.warning(f"  ⚠️ JSON 解析失败，跳过分类【{cat_name}】")
            continue

        description = ai_data.get("seoDescription", "") or ""
        schema      = ai_data.get("seoSchema", "")
        keyword     = ai_data.get("focusKeywords", "")
        title       = ai_data.get("seoTitle", "")

        # 序列化 schema
        try:
            schema_str = json.dumps(schema) if not isinstance(schema, str) else schema
        except Exception:
            schema_str = str(schema)

        # PATCH 更新分类的 description 和 schema meta
        patch_url = f"{cat_url}/{cat_id}"
        for attempt in range(5):
            try:
                patch_payload = {
                    "description": description,
                    "meta": {
                        "saswp_custom_schema_field": schema_str,
                    },
                }
                res_patch = await asyncio.to_thread(
                    requests.post, patch_url,
                    json=patch_payload,
                    auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD),
                    timeout=15,
                )
                res_patch.raise_for_status()
                logger.info(f"  ✅ 分类【{cat_name}】描述已更新（{len(description)} 字符）")
                break
            except Exception as e:
                logger.warning(f"  ⚠️ PATCH 分类失败 (尝试 {attempt+1}/5): {e}")
                if attempt < 4:
                    await asyncio.sleep(2)
                else:
                    logger.error(f"  ❌ 经过 5 次尝试更新分类【{cat_name}】依然失败。")
                    continue

        await asyncio.to_thread(
            sync_yoast_seo,
            cat_id,
            keyword,
            title,
            description,
        )

    logger.info(f"\n🎉 --update 模式完成！共处理 {len(need_update)} 个分类。")


async def scan_and_create_categories(client: AsyncOpenAI, base_dir: str):
    """扫描目录树，按照层级建立有父子关系的 WordPress 产品分类。

    目录规则：
        products/a/b/c/d/e.jpg
        - a 为一级分类（parent=0）
        - b、c 为逐级子分类（有父子关系）
        - d 为产品目录（倒数第二层），跳过不建分类
        - e.jpg 等文件，跳过

    Args:
        client:   AsyncOpenAI 客户端。
        base_dir: 扫描根目录（通常为 'products'）。
    """
    # 缓存：(parent_id, cat_name_lower) -> cat_id，避免重复查询/新建
    _cat_cache: dict[tuple[int, str], int] = {}

    async def _ensure_cat_with_parent(name: str, parent_id: int):
        """查找或新建指定父分类下的子分类，返回 cat_id。"""
        cache_key = (parent_id, name.lower())
        if cache_key in _cat_cache:
            logger.info(f"  [缓存] 分类【{name}】(parent:{parent_id}) 命中缓存 ID: {_cat_cache[cache_key]}")
            return _cat_cache[cache_key]

        cat_url = f"{WP_URL}/product_cat"

        # 先搜索是否已存在（同名 + 相同父分类）
        for attempt in range(5):
            try:
                params = {"search": name, "hide_empty": False, "parent": parent_id}
                res = await asyncio.to_thread(
                    requests.get, cat_url,
                    params=params,
                    auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD),
                    timeout=15,
                )
                res.raise_for_status()
                for cat in res.json():
                    if cat.get("name", "").lower() == name.lower() and cat.get("parent", -1) == parent_id:
                        _cat_cache[cache_key] = cat["id"]
                        logger.info(f"  ✅ 分类【{name}】已存在 (parent:{parent_id})，ID: {cat['id']}")
                        return cat["id"]
                break  # 搜索成功但未找到
            except Exception as e:
                logger.warning(f"  查询分类【{name}】异常 (尝试 {attempt+1}/5): {e}")
                if attempt < 4:
                    await asyncio.sleep(2)
                else:
                    return None

        # 未找到 → AI 生成描述并新建
        logger.info(f"  -> 分类【{name}】不存在 (parent:{parent_id})，正在生成描述并新建...")
        ai_data = await generate_with_score_retry(client, name)
        description = (ai_data.get("seoDescription", "") or "") if ai_data else ""
        schema = (ai_data.get("seoSchema", "")) if ai_data else ""
        keyword = (ai_data.get("focusKeywords", "")) if ai_data else ""
        title = (ai_data.get("seoTitle", "")) if ai_data else ""
        try:
            schema_str = json.dumps(schema) if not isinstance(schema, str) else schema
        except Exception:
            schema_str = str(schema)

        cat_id = None
        for attempt in range(5):
            try:
                payload = {
                    "name": name,
                    "parent": parent_id,
                    "description": description,
                    "meta": {"saswp_custom_schema_field": schema_str},
                }
                res_post = await asyncio.to_thread(
                    requests.post, cat_url,
                    json=payload,
                    auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD),
                    timeout=15,
                )
                res_post.raise_for_status()
                cat_id = res_post.json().get("id")
                logger.info(f"  ✅ 分类【{name}】新建成功 (parent:{parent_id})，ID: {cat_id}")
                break
            except Exception as e:
                logger.warning(f"  新建分类【{name}】异常 (尝试 {attempt+1}/5): {e}")
                if attempt < 4:
                    await asyncio.sleep(2)
                else:
                    logger.error(f"  ❌ 经过 5 次尝试新建分类【{name}】失败。")
                    return None

        if cat_id:
            _cat_cache[cache_key] = cat_id
            await asyncio.to_thread(
                sync_yoast_seo,
                cat_id,
                keyword,
                title,
                description,
            )

        return cat_id

    def _extract_id_and_name(raw_name: str) -> tuple:
        """从目录名提取 [ID] 前缀和真实分类名。
        '[42]Men Sportswear' → (42, 'Men Sportswear')
        'Men Sportswear'     → (None, 'Men Sportswear')
        """
        if raw_name.startswith('['):
            try:
                id_str = raw_name.split(']')[0].lstrip('[')
                if id_str.isdigit():
                    real_name = raw_name[len(id_str) + 2:]  # 跳过 '[' + id_str + ']'
                    return int(id_str), real_name
            except Exception:
                pass
        return None, raw_name

    # 收集所有需要建立分类的原始路径列表（保留目录名中的 [ID] 前缀）
    cat_paths: set[tuple[str, ...]] = set()

    for dirpath, dirnames, filenames in os.walk(base_dir):
        # 相对路径分段（去掉 base_dir 前缀）
        rel = os.path.relpath(dirpath, base_dir)
        parts = rel.split(os.sep) if rel != '.' else []

        if not parts:
            continue  # 根目录本身，跳过

        # 当前目录包含子目录时：自身层为分类
        # 当前目录没有子目录（只有文件）时：自身层为产品目录，跳过
        has_subdirs = any(
            os.path.isdir(os.path.join(dirpath, d)) for d in os.listdir(dirpath)
        ) if os.path.isdir(dirpath) else False

        if not has_subdirs:
            cat_parts = tuple(parts[:-1])   # 产品目录，取上层为分类
        else:
            cat_parts = tuple(parts)        # 分类目录本身

        for i in range(1, len(cat_parts) + 1):
            cat_paths.add(cat_parts[:i])

    if not cat_paths:
        logger.warning("⚠️ 未在目录中发现任何分类层级，请检查目录结构。")
        return

    # actual_names: path_tuple → 当前真实目录名（可能已含 [ID] 前缀，重命名后同步更新）
    # 初始化时每个节点的真实名就是原始目录名
    actual_names: dict[tuple, str] = {p: p[-1] for p in cat_paths}

    # 按层深排序，从浅到深递建
    sorted_paths = sorted(cat_paths, key=lambda p: len(p))
    logger.info(f"📂 共发现 {len(sorted_paths)} 个分类节点，开始递建...")

    for path_tuple in sorted_paths:
        raw_name = path_tuple[-1]
        prefilled_id, cat_name = _extract_id_and_name(raw_name)

        # 计算父 ID（用真实名称查缓存）
        parent_id = 0
        p_id = 0
        for i, raw_part in enumerate(path_tuple[:-1]):
            grand_id = 0 if i == 0 else p_id
            _, real_part = _extract_id_and_name(raw_part)
            p_id = _cat_cache.get((grand_id, real_part.lower()), 0)
        if path_tuple[:-1]:
            parent_id = p_id

        cache_key = (parent_id, cat_name.lower())

        if prefilled_id is not None:
            # 本地目录已标注 [ID]，直接预填缓存，跳过网络请求
            if cache_key not in _cat_cache:
                _cat_cache[cache_key] = prefilled_id
                logger.info(f"📁 【{cat_name}】已标注 ID={prefilled_id}，写入缓存，跳过新建。")
            else:
                logger.info(f"📁 【{cat_name}】缓存已存在 ID={_cat_cache[cache_key]}，跳过。")
            continue

        display_path = ' > '.join(_extract_id_and_name(p)[1] for p in path_tuple)
        logger.info(f"\n>>> 处理分类【{display_path}】 (parent_id={parent_id})")
        new_id = await _ensure_cat_with_parent(cat_name, parent_id)

        # 新建成功后，把本地目录重命名加上 [ID] 前缀作为缓存标记
        if new_id:
            try:
                # 逐层用 actual_names 重建当前目录的真实绝对路径
                # （父级可能已经被重命名，必须用更新后的名字）
                current_abs = base_dir
                for prefix_len in range(1, len(path_tuple) + 1):
                    prefix = path_tuple[:prefix_len]
                    current_abs = os.path.join(current_abs, actual_names.get(prefix, prefix[-1]))

                new_raw_name = f"[{new_id}]{cat_name}"
                new_abs = os.path.join(os.path.dirname(current_abs), new_raw_name)
                if os.path.exists(current_abs) and not os.path.exists(new_abs):
                    os.rename(current_abs, new_abs)
                    # 同步更新 actual_names，供子层路径重建使用
                    actual_names[path_tuple] = new_raw_name
                    logger.info(f"  🌟 本地目录已缓存标记: {new_raw_name}")
                elif os.path.exists(new_abs):
                    actual_names[path_tuple] = new_raw_name
            except Exception as rename_err:
                logger.warning(f"  ⚠️ 重命名本地目录失败（不影响分类建立）: {rename_err}")

    logger.info(f"\n🎉 --scan 模式完成！共处理 {len(sorted_paths)} 个分类节点。")



async def publish_tag_to_wordpress(category_name: str, client: AsyncOpenAI):
    """2. 将 AI 生成的一条组装数据作为目录发布到 WordPress。

    流程：查询分类 → (未找到则 AI 生成描述并新建) → 更新 Yoast SEO 元数据。

    Args:
        ai_data:       AI 返回的 SEO 数据字典。
        category_name: WordPress 产品分类名称，用于查找/新建分类。
        client:        AsyncOpenAI 客户端，为新建分类时生成描述使用。
    """
    try:
        # 获取字段，兼容多种可能的 key 命名
        keyword = ""
        title = ""
        description = ""
        schema = ""

        # 步骤 2.1: 查询分类，未找到则 AI 生成描述并新建
        search_url = f"{WP_URL}/product_cat"
        cat_id = await ensure_wp_category(category_name)
        if cat_id is None:
            logger.info(f"WordPress 中未找到分类【{category_name}】，正在生成描述并新建...")
            try:
                logger.info(f"  -> 正在为分类【{category_name}】生成 SEO 描述...")
                items = await generate_with_score_retry(client, category_name)

                # 获取字段，兼容多种可能的 key 命名
                keyword = items.get("focusKeywords", "")
                title = items.get("seoTitle", "")
                description = items.get("seoDescription", "")
                schema = items.get("seoSchema", "")
                
                try:
                    schema_dict = json.loads(schema) if schema else {}
                    schema_str = json.dumps(schema_dict)
                except (json.JSONDecodeError, TypeError):
                    logger.warning("❌ Schema 格式异常，尝试 literal_eval 还原")
                    try:
                        schema_dict = ast.literal_eval(str(schema))
                        schema_str = json.dumps(schema_dict)
                    except Exception:
                        logger.warning("❌ Schema 格式异常，回落字符串形式")
                        schema_str = str(schema)

                if description:
                    logger.info(f"  ✅ 分类描述生成成功（{len(description)} 字符）")
                else:
                    logger.warning("  ⚠️ AI 未能生成有效描述，将使用空描述新建分类。")
            except Exception as desc_err:
                logger.warning(f"  ⚠️ 生成分类描述时发生异常，将使用空描述继续: {desc_err}")

            for attempt in range(5):
                try:
                    # REST API 新建分类，通过 meta 字段同步写入 saswp_custom_schema_field
                    # 前提：functions.php 已通过 register_term_meta + show_in_rest=true 暴露该字段
                    payload = {
                        "name": category_name,
                        "description": description,
                        "meta": {
                            "saswp_custom_schema_field": schema_str,
                        },
                    }                    
                    res_post = await asyncio.to_thread(
                        requests.post, search_url,
                        json=payload,
                        auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD),
                        timeout=15,
                    )
                    res_post.raise_for_status()
                    cat_id = res_post.json().get("id")
                    logger.info(f"  ✅ 分类【{category_name}】已成功新建，ID: {cat_id}")
                    break
                except Exception as e:
                    logger.warning(f"新建分类【{category_name}】异常 (尝试 {attempt+1}/5): {e}")
                    if attempt < 4:
                        await asyncio.sleep(2)
                    else:
                        logger.error(f"❌ 经过 5 次尝试新建分类依然失败。")
                        return False
                    
            
        if not any((_normalize_focus_keyword(keyword), str(title or "").strip(), str(description or "").strip())):
            logger.info(f"分类【{category_name}】没有新生成的 SEO 数据，保留现有 Yoast SEO 元数据。")
            return True

        return await asyncio.to_thread(
            sync_yoast_seo,
            cat_id,
            keyword,
            title,
            description,
        )

    except Exception as e:
        logger.error(f"❌ 解析或者结构拼接前出现严重异常: {e}")

    return False


# === 启动主流程 ===

async def main():
    logger.info("\n🚀 [WP4AI] 目录分类检测管家已启动！")
    
    if not AI_API_KEY:
        logger.error("在开始之前，请务必保证你挂载了对应的 API 环境变量！")
        return

    base_dir = "products"
    if not os.path.exists(base_dir) or not os.path.isdir(base_dir):
        logger.error(f"当前目录下未找到 [{base_dir}] 文件夹，任务已退出。")
        return

    client = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)

    # 初始化：从 WordPress 拉取站点名称写入全局变量 SITENAME（最多重试 5 次）
    global SITENAME
    settings_url = f"{WP_URL}/settings"
    for attempt in range(1, 6):
        try:
            res_settings = await asyncio.to_thread(
                requests.get, settings_url,
                auth=HTTPBasicAuth(WP_USERNAME, WP_PASSWORD),
                timeout=10,
            )
            res_settings.raise_for_status()
            SITENAME = res_settings.json().get("title", "") or ""
            logger.info(f"🌐 站点名称已获取: {SITENAME!r}")
            break
        except Exception as e:
            logger.warning(f"⚠️ 获取站点名称失败 (尝试 {attempt}/5): {e}")
            if attempt < 5:
                await asyncio.sleep(2)
            else:
                logger.error("❌ 经过 5 次尝试仍无法获取站点名称，任务终止。")
                sys.exit(1)

    # === 模式判断 ===
    cli_args = sys.argv[1:]

    # 模式 1：--scan，递归扫描目录树，按父子层级新建分类
    if "--scan" in cli_args:
        logger.info("🔍 检测到 --scan 参数，进入目录树扫描模式...")
        await scan_and_create_categories(client, base_dir)
        return

    # 模式 2：--update ，获取所有分类，对无描述的逐一生成并更新
    if "--update" in cli_args:
        logger.info("🔄 检测到 --update 参数，进入分类描述补全模式...")
        await update_categories_description(client)
        return

    # 模式 2：带分类名参数，直接单个新建
    cat_name_args = [a for a in cli_args if not a.startswith("--")]
    if cat_name_args:
        # 单个模式：每个参数为一个分类名，直接生成并新建
        logger.info(f"🎯 检测到命令行参数，进入单个新建模式，共 {len(cat_name_args)} 个分类。")
        for cat_name in cat_name_args:
            cat_name = cat_name.strip()
            if not cat_name:
                continue
            logger.info(f"\n======================================")
            logger.info(f"📁 开始处理分类: 【{cat_name}】")
            await publish_tag_to_wordpress(cat_name, client)
        logger.info("\n🎉 单个新建模式处理完毕！")
        return

    # 批量模式：遍历 products 目录第一层
    for cat_name in os.listdir(base_dir):
        cat_path = os.path.join(base_dir, cat_name)
        if not os.path.isdir(cat_path):
            continue

        logger.info(f"\n======================================")

        cat_id = None
        current_cat_name = cat_name

        # 尝试从目录名中提取已经标注好的 [ID]
        if current_cat_name.startswith('['):
            try:
                extracted_id_str = current_cat_name.split(']')[0].replace('[', '')
                if extracted_id_str.isdigit():
                    cat_id = int(extracted_id_str)
                    logger.info(f"📁 发现本地已标记的分类层级: 【{current_cat_name}】，复用 ID: {cat_id}")
            except Exception:
                pass

        if not cat_id:
            logger.info(f"📁 发现未标记的分类层级: 【{current_cat_name}】")
            cat_id = await publish_tag_to_wordpress(current_cat_name, client)

            # 如果成功获取了分类ID，就把本地目录加上 [ID] 前缀做缓存
            if cat_id is not None:
                try:
                    new_cat_name = f"[{cat_id}]{current_cat_name}"
                    new_cat_path = os.path.join(base_dir, new_cat_name)
                    os.rename(cat_path, new_cat_path)
                    logger.info(f"    🌟 本地分类目录已缓存标记成功: {new_cat_name}")

                    # 极其关键：更新后续要遍历的路径
                    cat_path = new_cat_path
                    current_cat_name = new_cat_name
                except Exception as e:
                    logger.error(f"    ❌ 重命名本地分类目录用于本地缓存失败: {e}")

    logger.info("\n🎉 本次全自动化目录分类检测已成功跑完！")

if __name__ == "__main__":
    asyncio.run(main())
