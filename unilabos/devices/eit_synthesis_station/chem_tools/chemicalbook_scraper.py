# -*- coding: utf-8 -*-
"""
功能:
    提供 ChemicalBook 页面抓取, 缓存, 反爬识别, DOM 解析与结构化标准化能力.
    本模块仅处理按 CAS 直达的中文页与英文页, 输出稳定 JSON 结构.
参数:
    无.
返回:
    无.
"""

import copy
import html as html_lib
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from . import storage

try:
    from bs4 import BeautifulSoup
    from bs4.element import Tag
except ImportError as exc:  # pragma: no cover - 运行环境未安装依赖时走这里
    BeautifulSoup = None
    Tag = Any
    _BS4_IMPORT_ERROR = exc
else:
    _BS4_IMPORT_ERROR = None


logger = logging.getLogger("ChemicalBookScraper")

CAS_PATTERN = re.compile(r"^\d{2,7}-\d{2}-\d$")
FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?")
CELSIUS_RANGE_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*(?:to|~|-|—|–)\s*(-?\d+(?:\.\d+)?)\s*(?:°|º)?\s*C",
    re.IGNORECASE,
)
CELSIUS_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:°|º)?\s*C", re.IGNORECASE)
FAHRENHEIT_RANGE_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*(?:to|~|-|—|–)\s*(-?\d+(?:\.\d+)?)\s*(?:°|º)?\s*F",
    re.IGNORECASE,
)
FAHRENHEIT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:°|º)?\s*F", re.IGNORECASE)
GBK_META_RE = re.compile(r"charset\s*=\s*(gbk|gb2312|gb18030)", re.IGNORECASE)
BLOCK_KEYWORDS = [
    "验证码",
    "访问过于频繁",
    "安全验证",
    "输入验证码",
    "captcha",
    "robot",
    "verify",
]
DEFAULT_TIMEOUT = 15.0
DEFAULT_CACHE_TTL_S = 7 * 24 * 60 * 60  # 7 天, 中文名等基本信息几乎不变
DEFAULT_MIN_INTERVAL_S = 2.0
MAX_RETRIES = 3

CHEMICALBOOK_CN_URL = "https://www.chemicalbook.com/CAS_{cas}.htm"
CHEMICALBOOK_EN_URL = "https://www.chemicalbook.com/CASEN_{cas}.htm"

DEFAULT_SECTIONS = (
    "基本信息",
    "物理化学性质",
    "安全信息",
    "用途与合成方法",
    "上下游产品信息",
)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# UA 轮换池, 降低重试时的指纹一致性
_USER_AGENT_POOL = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
        "Gecko/20100101 Firefox/124.0"
    ),
]

SECTION_KEYWORDS = {
    "基本信息": [
        "基本信息",
        "产品名称",
        "化学品信息",
        "product information",
        "basic information",
    ],
    "物理化学性质": [
        "物理化学性质",
        "性质",
        "理化性质",
        "physical and chemical properties",
        "physical properties",
    ],
    "安全信息": [
        "安全信息",
        "危险类别",
        "安全术语",
        "hazard information",
        "safety information",
        "safety",
    ],
    "用途与合成方法": [
        "用途与合成方法",
        "用途",
        "应用",
        "use",
        "application",
        "description",
        "general description",
    ],
    "上下游产品信息": [
        "上下游产品信息",
        "上游产品",
        "下游产品",
        "upstream products",
        "downstream products",
        "related products",
    ],
}

FIELD_ALIASES = {
    "cn_name": ["中文名称", "中文名", "产品中文名称"],
    "en_name": [
        "英文名称",
        "英文名",
        "name",
        "product name",
        "chemical name",
        "casenname",
    ],
    "aliases": ["别名", "同义词", "别名或商品名", "中文别名", "英文别名", "synonyms", "alias"],
    "molecular_formula": ["分子式", "结构式", "molecular formula", "formula"],
    "molecular_weight": ["分子量", "分子量值", "molecular weight", "mol weight"],
    "density": ["密度", "density", "relative density", "specific gravity", "比重"],
    "melting_point": ["熔点", "熔点范围", "melting point", "mp"],
    "boiling_point": ["沸点", "boiling point", "bp"],
    "flash_point": ["闪点", "flash point", "fp"],
    "appearance": ["性状", "外观", "外观性质", "外观性状", "形态", "appearance", "color and form", "form"],
    "solubility": ["溶解度", "溶解性", "水溶解性", "solubility", "water solubility"],
    "storage_conditions": ["储存条件", "贮存条件", "存储类别", "storage temp", "storage", "store at"],
    "hazard_statements": [
        "危险说明",
        "危险性描述",
        "危险类别码",
        "hazard statements",
        "hazard codes",
        "ghs hazard statements",
        "hazard",
    ],
    "safety_statements": [
        "安全说明",
        "防范说明",
        "安全术语",
        "safety statements",
        "safety description",
        "ghs precautionary statements",
    ],
    "uses": ["用途", "应用", "用途与合成方法", "application", "use", "general description"],
    "upstream_products": ["上游产品", "上游原料", "raw materials", "upstream products"],
    "downstream_products": ["下游产品", "preparation products", "downstream products"],
}

FIELD_SECTION_MAP = {
    "cn_name": "基本信息",
    "en_name": "基本信息",
    "aliases": "基本信息",
    "molecular_formula": "基本信息",
    "molecular_weight": "基本信息",
    "density": "物理化学性质",
    "melting_point": "物理化学性质",
    "boiling_point": "物理化学性质",
    "flash_point": "物理化学性质",
    "appearance": "物理化学性质",
    "solubility": "物理化学性质",
    "storage_conditions": "安全信息",
    "hazard_statements": "安全信息",
    "safety_statements": "安全信息",
    "uses": "用途与合成方法",
    "upstream_products": "上下游产品信息",
    "downstream_products": "上下游产品信息",
}

PAGE_TYPES = ("cn", "en")
_LAST_REQUEST_AT_BY_CAS: Dict[str, float] = {}


@dataclass
class PageFetchResult:
    """
    功能:
        记录单个 ChemicalBook 页面抓取结果, 供抓取层与解析层传递状态.
    参数:
        page_type: str, 页面类型, cn 或 en.
        url: str, 页面 URL.
        status_code: Optional[int], HTTP 状态码.
        html: Optional[str], 解码后的 HTML 文本.
        blocked: bool, 是否命中反爬页面.
        blocked_reason: str, 反爬原因说明.
        from_cache: bool, 是否命中本地缓存.
        error_message: str, 抓取异常说明.
    返回:
        PageFetchResult.
    """

    page_type: str
    url: str
    status_code: Optional[int] = None
    html: Optional[str] = None
    blocked: bool = False
    blocked_reason: str = ""
    from_cache: bool = False
    error_message: str = ""


def fetch_chemicalbook_by_cas(
    cas: str,
    timeout: float = DEFAULT_TIMEOUT,
    save_raw_html: bool = False,
) -> Dict[str, Any]:
    """
    功能:
        根据 CAS 号抓取 ChemicalBook 中文页与英文页, 并返回标准化 JSON 结构.
    参数:
        cas: str, CAS 号, 例如 64-17-5.
        timeout: float, 单次请求超时秒数.
        save_raw_html: bool, 是否额外保存原始 HTML 文件到本地目录.
    返回:
        Dict[str, Any], 包含抓取状态, 标准化字段, 原始 sections 与调试信息.
    """
    storage.ensure_chemicalbook_data_layout()
    record = _build_empty_record(cas=cas)
    normalized_cas = str(cas).strip()
    record["cas"] = normalized_cas

    if CAS_PATTERN.match(normalized_cas) is None:
        record["status"] = "error"
        record["debug"]["blocked_reason"] = "CAS 格式不合法"
        logger.warning("ChemicalBook 抓取失败, CAS 格式不合法: %s", normalized_cas)
        return record

    if BeautifulSoup is None:
        logger.warning("未安装 beautifulsoup4, 将使用降级解析路径")

    session = _build_session()
    page_results: List[PageFetchResult] = []

    need_network = _has_cache_miss(normalized_cas)
    if need_network is True:
        _apply_cas_rate_limit(normalized_cas)

    for idx, page_type in enumerate(PAGE_TYPES):
        # CN 页和 EN 页之间加入随机延迟, 降低连续请求特征
        if idx > 0 and need_network is True:
            delay = random.uniform(1.5, 3.5)
            logger.debug("ChemicalBook 页面间等待 %.2f 秒", delay)
            time.sleep(delay)

        page_result = _fetch_page_with_cache(
            session=session,
            cas=normalized_cas,
            page_type=page_type,
            timeout=timeout,
        )
        page_results.append(page_result)

        status_key = f"http_status_{page_type}"
        cache_key = f"cache_hit_{page_type}"
        record["debug"][status_key] = page_result.status_code
        record["debug"][cache_key] = page_result.from_cache

        if page_result.blocked is True:
            record["debug"]["blocked"] = True
            if len(record["debug"]["blocked_reason"]) == 0:
                record["debug"]["blocked_reason"] = page_result.blocked_reason

    merged_sections = _build_empty_sections()
    all_pairs: List[Tuple[str, str]] = []

    for page_result in page_results:
        if page_result.html is None:
            continue

        parsed_page = _parse_chemicalbook_page(
            html=page_result.html,
            page_type=page_result.page_type,
        )
        _merge_sections(merged_sections, parsed_page["sections"])
        all_pairs.extend(parsed_page["pairs"])

        if save_raw_html is True:
            raw_path = _save_raw_html(
                cas=normalized_cas,
                page_type=page_result.page_type,
                html=page_result.html,
            )
            record["debug"]["raw_html_paths"][page_result.page_type] = str(raw_path)

    record["sections"] = merged_sections
    record["normalized"] = _normalize_from_pairs_and_sections(
        cas=normalized_cas,
        pairs=all_pairs,
        sections=merged_sections,
    )
    record = normalize_chemicalbook_record(record)
    record["status"] = _determine_record_status(record=record, page_results=page_results)
    return record


