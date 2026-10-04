# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN useradd --uid 10001 --create-home agent \
 && mkdir -p /evidence /app && chown agent:agent /evidence /app
WORKDIR /app
COPY services/core/pyproject.toml /app/core/pyproject.toml
COPY services/core/ledger_core /app/core/ledger_core
RUN pip install /app/core

FROM base AS runtime
USER agent
CMD ["python", "-m", "ledger_core.services.idle"]

FROM base AS test
RUN pip install "/app/core[dev]"
USER agent
