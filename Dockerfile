FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# Install minimal base packages & tini process manager
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements first for layer caching
COPY requirements.txt .
RUN pip install -r requirements.txt

# Automatically install all system library dependencies required for browser rendering
RUN python -m patchright install-deps firefox chromium

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