def normalize_chemicalbook_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    功能:
        基于已抓取 record 中的 sections 信息, 生成稳定的 normalized 字段.
        该函数不会删除 sections 中的未知字段, 仅补全或覆盖 normalized.
    参数:
        record: Dict[str, Any], fetch_chemicalbook_by_cas 生成或兼容的数据结构.
    返回:
        Dict[str, Any], 补全 normalized 后的新字典.
    """
    normalized_record = copy.deepcopy(record)
    cas = str(normalized_record.get("cas", "")).strip()
    sections = normalized_record.get("sections", {})
    pairs = _collect_pairs_from_sections(sections)
    normalized = _normalize_from_pairs_and_sections(
        cas=cas,
        pairs=pairs,
        sections=sections,
    )
    normalized_record["normalized"] = normalized
    return normalized_record


def _build_session() -> requests.Session:
    """
    功能:
        构建 requests Session, 统一请求头, 并默认忽略系统代理环境变量.
    参数:
        无.
    返回:
        requests.Session, 已配置好的会话对象.
    """
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    session.trust_env = False
    return session


def _build_empty_record(cas: str) -> Dict[str, Any]:
    """
    功能:
        构建标准输出骨架, 便于后续逐步填充抓取结果.
    参数:
        cas: str, CAS 号.
    返回:
        Dict[str, Any], 默认 JSON 结构.
    """
    return {
        "cas": cas,
        "status": "error",
        "source": "chemicalbook",
        "source_urls": {
            "cn": CHEMICALBOOK_CN_URL.format(cas=cas),
            "en": CHEMICALBOOK_EN_URL.format(cas=cas),
        },
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "normalized": _build_empty_normalized(cas),
        "sections": _build_empty_sections(),
        "debug": {
            "http_status_cn": None,
            "http_status_en": None,
            "blocked": False,
            "blocked_reason": "",
            "cache_hit_cn": False,
            "cache_hit_en": False,
            "raw_html_paths": {},
        },
    }


def _build_empty_sections() -> Dict[str, Dict[str, Any]]:
    """
    功能:
        构建默认 sections 容器.
    参数:
        无.
    返回:
        Dict[str, Dict[str, Any]], 默认 section 映射.
    """
    sections: Dict[str, Dict[str, Any]] = {}
    for section_name in DEFAULT_SECTIONS:
        sections[section_name] = {}
    return sections


def _build_empty_normalized(cas: str) -> Dict[str, Any]:
    """
    功能:
        构建标准化字段骨架.
    参数:
        cas: str, CAS 号.
    返回:
        Dict[str, Any], normalized 字段初始值.
    """
    return {
        "cn_name": "",
        "en_name": "",
        "aliases": [],
        "molecular_formula": "",
        "molecular_weight": None,
        "density": {"raw": "", "value": None, "unit": "g/mL"},
        "melting_point": {"raw": "", "value": None, "unit": "C"},
        "boiling_point": {"raw": "", "value": None, "unit": "C"},
        "flash_point": {"raw": "", "value": None, "unit": "C"},
        "appearance": "",
        "solubility": "",
        "storage_conditions": "",
        "hazard_statements": [],
        "safety_statements": [],
        "uses": [],
        "upstream_products": [],
        "downstream_products": [],
        "cas": cas,
    }


def _has_cache_miss(cas: str) -> bool:
    """
    功能:
        判断当前 CAS 是否存在缓存缺失, 用于决定是否需要执行限速等待.
    参数:
        cas: str, CAS 号.
    返回:
        bool, True 表示至少一个页面需要走网络抓取.
    """
    for page_type in PAGE_TYPES:
        cached_result = _load_cached_page(cas=cas, page_type=page_type)
        if cached_result is None:
            return True
    return False


def _apply_cas_rate_limit(cas: str) -> None:
    """
    功能:
        对同一 CAS 的连续抓取增加最小间隔, 降低触发反爬概率.
    参数:
        cas: str, CAS 号.
    返回:
        None.
    """
    last_request_at = _LAST_REQUEST_AT_BY_CAS.get(cas)
    if last_request_at is None:
        _LAST_REQUEST_AT_BY_CAS[cas] = time.time()
        return

    elapsed = time.time() - last_request_at
    if elapsed < DEFAULT_MIN_INTERVAL_S:
        wait_seconds = DEFAULT_MIN_INTERVAL_S - elapsed
        logger.info("ChemicalBook 抓取限速生效, CAS=%s, 等待 %.2f 秒", cas, wait_seconds)
        time.sleep(wait_seconds)

    _LAST_REQUEST_AT_BY_CAS[cas] = time.time()


def _fetch_page_with_cache(
    cas: str,
    page_type: str,
    timeout: float,
    session: Optional[requests.Session] = None,
) -> PageFetchResult:
    """
    功能:
        先尝试读取本地缓存, 失败后走网络抓取并更新缓存.
    参数:
        session: requests.Session, 已配置会话.
        cas: str, CAS 号.
        page_type: str, 页面类型, cn 或 en.
        timeout: float, 超时秒数.
    返回:
        PageFetchResult, 页面抓取结果.
    """
    storage.ensure_chemicalbook_data_layout()
    cached_result = _load_cached_page(cas=cas, page_type=page_type)
    if cached_result is not None:
        logger.info("ChemicalBook 命中缓存, CAS=%s, page=%s", cas, page_type)
        return cached_result

    if session is None:
        session = _build_session()

    url = _build_page_url(cas=cas, page_type=page_type)
    page_result = _fetch_page_with_playwright_direct(
        session=session,
        page_type=page_type,
        url=url,
        timeout=timeout,
        cas=cas,
    )

    if page_result.html is not None:
        _save_cached_page(cas=cas, page_type=page_type, result=page_result)
    return page_result


def _fetch_page_with_playwright_direct(
    session: requests.Session,
    page_type: str,
    url: str,
    timeout: float,
    cas: str = "",
) -> PageFetchResult:
    """
    功能:
        在缓存缺失时执行页面抓取.
        先尝试 requests, 若疑似触发反爬再切换到 playwright.
    参数:
        session: requests.Session, 已配置会话.
        page_type: str, 页面类型.
        url: str, 页面 URL.
        timeout: float, 超时秒数.
        cas: str, CAS 号, 用于补充请求头.
    返回:
        PageFetchResult, 页面抓取结果.
    """
    page_result = _fetch_page_from_network(
        session=session,
        page_type=page_type,
        url=url,
        timeout=timeout,
        cas=cas,
    )

    if page_result.blocked is True:
        logger.warning(
            "ChemicalBook 页面疑似触发反爬, CAS=%s, page=%s, reason=%s",
            cas,
            page_type,
            page_result.blocked_reason,
        )
        playwright_result = _fetch_page_with_playwright(
            page_type=page_type,
            url=url,
            timeout=timeout,
        )
        if playwright_result.html is not None and playwright_result.blocked is False:
            return playwright_result

    return page_result


def _fetch_page_from_network(
    session: requests.Session,
    page_type: str,
    url: str,
    timeout: float,
    cas: str = "",
) -> PageFetchResult:
    """
    功能:
        使用 requests 进行网络抓取, 包含重试, UA 轮换, 503 专项退避与 Referer 链.
    参数:
        session: requests.Session, 已配置会话.
        page_type: str, 页面类型.
        url: str, 页面 URL.
        timeout: float, 超时秒数.
        cas: str, CAS 号, 用于构建 Referer 链.
    返回:
        PageFetchResult, 页面抓取结果.
    """
    last_error_message = ""
    last_status_code = None

    for attempt in range(MAX_RETRIES):
        # 每次重试轮换 User-Agent, 降低指纹一致性
        session.headers["User-Agent"] = random.choice(_USER_AGENT_POOL)

        # 设置 Referer 链, 模拟自然浏览行为
        if page_type == "cn":
            session.headers["Referer"] = "https://www.chemicalbook.com/"
        elif cas != "":
            # EN 页通常从 CN 页跳转
            session.headers["Referer"] = CHEMICALBOOK_CN_URL.format(cas=cas)

        try:
            response = session.get(url, timeout=timeout)
            html = _decode_response_text(response)
            blocked, blocked_reason = _detect_blocked_page(html)
            result = PageFetchResult(
                page_type=page_type,
                url=url,
                status_code=response.status_code,
                html=html if response.status_code == 200 else None,
                blocked=blocked,
                blocked_reason=blocked_reason,
            )

            if response.status_code == 200:
                return result

            if response.status_code in (404, 410):
                logger.info("ChemicalBook 页面不存在, page=%s, url=%s", page_type, url)
                return result

            last_status_code = response.status_code
            last_error_message = f"HTTP {response.status_code}"
            logger.warning(
                "ChemicalBook 请求失败, page=%s, status=%s, attempt=%s, url=%s",
                page_type,
                response.status_code,
                attempt + 1,
                url,
            )
        except requests.RequestException as exc:
            last_error_message = str(exc)
            logger.warning(
                "ChemicalBook 请求异常, page=%s, attempt=%s, err=%s",
                page_type,
                attempt + 1,
                exc,
            )

        if attempt + 1 < MAX_RETRIES:
            if last_status_code == 503:
                # 503 通常意味着限速, 需要更长等待
                wait_seconds = (5 * (attempt + 1)) + random.uniform(1.0, 3.0)
            else:
                wait_seconds = (2 ** attempt) + random.uniform(0.5, 1.5)
            logger.debug("ChemicalBook 重试等待 %.2f 秒, attempt=%d", wait_seconds, attempt + 1)
            time.sleep(wait_seconds)

    return PageFetchResult(
        page_type=page_type,
        url=url,
        status_code=None,
        html=None,
        blocked=False,
        blocked_reason="",
        from_cache=False,
        error_message=last_error_message,
    )


def _fetch_page_with_playwright(
    page_type: str,
    url: str,
    timeout: float,
) -> PageFetchResult:
    """
    功能:
        当 requests 命中反爬页面时, 尝试使用 playwright 无头浏览器重新抓取.
    参数:
        page_type: str, 页面类型.
        url: str, 页面 URL.
        timeout: float, 超时秒数.
    返回:
        PageFetchResult, 浏览器抓取结果. 未安装 playwright 时返回 blocked 结果.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return PageFetchResult(
            page_type=page_type,
            url=url,
            status_code=None,
            html=None,
            blocked=True,
            blocked_reason="命中反爬页面, 且未安装 playwright",
            from_cache=False,
            error_message="playwright 未安装",
        )

    try:
        logger.info("使用 playwright 重试 ChemicalBook 页面, page=%s, url=%s", page_type, url)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            # 创建完整浏览器上下文, 模拟真实用户环境
            context = browser.new_context(
                user_agent=random.choice(_USER_AGENT_POOL),
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=int(timeout * 1000))
            # 模拟人类浏览: 页面加载后短暂等待
            page.wait_for_timeout(random.randint(1000, 2000))
            html = page.content()
            context.close()
            browser.close()
    except Exception as exc:  # pragma: no cover - 依赖外部浏览器
        return PageFetchResult(
            page_type=page_type,
            url=url,
            status_code=None,
            html=None,
            blocked=True,
            blocked_reason=f"playwright 抓取失败: {exc}",
            from_cache=False,
            error_message=str(exc),
        )

    blocked, blocked_reason = _detect_blocked_page(html)
    return PageFetchResult(
        page_type=page_type,
        url=url,
        status_code=200,
        html=html,
        blocked=blocked,
        blocked_reason=blocked_reason,
        from_cache=False,
    )


