# Product Extractor — Project Notes

## Tổng quan

Microservice FastAPI dùng để crawl trang sản phẩm bất kỳ và trả về dữ liệu JSON có cấu trúc thông qua LLM extraction. Hỗ trợ Redis cache và domain learning system để tối ưu tốc độ. Hoàn toàn độc lập, có thể deploy riêng bằng Docker.

## Stack

| Thành phần | Công nghệ |
|---|---|
| Web framework | FastAPI + uvicorn |
| Crawling | crawl4ai (Playwright/Chromium) |
| Fast fetch | httpx (cho SSR sites) |
| LLM extraction | litellm (openai/anthropic/groq/ollama...) |
| Schema validation | Pydantic v2 |
| Cache & storage | Redis 7+ |
| HTML parsing | BeautifulSoup4 + lxml |
| Containerization | Docker + Docker Compose |

## Cấu trúc file

```
product-extractor/
├── server.py            # FastAPI app, crawler lifecycle, endpoint logic
├── schemas.py           # Pydantic models: Product, ExtractionRequest/Response
├── redis_client.py      # Redis connection singleton
├── domain_learning.py   # Domain selector learning logic
├── requirements.txt     # Python dependencies
├── Dockerfile           # Build image (python:3.11-slim + Playwright)
├── docker-compose.yml   # Deploy config (port 13001 → 8000, Redis)
├── .env                 # Config thực (không commit)
├── .env.example         # Template config
└── CLAUDE.md            # File này
```

## API Endpoints

| Endpoint | Method | Mô tả |
|---|---|---|
| `/extract-product` | POST | Full extraction (LLM) + background learning |
| `/extract-description` | POST | Chỉ lấy description_html (dùng cached selector nếu có) |
| `/domain/{domain}` | GET | Xem learned config cho domain |
| `/domain/{domain}` | DELETE | Xoá domain config (force re-learn) |
| `/health` | GET | Health check + Redis status |
| `/cache` | DELETE | Xoá toàn bộ extraction cache |
| `/cache/{url}` | DELETE | Xoá cache cho một URL cụ thể |
| `/debug-html` | POST | Lấy rendered HTML để debug |

### `POST /extract-product`
Crawl một URL trang sản phẩm và trả về structured JSON. Sau khi trả kết quả, service học CSS selector của domain trong background.

**Request:**
```json
{ "url": "https://example.com/product/abc" }
```

**Response:**
```json
{
  "success": true,
  "url": "https://example.com/product/abc",
  "data": {
    "name": "Tên sản phẩm",
    "price": "$29.99",
    "original_price": "$39.99",
    "currency": "USD",
    "sku": "ABC-123",
    "brand": "Brand Name",
    "description": "Mô tả ngắn",
    "description_html": "<div class='product-description'>...<p>Full HTML description</p>...</div>",
    "images": ["https://..."],
    "variants": [{ "name": "Red / XL", "price": null, "sku": null, "available": true }],
    "availability": "In Stock",
    "category": "Electronics > Headphones",
    "seller": "Shop Name",
    "condition": "New",
    "rating": "4.5/5",
    "review_count": 128,
    "origin_code": "US"
  },
  "error": null
}
```

### `GET /health`
Health check kèm trạng thái Redis. Trả về `{"status": "ok", "redis": "connected"}`.

## Cấu hình `.env`

```env
LLM_PROVIDER=openai/gpt-4o-mini       # litellm format: provider/model
LLM_API_TOKEN=sk-...                   # API key
LLM_BASE_URL=https://api.yescale.io/v1 # Optional: proxy/custom endpoint

REDIS_URL=redis://localhost:6379/0     # Redis connection string
CACHE_TTL=3600                         # TTL cho extraction cache (giây)
DOMAIN_LEARNING_TTL=604800             # TTL cho domain config (giây, mặc định 7 ngày)
```

Hỗ trợ mọi provider của litellm: `openai/`, `anthropic/`, `groq/`, `ollama/`, v.v.

## Kiến trúc crawl

### Singleton Browser
`AsyncWebCrawler` được khởi tạo **một lần duy nhất** khi app start (FastAPI `lifespan`), tái sử dụng cho toàn bộ request. Tránh overhead boot/close Chromium mỗi request (~1-3s).

### SSR vs CSR Detection
Trước khi crawl, service xác định loại trang để chọn phương thức fetch phù hợp:

| Loại trang | Phương thức | Tốc độ |
|---|---|---|
| SSR (server-side render) | `httpx.get` trực tiếp | ~100ms |
| CSR (JS-heavy, SPA) | crawl4ai / Playwright | ~5s |

