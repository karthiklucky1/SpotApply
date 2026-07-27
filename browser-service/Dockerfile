# Chromium lives HERE, not in the main app container.
#
# The Playwright base image ships browser builds matched to the library version
# in its tag, plus every OS-level font/codec dependency they need. Installing
# that into the main app image is what put a ~400MB browser inside the same
# memory limit as torch, FAISS, and five background lanes.
#
# This tag MUST match the pinned `playwright==` in requirements.txt. The image's
# browsers are keyed to that exact library version; if pip installs a different
# one, Playwright rejects the pre-baked Chromium at launch with "Executable
# doesn't exist … run playwright install". Bump both together or neither.
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

WORKDIR /svc

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# NOTE: no `playwright install` here on purpose — the base image already carries
# browsers built for this exact library version. Running it would re-download a
# second copy for no benefit and add minutes to every build.

COPY main.py .

EXPOSE 8080

# One worker on purpose: the BrowserPool (single Chromium, disposable contexts,
# a semaphore sized to the container's RAM) is process-local state. A second
# worker would silently double both the browser count and the memory budget —
# the exact bug this service exists to fix. Scale with replicas, not workers.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
