FROM python:3.11-slim-bookworm

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
        libdbus-glib-1-2 \
        libdrm2 \
        libgbm1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        libxtst6 \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache: rebuilds only when requirements change)
COPY requirements.txt .
RUN pip install -r requirements.txt

# Pre-fetch Camoufox browser binary during build to avoid GitHub rate limit on Railway
RUN python -m camoufox fetch

# Install the Chromium browser binary that patchright manages
RUN python -m patchright install --with-deps chromium

# Copy the rest of the solver source
COPY . .

# tini reaps orphaned Chromium subprocesses when Python is PID 1
ENTRYPOINT ["/usr/bin/tini", "-s", "--"]

# --thread 1 to start (see "Memory" section below), bump later
# --headless True REQUIRES --useragent per the solver's CLI rules
CMD python api_solver_camoufox.py \
      --host 0.0.0.0 \
      --port $PORT \
      --browser_type camoufox \
      --thread 1 \
      --headless True \
      --proxy False