Detection dựa trên domain config đã học. Lần đầu tiên gặp domain mới sẽ dùng crawl4ai để probe, sau đó lưu vào Redis.

### JS Render Detection (probe pass)
Khi chưa có domain config, mỗi request thực hiện:

1. **Probe pass** — crawl nhanh không dùng LLM, lấy raw HTML
2. **Detect** — `detect_js_render(html)` phân tích:
   - Tìm signature của JS framework: `__NEXT_DATA__`, `__nuxt`, `ng-version`, `data-reactroot`, `_gatsby`, `__remix_context__`, v.v.
   - Kiểm tra text density: nếu tỉ lệ text/HTML < 8% → JS-heavy
3. **Adaptive config** — chọn `CrawlerRunConfig` phù hợp:

| Loại trang | `wait_for` | `delay` | `page_timeout` |
|---|---|---|---|
| Static HTML | `css:body` | 0s | 30s |
| JS-heavy (SPA) | `js:() => document.readyState === 'complete'` | 2s | 45s |

### Redis Cache
Hai loại dữ liệu được cache trong Redis:

1. **Extraction cache** — kết quả `/extract-product` keyed theo URL, TTL = `CACHE_TTL` (mặc định 1 giờ)
2. **Domain config** — CSS selector và render type của domain, TTL = `DOMAIN_LEARNING_TTL` (mặc định 7 ngày)

Module `redis_client.py` quản lý singleton connection. Nếu Redis không available, service vẫn hoạt động bình thường (graceful degradation) nhưng không có cache.

### Domain Learning System
Sau mỗi lần extraction thành công, service học CSS selector cho phần description của domain đó:

1. **Request đến** → kiểm tra Redis có domain config chưa
2. **Cache HIT** → dùng selector đã học, fetch theo render type (httpx hoặc crawl4ai)
3. **Cache MISS** → crawl4ai full crawl → LLM extraction → lưu selector vào Redis (background task)

Logic học được đặt trong `domain_learning.py`. Selector tự invalidate khi `fail_count` vượt ngưỡng.

**Flow diagram:**
```
Request → Check domain cache
    │
    ├─ HIT + SSR → httpx.get (fast) → apply selector → return
    ├─ HIT + CSR → crawl4ai → apply selector → return
    └─ MISS → crawl4ai → LLM extraction → return
                              └─ background: learn selector → save to Redis
```

### Origin Code Detection (originCode)
LLM instruction được build **dynamically** theo URL (`_build_instruction(url)`):
- Parse domain TLD → detect country code (`.vn` → VN, `.co.jp` → JP, v.v.)
- Truyền context về domain country vào prompt để LLM ưu tiên đúng
- Thứ tự ưu tiên: explicit "Made in" > footer/seller address > domain TLD > known marketplace > brand HQ
- **Browser locale warning**: prompt cảnh báo LLM rằng browser ở VN nên currency có thể bị format sai → không tin currency hiển thị, phải cross-check với domain

## Chạy local

```bash
pip install -r requirements.txt
playwright install chromium
# Khởi động Redis
docker run -d -p 6379:6379 redis:7-alpine
uvicorn server:app --host 0.0.0.0 --port 8000
```

## Chạy bằng Docker

```bash
cp .env.example .env
# Điền LLM_API_TOKEN vào .env
docker-compose up -d
# API tại http://localhost:13001
```

## Lưu ý khi mở rộng

- **JS render detection** dùng heuristic đơn giản. Nếu cần chính xác hơn, có thể thêm signature hoặc tăng/giảm `_MIN_TEXT_RATIO` (hiện tại 0.08).
- **`wait_for`** trong JS-heavy mode dùng `document.readyState === 'complete'`. Với SPA phức tạp hơn (lazy load, infinite scroll), cân nhắc đổi thành selector cụ thể của trang đó, ví dụ `css:.product-title`.
- **`delay_before_return_html=2.0`** trong JS mode là buffer cho late JS paint. Có thể giảm nếu trang load nhanh.
- **Domain learning** chỉ học selector cho `description_html`. Các field khác vẫn dùng LLM mỗi lần.
- **Redis TTL** có thể điều chỉnh: `CACHE_TTL` cho extraction cache, `DOMAIN_LEARNING_TTL` cho selector cache.
- Hiện tại chưa có **rate limiting** — cân nhắc thêm nếu deploy public.
- **API key** trong `.env` không được commit lên git (đã có trong `.dockerignore` và `.gitignore`).
