# Ledger — everything runs through docker compose. Host needs only Docker.
SHELL := /bin/bash
DC := docker compose
# Host port of this stack's API: from the environment, else .env (worktree stacks use their
# own ports), else 8000. Without this the chaos targets hit whichever stack owns :8000.
API_PORT ?= $(shell sed -n 's/^API_PORT=//p' .env 2>/dev/null | tail -n 1)
API := http://localhost:$(or $(API_PORT),8000)
# e2e (Track M): the whole stack on the scripted LLM backend; test container gets the docker socket
E2E_DC := $(DC) -f docker-compose.yml -f tests/e2e/compose.e2e.yml
E2E_SERVICES := redis espocrm mailpit api orchestrator verifier meta-reviewer worker-api worker-parser \
        worker-drafter worker-mailer worker-browser-1 worker-browser-2
RUN ?=
LEVEL ?= 1.0
ARGS ?=

.PHONY: help up down build logs ps seed snapshot test test-unit test-crm test-browser test-live \
        demo schema openapi clean e2e-up test-e2e chaos-false-claim chaos-kill-browser chaos-expire-session \
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

e2e-up:  ## start the stack for the e2e suite (scripted LLM, no web) and seed it
	$(E2E_DC) up -d --build --wait $(E2E_SERVICES)
	$(E2E_DC) run --rm seed

test-e2e:  ## e2e suite against the full stack, LLM_BACKEND=scripted (FRESH=1: make clean first; ARGS=)
	$(if $(FRESH),$(MAKE) clean)
	$(MAKE) e2e-up
	$(E2E_DC) run --rm test pytest -p no:cacheprovider -q -m e2e tests/e2e $(ARGS)

test-browser:  ## Playwright tests inside the browser image
	$(DC) run --rm --no-deps -v ./tests:/repo/tests:ro -v ./services/browser/browser_worker:/app/browser_worker:ro \
		-e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 worker-browser-1 \
		python -m pytest -p no:cacheprovider -q -m browser /repo/tests $(ARGS)

test-live:  ## smoke tests that call OpenRouter (uses the daily free budget)
	$(DC) run --rm -e LLM_LIVE_TESTS=1 test pytest -p no:cacheprovider -q -m live_llm tests $(ARGS)

demo:  ## run the one-line goal end to end and print the report (LLM_BACKEND=scripted, CRM_WRITE_PATH=api, ARGS=)
	$(DC) run --rm $(if $(LLM_BACKEND),-e LLM_BACKEND=$(LLM_BACKEND)) $(if $(CRM_WRITE_PATH),-e CRM_WRITE_PATH=$(CRM_WRITE_PATH)) \
		test python -m ledger_core.cli demo $(ARGS)

schema:  ## export protocol JSON schema for the web client
	$(DC) run --rm -v ./web:/repo/web test python /repo/services/core/scripts/export_schema.py /repo/web/src/api/protocol.schema.json

openapi:  ## export the API's OpenAPI schema to web/src/api/schema.json
	$(DC) run --rm -v ./web:/repo/web test python /repo/services/api/scripts/export_openapi.py /repo/web/src/api/schema.json

web-check:  ## web GUI: typecheck, lint and vitest inside node:22-alpine
	web/scripts/node.sh sh -c "npm ci --no-audit --no-fund >/dev/null && npm run typecheck && npm run lint && npm test"

clean:  ## stop and delete all volumes (CRM, ledger, evidence)
	$(DC) --profile tools down -v --remove-orphans

# ---- chaos (plans/05-demo-and-submission.md) --------------------------------
chaos-false-claim:  ## next browser step claims done without acting
	curl -fsS -X POST $(API)/chaos/false_claim && echo

chaos-kill-browser:  ## docker kill the browser operator currently holding a lease
	@holder=$$($(DC) exec -T api python -c 'import json,urllib.request as u; a=[x for x in json.load(u.urlopen("http://localhost:8000/agents")) if x.get("id","").startswith("worker-browser") and x.get("current_step")]; print(a[0]["id"] if a else "")' 2>/dev/null | tr -d '\r'); \
	if [ -z "$$holder" ]; then echo "no browser operator holds a lease right now"; exit 1; fi; \
	curl -fsS -X POST $(API)/chaos/kill_worker -H 'content-type: application/json' -d "{\"agent_id\":\"$$holder\",\"kill\":false}" >/dev/null 2>&1 \
		|| echo "(API unavailable: kill not recorded on the timeline)"; \
	echo "killing $$holder"; docker kill $$($(DC) ps -q $$holder)  # the replica only, not one-off `run` containers

chaos-expire-session:  ## invalidate the CRM session cookie (next browser step re-logs in)
	@curl -fsS -X POST $(API)/chaos/expire_session 2>/dev/null && echo \
		|| $(DC) exec -T redis redis-cli HSET $${LEDGER_NS:+$$LEDGER_NS:}faults expire_session 1

chaos-model-outage:  ## make the primary worker model fail
	curl -fsS -X POST $(API)/chaos/model_outage && echo

determinism:  ## make determinism LEVEL=1.0 [RUN=run_x]
	curl -fsS -X POST $(API)/runs/$(RUN)/determinism -H 'content-type: application/json' -d '{"level": $(LEVEL)}' && echo

replay:  ## make replay RUN=run_x  (cache-only, zero live LLM calls)
	curl -fsS -X POST $(API)/runs/$(RUN)/replay && echo
