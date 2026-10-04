# syntax=docker/dockerfile:1
# Playwright image ships Chromium + system deps; keep the pip version in step.
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_BREAK_SYSTEM_PACKAGES=1
RUN useradd --uid 10001 --create-home agent \
 && mkdir -p /evidence /app && chown agent:agent /evidence /app
WORKDIR /app
COPY services/core/pyproject.toml /app/core/pyproject.toml
COPY services/core/ledger_core /app/core/ledger_core
RUN pip install /app/core "playwright==1.55.0" "pytest>=8.3" "pytest-asyncio>=0.24"
COPY services/browser/browser_worker /app/browser_worker
USER agent
CMD ["python", "-m", "browser_worker"]
