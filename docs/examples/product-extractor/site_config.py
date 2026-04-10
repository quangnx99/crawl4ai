"""
Site configuration — persisted to DATA_DIR/sites.json.

Thay vì phân biệt SSR/CSR (dễ nhầm với Isomorphic rendering),
ta track xem domain có cần chờ JS load data không (need_js_wait).

Logic:
- Lần đầu crawl domain mới → dùng Chrome với wait mặc định → crawl + LLM
- Nếu LLM trả về thiếu price/name → đánh dấu need_js_wait=True, tăng delay
- Lần sau cùng domain → dùng config đã biết
- Domain confirmed không cần JS → dùng fetch nhẹ (SSR fast path)
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

# Mount this directory as a Docker volume so data survives redeploys.
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
SITES_FILE = DATA_DIR / "sites.json"

# How the domain is served
RenderType = Literal["SSR", "JS_WAIT", "UNKNOWN"]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
class SiteEntry(BaseModel):
    domain: str
    # SSR      = server renders full content, no JS wait needed → fast path
    # JS_WAIT  = needs JS execution + wait before data appears
    # UNKNOWN  = not yet determined (first request will use Chrome + detect)
    render_type: RenderType = "UNKNOWN"
    # Extra delay in seconds before grabbing HTML (JS_WAIT sites only)
    js_wait_delay: float = 2.0
    # CSS selectors to wait for (JS_WAIT sites only, optional)
    wait_selectors: list[str] = Field(default_factory=list)
    detected_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    success_count: int = 0
    fail_count: int = 0


class SiteRegistry:
    """
    Loads/saves site entries from/to a JSON file.
    """

    def __init__(self) -> None:
        self._data: dict[str, SiteEntry] = {}
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if SITES_FILE.exists():
            try:
                raw = json.loads(SITES_FILE.read_text(encoding="utf-8"))
                self._data = {k: SiteEntry(**v) for k, v in raw.items()}
            except Exception:
                self._data = {}

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SITES_FILE.write_text(
            json.dumps(
                {k: v.model_dump() for k, v in self._data.items()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @staticmethod
    def extract_domain(url: str) -> str:
        """Return 'example.com' from any URL."""
        host = urlparse(url).hostname or url
        return re.sub(r"^www\.", "", host.lower())

    def get(self, url: str) -> SiteEntry | None:
        return self._data.get(self.extract_domain(url))

    def upsert(self, url: str, entry: SiteEntry) -> None:
        self._data[self.extract_domain(url)] = entry
        self.save()

    def mark_js_wait(self, url: str, delay: float = 2.0) -> SiteEntry:
        """Mark domain as needing JS wait, optionally increase delay."""
        domain = self.extract_domain(url)
        entry = self._data.get(domain) or SiteEntry(domain=domain)
        entry.render_type = "JS_WAIT"
        entry.js_wait_delay = delay
        self._data[domain] = entry
        self.save()
        return entry

    def mark_ssr(self, url: str) -> SiteEntry:
        """Mark domain as SSR (no JS wait needed)."""
        domain = self.extract_domain(url)
        entry = self._data.get(domain) or SiteEntry(domain=domain)
        entry.render_type = "SSR"
        entry.js_wait_delay = 0.0
        self._data[domain] = entry
        self.save()
        return entry

    def record_success(self, url: str) -> None:
        entry = self.get(url)
        if entry:
            entry.success_count += 1
            self.save()

    def record_fail(self, url: str) -> None:
        entry = self.get(url)
        if entry:
            entry.fail_count += 1
            self.save()

    def all_entries(self) -> dict[str, dict]:
        return {k: v.model_dump() for k, v in self._data.items()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def product_data_complete(product_data: dict) -> bool:
    """
    Check if LLM returned meaningful product data.
    A result missing both price and name is considered incomplete —
    likely the page hadn't fully loaded when scraped.
    """
    name = product_data.get("name")
    price = product_data.get("price")
    return bool(name) and bool(price)


def build_wait_for(selectors: list[str]) -> str | None:
    """Convert a list of CSS selectors into a JS wait_for expression."""
    if not selectors:
        return None
    checks = " || ".join(f'document.querySelector("{s}")' for s in selectors)
    return f"js:() => {checks}"
