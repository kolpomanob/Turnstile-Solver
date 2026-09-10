FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# System libs Chromium needs at runtime. patchright's own installer fetches
# the browser binary but not all the shared libraries on slim images.
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

# Install Python deps first (layer cache: rebuilds only when requirements change)
COPY requirements.txt .
RUN pip install -r requirements.txt

# Install the Chromium browser binary that patchright manages
RUN python -m patchright install --with-deps chromium

# Copy the rest of the solver source
COPY . .

# tini reaps orphaned Chromium subprocesses when Python is PID 1
ENTRYPOINT ["/usr/bin/tini", "-s", "--"]

# --thread 1 to start (see "Memory" section below), bump later
# --headless True REQUIRES --useragent per the solver's CLI rules
CMD python api_solver.py \
      --host 0.0.0.0 \
      --port $PORT \
      --thread 1 \
      --headless True \
      --useragent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"