def _build_page_url(cas: str, page_type: str) -> str:
    """
    功能:
        根据 CAS 与页面类型生成 ChemicalBook URL.
    参数:
        cas: str, CAS 号.
        page_type: str, 页面类型, cn 或 en.
    返回:
        str, 页面 URL.
    """
    if page_type == "cn":
        return CHEMICALBOOK_CN_URL.format(cas=cas)
    return CHEMICALBOOK_EN_URL.format(cas=cas)


def _cache_file_paths(cas: str, page_type: str) -> Tuple[Path, Path]:
    """
    功能:
        计算缓存 HTML 与元数据文件路径.
    参数:
        cas: str, CAS 号.
        page_type: str, 页面类型.
    返回:
        Tuple[Path, Path], (html_path, meta_path).
    """
    storage.ensure_chemicalbook_data_layout()
    safe_cas = cas.replace("/", "_")
    html_path = storage.CHEMICALBOOK_CACHE_ROOT / f"{safe_cas}_{page_type}.html"
    meta_path = storage.CHEMICALBOOK_CACHE_ROOT / f"{safe_cas}_{page_type}.json"
    return html_path, meta_path


def _load_cached_page(cas: str, page_type: str) -> Optional[PageFetchResult]:
    """
    功能:
        从本地缓存读取页面内容. 超过 TTL 的缓存将被忽略.
    参数:
        cas: str, CAS 号.
        page_type: str, 页面类型.
    返回:
        Optional[PageFetchResult], 命中缓存返回结果, 否则返回 None.
    """
    html_path, meta_path = _cache_file_paths(cas=cas, page_type=page_type)
    if html_path.exists() is False or meta_path.exists() is False:
        return None

    age_seconds = time.time() - html_path.stat().st_mtime
    if age_seconds > DEFAULT_CACHE_TTL_S:
        return None

    try:
        html = html_path.read_text(encoding="utf-8")
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("ChemicalBook 读取缓存失败, CAS=%s, page=%s, err=%s", cas, page_type, exc)
        return None

    return PageFetchResult(
        page_type=page_type,
        url=str(metadata.get("url", _build_page_url(cas=cas, page_type=page_type))),
        status_code=metadata.get("status_code"),
        html=html,
        blocked=bool(metadata.get("blocked", False)),
        blocked_reason=str(metadata.get("blocked_reason", "")),
        from_cache=True,
        error_message=str(metadata.get("error_message", "")),
    )


