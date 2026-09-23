FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# System dependencies for Chromium AND Firefox/Camoufox
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-liberation \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcups2 \
        libdbus-1-3 \
        libdbus-glib-1-2 \
        libdrm2 \
        libgbm1 \
        libgdk-pixbuf2.0-0 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        libxt6 \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements first for layer caching
COPY requirements.txt .
RUN pip install -r requirements.txt

# Install Camoufox browser binaries
RUN python -m camoufox fetch

COPY . .

ENTRYPOINT ["/usr/bin/tini", "-s", "--"]

CMD python api_solver.py \
      --host 0.0.0.0 \
      --port $PORT \
      --thread 1 \
      --headless True \
      --browser_type camoufox \
      --proxy True