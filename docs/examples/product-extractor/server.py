"""
Product Extractor API — final version.

IMPORTANT SEMANTIC NOTE:
The schema field `originCode` (product.origin_code) is SEMANTICALLY
ship-from country, not manufacturing country. The name is legacy and kept
for API backward compatibility — do NOT rename it, downstream consumers
depend on this field name.

Throughout this file, "origin_code" refers to the ship-from country used
for shipping fee calculation.
"""

import json
import os
import re
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import uvicorn
from crawl4ai import (
    AsyncWebCrawler,
    BrowserConfig,
    CacheMode,
    CrawlerRunConfig,
    LLMConfig,
    LLMExtractionStrategy,
)
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI

from domain_learning import (
    DomainConfig,
    detect_render_type,
    extract_description_smart,
    extract_domain,
    extract_with_selector,
    fetch_html_direct,
    get_domain_config,
    learn_description_selector,
    save_domain_config,
)
from redis_client import close_redis, get_redis

# Single source of truth for URL/locale/marketplace logic
from reference import (
    _brand_default_shipfrom,
    _is_marketplace,
    build_instruction,
)
from reference import (
    _detect_locale_from_url as _detect_country_from_url,
)
from schemas import ExtractionRequest, ExtractionResponse, Product

# Local marketplaces (sellers predominantly ship from site locale).
# Listed here instead of in reference.py to avoid touching the shared file.
_LOCAL_MARKETPLACES = {
    "shopee.vn",
    "shopee.sg",
    "shopee.tw",
    "shopee.co.th",
    "shopee.com.my",
    "shopee.ph",
    "shopee.co.id",
    "lazada.vn",
    "lazada.sg",
    "lazada.co.th",
    "lazada.com.my",
    "tiki.vn",
    "sendo.vn",
    "mercari.jp",
    "mercari.com",
    "yahoo-auction.jp",
    "auctions.yahoo.co.jp",
    "rakuten.co.jp",
}


def _is_local_marketplace(hostname: str) -> bool:
    host = hostname.lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return any(host == m or host.endswith("." + m) for m in _LOCAL_MARKETPLACES)


load_dotenv()

