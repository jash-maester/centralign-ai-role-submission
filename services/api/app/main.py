"""Ledger API (plans/01-architecture.md §10). Routes live in app/routes/*."""

from __future__ import annotations

import importlib
import pkgutil
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ledger_core.keys import Keys
from ledger_core.redis_conn import connect
from ledger_core.settings import get_settings

from . import routes


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = connect()
    app.state.keys = Keys(get_settings().ledger_ns)
    yield
    await app.state.redis.aclose()


app = FastAPI(title="Ledger API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

for _mod in sorted(pkgutil.iter_modules(routes.__path__), key=lambda m: m.name):
    _module = importlib.import_module(f"{routes.__name__}.{_mod.name}")
    if hasattr(_module, "router"):
        app.include_router(_module.router)


@app.get("/health")
async def health() -> dict:
    await app.state.redis.ping()
    return {"status": "ok"}
