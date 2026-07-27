# Chromium lives HERE, not in the main app container.
#
# The Playwright base image ships a Chromium build matched to the library
# version plus every OS-level font/codec dependency it needs. Installing that
# into the main app image is what put a ~400MB browser inside the same memory
# limit as torch, FAISS, and five background lanes.
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /svc

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium only — this service never needs Firefox or WebKit, and each unused
# browser is ~300MB of image for nothing.
RUN playwright install chromium

COPY main.py .

EXPOSE 8080

# One worker on purpose: the BrowserPool (single Chromium, disposable contexts,
# a semaphore sized to the container's RAM) is process-local state. A second
# worker would silently double both the browser count and the memory budget —
# the exact bug this service exists to fix. Scale with replicas, not workers.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