# --- Config from env ---
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai/gpt-4o-mini")
LLM_API_TOKEN = os.getenv("LLM_API_TOKEN", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", None)
CACHE_TTL = int(os.getenv("CACHE_TTL", 3600))
EXTRACTION_CACHE_PREFIX = "extraction:"
MAX_LEARN_ATTEMPTS = 3


# --------------------------------------------------------------------------- #
# Browser locale — match URL country to avoid VN-locale leak
# --------------------------------------------------------------------------- #

_BROWSER_LOCALE_MAP = {
    "JP": ("ja-JP", "Asia/Tokyo"),
    "KR": ("ko-KR", "Asia/Seoul"),
    "CN": ("zh-CN", "Asia/Shanghai"),
    "TW": ("zh-TW", "Asia/Taipei"),
    "TH": ("th-TH", "Asia/Bangkok"),
    "VN": ("vi-VN", "Asia/Ho_Chi_Minh"),
    "SG": ("en-SG", "Asia/Singapore"),
    "MY": ("ms-MY", "Asia/Kuala_Lumpur"),
    "ID": ("id-ID", "Asia/Jakarta"),
    "PH": ("en-PH", "Asia/Manila"),
    "IN": ("en-IN", "Asia/Kolkata"),
    "AU": ("en-AU", "Australia/Sydney"),
    "US": ("en-US", "America/New_York"),
    "CA": ("en-CA", "America/Toronto"),
    "GB": ("en-GB", "Europe/London"),
    "DE": ("de-DE", "Europe/Berlin"),
    "FR": ("fr-FR", "Europe/Paris"),
    "IT": ("it-IT", "Europe/Rome"),
    "ES": ("es-ES", "Europe/Madrid"),
    "NL": ("nl-NL", "Europe/Amsterdam"),
    "BR": ("pt-BR", "America/Sao_Paulo"),
    "MX": ("es-MX", "America/Mexico_City"),
}


def _resolve_browser_locale(url: str) -> tuple[str, str]:
    """Match browser locale/timezone to URL country; fallback en-US/UTC."""
    country = _detect_country_from_url(url)
    return _BROWSER_LOCALE_MAP.get(country or "", ("en-US", "UTC"))


# --------------------------------------------------------------------------- #
# origin_code (= ship-from) sanitizer
# --------------------------------------------------------------------------- #

# Explicit ship-from text in HTML
_EXPLICIT_SHIP_FROM_MARKERS = (
    "ships from",
    "ship from",
    "dispatched from",
    "dispatches from",
    "item location",
    "located in",
    "warehouse:",
    "seller location",
    "sold by",
    "sold and shipped by",
    "発送元",
    "出荷元",
    "配送元",
    "発送地",
    "店舗所在地",
    "发货地",
    "发貨地",
    "發貨地",
    "giao từ",
    "gửi từ",
    "địa chỉ gửi hàng",
    "vận chuyển từ",
    "배송지",
    "발송지",
    "출고지",
    "จัดส่งจาก",
    "ส่งจาก",
    "dikirim dari",
    "versand aus",
    "versendet aus",
    "expédié de",
    "expédié depuis",
    "envía desde",
    "se envía desde",
    "spedito da",
)

# Local seller signals on local marketplaces
_LOCAL_SELLER_MARKERS = (
    "official",
    "mall",
    "chính hãng",
    "official store",
    "官方",
    "正規品",
    "공식",
)

# Cross-border flags — override local-seller assumption
_CROSSBORDER_MARKERS = (
    "overseas",
    "cross-border",
    "international shipping",
    "海外",
    "国际",
    "國際",
    "海外直邮",
    "海外直送",
    "hàng nhập khẩu",
    "hàng quốc tế",
)


def _sanitize_origin_code(
    origin_code: str | None, html: str, url: str
) -> tuple[str | None, str]:
    """Validate origin_code (which is ship-from country) against HTML.

    Returns (code_or_none, reason_tag) where reason_tag is used for logging.
    """
    if not origin_code:
        return None, "already_null"

    code = origin_code.strip().upper()
    html_lower = html.lower()
    hostname = (urlparse(url).hostname or "").lower().lstrip(".")
    if hostname.startswith("www."):
        hostname = hostname[4:]

    has_explicit = any(m in html_lower for m in _EXPLICIT_SHIP_FROM_MARKERS)
    has_local_signal = any(m in html_lower for m in _LOCAL_SELLER_MARKERS)
    has_crossborder_signal = any(m in html_lower for m in _CROSSBORDER_MARKERS)
    site_locale = _detect_country_from_url(url)
    is_local_mp = _is_local_marketplace(hostname)
    is_any_mp = _is_marketplace(hostname)
    is_crossborder_mp = is_any_mp and not is_local_mp
    brand_default = _brand_default_shipfrom(hostname)

    # A. Explicit ship-from text in HTML → trust LLM
    if has_explicit:
        return code, "explicit_text"

    # B. Known brand default match (shein→CN, etc)
    if brand_default and code == brand_default:
        return code, "brand_default"

    # C/D. Local marketplace (Shopee/Lazada/Tiki/Rakuten)
    if is_local_mp and code == site_locale:
        if has_crossborder_signal:
            return None, "local_mp_but_crossborder_marker"
        if has_local_signal:
            return code, "local_mp_local_signal"
        return code, "local_mp_default"

    # E. Single-brand site: LLM returned site locale
    if not is_any_mp and site_locale and code == site_locale:
        return code, "single_brand_locale"

    # F. Suspicious VN on non-VN site (classic "Ship TO Vietnam" confusion)
    if code == "VN" and site_locale and site_locale != "VN":
        return None, "suspicious_vn_banner"

    # G. Crossborder marketplace with no evidence → too risky
    if is_crossborder_mp:
        return None, "crossborder_no_evidence"

    # H. Trust with low confidence
    return code, "fallback_trust"


# --------------------------------------------------------------------------- #
# Guaranteed origin_code fallback chain (never returns None)
# --------------------------------------------------------------------------- #

# Best-effort ship-from by hostname for sites where the logic above may still
# yield null (non-local marketplaces + missing evidence, generic .com, etc).
# Listed only when ship-from is overwhelmingly one country.
_HOSTNAME_DEFAULT_SHIPFROM = {
    # Chinese marketplaces / dropshippers
    "aliexpress.com": "CN",
    "aliexpress.us": "CN",
    "taobao.com": "CN",
    "tmall.com": "CN",
    "1688.com": "CN",
    "shein.com": "CN",
    "temu.com": "CN",
    "banggood.com": "CN",
    "dhgate.com": "CN",
    "made-in-china.com": "CN",
    "yupoo.com": "CN",
    # Japan
    "rakuten.co.jp": "JP",
    "yahoo.co.jp": "JP",
    "mercari.jp": "JP",
    "zozo.jp": "JP",
    "zozotown.jp": "JP",
    "buyee.jp": "JP",
    "amazon.co.jp": "JP",
    "uniqlo.com": "JP",  # default, overridden by path locale below
    "muji.com": "JP",
    "yodobashi.com": "JP",
    "bic-camera.com": "JP",
    # Korea
    "coupang.com": "KR",
    "gmarket.co.kr": "KR",
    "11st.co.kr": "KR",
    "naver.com": "KR",
    # US
    "amazon.com": "US",
    "ebay.com": "US",
    "walmart.com": "US",
    "target.com": "US",
    "bestbuy.com": "US",
    "nordstrom.com": "US",
    "macys.com": "US",
    "nike.com": "US",
    "apple.com": "US",
    "etsy.com": "US",
    # UK
    "amazon.co.uk": "GB",
    "ebay.co.uk": "GB",
    "asos.com": "GB",
    "next.co.uk": "GB",
    # Germany
    "amazon.de": "DE",
    "otto.de": "DE",
    "zalando.de": "DE",
    # France
    "amazon.fr": "FR",
    "cdiscount.com": "FR",
    "fnac.com": "FR",
    # Taiwan
    "shopee.tw": "TW",
    "pchome.com.tw": "TW",
    "momoshop.com.tw": "TW",
    # Thailand
    "shopee.co.th": "TH",
    "lazada.co.th": "TH",
    # Singapore
    "shopee.sg": "SG",
    "lazada.sg": "SG",
    "qoo10.sg": "SG",
    # Vietnam
    "shopee.vn": "VN",
    "lazada.vn": "VN",
    "tiki.vn": "VN",
    "sendo.vn": "VN",
    "thegioididong.com": "VN",
    "dienmayxanh.com": "VN",
    "fptshop.com.vn": "VN",
}


def _hostname_default(hostname: str) -> str | None:
    """Look up a best-effort ship-from by hostname suffix."""
    host = hostname.lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    for known, code in _HOSTNAME_DEFAULT_SHIPFROM.items():
        if host == known or host.endswith("." + known):
            return code
    return None


# TLD-only fallback. Last-resort guess based on domain extension alone.
_TLD_FALLBACK = {
    ".vn": "VN",
    ".jp": "JP",
    ".co.jp": "JP",
    ".cn": "CN",
    ".com.cn": "CN",
    ".kr": "KR",
    ".co.kr": "KR",
    ".tw": "TW",
    ".com.tw": "TW",
    ".hk": "HK",
    ".com.hk": "HK",
    ".th": "TH",
    ".co.th": "TH",
    ".sg": "SG",
    ".com.sg": "SG",
    ".my": "MY",
    ".com.my": "MY",
    ".id": "ID",
    ".co.id": "ID",
    ".ph": "PH",
    ".com.ph": "PH",
    ".in": "IN",
    ".co.in": "IN",
    ".au": "AU",
    ".com.au": "AU",
    ".co.uk": "GB",
    ".uk": "GB",
    ".de": "DE",
    ".fr": "FR",
    ".it": "IT",
    ".es": "ES",
    ".nl": "NL",
    ".be": "BE",
    ".ch": "CH",
    ".at": "AT",
    ".se": "SE",
    ".no": "NO",
    ".dk": "DK",
    ".fi": "FI",
    ".pl": "PL",
    ".pt": "PT",
    ".ca": "CA",
    ".mx": "MX",
    ".br": "BR",
    ".ru": "RU",
    ".ae": "AE",
}


def _tld_fallback(hostname: str) -> str | None:
    host = hostname.lower().rstrip(".")
    # Compound TLDs first (.co.jp before .jp)
    for tld in sorted(_TLD_FALLBACK, key=len, reverse=True):
        if host.endswith(tld):
            return _TLD_FALLBACK[tld]
    return None


# --------------------------------------------------------------------------- #
# HTML-based country detection — generic parsers
# --------------------------------------------------------------------------- #

# ISO 639-1 language code → default country (used when lang tag has no region).
# Only include languages predominantly spoken in one country. Ambiguous ones
# (en, es, pt, zh, ar, fr, de) are intentionally omitted — for those we rely
# on the region subtag (en-GB, zh-TW, pt-BR, ...).
_LANG_TO_COUNTRY = {
    "vi": "VN",
    "ja": "JP",
    "ko": "KR",
    "th": "TH",
    "id": "ID",
    "tl": "PH",
    "ms": "MY",
    "hi": "IN",
    "bn": "BD",
    "ur": "PK",
    "tr": "TR",
    "el": "GR",
    "he": "IL",
    "fa": "IR",
    "pl": "PL",
    "cs": "CZ",
    "hu": "HU",
    "ro": "RO",
    "uk": "UA",
    "ru": "RU",
    "bg": "BG",
    "hr": "HR",
    "sr": "RS",
    "sk": "SK",
    "sl": "SI",
    "da": "DK",
    "fi": "FI",
    "no": "NO",
    "sv": "SE",
    "is": "IS",
    "et": "EE",
    "lv": "LV",
    "lt": "LT",
    "it": "IT",
    "nl": "NL",  # mostly safe defaults
}

# Currency code → country. Inverted from reference._NATIVE_CURRENCY, plus more.
# Shared currencies (EUR, USD) resolve to their most common single country
# only as a last resort; prefer lang/og:locale/addressCountry when available.
_CURRENCY_TO_COUNTRY = {
    "VND": "VN",
    "JPY": "JP",
    "KRW": "KR",
    "THB": "TH",
    "IDR": "ID",
    "MYR": "MY",
    "SGD": "SG",
    "PHP": "PH",
    "TWD": "TW",
    "HKD": "HK",
    "INR": "IN",
    "AUD": "AU",
    "CAD": "CA",
    "NZD": "NZ",
    "CNY": "CN",
    "RMB": "CN",
    "GBP": "GB",
    "CHF": "CH",
    "SEK": "SE",
    "NOK": "NO",
    "DKK": "DK",
    "PLN": "PL",
    "CZK": "CZ",
    "HUF": "HU",
    "RON": "RO",
    "BGN": "BG",
    "HRK": "HR",
    "RSD": "RS",
    "UAH": "UA",
    "RUB": "RU",
    "TRY": "TR",
    "ILS": "IL",
    "AED": "AE",
    "SAR": "SA",
    "EGP": "EG",
    "ZAR": "ZA",
    "BRL": "BR",
    "MXN": "MX",
    "ARS": "AR",
    "CLP": "CL",
    "COP": "CO",
    "PEN": "PE",
    # Ambiguous — only accepted when no stronger signal present
    "USD": "US",
    "EUR": "DE",
}

# Country name → ISO. Covers English + a few local self-names.
_COUNTRY_NAME_TO_CODE = {
    "vietnam": "VN",
    "viet nam": "VN",
    "việt nam": "VN",
    "japan": "JP",
    "nihon": "JP",
    "nippon": "JP",
    "日本": "JP",
    "korea": "KR",
    "south korea": "KR",
    "republic of korea": "KR",
    "한국": "KR",
    "china": "CN",
    "中国": "CN",
    "prc": "CN",
    "taiwan": "TW",
    "台灣": "TW",
    "台湾": "TW",
    "hong kong": "HK",
    "hongkong": "HK",
    "香港": "HK",
    "thailand": "TH",
    "ประเทศไทย": "TH",
    "indonesia": "ID",
    "malaysia": "MY",
    "singapore": "SG",
    "philippines": "PH",
    "india": "IN",
    "pakistan": "PK",
    "united states": "US",
    "usa": "US",
    "u.s.a.": "US",
    "america": "US",
    "united kingdom": "GB",
    "uk": "GB",
    "britain": "GB",
    "england": "GB",
    "germany": "DE",
    "deutschland": "DE",
    "france": "FR",
    "italy": "IT",
    "italia": "IT",
    "spain": "ES",
    "españa": "ES",
    "netherlands": "NL",
    "holland": "NL",
    "portugal": "PT",
    "belgium": "BE",
    "switzerland": "CH",
    "austria": "AT",
    "sweden": "SE",
    "norway": "NO",
    "denmark": "DK",
    "finland": "FI",
    "poland": "PL",
    "russia": "RU",
    "ukraine": "UA",
    "turkey": "TR",
    "israel": "IL",
    "saudi arabia": "SA",
    "united arab emirates": "AE",
    "uae": "AE",
    "australia": "AU",
    "new zealand": "NZ",
    "canada": "CA",
    "brazil": "BR",
    "brasil": "BR",
    "mexico": "MX",
    "méxico": "MX",
    "argentina": "AR",
    "chile": "CL",
    "colombia": "CO",
}

# Regex shared across parsers.
_LANG_ATTR_RE = re.compile(
    r"""<html\b[^>]*\blang\s*=\s*["']([a-zA-Z]{2,3}(?:[-_][a-zA-Z0-9]{2,4})?)["']""",
    re.I,
)
_OG_LOCALE_RE = re.compile(
    r"""["']og:locale["'][^>]*?\bcontent\s*=\s*["']([a-zA-Z]{2,3}(?:[-_][a-zA-Z0-9]{2,4})?)["']""",
    re.I,
)
_PRICE_CURRENCY_RE = re.compile(r'"priceCurrency"\s*:\s*"([A-Z]{3})"')
_ADDRESS_COUNTRY_RE = re.compile(r'"addressCountry"\s*:\s*"([^"]+)"', re.I)


def _locale_to_country(locale: str) -> str | None:
    """Parse a BCP-47-ish locale string (e.g. 'en-US', 'zh_TW', 'vi') into ISO alpha-2."""
    if not locale:
        return None
    locale = locale.strip()
    # Region subtag takes priority: 'en-GB' → GB, 'zh_TW' → TW, 'pt-br' → BR.
    parts = re.split(r"[-_]", locale)
    if len(parts) >= 2:
        region = parts[1].upper()
        if len(region) == 2 and region.isalpha():
            return region
    # Fall back to language → default country mapping.
    return _LANG_TO_COUNTRY.get(parts[0].lower())


def _detect_country_from_html(html: str) -> tuple[str | None, str]:
    """Detect site country from HTML content. Returns (code, signal_name).

    Signals tried in order (strongest first):
      1. <html lang="xx-YY">  → region subtag or default country of language
      2. og:locale meta       → same parsing
      3. JSON-LD priceCurrency → currency-to-country map
      4. JSON-LD addressCountry → ISO code or country name
    """
    if not html:
        return None, "no_html"
    head = html[:50000]  # <html>, <head>, JSON-LD almost always in first 50KB

    # 1. <html lang="...">
    m = _LANG_ATTR_RE.search(head)
    if m:
        code = _locale_to_country(m.group(1))
        if code:
            return code, "html_lang"

    # 2. og:locale
    m = _OG_LOCALE_RE.search(head)
    if m:
        code = _locale_to_country(m.group(1))
        if code:
            return code, "og_locale"

    # 3. JSON-LD priceCurrency — skip ambiguous USD/EUR if no other signal
    for m in _PRICE_CURRENCY_RE.finditer(head):
        code = _CURRENCY_TO_COUNTRY.get(m.group(1).upper())
        if code:
            return code, "price_currency"

    # 4. JSON-LD addressCountry — accept ISO code directly or country name
    m = _ADDRESS_COUNTRY_RE.search(head)
    if m:
        val = m.group(1).strip()
        if len(val) == 2 and val.isalpha():
            return val.upper(), "address_country"
        code = _COUNTRY_NAME_TO_CODE.get(val.lower())
        if code:
            return code, "address_country"

    return None, "none"


def _guarantee_origin_code(
    current: str | None, html: str | None, url: str
) -> tuple[str, str]:
    """Never-null ship-from resolver. Returns (code, reason).

    Strategy (tries in order, returns first hit):
      1. Keep LLM value if already set.
      2. URL path/TLD locale (uniqlo.com/jp/ → JP, shopee.vn → VN).
      3. Hostname default table (amazon.com → US, rakuten → JP, ...).
      4. HTML content signals (lang attr, og:locale, JSON-LD priceCurrency /
         addressCountry). Catches local shops on generic .com TLDs.
      5. TLD-only fallback (.de → DE, .fr → FR).
      6. LAST RESORT: "US".
    """
    if current:
        return current, "llm"

    locale = _detect_country_from_url(url)
    if locale:
        return locale, "url_locale"

    hostname = (urlparse(url).hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]

    host_default = _hostname_default(hostname)
    if host_default:
        return host_default, "hostname_default"

    html_country, signal = _detect_country_from_html(html or "")
    if html_country:
        return html_country, f"html_{signal}"

    tld_guess = _tld_fallback(hostname)
    if tld_guess:
        return tld_guess, "tld_fallback"

    return "US", "last_resort"


# --------------------------------------------------------------------------- #
# LLM output parsing (robust to markdown fences)
# --------------------------------------------------------------------------- #

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _parse_llm_json(raw) -> dict | None:
    """Parse LLM output into a single product dict. Handles fences and lists."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return raw[0] if raw else None
    if isinstance(raw, str):
        text = _JSON_FENCE_RE.sub("", raw.strip())
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                return None
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        if isinstance(parsed, list):
            return parsed[0] if parsed else None
        return parsed if isinstance(parsed, dict) else None
    return None


# --------------------------------------------------------------------------- #
# URL normalization for cache keys
# --------------------------------------------------------------------------- #

_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "ref",
    "referrer",
    "spm",
    "_ga",
    "mc_cid",
    "mc_eid",
}


def _normalize_url(url: str) -> str:
    """Strip tracking params so cache keys dedupe trivially-different URLs."""
    parsed = urlparse(url)
    kept = [(k, v) for k, v in parse_qsl(parsed.query) if k not in _TRACKING_PARAMS]
    return urlunparse(parsed._replace(query=urlencode(kept), fragment=""))


# --------------------------------------------------------------------------- #
# Shared crawler config
# --------------------------------------------------------------------------- #


def _make_crawl_config(
    url: str,
    extraction_strategy: LLMExtractionStrategy | None = None,
) -> CrawlerRunConfig:
    """Build CrawlerRunConfig with locale-matched browser."""
    locale, tz = _resolve_browser_locale(url)
    return CrawlerRunConfig(
        extraction_strategy=extraction_strategy,
        cache_mode=CacheMode.BYPASS,
        wait_for="js:() => document.readyState === 'complete'",
        delay_before_return_html=5.0,
        page_timeout=45000,
        locale=locale,
        timezone_id=tz,
    )


# --------------------------------------------------------------------------- #
# Redis cache
# --------------------------------------------------------------------------- #


async def cache_get(url: str) -> ExtractionResponse | None:
    try:
        redis = await get_redis()
        data = await redis.get(f"{EXTRACTION_CACHE_PREFIX}{_normalize_url(url)}")
        if data:
            return ExtractionResponse(**json.loads(data))
    except Exception:
        pass
    return None


async def cache_set(url: str, response: ExtractionResponse) -> None:
    try:
        redis = await get_redis()
        await redis.setex(
            f"{EXTRACTION_CACHE_PREFIX}{_normalize_url(url)}",
            CACHE_TTL,
            response.model_dump_json(by_alias=True),
        )
    except Exception:
        pass


async def cache_delete(url: str) -> bool:
    try:
        redis = await get_redis()
        return (
            await redis.delete(f"{EXTRACTION_CACHE_PREFIX}{_normalize_url(url)}")
        ) > 0
    except Exception:
        return False


async def cache_clear_all() -> int:
    try:
        redis = await get_redis()
        keys = [k async for k in redis.scan_iter(f"{EXTRACTION_CACHE_PREFIX}*")]
        return await redis.delete(*keys) if keys else 0
    except Exception:
        return 0


# --------------------------------------------------------------------------- #
# Background: learn domain selector (reuses existing HTML, no extra crawl)
# --------------------------------------------------------------------------- #


async def background_learn_domain(url: str, html: str) -> None:
    domain = extract_domain(url)
    config = await get_domain_config(domain)
    if config and config.description_selector:
        return
    if config and config.learn_attempts >= MAX_LEARN_ATTEMPTS:
        print(
            f"[Learning] Skip {domain}: max attempts reached ({config.learn_failure_reason})"
        )
        return

    render_type = detect_render_type(html)
    current = config.learn_attempts if config else 0
    learned = await learn_description_selector(html, url)

    if learned and learned.get("selector"):
        new_config = DomainConfig(
            domain=domain,
            render_type=render_type,
            description_selector=learned.get("selector"),
            description_xpath=learned.get("xpath"),
            success_count=0,
            fail_count=0,
            learn_attempts=current + 1,
            learn_failure_reason=None,
        )
        await save_domain_config(new_config)
        print(f"[Learning] Learned {domain}: {learned['selector']}")
        return

    reason = learned.get("reasoning") if learned else "LLM returned no selector"
    new_config = DomainConfig(
        domain=domain,
        render_type=render_type,
        description_selector=None,
        description_xpath=None,
        success_count=0,
        fail_count=0,
        learn_attempts=current + 1,
        learn_failure_reason=reason,
    )
    await save_domain_config(new_config)
    level = "FAILED" if new_config.learn_attempts >= MAX_LEARN_ATTEMPTS else "Retry"
    print(
        f"[Learning] {level} {domain} ({new_config.learn_attempts}/{MAX_LEARN_ATTEMPTS}): {reason}"
    )


# --------------------------------------------------------------------------- #
# App lifecycle
# --------------------------------------------------------------------------- #

_crawler: AsyncWebCrawler | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _crawler
    _crawler = AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False))
    await _crawler.start()
    yield
    await _crawler.close()
    await close_redis()
    _crawler = None


app = FastAPI(
    title="Product Extractor API",
    description="Extract structured product data for proxy shopping.",
    version="2.1.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# HTML fetch helper
# --------------------------------------------------------------------------- #


async def get_html_smart(url: str) -> tuple[str | None, str]:
    """Try direct fetch for known SSR domains, fall back to crawler."""
    domain = extract_domain(url)
    config = await get_domain_config(domain)

    if config and config.render_type == "ssr":
        html = await fetch_html_direct(url)
        if html and len(html) > 1000:
            return html, "direct"

    result = await _crawler.arun(url=url, config=_make_crawl_config(url))
    return (result.html, "crawler") if result.success else (None, "failed")


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@app.post(
    "/extract-product",
    response_model=ExtractionResponse,
    response_model_by_alias=True,
)
async def extract_product(req: ExtractionRequest, background_tasks: BackgroundTasks):
    try:
        # 1. Cache
        cached = await cache_get(req.url)
        if cached:
            return cached

        # 2. Build extraction strategy
        strategy = LLMExtractionStrategy(
            llm_config=LLMConfig(
                provider=LLM_PROVIDER,
                api_token=LLM_API_TOKEN,
                base_url=LLM_BASE_URL,
            ),
            schema=Product.model_json_schema(),
            extraction_type="schema",
            instruction=build_instruction(req.url),
            input_format="markdown",
            chunk_token_threshold=16000,
            overlap_rate=0.1,
        )

        result = await _crawler.arun(
            url=req.url,
            config=_make_crawl_config(req.url, extraction_strategy=strategy),
        )

        if not result.success:
            return ExtractionResponse(
                success=False, url=req.url, error=result.error_message
            )

        # 3. Background learning (reuse HTML, no extra crawl)
        if result.html:
            background_tasks.add_task(background_learn_domain, req.url, result.html)

        # 4. Parse LLM output
        product_data = _parse_llm_json(result.extracted_content)
        if not product_data:
            return ExtractionResponse(
                success=False, url=req.url, error="LLM returned unparseable output"
            )

        for k in ("error", "index", "tags"):
            product_data.pop(k, None)

        product = Product.model_validate(product_data)

        # 5. Validate origin_code (ship-from) with HTML evidence
        if result.html:
            sanitized, reason = _sanitize_origin_code(
                product.origin_code, result.html, req.url
            )
            if sanitized != product.origin_code:
                print(
                    f"[Origin Guard] {req.url}: "
                    f"{product.origin_code!r} → {sanitized!r} ({reason})"
                )
            product.origin_code = sanitized

        # 5b. Guarantee non-null origin_code via deterministic fallback chain.
        #     origin_code is REQUIRED for shipping fee calculation — must never be null.
        guaranteed, fb_reason = _guarantee_origin_code(
            product.origin_code, result.html, req.url
        )
        if guaranteed != product.origin_code:
            print(
                f"[Origin Fallback] {req.url}: "
                f"{product.origin_code!r} → {guaranteed!r} ({fb_reason})"
            )
        product.origin_code = guaranteed

        # 6. Description extraction
        if result.html:
            desc_html, _ = await extract_description_smart(
                req.url, result.html, use_cache=True
            )
            if desc_html:
                product.description_html = desc_html

        # 7. Sanity check
        if not product.name:
            return ExtractionResponse(
                success=False,
                url=req.url,
                data=product,
                error="Could not extract product name — page may not have loaded fully",
            )

        response = ExtractionResponse(success=True, url=req.url, data=product)
        await cache_set(req.url, response)
        return response

    except Exception as e:
        return ExtractionResponse(success=False, url=req.url, error=str(e))


@app.post("/extract-description")
async def extract_description(
    req: ExtractionRequest, background_tasks: BackgroundTasks
):
    """Extract only product description HTML using learned selectors."""
    domain = extract_domain(req.url)
    config = await get_domain_config(domain)

    # Fast path
    if config and config.description_selector:
        html, method = await get_html_smart(req.url)
        if html:
            desc_html = extract_with_selector(html, config.description_selector)
            if desc_html:
                return {
                    "success": True,
                    "url": req.url,
                    "description_html": desc_html,
                    "method": f"cached_selector ({method})",
                    "domain_config": config.to_dict(),
                }

    # Max attempts reached
    if config and config.learn_attempts >= MAX_LEARN_ATTEMPTS:
        return {
            "success": False,
            "url": req.url,
            "description_html": None,
            "method": "max_attempts_reached",
            "error": f"Failed after {MAX_LEARN_ATTEMPTS} attempts: {config.learn_failure_reason}",
            "domain_config": config.to_dict(),
        }

    # Slow path
    result = await _crawler.arun(url=req.url, config=_make_crawl_config(req.url))
    if not result.success or not result.html:
        return {
            "success": False,
            "url": req.url,
            "description_html": None,
            "method": "failed",
            "error": result.error_message or "Crawl failed",
        }

    current = config.learn_attempts if config else 0
    render_type = detect_render_type(result.html)
    learned = await learn_description_selector(result.html, req.url)

    if learned and learned.get("selector"):
        new_config = DomainConfig(
            domain=domain,
            render_type=render_type,
            description_selector=learned["selector"],
            description_xpath=learned.get("xpath"),
            learn_attempts=current + 1,
            learn_failure_reason=None,
        )
        await save_domain_config(new_config)
        desc_html = extract_with_selector(result.html, learned["selector"])
        return {
            "success": desc_html is not None,
            "url": req.url,
            "description_html": desc_html,
            "method": "learned",
            "domain_config": new_config.to_dict(),
        }

    reason = learned.get("reasoning") if learned else "LLM returned no selector"
    new_config = DomainConfig(
        domain=domain,
        render_type=render_type,
        description_selector=None,
        description_xpath=None,
        learn_attempts=current + 1,
        learn_failure_reason=reason,
    )
    await save_domain_config(new_config)

    return {
        "success": False,
        "url": req.url,
        "description_html": None,
        "method": "failed",
        "error": f"Could not learn selector ({new_config.learn_attempts}/{MAX_LEARN_ATTEMPTS}): {reason}",
        "domain_config": new_config.to_dict(),
    }


@app.get("/domain/{domain}")
async def get_domain_info(domain: str):
    config = await get_domain_config(domain)
    return {"found": bool(config), "config": config.to_dict() if config else None}


@app.delete("/domain/{domain}")
async def delete_domain_config(domain: str):
    redis = await get_redis()
    result = await redis.delete(f"domain_config:{domain}")
    return {"deleted": result > 0, "domain": domain}


@app.get("/health")
async def health():
    try:
        redis = await get_redis()
        await redis.ping()
        redis_status = "connected"
    except Exception as e:
        redis_status = f"error: {e}"
    return {
        "status": "ok",
        "redis": redis_status,
        "crawler": "running" if _crawler else "stopped",
    }


@app.delete("/cache")
async def clear_cache():
    return {"cleared": await cache_clear_all()}


@app.delete("/cache/{url:path}")
async def clear_cache_url(url: str):
    return {"removed": await cache_delete(url), "url": url}


@app.post("/debug-html")
async def debug_html(req: ExtractionRequest):
    from fastapi.responses import HTMLResponse

    result = await _crawler.arun(url=req.url, config=_make_crawl_config(req.url))
    if not result.success:
        return {"success": False, "error": result.error_message}
    return HTMLResponse(content=result.html or "", media_type="text/html")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
