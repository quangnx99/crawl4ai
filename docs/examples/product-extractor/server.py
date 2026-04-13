import json
import os
import time
from contextlib import asynccontextmanager

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
from fastapi import FastAPI

from schemas import ExtractionRequest, ExtractionResponse, Product

load_dotenv()

# --- LLM config from env ---
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai/gpt-4o-mini")
LLM_API_TOKEN = os.getenv("LLM_API_TOKEN", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", None)

# --- Extraction result cache (in-memory) ---
_CACHE_TTL = int(os.getenv("CACHE_TTL", 3600))  # seconds, default 1 hour
_extraction_cache: dict[str, tuple[ExtractionResponse, float]] = {}

INSTRUCTION = (
    "Extract product information from this page. "
    "Return a single JSON object matching the provided schema. "
    "For images, return full absolute URLs. "
    "For price and original_price, return numeric value only — no currency symbol, no currency code (e.g. '29.99' not '$29.99' or 'USD 29.99'). "
    "Put the currency code (e.g. 'USD', 'VND', 'JPY') in the currency field separately. "
    "For isSoldOut, return true if the product is sold out / unavailable, false if it is in stock / available. "
    "If stock status is not found on the page, use null. "
    "For originCode, return the ISO 3166-1 alpha-2 country code. "
    "Priority order: "
    "1) Explicit manufacturing origin ('Made in', 'Xuất xứ', 'Origin', '原産国'). "
    "2) Brand's known country of origin (e.g. Samsung → 'KR', Apple → 'US', Sony → 'JP'). "
    "3) Marketplace or seller country — infer from domain (amazon.com → 'US', amazon.co.jp → 'JP', shopee.vn → 'VN', rakuten.co.jp → 'JP'), "
    "currency, page language, or seller address. "
    "You MUST return a value — always pick the best available signal. Never return null."
)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _cache_get(url: str) -> ExtractionResponse | None:
    entry = _extraction_cache.get(url)
    if entry is None:
        return None
    response, ts = entry
    if time.time() - ts > _CACHE_TTL:
        del _extraction_cache[url]
        return None
    return response


def _cache_set(url: str, response: ExtractionResponse) -> None:
    _extraction_cache[url] = (response, time.time())


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
    _crawler = None


app = FastAPI(
    title="Product Extractor API",
    description="Extract structured product data from any URL using LLM",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post(
    "/extract-product", response_model=ExtractionResponse, response_model_by_alias=True
)
async def extract_product(req: ExtractionRequest):
    """Crawl a product page and return structured JSON."""
    try:
        # 1. Cache hit → return immediately
        cached = _cache_get(req.url)
        if cached:
            return cached

        # 2. Crawl with Chrome
        extraction_strategy = LLMExtractionStrategy(
            llm_config=LLMConfig(
                provider=LLM_PROVIDER,
                api_token=LLM_API_TOKEN,
                base_url=LLM_BASE_URL,
            ),
            schema=Product.model_json_schema(),
            extraction_type="schema",
            instruction=INSTRUCTION,
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

        # 3. Parse LLM output
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

        if not product.name:
            return ExtractionResponse(
                success=False,
                url=req.url,
                data=product,
                error="Could not extract product name — page may not have loaded fully",
            )

        # 4. Cache and return
        response = ExtractionResponse(success=True, url=req.url, data=product)
        _cache_set(req.url, response)
        return response

    except json.JSONDecodeError as e:
        return ExtractionResponse(
            success=False,
            url=req.url,
            error=f"Failed to parse LLM response as JSON: {e}",
        )
    except Exception as e:
        return ExtractionResponse(success=False, url=req.url, error=str(e))


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.delete("/cache")
async def clear_cache():
    """Clear all cached extraction results."""
    count = len(_extraction_cache)
    _extraction_cache.clear()
    return {"cleared": count}


@app.delete("/cache/{url:path}")
async def clear_cache_url(url: str):
    """Clear cached result for a specific URL."""
    removed = _extraction_cache.pop(url, None)
    return {"removed": removed is not None, "url": url}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
