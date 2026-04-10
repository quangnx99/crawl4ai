# Product Extractor — Project Notes

## Tổng quan

Microservice FastAPI dùng để crawl trang sản phẩm bất kỳ và trả về dữ liệu JSON có cấu trúc thông qua LLM extraction. Hoàn toàn độc lập, có thể deploy riêng bằng Docker.

## Stack

| Thành phần | Công nghệ |
|---|---|
| Web framework | FastAPI + uvicorn |
| Crawling | crawl4ai (Playwright/Chromium) |
| LLM extraction | litellm (openai/anthropic/groq/ollama...) |
| Schema validation | Pydantic v2 |
| Containerization | Docker + Docker Compose |

## Cấu trúc file

```
product-extractor/
├── server.py          # FastAPI app, crawler lifecycle, endpoint logic
├── schemas.py         # Pydantic models: Product, ExtractionRequest/Response
├── requirements.txt   # Python dependencies
├── Dockerfile         # Build image (python:3.11-slim + Playwright)
├── docker-compose.yml # Deploy config (port 13001 → 8000)
├── .env               # Config thực (không commit)
├── .env.example       # Template config
└── CLAUDE.md          # File này
```

## API Endpoints

### `POST /extract-product`
Crawl một URL trang sản phẩm và trả về structured JSON.

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
    "images": ["https://..."],
    "variants": [{ "name": "Red / XL", "price": null, "sku": null, "available": true }],
    "availability": "In Stock",
    "category": "Electronics > Headphones",
    "seller": "Shop Name",
    "condition": "New",
    "rating": "4.5/5",
    "review_count": 128
  },
  "error": null
}
```

### `GET /health`
Kiểm tra service còn sống không. Trả về `{"status": "ok"}`.

## Cấu hình `.env`

```env
LLM_PROVIDER=openai/gpt-4o-mini       # litellm format: provider/model
LLM_API_TOKEN=sk-...                   # API key
LLM_BASE_URL=https://api.yescale.io/v1 # Optional: proxy/custom endpoint
```

Hỗ trợ mọi provider của litellm: `openai/`, `anthropic/`, `groq/`, `ollama/`, v.v.

## Kiến trúc crawl

### Singleton Browser
`AsyncWebCrawler` được khởi tạo **một lần duy nhất** khi app start (FastAPI `lifespan`), tái sử dụng cho toàn bộ request. Tránh overhead boot/close Chromium mỗi request (~1-3s).

### JS Render Detection (2-pass crawl)
Mỗi request thực hiện **2 bước**:

1. **Probe pass** — crawl nhanh không dùng LLM, lấy raw HTML
2. **Detect** — `detect_js_render(html)` phân tích:
   - Tìm signature của JS framework: `__NEXT_DATA__`, `__nuxt`, `ng-version`, `data-reactroot`, `_gatsby`, `__remix_context__`, v.v.
   - Kiểm tra text density: nếu tỉ lệ text/HTML < 8% → JS-heavy
3. **Adaptive config** — chọn `CrawlerRunConfig` phù hợp:

| Loại trang | `wait_for` | `delay` | `page_timeout` |
|---|---|---|---|
| Static HTML | `css:body` | 0s | 30s |
| JS-heavy (SPA) | `js:() => document.readyState === 'complete'` | 2s | 45s |

### Cache
`CacheMode.ENABLED` — crawl4ai cache lại HTML theo URL. Cùng URL gọi lại không cần crawl lại từ đầu.

## Chạy local

```bash
pip install -r requirements.txt
playwright install chromium
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
- Hiện tại chưa có **rate limiting** — cân nhắc thêm nếu deploy public.
- **API key** trong `.env` không được commit lên git (đã có trong `.dockerignore` và `.gitignore`).
