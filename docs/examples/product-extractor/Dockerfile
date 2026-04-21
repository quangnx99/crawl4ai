FROM python:3.11-slim

# Chromium system deps (no CJK fonts, no gnupg)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libwayland-client0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# Install Python deps + Playwright Chromium in one layer, then strip source maps
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium \
    && find /root/.cache/ms-playwright -name "*.map" -delete \
    && find /root/.cache/ms-playwright -name "METADATA" -delete \
    && pip cache purge

COPY schemas.py server.py domain_learning.py redis_client.py reference.py site_config.py ./

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
