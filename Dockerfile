FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# System libs Chromium needs at runtime, PLUS the X11 stack that Xvfb provides.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-liberation \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libnspr4 \
        libnss3 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        libx11-6 \
        libxcb1 \
        libxext6 \
        libxi6 \
        libxtst6 \
        xvfb \
        xauth \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

RUN python -m patchright install --with-deps chromium

COPY . .

ENTRYPOINT ["/usr/bin/tini", "-s", "--"]

# Headful Chromium under Xvfb — this is what defeats the headless fingerprint.
# --headless is intentionally OMITTED (default is False).
# --useragent is kept only as a fallback; per-request ?ua= takes priority.
CMD xvfb-run -a --server-args="-screen 0 1280x720x24" \
    python api_solver.py \
      --host 0.0.0.0 \
      --port $PORT \
      --thread 1 \
      --proxy True \
      --useragent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"