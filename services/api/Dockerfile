# syntax=docker/dockerfile:1
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN useradd --uid 10001 --create-home agent \
 && mkdir -p /evidence /app && chown agent:agent /evidence /app
WORKDIR /app
COPY services/core/pyproject.toml /app/core/pyproject.toml
COPY services/core/ledger_core /app/core/ledger_core
RUN pip install "/app/core[dev]"
COPY services/api/app /app/app
USER agent
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
