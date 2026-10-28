# syntax=docker/dockerfile:1
#
# Production image for the AdLinkFly resolver API.
# Uses Python 3.12 and installs Chromium + its OS dependencies via Playwright.

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Install browsers to a shared path so a non-root user can reach them.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# 1) Python dependencies (cached layer).
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# 2) Chromium + the system libraries it needs (requires root; runs before USER).
RUN playwright install --with-deps chromium

# 3) Application code and metadata.
COPY app ./app
COPY pyproject.toml README.md ./

# 4) Drop privileges. Give the runtime user ownership of the app and browsers.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app /ms-playwright
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" \
    || exit 1

# Single Uvicorn process. Each process launches one shared Chromium instance;
# scale horizontally (more containers) rather than many workers per container.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
