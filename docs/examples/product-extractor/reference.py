"""
LLM extraction instruction builder for PROXY SHOPPING / MUA HỘ crawlers.

Use case: compute shipping fee + service fee by knowing WHERE the product
ships FROM (the seller's warehouse country), NOT where it was manufactured.

Key distinction:
- `shipFromCountry` = country the parcel will be dispatched from (what we need)
- `originCode`      = manufacturing country (irrelevant for shipping cost)
- `brandCountry`    = brand HQ (irrelevant for shipping cost)

Logic:
1. For most single-brand or national e-commerce sites: site locale = ship-from.
   e.g. uniqlo.com/jp/ → JP, rakuten.co.jp → JP, zozotown.jp → JP
2. For marketplaces (Amazon, eBay, AliExpress, Rakuten, Shopee):
   MUST extract the seller's ship-from address from the page, because
   third-party sellers may ship from a different country.
3. For .com sites with geo-redirect, use URL locale prefix if present.
"""

from typing import Optional
from urllib.parse import urlparse

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_NATIVE_CURRENCY = {
    "VN": "VND",
    "JP": "JPY",
    "US": "USD",
    "GB": "GBP",
    "FR": "EUR",
    "DE": "EUR",
    "ES": "EUR",
    "IT": "EUR",
    "NL": "EUR",
    "CN": "CNY",
    "KR": "KRW",
    "TH": "THB",
    "ID": "IDR",
    "MY": "MYR",
    "SG": "SGD",
    "PH": "PHP",
    "TW": "TWD",
    "HK": "HKD",
    "IN": "INR",
    "AU": "AUD",
    "CA": "CAD",
    "SE": "SEK",
    "CH": "CHF",
}

# Marketplaces: multiple sellers, ship-from varies per listing → MUST extract
# from page content, can't rely on site locale alone.
_MARKETPLACES = {
    "amazon.com",
    "amazon.co.jp",
    "amazon.de",
    "amazon.co.uk",
    "amazon.fr",
    "amazon.it",
    "amazon.es",
    "amazon.ca",
    "amazon.com.au",
    "amazon.sg",
    "ebay.com",
    "ebay.co.uk",
    "ebay.de",
    "ebay.com.au",
    "aliexpress.com",
    "aliexpress.us",
    "taobao.com",
    "tmall.com",
    "1688.com",
    "rakuten.co.jp",
    "rakuten.com",
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
    "etsy.com",
    "mercari.com",
    "mercari.jp",
    "yahoo-auction.jp",
    "auctions.yahoo.co.jp",
}

