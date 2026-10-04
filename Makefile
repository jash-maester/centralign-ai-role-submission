# Ledger — everything runs through docker compose. Host needs only Docker.
SHELL := /bin/bash
DC := docker compose
API := http://localhost:$${API_PORT:-8000}
RUN ?=
LEVEL ?= 1.0
ARGS ?=

.PHONY: help up down build logs ps seed snapshot test test-unit test-crm test-browser test-live \
        demo schema clean chaos-false-claim chaos-kill-browser chaos-expire-session \
        chaos-model-outage determinism replay web-check

help:  ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-22s %s\n", $$1, $$2}'

up:  ## build and start the whole stack, wait until healthy
	@test -f .env || (echo "copy .env.example to .env and fill OPENROUTER_API_KEY" && exit 1)
	$(DC) up -d --build --wait

down:  ## stop the stack (keeps volumes)
	$(DC) down

build:  ## build all images
	$(DC) --profile tools build

logs:  ## follow logs (SVC=name to filter)
	$(DC) logs -f --tail=100 $(SVC)

ps:
	$(DC) ps

seed:  ## seed EspoCRM (idempotent)
	$(DC) run --rm seed

snapshot:  ## print a canonical digest of CRM state (seed idempotency check)
	$(DC) run --rm seed python -m ledger_core.seed --snapshot

test:  ## full suite: unit + CRM integration (needs `make up && make seed`)
	$(DC) run --rm test pytest -p no:cacheprovider -q -m "not live_llm and not e2e" tests $(ARGS)

test-unit:  ## unit tests only (Redis, no CRM)
	$(DC) run --rm test pytest -p no:cacheprovider -q -m "not crm and not live_llm and not e2e" tests $(ARGS)

test-crm:  ## CRM integration tests only
	$(DC) run --rm test pytest -p no:cacheprovider -q -m crm tests $(ARGS)

test-browser:  ## Playwright tests inside the browser image
	$(DC) run --rm --no-deps -v ./tests:/repo/tests:ro -v ./services/browser/browser_worker:/app/browser_worker:ro \
		-e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 worker-browser-1 \
		python -m pytest -p no:cacheprovider -q -m browser /repo/tests $(ARGS)

test-live:  ## smoke tests that call OpenRouter (uses the daily free budget)
	$(DC) run --rm -e LLM_LIVE_TESTS=1 test pytest -p no:cacheprovider -q -m live_llm tests $(ARGS)

demo:  ## run the one-line goal end to end and print the report
	$(DC) run --rm test python -m ledger_core.cli demo

schema:  ## export protocol JSON schema for the web client
	$(DC) run --rm -v ./web:/repo/web test python /repo/services/core/scripts/export_schema.py /repo/web/src/api/protocol.schema.json

web-check:  ## web GUI: typecheck, lint and vitest inside node:22-alpine
	web/scripts/node.sh sh -c "npm ci --no-audit --no-fund >/dev/null && npm run typecheck && npm run lint && npm test"

clean:  ## stop and delete all volumes (CRM, ledger, evidence)
	$(DC) --profile tools down -v --remove-orphans

# ---- chaos (plans/05-demo-and-submission.md) --------------------------------
chaos-false-claim:  ## next browser step claims done without acting
	curl -fsS -X POST $(API)/chaos/false_claim && echo

chaos-kill-browser:  ## docker kill the browser operator currently holding a lease
	@holder=$$(curl -fsS $(API)/agents | python3 -c 'import json,sys; a=[x for x in json.load(sys.stdin) if x.get("id","").startswith("worker-browser") and x.get("current_step")]; print(a[0]["id"] if a else "")'); \
	if [ -z "$$holder" ]; then echo "no browser operator holds a lease right now"; exit 1; fi; \
	curl -fsS -X POST $(API)/chaos/kill_worker -H 'content-type: application/json' -d "{\"agent_id\":\"$$holder\"}" >/dev/null; \
	echo "killing $$holder"; $(DC) kill $$holder

chaos-expire-session:  ## invalidate the CRM session cookie
	curl -fsS -X POST $(API)/chaos/expire_session && echo

chaos-model-outage:  ## make the primary worker model fail
	curl -fsS -X POST $(API)/chaos/model_outage && echo

determinism:  ## make determinism LEVEL=1.0 [RUN=run_x]
	curl -fsS -X POST $(API)/runs/$(RUN)/determinism -H 'content-type: application/json' -d '{"level": $(LEVEL)}' && echo

replay:  ## make replay RUN=run_x  (cache-only, zero live LLM calls)
	curl -fsS -X POST $(API)/runs/$(RUN)/replay && echo
