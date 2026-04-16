"""Quick debug script to inspect what markdown crawl4ai produces for a URL."""

import asyncio
import os
import sys

os.environ["PYTHONIOENCODING"] = "utf-8"

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig


async def main(url: str):
    browser_config = BrowserConfig(headless=True, verbose=False)
    crawler = AsyncWebCrawler(config=browser_config)
    await crawler.start()

    config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        wait_for="js:() => document.readyState === 'complete'",
        delay_before_return_html=5.0,
        page_timeout=45000,
    )

    result = await crawler.arun(url=url, config=config)
    await crawler.close()

    if not result.success:
        print(f"CRAWL FAILED: {result.error_message}")
        return

    md = result.markdown.raw_markdown if result.markdown else ""
    print(f"=== Markdown length: {len(md)} chars ===")
    print(f"=== Approx tokens: {len(md.split())} words ===\n")

    # Check if price-related content exists in markdown
    price_keywords = ["12.590.000", "12590000", "17.290.000", "17290000", "₫", "VND"]
    print("=== Price keywords found in markdown ===")
    for kw in price_keywords:
        count = md.count(kw)
        if count:
            # find position
            pos = md.find(kw)
            snippet = md[max(0, pos - 50) : pos + len(kw) + 50]
            print(f"  '{kw}': {count} times, first at char {pos}")
            print(f"    ...{snippet}...")
        else:
            print(f"  '{kw}': NOT FOUND")

    # Simulate chunking with default threshold (2048 tokens ~ 1500 words)
    words = md.split()
    chunk_size = 1500  # approx 2048 tokens
    chunks = [
        " ".join(words[i : i + chunk_size])
        for i in range(0, len(words), chunk_size)
    ]
    print(f"\n=== Chunks: {len(chunks)} (at ~2048 tokens each) ===")
    for i, chunk in enumerate(chunks):
        has_name = "iphone 13" in chunk.lower() or "iPhone 13" in chunk
        has_price = any(p in chunk for p in ["12.590.000", "12590000"])
        print(f"  Chunk {i}: {len(chunk.split())} words | name={'YES' if has_name else 'no':3s} | price={'YES' if has_price else 'no':3s}")

    # Save full markdown for manual inspection
    with open("debug_markdown.txt", "w", encoding="utf-8") as f:
        f.write(md)
    print("\n=== Full markdown saved to debug_markdown.txt ===")


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://hoanghamobile.com/dien-thoai-di-dong/apple-iphone-13-128gb-chinh-hang-vn-a"
    asyncio.run(main(url))
