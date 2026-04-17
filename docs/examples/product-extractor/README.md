# Product Extractor v2.0

Microservice crawl trang sản phẩm bất kỳ và trả về dữ liệu JSON có cấu trúc thông qua LLM extraction. Hỗ trợ Redis cache và domain learning để tăng tốc các lần truy vấn sau.

## Yêu cầu

- Python 3.11+
- Playwright (Chromium)
- Redis 7+

## Cấu hình

```bash
cp .env.example .env
```

Mở `.env` và điền API key:

```env
LLM_PROVIDER=openai/gpt-4o-mini       # litellm format: provider/model
LLM_API_TOKEN=sk-...                   # API key của bạn
# LLM_BASE_URL=http://localhost:11434  # Tuỳ chọn: custom endpoint

REDIS_URL=redis://localhost:6379/0
CACHE_TTL=3600
DOMAIN_LEARNING_TTL=604800             # 7 ngày
```

Hỗ trợ mọi provider của litellm: `openai/`, `anthropic/`, `groq/`, `ollama/`, v.v.

## Chạy local

```bash
pip install -r requirements.txt
playwright install chromium
# Khởi động Redis (cần có Docker hoặc Redis cài sẵn)
docker run -d -p 6379:6379 redis:7-alpine
uvicorn server:app --host 0.0.0.0 --port 8000
```

API tại `http://localhost:8000`

## Chạy bằng Docker

```bash
cp .env.example .env
# Điền LLM_API_TOKEN vào .env
docker-compose up -d
```

API tại `http://localhost:13001`

Docker Compose sẽ tự động khởi động cả service và Redis container.

## API

### `GET /health`

Kiểm tra service status và Redis connection.

```json
{
  "status": "ok",
  "redis": "connected"
}
```

### `POST /extract-product`

Crawl URL sản phẩm và trả về structured JSON. Kết quả được cache theo URL. Sau khi trả về, service sẽ học CSS selector của domain trong background.

**Request:**

```bash
curl -X POST http://localhost:8000/extract-product \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/product/abc"}'
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

### `POST /extract-description`

Trích xuất chỉ phần `description_html` của sản phẩm. Nếu domain đã có selector được học, sẽ dùng trực tiếp mà không cần LLM (nhanh hơn nhiều).

**Request:**

```bash
curl -X POST http://localhost:8000/extract-description \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/product/abc"}'
```

**Response:**

```json
{
  "success": true,
  "url": "https://example.com/product/abc",
  "description_html": "<div class='product-description'>...</div>",
  "error": null
}
```

### `GET /domain/{domain}`

Lấy domain config đã được học (CSS selector, loại trang, v.v.).

```bash
curl http://localhost:8000/domain/example.com
```

**Response:**

```json
{
  "domain": "example.com",
  "selector": ".product-description",
  "render_type": "ssr",
  "success_count": 12,
  "fail_count": 0,
  "learned_at": "2024-01-15T10:30:00Z"
}
```

### `DELETE /domain/{domain}`

Xoá domain config để force re-learn lần sau.

```bash
curl -X DELETE http://localhost:8000/domain/example.com
```

### `DELETE /cache`

Xoá toàn bộ extraction cache.

```bash
curl -X DELETE http://localhost:8000/cache
```

### `DELETE /cache/{url}`

Xoá cache cho một URL cụ thể (URL cần được URL-encode).

```bash
curl -X DELETE "http://localhost:8000/cache/https%3A%2F%2Fexample.com%2Fproduct%2Fabc"
```

### `POST /debug-html`

Lấy rendered HTML của trang để debug.

**Request:**

```bash
curl -X POST http://localhost:8000/debug-html \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/product/abc"}'
```
