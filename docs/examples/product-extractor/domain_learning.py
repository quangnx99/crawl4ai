"""
Domain learning module for product description extraction.

This module handles:
1. Learning CSS/XPath selectors for product descriptions per domain
2. Detecting SSR vs CSR render type
3. Caching learned configurations in Redis
4. Background job for selector learning
"""

import json
import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx
import litellm
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from redis_client import get_redis

load_dotenv()

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai/gpt-4o-mini")
LLM_API_TOKEN = os.getenv("LLM_API_TOKEN", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", None)
DOMAIN_LEARNING_TTL = int(os.getenv("DOMAIN_LEARNING_TTL", 604800))  # 7 days

# Redis key prefixes
DOMAIN_CONFIG_PREFIX = "domain_config:"

# JS framework signatures for CSR detection
_JS_SIGNATURES = [
    "__NEXT_DATA__",
    "__nuxt",
    "__NUXT__",
    "ng-version",
    "data-reactroot",
    "data-react-helmet",
    "_gatsby",
    "__remix_context__",
    "__REMIX_CONTEXT__",
    "window.__INITIAL_STATE__",
    "window.__PRELOADED_STATE__",
]


@dataclass
class DomainConfig:
    """Learned configuration for a domain."""

    domain: str
    render_type: str  # "ssr" or "csr"
    description_selector: Optional[str] = None
    description_xpath: Optional[str] = None
    success_count: int = 0
    fail_count: int = 0
    learn_attempts: int = 0  # Number of learning attempts
    learn_failure_reason: Optional[str] = None  # Why learning failed

    def to_dict(self) -> dict:
        return {
            "domain": self.domain,
            "render_type": self.render_type,
            "description_selector": self.description_selector,
            "description_xpath": self.description_xpath,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "learn_attempts": self.learn_attempts,
            "learn_failure_reason": self.learn_failure_reason,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DomainConfig":
        return cls(
            domain=data["domain"],
            render_type=data["render_type"],
            description_selector=data.get("description_selector"),
            description_xpath=data.get("description_xpath"),
            success_count=data.get("success_count", 0),
            fail_count=data.get("fail_count", 0),
            learn_attempts=data.get("learn_attempts", 0),
            learn_failure_reason=data.get("learn_failure_reason"),
        )


def extract_domain(url: str) -> str:
    """Extract domain from URL (e.g., 'fptshop.com.vn' from 'https://fptshop.com.vn/abc')."""
    hostname = urlparse(url).hostname or ""
    return hostname.lower().lstrip("www.")


def detect_render_type(html: str) -> str:
    """Detect if page is SSR or CSR based on JS framework signatures."""
    html_lower = html.lower()

    # Check for JS framework signatures
    for sig in _JS_SIGNATURES:
        if sig.lower() in html_lower:
            return "csr"

    # Check text density - if very low, likely CSR
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text_ratio = len(text) / max(len(html), 1)

    if text_ratio < 0.05:  # Less than 5% text content
        return "csr"

    return "ssr"


def compress_html(html: str) -> str:
    """Compress HTML by removing scripts, styles, and unnecessary attributes."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove unnecessary tags
    for tag in soup(["script", "style", "svg", "head", "meta", "link", "noscript"]):
        tag.decompose()

    # Remove comments
    for comment in soup.find_all(
        string=lambda t: isinstance(t, str) and t.strip().startswith("<!--")
    ):
        comment.extract()

    # Keep only essential attributes
    essential_attrs = {
        "class",
        "id",
        "data-testid",
        "itemprop",
        "itemtype",
        "role",
        "aria-label",
    }
    for tag in soup.find_all(True):
        attrs_to_remove = [attr for attr in tag.attrs if attr not in essential_attrs]
        for attr in attrs_to_remove:
            del tag[attr]

    return str(soup)


async def get_domain_config(domain: str) -> Optional[DomainConfig]:
    """Get cached domain config from Redis."""
    redis = await get_redis()
    key = f"{DOMAIN_CONFIG_PREFIX}{domain}"
    data = await redis.get(key)
    if data:
        return DomainConfig.from_dict(json.loads(data))
    return None


async def save_domain_config(config: DomainConfig) -> None:
    """Save domain config to Redis."""
    redis = await get_redis()
    key = f"{DOMAIN_CONFIG_PREFIX}{config.domain}"
    await redis.setex(key, DOMAIN_LEARNING_TTL, json.dumps(config.to_dict()))


async def increment_domain_stats(domain: str, success: bool) -> None:
    """Increment success/fail counter for domain."""
    config = await get_domain_config(domain)
    if config:
        if success:
            config.success_count += 1
        else:
            config.fail_count += 1
        await save_domain_config(config)


async def learn_description_selector(html: str, url: str) -> Optional[dict]:
    """Use LLM to learn CSS selector for product description.

    Returns:
        dict with 'selector' and optionally 'xpath', or None if failed
    """
    compressed = compress_html(html)

    # Truncate if too long (keep first 50K chars)
    if len(compressed) > 50000:
        compressed = compressed[:50000]

    prompt = f"""Analyze this e-commerce product page HTML and find the CSS selector for the main product description block.

The product description is the detailed information section that contains:
- Product specifications, features, usage instructions
- Usually has multiple paragraphs or a table of specs
- NOT the short summary near the price
- NOT the reviews section
- NOT the seller/shipping info

URL: {url}

HTML:
```html
{compressed}
```

Return ONLY a JSON object:
{{
  "found": true/false,
  "selector": "CSS selector string or null",
  "xpath": "XPath expression or null (optional, for complex cases)",
  "confidence": "high/medium/low",
  "reasoning": "brief explanation"
}}

Tips for stable selectors:
- Prefer semantic attributes: [itemprop="description"], [data-testid="..."], #product-description
- Avoid dynamic class names with hashes (e.g., .css-1abc2de)
- Use tag + class combinations: div.product-description, section.detail-content
- For Japanese sites, look for: 商品説明, 商品詳細
- For Vietnamese sites, look for: mô tả sản phẩm, chi tiết sản phẩm"""

    try:
        response = await litellm.acompletion(
            model=LLM_PROVIDER,
            api_key=LLM_API_TOKEN,
            base_url=LLM_BASE_URL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0,
        )

        result_text = response.choices[0].message.content

        # Parse JSON from response
        if "```json" in result_text:
            result_text = result_text.split("```json")[1].split("```")[0]
        elif "```" in result_text:
            result_text = result_text.split("```")[1].split("```")[0]

        result = json.loads(result_text.strip())

        if result.get("found") and result.get("selector"):
            return {
                "selector": result["selector"],
                "xpath": result.get("xpath"),
                "confidence": result.get("confidence", "medium"),
            }

        return None

    except Exception as e:
        print(f"LLM selector learning error: {e}")
        return None


def _clean_description_html(element) -> str:
    """Clean extracted description element for frontend rendering."""
    from bs4 import Comment

    # Remove unwanted tags
    for tag in element.find_all(["script", "style", "noscript", "iframe"]):
        tag.decompose()

    # Remove HTML comments (wovn-src, etc.)
    for comment in element.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    # Strip noisy attributes — keep only presentation-relevant ones
    keep_attrs = {"class", "id", "style", "href", "src", "alt", "width", "height", "colspan", "rowspan"}
    for tag in element.find_all(True):
        attrs_to_remove = [a for a in list(tag.attrs) if a not in keep_attrs]
        for a in attrs_to_remove:
            del tag[a]

    raw = str(element)

    # Compact: collapse whitespace (including newlines) between tags so
    # text nodes don't appear as literal \n in the frontend
    import re
    raw = re.sub(r">\s+<", "><", raw)
    raw = re.sub(r" {2,}", " ", raw)
    return raw.strip()


def extract_with_selector(html: str, selector: str) -> Optional[str]:
    """Extract HTML content using CSS selector."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        element = soup.select_one(selector)

        if element and len(element.get_text(strip=True)) > 50:
            return _clean_description_html(element)

        return None
    except Exception:
        return None


def extract_with_xpath(html: str, xpath: str) -> Optional[str]:
    """Extract HTML content using XPath (requires lxml)."""
    try:
        from lxml import etree

        tree = etree.HTML(html)
        elements = tree.xpath(xpath)

        if elements:
            element = elements[0]
            # Convert back to string
            result = etree.tostring(element, encoding="unicode", method="html")
            if len(result) > 50:
                return result

        return None
    except Exception:
        return None


async def fetch_html_direct(url: str, timeout: float = 30.0) -> Optional[str]:
    """Fetch HTML directly via HTTP (for SSR sites)."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text
    except Exception as e:
        print(f"Direct fetch error: {e}")
        return None


async def extract_description_smart(
    url: str,
    html: str,
    use_cache: bool = True,
) -> tuple[Optional[str], bool]:
    """Smart extraction using cached selector if available.

    Returns:
        tuple of (description_html, used_cache)
    """
    domain = extract_domain(url)

    if use_cache:
        config = await get_domain_config(domain)

        if config and config.description_selector:
            # Try cached selector
            result = extract_with_selector(html, config.description_selector)
            if result:
                await increment_domain_stats(domain, success=True)
                return result, True

            # Try XPath if available
            if config.description_xpath:
                result = extract_with_xpath(html, config.description_xpath)
                if result:
                    await increment_domain_stats(domain, success=True)
                    return result, True

            # Selector failed - increment fail count
            await increment_domain_stats(domain, success=False)

            # If too many failures, invalidate cache
            config = await get_domain_config(domain)
            if (
                config
                and config.fail_count > 5
                and config.fail_count > config.success_count * 0.3
            ):
                # Invalidate - will re-learn on next request
                redis = await get_redis()
                await redis.delete(f"{DOMAIN_CONFIG_PREFIX}{domain}")

    return None, False
