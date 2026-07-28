FROM python:3.11-slim

WORKDIR /app

# Install system dependencies needed for compiling python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser and its OS-level system dependencies.
#
# Still needed here for exactly two things: founder autofill / form preview
# (app/autofill/agent.py — stateful interactive sessions that were deliberately
# NOT moved to browser-service/), and the local fallback in
# app/common/browser_client.py when the browser service is unreachable.
#
# Once BROWSER_SERVICE_URL is set, BROWSER_SERVICE_FALLBACK_LOCAL=0, and
# server-side autofill is off (autofill_multi_user_enabled=False with no founder
# fills — every other tenant autofills via the MV3 extension), this layer can be
# deleted: ~400MB off the image and no browser can ever start in this container.
# Do NOT drop it while any of those three still hold.
RUN playwright install --with-deps chromium

COPY . .

# glibc allocator discipline — the RSS-climbs-forever OOM (docs/MEMORY.md).
# This is a multi-threaded torch app: every worker thread that touches malloc
# can pin a 64MB glibc arena, arenas are capped at 8 x HOST cores (not the
# container quota), and glibc ratchets its trim threshold upward on torch's
# large frees so freed memory is never returned to the OS. Two arenas + a fixed
# 128KB trim/mmap threshold keep RSS flat. MUST be process env (glibc reads
# them before Python starts) — setting them in app/__init__.py does nothing.
ENV MALLOC_ARENA_MAX=2 \
    MALLOC_TRIM_THRESHOLD_=131072 \
    MALLOC_MMAP_THRESHOLD_=131072 \
    MALLOC_TOP_PAD_=131072 \
    OPENBLAS_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    TOKENIZERS_PARALLELISM=false

CMD ["sh", "-c", "exec uvicorn app.api.server:app --host 0.0.0.0 --port ${PORT:-8080}"]

