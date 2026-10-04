# Ledger web GUI

React 18 + Vite + TypeScript + Tailwind, built from `web/design/*.dc.html`
(read-only reference). Served by nginx in the `web` compose service; nginx
proxies `/api/*` to `api:8000` (prefix stripped, SSE unbuffered, WebSocket
upgrade for the agent shell).

## Layout

| Path | What |
|---|---|
| `src/api/types.ts` | Protocol models (mirrors `protocol.py`; enums tested against `protocol.schema.json`) + response shapes the GUI expects |
| `src/api/http.ts` | Typed client for every endpoint in plans/01 §10 (+ escalations, run config, replay, budget) |
| `src/api/sse.ts` | fetch-based SSE with `Last-Event-ID` resume and reconnect |
| `src/store/` | `reduce.ts` (pure event → step/run/facts reducer) and the zustand store (one SSE connection per run) |
| `src/api/mock/` | Mock backend, bundled **only** when built with `--mode mock` / `VITE_MOCK=1` |
| `src/views/` | `dashboard/`, `report/`, `builder/` |

## Commands (all in docker)

```
make web-check                                   # typecheck + lint + vitest
web/scripts/node.sh npm run build                # production build (real API)
web/scripts/node.sh npx vite build --mode mock --outDir dist-mock
docker compose build web                         # nginx image
```

Screenshot smoke test against a mock build served on host port 20531:

```
docker run -d --name ledger-mockweb -p 127.0.0.1:20531:80 -v "$PWD/web/dist-mock":/usr/share/nginx/html:ro nginx:alpine
docker compose run --rm --no-deps -v ./web/scripts:/scripts -v ./web/screenshots:/out \
  worker-browser-1 python /scripts/screenshots.py http://host.docker.internal:20531
```

## API expectations (parsed leniently, see `src/api/normalize.ts`)

- Lists may be bare arrays or `{items|runs|steps|events|agents|escalations: [...]}`.
- `GET /runs/{id}/facts`: hash `key -> {value, source_step}` (JSON strings ok) or a list of `Fact`.
- `GET /runs/{id}/config`: `RunConfig` or `{config, config_hash}`. `PUT` takes a partial `RunConfig`.
  Determinism/seed changes go through `POST /runs/{id}/determinism {level, seed?}`.
- `GET /stream?run_id=`: `data:` is an `Event` JSON; `id:` is the Redis stream id. Named events are fine.
- `GET /agents`: `AgentCard` plus `alive, last_heartbeat_ms, current_step, lease_ttl_ms, lease_total_ms, lease_fence, models[], model, steps_done, rejections`.
- `GET/PUT /agents/{id}/config`: `{prompt, prompt_version, layers[{id,name,source,tokens,enabled,locked,preview}], tools[ToolSpec], recent_calls[]}`; PUT takes `{prompt?} | {layers: {id: bool}} | {tools: ToolSpec[]}`.
- `WS /agents/{id}/exec`: raw terminal bytes both ways; resize sent as `{"type":"resize","cols","rows"}`.
- `GET /runs/{id}/report`: the `Report` shape in `types.ts`; `?format=md` (or a `markdown` field) for the export, otherwise the GUI renders markdown itself.
- `GET /llm/budget`: `{used, limit, remaining, spent_usd?, models?: {role: [ids]}}`.
- Proposed additive: `GET /playbooks/{name}` (markdown or `{sections[]}`) for the playbook Content tab; a 404 is shown as such.
