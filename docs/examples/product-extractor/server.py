import asyncio
import json
import os
from contextlib import asynccontextmanager
from urllib.parse import urlparse

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
from schemas import ExtractionRequest, ExtractionResponse, Product

load_dotenv()

# --- LLM config from env ---
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai/gpt-4o-mini")
LLM_API_TOKEN = os.getenv("LLM_API_TOKEN", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", None)

# --- Cache config ---
CACHE_TTL = int(os.getenv("CACHE_TTL", 3600))  # seconds

# Redis key prefixes
EXTRACTION_CACHE_PREFIX = "extraction:"

# --- URL path locale segment to country code (e.g. /jp/ja/ → JP) ---
_PATH_LOCALE_MAP = {
    "/jp/": "JP", "/ja/": "JP",
    "/kr/": "KR", "/ko/": "KR",
    "/cn/": "CN", "/zh/": "CN",
    "/tw/": "TW",
    "/vn/": "VN", "/vi/": "VN",
    "/th/": "TH",
    "/sg/": "SG",
    "/my/": "MY",
    "/id/": "ID",
    "/ph/": "PH",
    "/in/": "IN",
    "/au/": "AU",
    "/us/": "US", "/en-us/": "US",
    "/uk/": "GB", "/en-gb/": "GB",
    "/de/": "DE",
    "/fr/": "FR",
    "/it/": "IT",
    "/es/": "ES",
    "/nl/": "NL",
    "/br/": "BR",
    "/mx/": "MX",
    "/ca/": "CA",
    "/ru/": "RU",
    "/se/": "SE",
    "/no/": "NO",
    "/dk/": "DK",
    "/fi/": "FI",
    "/pl/": "PL",
    "/pt/": "PT",
}

# --- TLD to country code mapping ---
_TLD_COUNTRY_MAP = {
    ".vn": "VN", ".jp": "JP", ".kr": "KR", ".cn": "CN", ".tw": "TW",
    ".th": "TH", ".sg": "SG", ".my": "MY", ".id": "ID", ".ph": "PH",
    ".in": "IN", ".au": "AU", ".nz": "NZ", ".uk": "GB", ".de": "DE",
    ".fr": "FR", ".it": "IT", ".es": "ES", ".nl": "NL", ".be": "BE",
    ".br": "BR", ".mx": "MX", ".ca": "CA", ".ru": "RU", ".se": "SE",
    ".no": "NO", ".dk": "DK", ".fi": "FI", ".pl": "PL", ".pt": "PT",
    ".ar": "AR", ".cl": "CL", ".co": "CO", ".pe": "PE",
}

_COMPOUND_TLD_MAP = {
    ".co.jp": "JP", ".co.uk": "GB", ".co.kr": "KR", ".co.th": "TH",
    ".co.id": "ID", ".co.nz": "NZ", ".com.au": "AU", ".com.br": "BR",
    ".com.mx": "MX", ".com.sg": "SG", ".com.my": "MY", ".com.ph": "PH",
    ".com.tw": "TW", ".com.vn": "VN", ".com.cn": "CN", ".com.ar": "AR",
    ".com.co": "CO", ".com.pe": "PE", ".co.in": "IN",
}


def _detect_country_from_url(url: str) -> str | None:
    """Detect country code from URL — checks path locale segments first, then TLD."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    path = (parsed.path or "").lower()

    # 1. Path locale segments (e.g. /jp/ja/, /en-us/) — highest signal on global .com sites
    for segment, code in _PATH_LOCALE_MAP.items():
        if segment in path:
            return code

    # 2. Compound TLDs (e.g. .co.jp, .com.vn)
    for tld, code in _COMPOUND_TLD_MAP.items():
        if hostname.endswith(tld):
            return code

    # 3. Simple TLDs
    for tld, code in _TLD_COUNTRY_MAP.items():
        if hostname.endswith(tld):
            return code

    return None


def _build_instruction(url: str) -> str:
    """Build LLM extraction instruction with URL context."""
    domain_country = _detect_country_from_url(url)
    hostname = urlparse(url).hostname or ""
    parsed_path = urlparse(url).path or ""

    if domain_country:
        origin_hint = (
            f"URL LOCALE SIGNAL: The URL '{url}' contains locale/TLD signals pointing to country '{domain_country}' "
            f"(detected from path segments like /jp/, /us/ or TLD like .co.jp, .com.vn). "
            f"Use this as a strong signal for originCode UNLESS the page explicitly states a different manufacturing country."
        )
    else:
        origin_hint = (
            f"URL LOCALE SIGNAL: The URL domain '{hostname}' uses a generic TLD (.com). "
            f"Determine originCode from page content only: look for 'Made in', '原産国', 'Xuất xứ', "
            f"footer address, seller country, or known marketplace rules "
            f"(amazon.com → US, aliexpress.com → CN, rakuten.co.jp → JP)."
        )

    return (
        "Extract product information from this page. "
        "Return a single JSON object matching the provided schema. "
        "For images, return full absolute URLs. "
        "For price and original_price, return numeric value only — no currency symbol (e.g. '29.99' not '$29.99'). "
        "Put the currency code (e.g. 'USD', 'VND', 'JPY') in the currency field separately. "
        "Determine the correct currency from the page content itself (currency symbols, price labels, country context) — "
        "do NOT assume any currency based on where the browser is running. "
        "For isSoldOut, return true if the product is sold out/unavailable, false if in stock. "
        "If stock status is not on the page, use null. "
        "For originCode, return the ISO 3166-1 alpha-2 country code using this priority order: "
        "1) Explicit manufacturing origin TEXT on the page ('Made in X', 'Xuất xứ: X', '原産国: X', 'Country of Origin'). "
        "2) Footer/about page company address country. "
        f"3) {origin_hint} "
        "4) Brand HQ as last resort (UNIQLO → JP, Samsung → KR, Apple → US, Sony → JP, Zara → ES). "
        "You MUST return a value — never return null for originCode."
    )


# ---------------------------------------------------------------------------
# Redis cache helpers
# ---------------------------------------------------------------------------


async def cache_get(url: str) -> ExtractionResponse | None:
    """Get cached extraction result from Redis."""
    try:
        redis = await get_redis()
        key = f"{EXTRACTION_CACHE_PREFIX}{url}"
        data = await redis.get(key)
        if data:
            parsed = json.loads(data)
            return ExtractionResponse(**parsed)
        return None
    except Exception:
        return None


async def cache_set(url: str, response: ExtractionResponse) -> None:
    """Cache extraction result to Redis."""
    try:
        redis = await get_redis()
        key = f"{EXTRACTION_CACHE_PREFIX}{url}"
        await redis.setex(key, CACHE_TTL, response.model_dump_json(by_alias=True))
    except Exception:
        pass


async def cache_delete(url: str) -> bool:
    """Delete cached extraction result."""
    try:
        redis = await get_redis()
        key = f"{EXTRACTION_CACHE_PREFIX}{url}"
        result = await redis.delete(key)
        return result > 0
    except Exception:
        return False


async def cache_clear_all() -> int:
    """Clear all extraction cache."""
    try:
        redis = await get_redis()
        keys = []
        async for key in redis.scan_iter(f"{EXTRACTION_CACHE_PREFIX}*"):
            keys.append(key)
        if keys:
            return await redis.delete(*keys)
        return 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Background learning job
# ---------------------------------------------------------------------------

MAX_LEARN_ATTEMPTS = 3


async def background_learn_domain(url: str, html: str) -> None:
    """Background job to learn domain selectors."""
    domain = extract_domain(url)

    # Check if already learned or max attempts reached
    config = await get_domain_config(domain)
    if config:
        if config.description_selector:
            # Already have selector, skip
            return
        if config.learn_attempts >= MAX_LEARN_ATTEMPTS:
            # Max attempts reached, skip
            print(f"[Learning] Skipping {domain} - max attempts ({MAX_LEARN_ATTEMPTS}) reached. Reason: {config.learn_failure_reason}")
            return

    # Detect render type
    render_type = detect_render_type(html)

    # Get current attempt count
    current_attempts = config.learn_attempts if config else 0

    # Learn selector via LLM
    learned = await learn_description_selector(html, url)

    if learned and learned.get("selector"):
        new_config = DomainConfig(
            domain=domain,
            render_type=render_type,
            description_selector=learned.get("selector"),
            description_xpath=learned.get("xpath"),
            success_count=0,
            fail_count=0,
            learn_attempts=current_attempts + 1,
            learn_failure_reason=None,
        )
        await save_domain_config(new_config)
        print(f"[Learning] Learned selector for {domain}: {learned['selector']}")
    else:
        # Save failed attempt with reason
        failure_reason = learned.get("reasoning") if learned else "LLM returned no selector"
        new_config = DomainConfig(
            domain=domain,
            render_type=render_type,
            description_selector=None,
            description_xpath=None,
            success_count=0,
            fail_count=0,
            learn_attempts=current_attempts + 1,
            learn_failure_reason=failure_reason,
        )
        await save_domain_config(new_config)

        if new_config.learn_attempts >= MAX_LEARN_ATTEMPTS:
            print(f"[Learning] FAILED {domain} after {MAX_LEARN_ATTEMPTS} attempts. Reason: {failure_reason}")
        else:
            print(f"[Learning] Attempt {new_config.learn_attempts}/{MAX_LEARN_ATTEMPTS} failed for {domain}. Reason: {failure_reason}")


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

_crawler: AsyncWebCrawler | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _crawler
    browser_config = BrowserConfig(headless=True, verbose=False)
    _crawler = AsyncWebCrawler(config=browser_config)
    await _crawler.start()
    yield
    await _crawler.close()
    await close_redis()
    _crawler = None


app = FastAPI(
    title="Product Extractor API",
    description="Extract structured product data from any URL using LLM with smart caching",
    version="2.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Helper: Get HTML (smart - SSR direct fetch or CSR via crawler)
# ---------------------------------------------------------------------------


async def get_html_smart(url: str) -> tuple[str | None, str]:
    """Get HTML using the best method based on domain config.

    Returns:
        tuple of (html, method_used: "direct" | "crawler")
    """
    domain = extract_domain(url)
    config = await get_domain_config(domain)

    # If known SSR, try direct fetch first
    if config and config.render_type == "ssr":
        html = await fetch_html_direct(url)
        if html and len(html) > 1000:
            return html, "direct"

    # Fall back to crawler
    crawl_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        wait_for="js:() => document.readyState === 'complete'",
        delay_before_return_html=5.0,
        page_timeout=45000,
    )
    result = await _crawler.arun(url=url, config=crawl_config)

    if result.success:
        return result.html, "crawler"

    return None, "failed"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post(
    "/extract-product", response_model=ExtractionResponse, response_model_by_alias=True
)
async def extract_product(req: ExtractionRequest, background_tasks: BackgroundTasks):
    """Crawl a product page and return structured JSON."""
    try:
        # 1. Cache hit → return immediately
        cached = await cache_get(req.url)
        if cached:
            return cached

        # 2. Crawl with Chrome (always use crawler for main extraction to ensure JS rendering)
        extraction_strategy = LLMExtractionStrategy(
            llm_config=LLMConfig(
                provider=LLM_PROVIDER,
                api_token=LLM_API_TOKEN,
                base_url=LLM_BASE_URL,
            ),
            schema=Product.model_json_schema(),
            extraction_type="schema",
            instruction=_build_instruction(req.url),
            input_format="markdown",
            chunk_token_threshold=16000,
            overlap_rate=0.1,
        )

        config = CrawlerRunConfig(
            extraction_strategy=extraction_strategy,
            cache_mode=CacheMode.BYPASS,
            wait_for="js:() => document.readyState === 'complete'",
            delay_before_return_html=5.0,
            page_timeout=45000,
        )

        result = await _crawler.arun(url=req.url, config=config)

        if not result.success:
            return ExtractionResponse(
                success=False, url=req.url, error=result.error_message
            )

        # 3. Background job: learn domain selectors
        if result.html:
            background_tasks.add_task(background_learn_domain, req.url, result.html)

        # 4. Parse LLM output
        raw = result.extracted_content
        if not raw:
            return ExtractionResponse(
                success=False, url=req.url, error="LLM returned empty extraction"
            )

        parsed = json.loads(raw) if isinstance(raw, str) else raw
        product_data = parsed[0] if isinstance(parsed, list) and parsed else parsed

        if isinstance(product_data, dict):
            product_data.pop("error", None)
            product_data.pop("index", None)
            product_data.pop("tags", None)

        product = Product.model_validate(product_data)

        # 5. Try to extract description_html using learned selector
        if result.html:
            desc_html, used_cache = await extract_description_smart(
                req.url, result.html, use_cache=True
            )
            if desc_html:
                product.description_html = desc_html

        if not product.name:
            return ExtractionResponse(
                success=False,
                url=req.url,
                data=product,
                error="Could not extract product name — page may not have loaded fully",
            )

        # 6. Cache and return
        response = ExtractionResponse(success=True, url=req.url, data=product)
        await cache_set(req.url, response)
        return response

    except json.JSONDecodeError as e:
        return ExtractionResponse(
            success=False,
            url=req.url,
            error=f"Failed to parse LLM response as JSON: {e}",
        )
    except Exception as e:
        return ExtractionResponse(success=False, url=req.url, error=str(e))


@app.post("/extract-description")
async def extract_description(req: ExtractionRequest, background_tasks: BackgroundTasks):
    """Extract only product description HTML using learned selectors.

    This endpoint:
    1. Checks if domain has learned selector
    2. If SSR + selector: fetch HTML directly, apply selector (fast!)
    3. If CSR or no selector: use crawler + LLM learning

    Returns:
        {
            "success": bool,
            "url": str,
            "description_html": str | null,
            "method": "cached_selector" | "learned" | "failed" | "max_attempts_reached",
            "domain_config": {...} | null
        }
    """
    domain = extract_domain(req.url)
    config = await get_domain_config(domain)

    # Fast path: known selector
    if config and config.description_selector:
        # Get HTML (smart: direct for SSR, crawler for CSR)
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

    # Check if max attempts reached
    if config and config.learn_attempts >= MAX_LEARN_ATTEMPTS:
        return {
            "success": False,
            "url": req.url,
            "description_html": None,
            "method": "max_attempts_reached",
            "error": f"Learning failed after {MAX_LEARN_ATTEMPTS} attempts: {config.learn_failure_reason}",
            "domain_config": config.to_dict(),
        }

    # Slow path: need to crawl and possibly learn
    crawl_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        wait_for="js:() => document.readyState === 'complete'",
        delay_before_return_html=5.0,
        page_timeout=45000,
    )
    result = await _crawler.arun(url=req.url, config=crawl_config)

    if not result.success or not result.html:
        return {
            "success": False,
            "url": req.url,
            "description_html": None,
            "method": "failed",
            "error": result.error_message or "Crawl failed",
        }

    # Get current attempt count
    current_attempts = config.learn_attempts if config else 0
    render_type = detect_render_type(result.html)

    # Try to learn and extract
    learned = await learn_description_selector(result.html, req.url)

    if learned and learned.get("selector"):
        # Save successful config
        new_config = DomainConfig(
            domain=domain,
            render_type=render_type,
            description_selector=learned["selector"],
            description_xpath=learned.get("xpath"),
            learn_attempts=current_attempts + 1,
            learn_failure_reason=None,
        )
        await save_domain_config(new_config)

        # Extract with new selector
        desc_html = extract_with_selector(result.html, learned["selector"])
        return {
            "success": desc_html is not None,
            "url": req.url,
            "description_html": desc_html,
            "method": "learned",
            "domain_config": new_config.to_dict(),
        }

    # Save failed attempt
    failure_reason = learned.get("reasoning") if learned else "LLM returned no selector"
    new_config = DomainConfig(
        domain=domain,
        render_type=render_type,
        description_selector=None,
        description_xpath=None,
        learn_attempts=current_attempts + 1,
        learn_failure_reason=failure_reason,
    )
    await save_domain_config(new_config)

    if new_config.learn_attempts >= MAX_LEARN_ATTEMPTS:
        print(f"[Learning] FAILED {domain} after {MAX_LEARN_ATTEMPTS} attempts. Reason: {failure_reason}")

    return {
        "success": False,
        "url": req.url,
        "description_html": None,
        "method": "failed",
        "error": f"Could not learn selector (attempt {new_config.learn_attempts}/{MAX_LEARN_ATTEMPTS}): {failure_reason}",
        "domain_config": new_config.to_dict(),
    }


@app.get("/domain/{domain}")
async def get_domain_info(domain: str):
    """Get learned configuration for a domain."""
    config = await get_domain_config(domain)
    if config:
        return {"found": True, "config": config.to_dict()}
    return {"found": False, "config": None}


@app.delete("/domain/{domain}")
async def delete_domain_config(domain: str):
    """Delete learned configuration for a domain (force re-learn)."""
    redis = await get_redis()
    key = f"domain_config:{domain}"
    result = await redis.delete(key)
    return {"deleted": result > 0, "domain": domain}


@app.get("/health")
async def health():
    """Health check including Redis status."""
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
    """Clear all cached extraction results."""
    count = await cache_clear_all()
    return {"cleared": count}


@app.delete("/cache/{url:path}")
async def clear_cache_url(url: str):
    """Clear cached result for a specific URL."""
    removed = await cache_delete(url)
    return {"removed": removed, "url": url}


@app.post("/debug-html")
async def debug_html(req: ExtractionRequest):
    """Crawl a URL and return the rendered HTML for debugging selectors."""
    config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        wait_for="js:() => document.readyState === 'complete'",
        delay_before_return_html=5.0,
        page_timeout=45000,
    )
    result = await _crawler.arun(url=req.url, config=config)
    if not result.success:
        return {"success": False, "error": result.error_message}

    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=result.html or "", media_type="text/html")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