def _save_cached_page(cas: str, page_type: str, result: PageFetchResult) -> None:
    """
    功能:
        将页面抓取结果写入本地缓存.
    参数:
        cas: str, CAS 号.
        page_type: str, 页面类型.
        result: PageFetchResult, 待缓存的页面结果.
    返回:
        None.
    """
    storage.ensure_chemicalbook_data_layout()
    storage.CHEMICALBOOK_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    html_path, meta_path = _cache_file_paths(cas=cas, page_type=page_type)
    metadata = {
        "url": result.url,
        "status_code": result.status_code,
        "blocked": result.blocked,
        "blocked_reason": result.blocked_reason,
        "error_message": result.error_message,
        "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    try:
        html_path.write_text(result.html or "", encoding="utf-8")
        meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("ChemicalBook 写入缓存失败, CAS=%s, page=%s, err=%s", cas, page_type, exc)


def _save_raw_html(cas: str, page_type: str, html: str) -> Path:
    """
    功能:
        将原始 HTML 保存到独立目录, 便于人工排查解析问题.
    参数:
        cas: str, CAS 号.
        page_type: str, 页面类型.
        html: str, 原始 HTML 文本.
    返回:
        Path, 保存后的文件路径.
    """
    storage.ensure_chemicalbook_data_layout()
    storage.CHEMICALBOOK_RAW_ROOT.mkdir(parents=True, exist_ok=True)
    raw_path = storage.CHEMICALBOOK_RAW_ROOT / f"{cas}_{page_type}.html"
    raw_path.write_text(html, encoding="utf-8")
    return raw_path


def _decode_response_text(response: requests.Response) -> str:
    """
    功能:
        优先根据响应头与 apparent_encoding 解码 HTML, 尽量避免中文乱码.
    参数:
        response: requests.Response, HTTP 响应对象.
    返回:
        str, 解码后的 HTML 文本.
    """
    declared_encoding = response.encoding
    if declared_encoding is None:
        content_type = str(response.headers.get("Content-Type", ""))
        meta_match = GBK_META_RE.search(content_type)
        if meta_match is not None:
            declared_encoding = meta_match.group(1)

    apparent_encoding = getattr(response, "apparent_encoding", None)
    if apparent_encoding is not None and len(str(apparent_encoding).strip()) > 0:
        declared_encoding = str(apparent_encoding)

    if declared_encoding is not None and len(str(declared_encoding).strip()) > 0:
        try:
            return response.content.decode(str(declared_encoding), errors="replace")
        except (LookupError, UnicodeDecodeError):
            pass

    try:
        return response.content.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return response.text


def _detect_blocked_page(html: str) -> Tuple[bool, str]:
    """
    功能:
        根据页面标题与正文关键词识别是否触发了反爬或安全验证页面.
    参数:
        html: str, HTML 文本.
    返回:
        Tuple[bool, str], (是否命中反爬, 原因说明).
    """
    text = _clean_text(_strip_html_tags(html)).lower()
    for keyword in BLOCK_KEYWORDS:
        if keyword.lower() in text:
            return True, f"页面包含反爬关键词: {keyword}"
    return False, ""


def _parse_chemicalbook_page(html: str, page_type: str) -> Dict[str, Any]:
    """
    功能:
        解析单个 ChemicalBook 页面, 输出 sections 与标签值对列表.
    参数:
        html: str, 页面 HTML.
        page_type: str, 页面类型, cn 或 en.
    返回:
        Dict[str, Any], 包含 sections 与 pairs.
    """
    if BeautifulSoup is None:
        return _parse_chemicalbook_page_fallback(html=html, page_type=page_type)

    soup = BeautifulSoup(html, "html.parser")
    _remove_noise_nodes(soup)

    sections = _build_empty_sections()
    pairs: List[Tuple[str, str]] = []

    title_text = _clean_text(soup.title.get_text(" ", strip=True)) if soup.title is not None else ""
    if len(title_text) > 0:
        _merge_section_value(sections["基本信息"], "页面标题", title_text)
        pairs.append(("页面标题", title_text))

    h1 = soup.find("h1")
    if h1 is not None:
        header_text = _clean_text(h1.get_text(" ", strip=True))
        if len(header_text) > 0:
            _merge_section_value(sections["基本信息"], "页面主标题", header_text)
            pairs.append(("页面主标题", header_text))

    if page_type == "cn":
        for label_text, value_text in _extract_cn_basic_summary_pairs(soup):
            pairs.append((label_text, value_text))
            _merge_section_value(sections["基本信息"], label_text, value_text)

    structured_result = _parse_structured_detail_container(soup=soup, page_type=page_type)
    if structured_result is not None:
        _merge_sections(sections, structured_result["sections"])
        pairs.extend(structured_result["pairs"])
        _extract_page_specific_title_info(page_type=page_type, pairs=pairs, sections=sections)
        return {
            "sections": sections,
            "pairs": pairs,
        }

    table_pairs = _extract_pairs_from_tables(soup)
    text_pairs = _extract_pairs_from_text_nodes(soup)
    definition_pairs = _extract_pairs_from_definitions(soup)

    seen_pairs = set()
    for label, value in table_pairs + definition_pairs + text_pairs:
        normalized_label = _normalize_text_for_match(label)
        normalized_value = _normalize_text_for_match(value)
        pair_key = (normalized_label, normalized_value)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        pairs.append((label, value))
        section_name = _classify_section_by_label(label)
        _merge_section_value(sections[section_name], label, value)

    heading_blocks = _extract_heading_blocks(soup)
    for section_name, block_data in heading_blocks.items():
        current_section = sections[section_name]
        text_blocks = block_data.get("text_blocks", [])
        if len(text_blocks) > 0:
            _merge_section_special_list(current_section, "_text_blocks", text_blocks)
        links = block_data.get("links", [])
        if len(links) > 0:
            _merge_section_special_list(current_section, "_links", links)

    _extract_named_product_lists(soup, sections)
    _extract_page_specific_title_info(page_type=page_type, pairs=pairs, sections=sections)

    return {
        "sections": sections,
        "pairs": pairs,
    }


def _parse_structured_detail_container(
    soup: BeautifulSoup,
    page_type: str,
) -> Optional[Dict[str, Any]]:
    """
    功能:
        优先解析 ChemicalBook 详情主容器, 避免将供应商报价和登录区误当作主数据.
    参数:
        soup: BeautifulSoup, 页面 DOM.
        page_type: str, 页面类型, cn 或 en.
    返回:
        Optional[Dict[str, Any]], 命中结构化详情容器时返回 sections 与 pairs, 否则返回 None.
    """
    if page_type == "cn":
        container = soup.find(id="SubClass")
        if container is not None:
            return _parse_cn_detail_container(container)
        return None

    container = soup.find(id="ContentPlaceHolder1_SubClass")
    if container is not None:
        return _parse_en_detail_container(container)
    return None


def _parse_cn_detail_container(container: Tag) -> Dict[str, Any]:
    """
    功能:
        解析中文页详情主容器中的 section 结构.
    参数:
        container: Tag, id=SubClass 的详情容器.
    返回:
        Dict[str, Any], 包含 sections 与 pairs.
    """
    sections = _build_empty_sections()
    pairs: List[Tuple[str, str]] = []

    section_blocks = container.find_all("div", class_="sxlist", recursive=False)
    for block in section_blocks:
        heading_tag = block.find("h2")
        if heading_tag is None:
            continue

        raw_heading = _clean_text(heading_tag.get_text(" ", strip=True))
        mapped_section = _map_cn_section_heading(raw_heading)
        if mapped_section is None:
            continue

        for text_label, text_value in _extract_cn_block_pairs(block):
            if len(text_label) == 0 or len(text_value) == 0:
                continue
            pairs.append((text_label, text_value))
            _merge_section_value(sections[mapped_section], text_label, text_value)
            if mapped_section == "用途与合成方法":
                _merge_section_special_list(sections[mapped_section], "_text_blocks", [text_value])

        if mapped_section == "上下游产品信息":
            upstream_items = _extract_cn_product_links(block=block, label_keywords=["上游产品", "上游原料"])
            downstream_items = _extract_cn_product_links(block=block, label_keywords=["下游产品"])
            if len(upstream_items) > 0:
                _merge_section_value(sections[mapped_section], "上游原料", upstream_items)
                for item in upstream_items:
                    pairs.append(("上游原料", item))
            if len(downstream_items) > 0:
                _merge_section_value(sections[mapped_section], "下游产品", downstream_items)
                for item in downstream_items:
                    pairs.append(("下游产品", item))
    return {
        "sections": sections,
        "pairs": pairs,
    }


def _extract_cn_basic_summary_pairs(soup: BeautifulSoup) -> List[Tuple[str, str]]:
    """
    功能:
        从中文页顶部 Basicsl 区域提取基础摘要键值对.
        该区域位于 SubClass 之前, 若只解析详情 section 会漏掉中文名称等核心字段.
    参数:
        soup: BeautifulSoup, 页面 DOM.
    返回:
        List[Tuple[str, str]], 摘要区域的键值对列表.
    """
    pairs: List[Tuple[str, str]] = []
    basics_container = soup.find("div", class_="Basicsl")
    if basics_container is None:
        return pairs

    seen_pairs = set()
    for row_tag in basics_container.find_all("div", recursive=False):
        label_tag = row_tag.find("span")
        if label_tag is None:
            continue

        label_text = _clean_text(label_tag.get_text(" ", strip=True))
        value_text = _extract_text_after_node(label_tag, row_tag)
        if len(label_text) == 0 or len(value_text) == 0:
            continue

        pair_key = (_normalize_text_for_match(label_text), _normalize_text_for_match(value_text))
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        pairs.append((label_text, value_text))

    return pairs


def _parse_en_detail_container(container: Tag) -> Dict[str, Any]:
    """
    功能:
        解析英文页详情主容器中的 section 表格结构.
    参数:
        container: Tag, id=ContentPlaceHolder1_SubClass 的详情容器.
    返回:
        Dict[str, Any], 包含 sections 与 pairs.
    """
    sections = _build_empty_sections()
    pairs: List[Tuple[str, str]] = []

    tables = container.find_all("table", recursive=False)
    for table in tables:
        raw_heading = _extract_en_table_heading(table)
        mapped_section = _map_en_section_heading(raw_heading)
        if mapped_section is None:
            continue

        rows = table.find_all("tr", recursive=False)
        for row_idx, row in enumerate(rows):
            if row_idx == 0:
                continue
            cells = row.find_all("td", recursive=False)
            if len(cells) == 0:
                continue
            content_cell = cells[0]
            label_tag = content_cell.find("font")
            if label_tag is None:
                continue
            label_text = _clean_text(_strip_en_bracket_label(label_tag.get_text(" ", strip=True)))
            value_text = _extract_text_after_node(label_tag, content_cell)
            if len(label_text) == 0 or len(value_text) == 0:
                continue

            pairs.append((label_text, value_text))
            _merge_section_value(sections[mapped_section], label_text, value_text)

            if mapped_section == "用途与合成方法":
                _merge_section_special_list(sections[mapped_section], "_text_blocks", [value_text])

            if mapped_section == "上下游产品信息":
                if "raw materials" in label_text.lower():
                    product_items = _collect_anchor_texts(content_cell)
                    if len(product_items) == 0:
                        product_items = _split_multi_value_text(value_text)
                    if len(product_items) > 0:
                        _merge_section_value(sections[mapped_section], "Raw materials", product_items)
                        for item in product_items:
                            pairs.append(("Raw materials", item))
                if "preparation products" in label_text.lower() or "downstream products" in label_text.lower():
                    product_items = _collect_anchor_texts(content_cell)
                    if len(product_items) == 0:
                        product_items = _split_multi_value_text(value_text)
                    if len(product_items) > 0:
                        _merge_section_value(sections[mapped_section], "Preparation Products", product_items)
                        for item in product_items:
                            pairs.append(("Preparation Products", item))

    return {
        "sections": sections,
        "pairs": pairs,
    }


def _map_cn_section_heading(heading: str) -> Optional[str]:
    """
    功能:
        将中文页 section 标题映射到统一输出 section.
    参数:
        heading: str, 中文页原始标题.
    返回:
        Optional[str], 目标 section 名称, None 表示忽略该标题.
    """
    if heading == "基本信息":
        return "基本信息"
    if heading == "物理化学性质":
        return "物理化学性质"
    if heading in ("安全数据", "化学品安全说明书(MSDS)", "毒性防护", "包装储运"):
        return "安全信息"
    if "安全特性" in heading or "毒性" in heading and "储运" in heading:
        return "安全信息"
    if heading in ("应用领域", "制备方法", "常见问题列表"):
        return "用途与合成方法"
    if heading == "上下游产品信息":
        return "上下游产品信息"
    return None


def _map_en_section_heading(heading: str) -> Optional[str]:
    """
    功能:
        将英文页 section 标题映射到统一输出 section.
    参数:
        heading: str, 英文页原始标题.
    返回:
        Optional[str], 目标 section 名称, None 表示忽略该标题.
    """
    lower_heading = heading.lower()
    if lower_heading == "identification":
        return "基本信息"
    if lower_heading == "chemical properties":
        return "物理化学性质"
    if lower_heading in ("hazard information", "safety data", "material safety data sheet(msds)"):
        return "安全信息"
    if lower_heading == "raw materials and preparation products":
        return "上下游产品信息"
    if lower_heading == "questions and answer":
        return "用途与合成方法"
    return None


def _extract_cn_block_pairs(block: Tag) -> List[Tuple[str, str]]:
    """
    功能:
        从中文页单个 sxlist 块中提取键值对.
    参数:
        block: Tag, 单个 sxlist 容器.
    返回:
        List[Tuple[str, str]], 该 section 提取出的键值对.
    """
    pairs: List[Tuple[str, str]] = []

    for property_row in block.select(".xztable .xztr"):
        label_tag = property_row.find("span")
        if label_tag is None:
            continue
        label_text = _clean_text(label_tag.get_text(" ", strip=True))
        value_text = _extract_text_after_node(label_tag, property_row)
        if len(label_text) == 0 or len(value_text) == 0:
            continue
        pairs.append((label_text, value_text))

    for content_block in block.find_all("div", class_="cwb", recursive=False):
        direct_children = [child for child in content_block.children if isinstance(child, Tag)]
        pending_label = ""
        for child in direct_children:
            classes = child.get("class", [])
            if "tbt" in classes:
                pending_label = _clean_text(child.get_text(" ", strip=True))
                continue
            if len(pending_label) == 0:
                continue
            value_text = _clean_text(child.get_text("\n", strip=True))
            if len(value_text) > 0:
                pairs.append((pending_label, value_text))
            pending_label = ""

    for msds_block in block.find_all("div", class_="MSDS", recursive=False):
        label_text = ""
        header_span = msds_block.find("span")
        if header_span is not None:
            label_text = _clean_text(header_span.get_text(" ", strip=True))
        if len(label_text) == 0:
            label_text = "MSDS 信息"
        for anchor_text in _collect_anchor_texts(msds_block):
            pairs.append((label_text, anchor_text))

    return pairs


def _extract_cn_product_links(block: Tag, label_keywords: List[str]) -> List[str]:
    """
    功能:
        从中文页上下游 section 中提取指定标题下的链接文本.
    参数:
        block: Tag, 上下游 section 容器.
        label_keywords: List[str], 标题关键词列表.
    返回:
        List[str], 产品名称列表.
    """
    product_items: List[str] = []
    for inner_block in block.find_all("div", class_="tyc", recursive=False):
        title_tag = inner_block.find("div", class_="tbt")
        if title_tag is None:
            continue
        title_text = _clean_text(title_tag.get_text(" ", strip=True))
        if not any(keyword in title_text for keyword in label_keywords):
            continue
        _extend_unique(product_items, _collect_anchor_texts(inner_block))
    return product_items


def _extract_en_table_heading(table: Tag) -> str:
    """
    功能:
        从英文页详情表格首行提取 section 标题.
    参数:
        table: Tag, 单个详情表格.
    返回:
        str, 标题文本.
    """
    header_anchor = table.find("a", attrs={"name": True})
    if header_anchor is not None:
        return _clean_text(header_anchor.get_text(" ", strip=True))

    first_font = table.find("font")
    if first_font is not None:
        return _clean_text(first_font.get_text(" ", strip=True))
    return ""


def _strip_en_bracket_label(label: str) -> str:
    """
    功能:
        去除英文页字段标签两侧的中括号.
    参数:
        label: str, 原始标签文本.
    返回:
        str, 清洗后的标签文本.
    """
    return label.strip("[] ")


def _extract_text_after_node(start_node: Tag, parent_node: Tag) -> str:
    """
    功能:
        提取父节点中指定子节点之后的文本, 保留换行分隔.
    参数:
        start_node: Tag, 起始标签节点.
        parent_node: Tag, 父节点.
    返回:
        str, 提取到的文本内容.
    """
    text_parts: List[str] = []
    for sibling in start_node.next_siblings:
        if isinstance(sibling, Tag):
            sibling_text = _clean_text(sibling.get_text("\n", strip=True))
        else:
            sibling_text = _clean_text(str(sibling))
        if len(sibling_text) > 0:
            text_parts.append(sibling_text)

    if len(text_parts) == 0:
        parent_text = _clean_text(parent_node.get_text("\n", strip=True))
        start_text = _clean_text(start_node.get_text(" ", strip=True))
        if parent_text.startswith(start_text):
            stripped = _clean_text(parent_text[len(start_text):])
            return stripped
        return parent_text

    return _clean_text("\n".join(text_parts))


def _collect_anchor_texts(node: Tag) -> List[str]:
    """
    功能:
        收集节点内所有链接文本并去重.
    参数:
        node: Tag, 任意 DOM 节点.
    返回:
        List[str], 链接文本列表.
    """
    anchor_texts: List[str] = []
    for anchor in node.find_all("a"):
        text = _clean_text(anchor.get_text(" ", strip=True))
        if len(text) > 0:
            anchor_texts.append(text)
    deduped: List[str] = []
    _extend_unique(deduped, anchor_texts)
    return deduped


def _extract_pairs_from_tables(soup: BeautifulSoup) -> List[Tuple[str, str]]:
    """
    功能:
        从 table 结构中提取 label/value 对.
    参数:
        soup: BeautifulSoup, 页面 DOM.
    返回:
        List[Tuple[str, str]], 表格提取出的键值对列表.
    """
    pairs: List[Tuple[str, str]] = []
    for row in soup.find_all("tr"):
        cells = []
        for cell in row.find_all(["th", "td"]):
            cell_text = _clean_text(cell.get_text(" ", strip=True))
            if len(cell_text) > 0:
                cells.append(cell_text)

        if len(cells) < 2:
            continue

        idx = 0
        while idx + 1 < len(cells):
            label = cells[idx]
            value = cells[idx + 1]
            if _looks_like_label(label) is True and len(value) > 0:
                pairs.append((label, value))
            idx += 2
    return pairs


def _extract_pairs_from_definitions(soup: BeautifulSoup) -> List[Tuple[str, str]]:
    """
    功能:
        从 dl/dt/dd 结构中提取 label/value 对.
    参数:
        soup: BeautifulSoup, 页面 DOM.
    返回:
        List[Tuple[str, str]], 定义列表提取结果.
    """
    pairs: List[Tuple[str, str]] = []
    for definition in soup.find_all("dl"):
        labels = definition.find_all("dt")
        values = definition.find_all("dd")
        pair_count = min(len(labels), len(values))
        for idx in range(pair_count):
            label_text = _clean_text(labels[idx].get_text(" ", strip=True))
            value_text = _clean_text(values[idx].get_text(" ", strip=True))
            if len(label_text) == 0 or len(value_text) == 0:
                continue
            pairs.append((label_text, value_text))
    return pairs


def _extract_pairs_from_text_nodes(soup: BeautifulSoup) -> List[Tuple[str, str]]:
    """
    功能:
        从列表项, 段落等文本节点中提取 label:value 形式的键值对.
    参数:
        soup: BeautifulSoup, 页面 DOM.
    返回:
        List[Tuple[str, str]], 文本节点提取出的键值对列表.
    """
    pairs: List[Tuple[str, str]] = []
    seen_texts = set()
    candidate_tags = soup.find_all(["li", "p", "span", "div"])

    for tag in candidate_tags:
        if tag.find(["table", "tr", "td", "th", "dl", "dt", "dd"]) is not None:
            continue

        text = _clean_text(tag.get_text(" ", strip=True))
        if len(text) == 0:
            continue

        normalized_text = _normalize_text_for_match(text)
        if normalized_text in seen_texts:
            continue
        seen_texts.add(normalized_text)

        pair = _split_label_value_text(text)
        if pair is None:
            continue
        pairs.append(pair)

    return pairs


def _extract_heading_blocks(soup: BeautifulSoup) -> Dict[str, Dict[str, List[str]]]:
    """
    功能:
        基于页面标题节点提取 section 文本块与链接列表.
    参数:
        soup: BeautifulSoup, 页面 DOM.
    返回:
        Dict[str, Dict[str, List[str]]], section 到文本块/链接的映射.
    """
    block_map: Dict[str, Dict[str, List[str]]] = {}
    for section_name in DEFAULT_SECTIONS:
        block_map[section_name] = {"text_blocks": [], "links": []}

    heading_tags = soup.find_all(["h1", "h2", "h3", "h4", "strong", "b", "span", "div", "td", "th"])
    for tag in heading_tags:
        heading_text = _clean_text(tag.get_text(" ", strip=True))
        if len(heading_text) == 0 or len(heading_text) > 40:
            continue

        matched_section = _match_section_by_heading(heading_text)
        if matched_section is None:
            continue

        base_node = tag.parent if isinstance(tag.parent, Tag) else tag
        current = base_node.next_sibling
        section_texts: List[str] = []
        section_links: List[str] = []
        sibling_count = 0

        while current is not None and sibling_count < 15:
            sibling_count += 1
            if isinstance(current, Tag) is False:
                current = current.next_sibling
                continue

            current_text = _clean_text(current.get_text(" ", strip=True))
            if len(current_text) > 0:
                next_section = _match_section_by_heading(current_text)
                if next_section is not None and next_section != matched_section and len(current_text) <= 40:
                    break
                if current_text != heading_text:
                    section_texts.append(current_text)

            for anchor in current.find_all("a"):
                anchor_text = _clean_text(anchor.get_text(" ", strip=True))
                if len(anchor_text) > 0:
                    section_links.append(anchor_text)

            current = current.next_sibling

        if len(section_texts) > 0:
            _extend_unique(block_map[matched_section]["text_blocks"], section_texts)
        if len(section_links) > 0:
            _extend_unique(block_map[matched_section]["links"], section_links)
    return block_map


def _extract_named_product_lists(soup: BeautifulSoup, sections: Dict[str, Dict[str, Any]]) -> None:
    """
    功能:
        从含有上游产品, 下游产品等标题的节点附近提取产品名称列表.
    参数:
        soup: BeautifulSoup, 页面 DOM.
        sections: Dict[str, Dict[str, Any]], 当前 sections 容器.
    返回:
        None.
    """
    product_heading_map = {
        "上游产品": "upstream_products",
        "上游原料": "upstream_products",
        "下游产品": "downstream_products",
        "Upstream products": "upstream_products",
        "Downstream products": "downstream_products",
    }

    for tag in soup.find_all(["h2", "h3", "h4", "strong", "b", "div", "span", "td"]):
        heading_text = _clean_text(tag.get_text(" ", strip=True))
        if len(heading_text) == 0 or len(heading_text) > 40:
            continue

        field_name = None
        for candidate, mapped_field in product_heading_map.items():
            if candidate.lower() in heading_text.lower():
                field_name = mapped_field
                break
        if field_name is None:
            continue

        section_name = FIELD_SECTION_MAP[field_name]
        product_items: List[str] = []
        base_node = tag.parent if isinstance(tag.parent, Tag) else tag
        current = base_node.next_sibling
        sibling_count = 0
        while current is not None and sibling_count < 10:
            sibling_count += 1
            if isinstance(current, Tag) is False:
                current = current.next_sibling
                continue

            current_text = _clean_text(current.get_text(" ", strip=True))
            if len(current_text) == 0:
                current = current.next_sibling
                continue

            matched_section = _match_section_by_heading(current_text)
            if matched_section is not None and len(current_text) <= 40:
                break

            for anchor in current.find_all("a"):
                anchor_text = _clean_text(anchor.get_text(" ", strip=True))
                if len(anchor_text) > 0:
                    product_items.append(anchor_text)

            if len(product_items) == 0:
                split_items = _split_multi_value_text(current_text)
                if len(split_items) > 0:
                    product_items.extend(split_items)

            current = current.next_sibling

        if len(product_items) > 0:
            _merge_section_value(sections[section_name], field_name, product_items)


def _extract_page_specific_title_info(
    page_type: str,
    pairs: List[Tuple[str, str]],
    sections: Dict[str, Dict[str, Any]],
) -> None:
    """
    功能:
        使用页面标题类字段补强中英文名称.
    参数:
        page_type: str, 页面类型.
        pairs: List[Tuple[str, str]], 当前页面键值对列表.
        sections: Dict[str, Dict[str, Any]], 当前 sections 容器.
    返回:
        None.
    """
    title_text = ""
    basic_section = sections["基本信息"]
    if "页面主标题" in basic_section:
        title_text = _coerce_to_text(basic_section["页面主标题"])
    elif "页面标题" in basic_section:
        title_text = _coerce_to_text(basic_section["页面标题"])

    if len(title_text) == 0:
        return

    if page_type == "cn":
        if re.search(r"[\u4e00-\u9fff]", title_text) is not None:
            pairs.append(("中文名称", title_text))
    else:
        cleaned_title = title_text.split("|")[0].split("CAS")[0].strip(" -")
        if len(cleaned_title) > 0:
            pairs.append(("Name", cleaned_title))


def _parse_chemicalbook_page_fallback(html: str, page_type: str) -> Dict[str, Any]:
    """
    功能:
        在未安装 beautifulsoup4 时, 使用标准库降级解析 HTML.
        该路径主要保证离线测试与基础抓取可运行, 解析稳定性低于 bs4 主路径.
    参数:
        html: str, 页面 HTML.
        page_type: str, 页面类型.
    返回:
        Dict[str, Any], 包含 sections 与 pairs.
    """
    sections = _build_empty_sections()
    pairs: List[Tuple[str, str]] = []

    title_text = _extract_first_tag_text(html=html, tag_name="title")
    if len(title_text) > 0:
        _merge_section_value(sections["基本信息"], "页面标题", title_text)
        pairs.append(("页面标题", title_text))

    h1_text = _extract_first_tag_text(html=html, tag_name="h1")
    if len(h1_text) > 0:
        _merge_section_value(sections["基本信息"], "页面主标题", h1_text)
        pairs.append(("页面主标题", h1_text))

    if page_type == "cn":
        for label_text, value_text in _fallback_extract_cn_basic_summary_pairs(html):
            pairs.append((label_text, value_text))
            _merge_section_value(sections["基本信息"], label_text, value_text)

    seen_pairs = set()
    extracted_pairs = []
    extracted_pairs.extend(_fallback_extract_table_pairs(html))
    extracted_pairs.extend(_fallback_extract_definition_pairs(html))
    extracted_pairs.extend(_fallback_extract_text_pairs(html))

    for label, value in extracted_pairs:
        pair_key = (_normalize_text_for_match(label), _normalize_text_for_match(value))
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        pairs.append((label, value))
        section_name = _classify_section_by_label(label)
        _merge_section_value(sections[section_name], label, value)

    heading_blocks = _fallback_extract_heading_blocks(html)
    for section_name, block_data in heading_blocks.items():
        if len(block_data["text_blocks"]) > 0:
            _merge_section_special_list(sections[section_name], "_text_blocks", block_data["text_blocks"])
        if len(block_data["links"]) > 0:
            _merge_section_special_list(sections[section_name], "_links", block_data["links"])

    _fallback_extract_named_product_lists(html=html, sections=sections)
    _extract_page_specific_title_info(page_type=page_type, pairs=pairs, sections=sections)
    return {
        "sections": sections,
        "pairs": pairs,
    }


def _extract_first_tag_text(html: str, tag_name: str) -> str:
    """
    功能:
        从 HTML 中提取指定标签的首个文本内容.
    参数:
        html: str, 页面 HTML.
        tag_name: str, 标签名称.
    返回:
        str, 标签文本.
    """
    pattern = re.compile(
        rf"<{tag_name}\b[^>]*>(.*?)</{tag_name}>",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(html)
    if match is None:
        return ""
    return _clean_text(html_lib.unescape(_strip_html_tags(match.group(1))))


def _fallback_extract_cn_basic_summary_pairs(html: str) -> List[Tuple[str, str]]:
    """
    功能:
        在降级解析路径中, 从中文页顶部 Basicsl 区域提取中文名称等基础摘要字段.
    参数:
        html: str, 页面 HTML.
    返回:
        List[Tuple[str, str]], 摘要区域的键值对列表.
    """
    pairs: List[Tuple[str, str]] = []
    block_match = re.search(
        r'<div class="Basicsl"[^>]*>(.*?)</div>\s*</div>',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if block_match is None:
        return pairs

    seen_pairs = set()
    row_pattern = re.compile(
        r"<div[^>]*>\s*<span>([^<]+)</span>(.*?)</div>",
        re.IGNORECASE | re.DOTALL,
    )
    for label_html, value_html in row_pattern.findall(block_match.group(1)):
        label_text = _clean_text(html_lib.unescape(_strip_html_tags(label_html)))
        value_text = _clean_text(html_lib.unescape(_strip_html_tags(value_html)))
        if len(label_text) == 0 or len(value_text) == 0:
            continue

        pair_key = (_normalize_text_for_match(label_text), _normalize_text_for_match(value_text))
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        pairs.append((label_text, value_text))

    return pairs


def _fallback_extract_table_pairs(html: str) -> List[Tuple[str, str]]:
    """
    功能:
        使用正则从 table/tr/td 结构中提取键值对.
    参数:
        html: str, 页面 HTML.
    返回:
        List[Tuple[str, str]], 表格中的键值对列表.
    """
    pairs: List[Tuple[str, str]] = []
    for row_html in re.findall(r"<tr\b[^>]*>.*?</tr>", html, re.IGNORECASE | re.DOTALL):
        cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row_html, re.IGNORECASE | re.DOTALL)
        cell_texts = []
        for cell_html in cells:
            cell_text = _clean_text(html_lib.unescape(_strip_html_tags(cell_html)))
            if len(cell_text) > 0:
                cell_texts.append(cell_text)

        if len(cell_texts) < 2:
            continue

        idx = 0
        while idx + 1 < len(cell_texts):
            label = cell_texts[idx]
            value = cell_texts[idx + 1]
            if _looks_like_label(label) is True and len(value) > 0:
                pairs.append((label, value))
            idx += 2
    return pairs


def _fallback_extract_definition_pairs(html: str) -> List[Tuple[str, str]]:
    """
    功能:
        使用正则从 dt/dd 结构中提取键值对.
    参数:
        html: str, 页面 HTML.
    返回:
        List[Tuple[str, str]], 定义列表中的键值对列表.
    """
    pairs: List[Tuple[str, str]] = []
    pattern = re.compile(
        r"<dt\b[^>]*>(.*?)</dt>\s*<dd\b[^>]*>(.*?)</dd>",
        re.IGNORECASE | re.DOTALL,
    )
    for label_html, value_html in pattern.findall(html):
        label_text = _clean_text(html_lib.unescape(_strip_html_tags(label_html)))
        value_text = _clean_text(html_lib.unescape(_strip_html_tags(value_html)))
        if len(label_text) == 0 or len(value_text) == 0:
            continue
        pairs.append((label_text, value_text))
    return pairs


def _fallback_extract_text_pairs(html: str) -> List[Tuple[str, str]]:
    """
    功能:
        使用正则从常见块级标签中提取 label:value 文本键值对.
    参数:
        html: str, 页面 HTML.
    返回:
        List[Tuple[str, str]], 提取出的键值对列表.
    """
    pairs: List[Tuple[str, str]] = []
    seen_texts = set()
    block_pattern = re.compile(
        r"<(li|p|span|div)\b[^>]*>(.*?)</\1>",
        re.IGNORECASE | re.DOTALL,
    )
    for _, block_html in block_pattern.findall(html):
        text = _clean_text(html_lib.unescape(_strip_html_tags(block_html)))
        if len(text) == 0:
            continue
        normalized = _normalize_text_for_match(text)
        if normalized in seen_texts:
            continue
        seen_texts.add(normalized)
        pair = _split_label_value_text(text)
        if pair is not None:
            pairs.append(pair)
    return pairs


def _fallback_extract_heading_blocks(html: str) -> Dict[str, Dict[str, List[str]]]:
    """
    功能:
        使用顺序块扫描的方式提取标题 section 与其后续文本.
    参数:
        html: str, 页面 HTML.
    返回:
        Dict[str, Dict[str, List[str]]], section 文本块与链接列表.
    """
    block_map: Dict[str, Dict[str, List[str]]] = {}
    for section_name in DEFAULT_SECTIONS:
        block_map[section_name] = {"text_blocks": [], "links": []}

    blocks = _fallback_iter_blocks(html)
    current_section = None
    for block in blocks:
        text = block["text"]
        matched_section = _match_section_by_heading(text)
        if matched_section is not None and len(text) <= 40:
            current_section = matched_section
            continue
        if current_section is None:
            continue
        if block["tag"] == "a":
            _extend_unique(block_map[current_section]["links"], [text])
        else:
            _extend_unique(block_map[current_section]["text_blocks"], [text])
    return block_map


def _fallback_extract_named_product_lists(html: str, sections: Dict[str, Dict[str, Any]]) -> None:
    """
    功能:
        在降级解析路径中提取上游与下游产品列表.
    参数:
        html: str, 页面 HTML.
        sections: Dict[str, Dict[str, Any]], 当前 sections 容器.
    返回:
        None.
    """
    blocks = _fallback_iter_blocks(html)
    current_field = None
    for block in blocks:
        text = block["text"]
        lower_text = text.lower()
        if "上游产品" in text or "上游原料" in text or "upstream products" in lower_text:
            current_field = "upstream_products"
            continue
        if "下游产品" in text or "downstream products" in lower_text:
            current_field = "downstream_products"
            continue
        if _match_section_by_heading(text) is not None and len(text) <= 40:
            current_field = None
            continue
        if current_field is None:
            continue

        values = []
        if block["tag"] == "a":
            values = [text]
        else:
            values = _split_multi_value_text(text)
            if len(values) == 1 and values[0] == text:
                whitespace_parts = [part.strip() for part in text.split() if len(part.strip()) > 0]
                if len(whitespace_parts) > 1:
                    values = whitespace_parts
        if len(values) > 0:
            section_name = FIELD_SECTION_MAP[current_field]
            _merge_section_value(sections[section_name], current_field, values)


def _fallback_iter_blocks(html: str) -> List[Dict[str, str]]:
    """
    功能:
        将常见 HTML 标签顺序展开为简单块列表, 供降级解析路径使用.
    参数:
        html: str, 页面 HTML.
    返回:
        List[Dict[str, str]], 顺序块列表.
    """
    blocks: List[Dict[str, str]] = []
    pattern = re.compile(
        r"<(h1|h2|h3|h4|div|p|li|span|a|td|th)\b[^>]*>(.*?)</\1>",
        re.IGNORECASE | re.DOTALL,
    )
    for tag_name, block_html in pattern.findall(html):
        text = _clean_text(html_lib.unescape(_strip_html_tags(block_html)))
        if len(text) == 0:
            continue
        blocks.append({"tag": tag_name.lower(), "text": text})
    return blocks


def _normalize_from_pairs_and_sections(
    cas: str,
    pairs: List[Tuple[str, str]],
    sections: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """
    功能:
        将原始标签值对与 section 内容标准化为稳定 JSON 字段.
    参数:
        cas: str, CAS 号.
        pairs: List[Tuple[str, str]], 原始标签值对.
        sections: Dict[str, Dict[str, Any]], 原始 section 数据.
    返回:
        Dict[str, Any], 标准化结果.
    """
    normalized = _build_empty_normalized(cas)
    alias_to_field = _build_label_to_field_map()

    for label, value in pairs:
        clean_label = _normalize_text_for_match(label)
        field_name = alias_to_field.get(clean_label)
        if field_name is None:
            continue

        clean_value = _coerce_to_text(value)
        if len(clean_value) == 0:
            continue

        if field_name in ("aliases", "hazard_statements", "safety_statements", "uses"):
            _extend_unique(normalized[field_name], _split_multi_value_text(clean_value))
            continue

        if field_name in ("upstream_products", "downstream_products"):
            _extend_unique(normalized[field_name], _split_multi_value_text(clean_value))
            continue

        if field_name in ("density", "melting_point", "boiling_point", "flash_point"):
            _update_measurement_field(field=normalized[field_name], raw_text=clean_value, field_name=field_name)
            continue

        if field_name == "molecular_weight":
            parsed_weight = _parse_first_float(clean_value)
            if parsed_weight is not None and normalized["molecular_weight"] is None:
                normalized["molecular_weight"] = parsed_weight
            continue

        if field_name in ("cn_name", "en_name", "molecular_formula", "appearance", "solubility", "storage_conditions"):
            if len(str(normalized[field_name]).strip()) == 0:
                normalized[field_name] = clean_value

    use_section = sections.get("用途与合成方法", {})
    _merge_text_block_field(normalized["uses"], use_section.get("_text_blocks"))

    relation_section = sections.get("上下游产品信息", {})
    _merge_text_block_field(normalized["upstream_products"], relation_section.get("upstream_products"))
    _merge_text_block_field(normalized["downstream_products"], relation_section.get("downstream_products"))

    relation_links = relation_section.get("_links")
    if isinstance(relation_links, list) is True:
        downstream_items = relation_section.get("downstream_products")
        if downstream_items is None:
            _merge_text_block_field(normalized["downstream_products"], relation_links)

    safety_section = sections.get("安全信息", {})
    _merge_text_block_field(normalized["hazard_statements"], safety_section.get("危险说明"))
    _merge_text_block_field(normalized["safety_statements"], safety_section.get("安全说明"))

    if len(normalized["en_name"]) == 0 and len(normalized["cn_name"]) > 0:
        title_from_basic = sections.get("基本信息", {}).get("页面标题")
        if title_from_basic is not None:
            title_text = _coerce_to_text(title_from_basic)
            english_candidate = re.split(r"[|｜]", title_text)[0].strip()
            if re.search(r"[A-Za-z]", english_candidate) is not None:
                normalized["en_name"] = english_candidate

    return normalized


def _update_measurement_field(field: Dict[str, Any], raw_text: str, field_name: str) -> None:
    """
    功能:
        更新数值型标准字段, 保留原始文本并尽量解析数值.
    参数:
        field: Dict[str, Any], 目标 measurement 字段.
        raw_text: str, 原始文本.
        field_name: str, 字段名称.
    返回:
        None.
    """
    if len(str(field.get("raw", "")).strip()) == 0:
        field["raw"] = raw_text

    if field.get("value") is not None:
        return

    if field_name == "density":
        value = _parse_density_value(raw_text)
        if value is not None:
            field["value"] = value
        return

    value = _parse_temperature_value(raw_text)
    if value is not None:
        field["value"] = value


def _collect_pairs_from_sections(sections: Dict[str, Any]) -> List[Tuple[str, str]]:
    """
    功能:
        从 sections 中回收可标准化的键值对, 供 normalize_chemicalbook_record 使用.
    参数:
        sections: Dict[str, Any], sections 数据.
    返回:
        List[Tuple[str, str]], 扁平化后的键值对列表.
    """
    pairs: List[Tuple[str, str]] = []
    for section_value in sections.values():
        if isinstance(section_value, dict) is False:
            continue
        for label, value in section_value.items():
            if label.startswith("_"):
                continue
            if isinstance(value, list):
                for item in value:
                    item_text = _coerce_to_text(item)
                    if len(item_text) > 0:
                        pairs.append((label, item_text))
            else:
                value_text = _coerce_to_text(value)
                if len(value_text) > 0:
                    pairs.append((label, value_text))
    return pairs


def _determine_record_status(record: Dict[str, Any], page_results: List[PageFetchResult]) -> str:
    """
    功能:
        根据页面抓取与解析结果确定最终状态.
    参数:
        record: Dict[str, Any], 当前结果记录.
        page_results: List[PageFetchResult], 页面抓取结果列表.
    返回:
        str, ok / parse_partial / not_found / blocked / error.
    """
    has_html = False
    has_not_found = False
    has_error = False
    has_blocked = False
    for page_result in page_results:
        if page_result.html is not None:
            has_html = True
        if page_result.status_code in (404, 410):
            has_not_found = True
        if page_result.blocked is True:
            has_blocked = True
        if page_result.error_message != "" and page_result.html is None and page_result.status_code is None:
            has_error = True

    normalized = record.get("normalized", {})
    normalized_hit_count = _count_normalized_hits(normalized)
    section_content_count = _count_section_content(record.get("sections", {}))

    if has_html is False and has_not_found is True:
        return "not_found"

    if has_html is False and has_blocked is True:
        return "blocked"

    if has_html is False and has_error is True:
        return "error"

    if normalized_hit_count == 0 and section_content_count == 0:
        if has_blocked is True:
            return "blocked"
        if has_not_found is True:
            return "not_found"
        return "error"

    missing_page_count = sum(1 for page_result in page_results if page_result.html is None)
    if missing_page_count > 0:
        return "parse_partial"

    if normalized_hit_count < 4:
        return "parse_partial"

    return "ok"


def _count_normalized_hits(normalized: Dict[str, Any]) -> int:
    """
    功能:
        统计 normalized 中命中的有效字段数量.
    参数:
        normalized: Dict[str, Any], 标准化结果.
    返回:
        int, 命中字段数.
    """
    hit_count = 0
    for field_name, value in normalized.items():
        if field_name == "cas":
            continue
        if isinstance(value, dict):
            raw_text = str(value.get("raw", "")).strip()
            numeric_value = value.get("value")
            if len(raw_text) > 0 or numeric_value is not None:
                hit_count += 1
            continue
        if isinstance(value, list):
            if len(value) > 0:
                hit_count += 1
            continue
        if value is not None and len(str(value).strip()) > 0:
            hit_count += 1
    return hit_count


def _count_section_content(sections: Dict[str, Any]) -> int:
    """
    功能:
        统计 sections 中包含内容的字段数量.
    参数:
        sections: Dict[str, Any], sections 结构.
    返回:
        int, 有效字段数量.
    """
    item_count = 0
    for section in sections.values():
        if isinstance(section, dict) is False:
            continue
        for value in section.values():
            if isinstance(value, list):
                if len(value) > 0:
                    item_count += 1
            elif value is not None and len(str(value).strip()) > 0:
                item_count += 1
    return item_count


def _remove_noise_nodes(soup: BeautifulSoup) -> None:
    """
    功能:
        移除 script, style 等无关节点, 降低解析噪声.
    参数:
        soup: BeautifulSoup, 页面 DOM.
    返回:
        None.
    """
    for tag in soup.find_all(["script", "style", "noscript", "iframe"]):
        tag.decompose()


def _classify_section_by_label(label: str) -> str:
    """
    功能:
        根据标签名将字段归类到预定义 section.
    参数:
        label: str, 原始标签名.
    返回:
        str, section 名称.
    """
    alias_to_field = _build_label_to_field_map()
    field_name = alias_to_field.get(_normalize_text_for_match(label))
    if field_name is None:
        return "基本信息"
    return FIELD_SECTION_MAP.get(field_name, "基本信息")


def _build_label_to_field_map() -> Dict[str, str]:
    """
    功能:
        将中英文标签别名构建为可直接查找的映射.
    参数:
        无.
    返回:
        Dict[str, str], 规范化标签到字段名的映射.
    """
    label_to_field: Dict[str, str] = {}
    for field_name, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            label_to_field[_normalize_text_for_match(alias)] = field_name
    return label_to_field


def _merge_sections(target: Dict[str, Dict[str, Any]], incoming: Dict[str, Dict[str, Any]]) -> None:
    """
    功能:
        将单页解析结果合并进最终 sections, 避免覆盖已有信息.
    参数:
        target: Dict[str, Dict[str, Any]], 目标 sections.
        incoming: Dict[str, Dict[str, Any]], 单页 sections.
    返回:
        None.
    """
    for section_name, values in incoming.items():
        if section_name not in target:
            target[section_name] = {}
        for label, value in values.items():
            if label.startswith("_"):
                _merge_section_special_list(target[section_name], label, value)
                continue
            _merge_section_value(target[section_name], label, value)


def _merge_section_value(section: Dict[str, Any], label: str, value: Any) -> None:
    """
    功能:
        向 section 中写入字段, 重复标签时自动合并为列表并去重.
    参数:
        section: Dict[str, Any], 目标 section.
        label: str, 标签名.
        value: Any, 标签值.
    返回:
        None.
    """
    if label not in section:
        section[label] = value
        return

    existing = section[label]
    merged_items: List[str] = []
    _extend_unique(merged_items, _coerce_to_list(existing))
    _extend_unique(merged_items, _coerce_to_list(value))
    section[label] = merged_items


def _merge_section_special_list(section: Dict[str, Any], key: str, values: Any) -> None:
    """
    功能:
        合并 section 中的特殊列表字段, 例如 _text_blocks 与 _links.
    参数:
        section: Dict[str, Any], 目标 section.
        key: str, 特殊字段名.
        values: Any, 待合并值.
    返回:
        None.
    """
    if key not in section:
        section[key] = []
    _extend_unique(section[key], _coerce_to_list(values))


def _match_section_by_heading(text: str) -> Optional[str]:
    """
    功能:
        根据标题文本匹配预定义 section.
    参数:
        text: str, 标题文本.
    返回:
        Optional[str], 命中的 section 名称, 未命中返回 None.
    """
    normalized = text.lower().strip()
    for section_name, keywords in SECTION_KEYWORDS.items():
        for keyword in keywords:
            if keyword.lower() in normalized:
                return section_name
    return None


def _merge_text_block_field(target_list: List[str], raw_value: Any) -> None:
    """
    功能:
        将单值或多值文本安全地合并到目标列表.
    参数:
        target_list: List[str], 目标列表.
        raw_value: Any, 待合并值.
    返回:
        None.
    """
    if raw_value is None:
        return
    if isinstance(raw_value, list):
        values = raw_value
    else:
        values = [raw_value]
    cleaned_values = []
    for item in values:
        item_text = _coerce_to_text(item)
        if len(item_text) > 0:
            cleaned_values.append(item_text)
    _extend_unique(target_list, cleaned_values)


def _looks_like_label(text: str) -> bool:
    """
    功能:
        判断一段文本是否更像属性标签而非普通段落.
    参数:
        text: str, 待判断文本.
    返回:
        bool, True 表示更像标签.
    """
    cleaned = _clean_text(text)
    if len(cleaned) == 0 or len(cleaned) > 40:
        return False

    if _normalize_text_for_match(cleaned) in _build_label_to_field_map():
        return True

    chinese_chars = re.findall(r"[\u4e00-\u9fff]", cleaned)
    if 0 < len(chinese_chars) <= 12:
        return True

    english_word_count = len(cleaned.split())
    if 0 < english_word_count <= 5:
        return True

    return False


def _split_label_value_text(text: str) -> Optional[Tuple[str, str]]:
    """
    功能:
        将 label:value 形式的文本拆分为键值对.
    参数:
        text: str, 原始文本.
    返回:
        Optional[Tuple[str, str]], 成功时返回键值对, 否则返回 None.
    """
    for separator in ("：", ":"):
        if separator not in text:
            continue
        label, value = text.split(separator, 1)
        label = _clean_text(label)
        value = _clean_text(value)
        if len(label) == 0 or len(value) == 0:
            continue
        if _looks_like_label(label) is False:
            continue
        return label, value
    return None


def _coerce_to_text(value: Any) -> str:
    """
    功能:
        将任意值转换为清洗后的字符串.
    参数:
        value: Any, 任意输入值.
    返回:
        str, 清洗后的字符串.
    """
    if value is None:
        return ""
    return _clean_text(str(value))


def _coerce_to_list(value: Any) -> List[str]:
    """
    功能:
        将单值或多值输入统一转为字符串列表.
    参数:
        value: Any, 任意输入值.
    返回:
        List[str], 字符串列表.
    """
    if value is None:
        return []
    if isinstance(value, list):
        result = []
        for item in value:
            item_text = _coerce_to_text(item)
            if len(item_text) > 0:
                result.append(item_text)
        return result

    value_text = _coerce_to_text(value)
    if len(value_text) == 0:
        return []
    return [value_text]


def _clean_text(text: str) -> str:
    """
    功能:
        清洗 HTML 提取出的文本, 压缩空白并去掉无意义分隔.
    参数:
        text: str, 原始文本.
    返回:
        str, 清洗后的文本.
    """
    replaced = text.replace("\xa0", " ").replace("\u3000", " ")
    replaced = replaced.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    replaced = re.sub(r"\s+", " ", replaced)
    return replaced.strip(" |:")


def _normalize_text_for_match(text: str) -> str:
    """
    功能:
        将标签或文本归一化, 便于做别名匹配与去重.
    参数:
        text: str, 原始文本.
    返回:
        str, 归一化后的文本.
    """
    cleaned = _clean_text(text).lower()
    cleaned = cleaned.replace("：", ":")
    cleaned = cleaned.replace("（", "(").replace("）", ")")
    cleaned = re.sub(r"[\s\-_()/\[\]{}|]+", "", cleaned)
    return cleaned


def _split_multi_value_text(text: str) -> List[str]:
    """
    功能:
        将多值文本拆分为稳定列表, 用于用途, 别名, 上下游等字段.
    参数:
        text: str, 原始文本.
    返回:
        List[str], 去重后的项目列表.
    """
    cleaned = _clean_text(text)
    if len(cleaned) == 0:
        return []

    if len(cleaned) > 120 and " " in cleaned and "。" not in cleaned and ";" not in cleaned:
        return [cleaned]

    parts = re.split(r"[;；,，/、|\n]+", cleaned)
    result: List[str] = []
    for part in parts:
        item = _clean_text(part)
        if len(item) == 0:
            continue
        if len(item) > 2 and item.lower() not in ("more", "details"):
            result.append(item)
    deduped: List[str] = []
    _extend_unique(deduped, result)
    return deduped


def _extend_unique(target: List[str], values: Iterable[str]) -> None:
    """
    功能:
        将一组字符串按出现顺序去重后合并到目标列表.
    参数:
        target: List[str], 目标列表.
        values: Iterable[str], 待追加值.
    返回:
        None.
    """
    existing_keys = {_normalize_text_for_match(item) for item in target}
    for value in values:
        text = _coerce_to_text(value)
        if len(text) == 0:
            continue
        normalized = _normalize_text_for_match(text)
        if normalized in existing_keys:
            continue
        existing_keys.add(normalized)
        target.append(text)


def _parse_first_float(text: str) -> Optional[float]:
    """
    功能:
        从文本中提取第一个浮点数.
    参数:
        text: str, 原始文本.
    返回:
        Optional[float], 提取成功返回数值, 否则返回 None.
    """
    match = FLOAT_RE.search(text)
    if match is None:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _parse_density_value(text: str) -> Optional[float]:
    """
    功能:
        解析密度文本中的数值, 默认按 g/mL 输出.
    参数:
        text: str, 原始密度文本.
    返回:
        Optional[float], 解析到的密度值.
    """
    parsed = _parse_first_float(text)
    if parsed is None:
        return None

    if 0.0 < parsed < 30.0:
        return round(parsed, 4)
    return None


def _parse_temperature_value(text: str) -> Optional[float]:
    """
    功能:
        解析温度类文本中的摄氏度数值. 遇到区间时返回均值.
    参数:
        text: str, 原始温度文本.
    返回:
        Optional[float], 解析到的摄氏度值.
    """
    range_match = CELSIUS_RANGE_RE.search(text)
    if range_match is not None:
        low = float(range_match.group(1))
        high = float(range_match.group(2))
        return round((low + high) / 2, 2)

    single_match = CELSIUS_RE.search(text)
    if single_match is not None:
        return round(float(single_match.group(1)), 2)

    range_f_match = FAHRENHEIT_RANGE_RE.search(text)
    if range_f_match is not None:
        low_f = float(range_f_match.group(1))
        high_f = float(range_f_match.group(2))
        avg_f = (low_f + high_f) / 2
        return round((avg_f - 32) * 5 / 9, 2)

    single_f_match = FAHRENHEIT_RE.search(text)
    if single_f_match is not None:
        fahrenheit = float(single_f_match.group(1))
        return round((fahrenheit - 32) * 5 / 9, 2)

    plain_value = _parse_first_float(text)
    if plain_value is not None and -250.0 <= plain_value <= 1000.0:
        return round(plain_value, 2)
    return None


def _strip_html_tags(html: str) -> str:
    """
    功能:
        粗略去除 HTML 标签, 用于反爬关键词识别.
    参数:
        html: str, 原始 HTML.
    返回:
        str, 近似纯文本.
    """
    return re.sub(r"<[^>]+>", " ", html)