# Single-brand sites where site locale reliably equals ship-from country.
# Ship-from fallback when URL has no locale prefix.
_BRAND_DEFAULT_SHIPFROM = {
    "shein.com": "CN",  # .com but ships from CN
    "aliexpress.com": "CN",  # even though marketplace, default is CN
    "temu.com": "CN",
    "taobao.com": "CN",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _detect_locale_from_url(url: str) -> Optional[str]:
    """Detect site locale (= ship-from for non-marketplace sites). ISO alpha-2."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()

    tld_map = {
        ".vn": "VN",
        ".jp": "JP",
        ".co.jp": "JP",
        ".co.uk": "GB",
        ".de": "DE",
        ".fr": "FR",
        ".es": "ES",
        ".it": "IT",
        ".cn": "CN",
        ".kr": "KR",
        ".co.kr": "KR",
        ".th": "TH",
        ".co.th": "TH",
        ".id": "ID",
        ".co.id": "ID",
        ".my": "MY",
        ".com.my": "MY",
        ".sg": "SG",
        ".com.sg": "SG",
        ".ph": "PH",
        ".com.ph": "PH",
        ".tw": "TW",
        ".com.tw": "TW",
        ".hk": "HK",
        ".com.hk": "HK",
        ".in": "IN",
        ".co.in": "IN",
        ".au": "AU",
        ".com.au": "AU",
        ".ca": "CA",
    }
    for tld, code in tld_map.items():
        if host.endswith(tld):
            return code

    # URL path locale: /jp/, /us/, /en-us/, /ja-jp/
    segments = path.strip("/").split("/")
    if segments:
        first = segments[0]
        if len(first) == 2 and first.isalpha():
            candidate = first.upper()
            if candidate in _NATIVE_CURRENCY:
                return candidate
        if "-" in first and len(first) == 5:
            candidate = first.split("-")[1].upper()
            if candidate in _NATIVE_CURRENCY:
                return candidate

    return None


def _is_marketplace(hostname: str) -> bool:
    host = hostname.lower().replace("www.", "")
    return any(host == m or host.endswith("." + m) for m in _MARKETPLACES)


def _brand_default_shipfrom(hostname: str) -> Optional[str]:
    host = hostname.lower().replace("www.", "")
    for brand_host, code in _BRAND_DEFAULT_SHIPFROM.items():
        if host == brand_host or host.endswith("." + brand_host):
            return code
    return None


# --------------------------------------------------------------------------- #
# SYSTEM PROMPT — focus on shipping, not origin
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """\
You are an e-commerce product data extractor for a PROXY SHOPPING service.
The service buys products on behalf of customers in Vietnam, so the most
important field is `shipFromCountry` — where the parcel will be dispatched
from. This determines international shipping fees and service fees.

Start your response with `{` and end with `}`. No markdown. No commentary.

========== CRITICAL FIELD: shipFromCountry ==========

`shipFromCountry` — ISO 3166-1 alpha-2 code of the country the product will
  be SHIPPED FROM when purchased on this page. This is the seller's warehouse
  country, NOT the manufacturing country, NOT the brand HQ.

Examples:
  - Uniqlo Japan site: shipFromCountry = JP (ships from Uniqlo JP warehouse),
    even if the product was made in Vietnam.
  - Amazon.com 3rd-party seller from China: shipFromCountry = CN, even
    though the site is amazon.com (US).
  - Nike.com (geo-redirect): check the URL locale — /jp/ → JP, /us/ → US.

========== EXTRACTION PRIORITY ==========

Use the FIRST rule that yields clear evidence, then STOP.

1. EXPLICIT ship-from text on the page. Search (multilingual):
     EN: "Ships from X", "Ship from X", "Dispatched from X", "Sold by ... Ships from X",
         "Item location: X", "Located in X", "Warehouse: X"
     JA: "発送元", "出荷元", "配送元", "発送地"
     ZH: "发货地", "发货地点", "从...发货", "发货地区"
     VI: "Giao từ", "Gửi từ", "Địa chỉ gửi hàng", "Kho hàng"
     KO: "배송지", "발송지", "출고지"
     → confidence = high

2. Seller info block / "Sold by" / "Seller location":
     Amazon: "Ships from" row in the buy-box
     eBay: "Item location" in the seller info
     AliExpress: seller's country under the seller name
     Rakuten: 店舗所在地 (shop location)
     → confidence = high

3. Site context hint provided below (single-brand site locale).
     → confidence = medium

4. If site is a generic .com with no locale and no shipping info on page:
     → shipFromCountry = null, confidence = null

========== ANTI-HALLUCINATION RULES ==========

The crawler browser runs in Vietnam. HTML may contain VN artifacts:
  - "Ship to Vietnam" / "Giao hàng đến Việt Nam" banners
  - Prices converted to VND via geo-IP
  - Vietnamese UI text from geolocation scripts

⚠️ CRITICAL: "Ship TO Vietnam" ≠ "Ship FROM Vietnam". The banner is the
destination (us buying), NOT the origin. Do NOT use these banners as evidence.
Look for ship-FROM phrasing specifically.

Also:
  - NEVER confuse "Made in X" with shipFromCountry. A product made in VN
    sold on a JP site still ships FROM JP.
  - NEVER use the buyer's country (VN) as shipFromCountry.
  - `shipFromEvidence` must be a substring actually present on the page.

========== OTHER FIELDS ==========

Extract `name`, `price`, `originalPrice`, `sku`, `brand`, `description`,
`images`, `variants`, `isSoldOut`, `category`, `seller`, `condition`,
`rating`, `reviewCount`, `currency` per schema. Prices: numeric strings only.

Currency rule: prefer JSON-LD `priceCurrency` or explicit currency code on
page. If the page shows VND but the site locale is non-VN, that's geo-leak
— use the site's native currency from context instead.

Return ONLY the JSON object.
"""


# --------------------------------------------------------------------------- #
# USER PROMPT — per-URL context
# --------------------------------------------------------------------------- #


def build_user_context(url: str) -> str:
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    locale = _detect_locale_from_url(url)
    native_ccy = _NATIVE_CURRENCY.get(locale or "", None)
    is_marketplace = _is_marketplace(hostname)
    brand_default = _brand_default_shipfrom(hostname)

    lines = ["SITE CONTEXT", f"- Hostname: {hostname}"]

    if locale:
        lines.append(f"- Site locale (from URL): {locale}")
        lines.append(f"- Native currency: {native_ccy or 'unknown'}")

    if is_marketplace:
        lines.append(
            "- SITE TYPE: MARKETPLACE (multiple third-party sellers). "
            "shipFromCountry VARIES per listing — you MUST extract from "
            "seller info / 'Ships from' / 'Item location' on the page. "
            "Do NOT assume shipFromCountry = site locale."
        )
    elif brand_default:
        lines.append(
            f"- SITE TYPE: Single-brand site with default warehouse in "
            f"{brand_default}. Use shipFromCountry = {brand_default} with "
            f"confidence=medium unless page explicitly says otherwise."
        )
    elif locale:
        lines.append(
            f"- SITE TYPE: Single-brand / national site. "
            f"shipFromCountry defaults to {locale} (confidence=medium) "
            f"unless the page explicitly states a different ship-from location."
        )
    else:
        lines.append(
            "- SITE TYPE: Generic TLD, unknown warehouse location. "
            "You MUST find explicit ship-from info on the page, or return null."
        )

    lines.append("")
    lines.append(
        "Extract the product per schema. Focus on shipFromCountry. Return JSON only."
    )
    return "\n".join(lines)


def build_instruction(url: str) -> str:
    """Combined prompt for frameworks that accept single string."""
    return SYSTEM_PROMPT + "\n\n" + build_user_context(url)


# --------------------------------------------------------------------------- #
# Sanity check
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    test_urls = [
        "https://www.uniqlo.com/jp/ja/products/E123456",  # → JP (brand site)
        "https://www.amazon.com/dp/B08N5WRWNW",  # → marketplace, must extract
        "https://www.amazon.co.jp/dp/B08N5WRWNW",  # → marketplace JP, still must extract
        "https://www.aliexpress.com/item/1234.html",  # → CN default, but marketplace
        "https://shopee.vn/product/123/456",  # → marketplace VN
        "https://www.shein.com/dress-p-12345.html",  # → CN (known)
        "https://www.nike.com/jp/t/air-zoom",  # → JP (path locale)
        "https://www.rakuten.co.jp/shop/item/abc",  # → marketplace JP
    ]
    for url in test_urls:
        print("=" * 72)
        print("URL:", url)
        print("-" * 72)
        print(build_user_context(url))
        print()
