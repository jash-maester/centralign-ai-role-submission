# Scripted LLM fixtures

`LLM_BACKEND=scripted` answers every `llm.complete()` call from the JSON files
in this directory (override with `LLM_FIXTURES_DIR`). Use it for offline
end-to-end runs when the free OpenRouter budget (50 requests/day, shared) is
gone, and for integration tests that need realistic multi-call conversations.
Implementation: `services/core/ledger_core/llm_scripted.py`.

## File format

Every `*.json` file (any name, subfolders allowed) holds one fixture object or
a list of them:

```json
[
  {
    "role": "meta_reviewer",
    "schema": "ReviewDecision",
    "fixture_key": "lead:7",
    "responses": [
      {"decision": "match_existing", "confidence": 0.62, "threshold": 0.8},
      {"decision": "match_existing", "confidence": 0.91, "threshold": 0.8}
    ]
  },
  {
    "role": "orchestrator",
    "schema": "Plan",
    "response": {"steps": []}
  }
]
```

| Field         | Required | Meaning |
|---------------|----------|---------|
| `role`        | yes | `orchestrator`, `worker`, `verifier` or `meta_reviewer` (the `role` passed to `complete`) |
| `schema`      | yes | The pydantic class name passed as `schema` (`schema.__name__`) |
| `response`    | one of | The answer, validated against the schema |
| `responses`   | one of | Answers in call order; the last one repeats |
| `error`       | one of | Raise `LLMError(<error>)` instead (scripted outage) |
| `fixture_key` | no  | Only used when the caller passes the same key (see below) |
| `match`       | no  | Only used when this substring occurs in the messages |
| `model`       | no  | Model label for the `llm.call` event (default `scripted:<file>`) |

## Lookup order for a call (role, schema)

1. **`fixture_key`**: the caller puts a hint line `[fixture_key: <key>]` in a
   *system* message. `prompts.assemble(..., fixture_key="lead:7")` adds it for
   you. Keys must be stable across runs (a lane such as `lead:7`, a step kind,
   a CSV row), never a run or step id.
2. **`match`**: the first fixture (without `fixture_key`) whose `match`
   substring appears in any message content.
3. **default**: the first fixture with neither `fixture_key` nor `match`.

A miss raises `LLMError` naming the role, schema and fixture key to add, so a
missing fixture is never silently papered over.

## Conventions for tracks adding fixtures

- One file per agent/flow, e.g. `orchestrator.json`, `meta_reviewer.json`,
  `drafter.json`, `judge.json`. Keep answers realistic: they double as
  documentation of what the model is expected to return.
- Use `fixture_key` for per-lead answers (`lead:<row>`), a default fixture for
  the common case.
- Every fixture here is loaded by `tests/test_llm_backends.py::test_repo_fixture_dir_loads`;
  keep them valid JSON with `role` and `schema`.
- The scripted backend still emits `llm.call` events (`scripted: true`, zero
  tokens and cost), so timelines look the same as a live run.
