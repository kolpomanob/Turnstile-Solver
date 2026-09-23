FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

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
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Fetch Camoufox browser binaries
RUN python -m camoufox fetch

COPY . .

ENTRYPOINT ["/usr/bin/tini", "-s", "--"]

# Launch solver using camoufox browser type
CMD python api_solver.py \
      --host 0.0.0.0 \
      --port $PORT \
      --thread 1 \
      --headless True \
      --browser_type camoufox \
      --proxy True