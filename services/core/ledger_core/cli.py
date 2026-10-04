"""Ledger CLI (python -m ledger_core.cli ...). Runs in the `test` container:

    docker compose run --rm test python -m ledger_core.cli submit tests/specs/parse_one_step.json
    docker compose run --rm test python -m ledger_core.cli submit --example parse --follow
    docker compose run --rm test python -m ledger_core.cli submit --goal "Process yesterday's leads"
    ... cli tail [--run RUN] [--until-done] [--timeout 60]
    ... cli steps RUN       ... cli facts RUN       ... cli runs       ... cli release RUN
    ... cli example parse   (print a run spec to start from)

A run spec is a hand-written plan (no LLM):

    {"goal": "...", "input_file": "event_attendees.csv", "config": {<RunConfig overrides>},
     "steps": [{"ref": "parse", "kind": "file.parse", "title": "...",
                "inputs": {"file": "event_attendees.csv"},
                "postcondition": {"check": "file.parsed_rows", "args": {...}, "expect": {...}},
                "depends_on": ["<ref>"], "skill": "<optional, default from kind>"}]}

`submit` stores the run and steps (plan.created), marks the run running and
moves dependency-free steps to ready. With no orchestrator yet (Track G),
`release RUN` (or `submit --follow`) moves planned steps whose dependencies
are committed to ready. `--goal` only creates the run for the orchestrator.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import ledger
from .config import RunConfig
from .events import read_events
from .keys import Keys
from .protocol import (
    DEFAULT_CHECK,
    SKILL_KINDS,
    TERMINAL_STATUSES,
    Event,
    EventType,
    Postcondition,
    Run,
    RunStatus,
    Skill,
    Step,
    StepKind,
)
from .redis_conn import connect
from .settings import get_settings

ACTOR = "cli"

EXAMPLES: dict[str, dict[str, Any]] = {
    "parse": {
        "goal": "Parse the Signal Summit attendee export into normalised leads",
        "input_file": "event_attendees.csv",
        "steps": [{
            "ref": "parse", "kind": "file.parse", "title": "Parse event_attendees.csv",
            "inputs": {"file": "event_attendees.csv"},
            "postcondition": {"check": "file.parsed_rows", "args": {"file": "event_attendees.csv"},
                              "expect": {"required_columns": ["name", "email", "company"]}},
        }],
    },
}


def default_skill(kind: StepKind) -> Skill:
    for skill, kinds in SKILL_KINDS.items():
        if kind in kinds:
            return skill
    raise ValueError(f"no skill handles kind {kind.value}")


def _data_file(name: str) -> Path | None:
    p = Path(name)
    candidates = [p] if p.is_absolute() else [Path(get_settings().data_dir) / p, Path("/repo/data") / p,
                                               Path("data") / p]
    return next((c for c in candidates if c.is_file()), None)


def build_steps(spec: dict[str, Any], run_id: str, cfg: RunConfig) -> list[Step]:
    refs: dict[str, str] = {}
    built: list[tuple[dict[str, Any], Step]] = []
    for i, s in enumerate(spec.get("steps", [])):
        kind = StepKind(s["kind"])
        inputs = s.get("inputs", {})
        pc = s.get("postcondition") or {"check": DEFAULT_CHECK[kind], "args": inputs}
        step = Step(
            run_id=run_id, kind=kind, skill=Skill(s.get("skill") or default_skill(kind)),
            title=s.get("title", kind.value), inputs=inputs, postcondition=Postcondition(**pc),
            side_effect=s.get("side_effect", False), idempotency_key=s.get("idempotency_key"),
            lane=s.get("lane"), max_attempts=s.get("max_attempts", cfg.max_attempts),
        )
        refs[s.get("ref", f"step{i + 1}")] = step.id
        built.append((s, step))
    for s, step in built:
        step.depends_on = [refs.get(d, d) for d in s.get("depends_on", [])]
    return [step for _, step in built]


async def submit_spec(r, keys: Keys, spec: dict[str, Any], *, actor: str = ACTOR) -> tuple[Run, list[Step]]:
    """Create a run from a hand-written spec and release its first steps."""
    run = Run(goal=spec["goal"], input_file=spec.get("input_file"), playbook=spec.get("playbook", "event-leads.md"))
    if run.input_file and (f := _data_file(run.input_file)):
        from .workers.parser import file_sha256

        run.input_sha256 = file_sha256(f)
    cfg = RunConfig(**spec.get("config", {}))
    run.config_hash = cfg.config_hash()
    await ledger.create_run(r, keys, run, actor=actor)
    if spec.get("config"):
        await ledger.set_run_config(r, keys, run.id, cfg, actor=actor)
    steps = build_steps(spec, run.id, cfg)
    if steps:
        await ledger.create_steps(r, keys, steps, actor=actor, payload={"source": "hand-written spec"})
        await ledger.set_run_status(r, keys, run.id, RunStatus.RUNNING, actor=actor)
        await ledger.release_dependents(r, keys, run.id, actor=actor)
    return run, steps


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------


def _ts(ms: int) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(ms / 1000)) + f".{ms % 1000:03d}"


def format_event(ev: Event) -> str:
    p = ev.payload
    head = f"{_ts(ev.ts)}  {ev.type.value:<20} {ev.actor:<16} {ev.step_id or ev.run_id or '-':<15}"
    if "from" in p and "to" in p:
        extra = f"{p['from']} -> {p['to']}"
        if p.get("fence"):
            extra += f"  fence={p['fence']}"
        if p.get("attempt"):
            extra += f"  attempt={p['attempt']}"
        if ev.type == EventType.STEP_CLAIMED:
            extra += f"  acted={p.get('acted')}  \"{p.get('summary', '')}\""
        elif p.get("reason"):
            extra += f"  reason: {p['reason']}"
        if p.get("facts"):
            extra += f"  facts={len(p['facts'])}"
        return f"{head} {extra}"
    if ev.type == EventType.FACT_COMMITTED:
        return f"{head} {p.get('key')} = {json.dumps(p.get('value'), separators=(',', ':'))[:90]}"
    if "steps" in p:
        return f"{head} " + ", ".join(f"{s['id']} {s['kind']} [{s['status']}]" for s in p["steps"])
    return f"{head} {json.dumps(p, separators=(',', ':'))[:140]}"


def format_steps(steps: list[Step]) -> str:
    lines = [f"{'step':<15} {'kind':<20} {'status':<14} {'att':>3} {'fence':>5} {'owner':<16} title / last verdict"]
    for s in steps:
        verdict = f"  [{'ok' if s.verdict.ok else 'rejected'}: {s.verdict.reason}]" if s.verdict else ""
        lines.append(f"{s.id:<15} {s.kind.value:<20} {s.status.value:<14} {s.attempt:>3} {s.fence:>5} "
                     f"{s.lease_owner or '-':<16} {s.title}{verdict}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


async def _all_terminal(r, keys: Keys, run_id: str) -> bool:
    steps = await ledger.list_steps(r, keys, run_id)
    return bool(steps) and all(s.status in TERMINAL_STATUSES for s in steps)


async def cmd_tail(r, keys: Keys, run_id: str | None, *, from_start: bool, until_done: bool,
                   timeout: float | None, release: bool = False) -> None:
    deadline = time.monotonic() + timeout if timeout else None
    last = "$"
    if from_start:
        history = await ledger.run_events(r, keys, run_id) if run_id else await read_events(r, keys, count=1000)
        for ev in history:
            print(format_event(ev), flush=True)
            last = ev.id or last
        if last == "$":
            last = "0-0"
    if until_done and run_id and await _all_terminal(r, keys, run_id):
        return
    async for ev in _tail(r, keys, last, run_id, deadline):
        if ev is not None:
            print(format_event(ev), flush=True)
            if release and run_id and ev.type == EventType.STEP_COMMITTED:
                await ledger.release_dependents(r, keys, run_id, actor=ACTOR)
        elif until_done and run_id and await _all_terminal(r, keys, run_id):
            return  # only once caught up, so the last events are printed


async def _tail(r, keys: Keys, last: str, run_id: str | None, deadline: float | None):
    """Like events.tail_events, but yields None every second so callers can
    check exit conditions and the deadline."""
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            print("tail: timeout", file=sys.stderr)
            return
        rows = await r.xread({keys.events: last}, block=1000, count=200)
        n = 0
        for _stream, entries in rows or []:
            for sid, fields in entries:
                n += 1
                last = sid
                ev = Event.model_validate_json(fields["json"])
                ev.id = sid
                if run_id is None or ev.run_id == run_id:
                    yield ev
        if n < 200:
            yield None  # caught up with the stream


async def main_async(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ledger_core.cli", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("submit", help="submit a hand-written run spec (or --goal for the orchestrator)")
    p.add_argument("spec", nargs="?", help="path to a run spec JSON file")
    p.add_argument("--goal")
    p.add_argument("--input-file")
    p.add_argument("--example", choices=sorted(EXAMPLES))
    p.add_argument("--follow", action="store_true", help="tail until every step is terminal")
    p.add_argument("--timeout", type=float, default=120)
    p = sub.add_parser("tail", help="follow ledger events")
    p.add_argument("--run")
    p.add_argument("--new", action="store_true", help="only new events (default: replay the run first)")
    p.add_argument("--until-done", action="store_true", help="exit once every step of --run is terminal")
    p.add_argument("--timeout", type=float)
    p.add_argument("--release", action="store_true", help="release dependent steps as steps commit")
    for name in ("steps", "facts", "release"):
        p = sub.add_parser(name)
        p.add_argument("run")
        p.add_argument("--json", action="store_true")
    sub.add_parser("runs")
    p = sub.add_parser("example", help="print an example run spec")
    p.add_argument("name", choices=sorted(EXAMPLES))
    sub.add_parser("demo", help="end-to-end demo (Track G)")
    a = ap.parse_args(argv)

    if a.cmd == "demo":
        print("demo: not implemented until Track G (orchestrator); use `submit --example parse --follow`")
        return 2
    if a.cmd == "example":
        print(json.dumps(EXAMPLES[a.name], indent=2))
        return 0

    r = connect()
    keys = Keys(get_settings().ledger_ns)
    try:
        if a.cmd == "submit":
            if a.goal:
                run = await ledger.create_run(r, keys, Run(goal=a.goal, input_file=a.input_file), actor=ACTOR)
                print(f"run {run.id} created (status {run.status.value}); the orchestrator plans it")
                return 0
            if a.example:
                spec = EXAMPLES[a.example]
            elif a.spec:
                spec = json.loads(Path(a.spec).read_text())
            else:
                print("submit needs a spec path, --example or --goal", file=sys.stderr)
                return 2
            run, steps = await submit_spec(r, keys, spec)
            print(f"run {run.id} submitted: {len(steps)} step(s)")
            print(format_steps(await ledger.list_steps(r, keys, run.id)))
            if a.follow:
                await cmd_tail(r, keys, run.id, from_start=True, until_done=True, timeout=a.timeout, release=True)
                print(format_steps(await ledger.list_steps(r, keys, run.id)))
            return 0
        if a.cmd == "tail":
            await cmd_tail(r, keys, a.run, from_start=not a.new, until_done=a.until_done, timeout=a.timeout,
                           release=a.release)
            return 0
        if a.cmd == "steps":
            steps = await ledger.list_steps(r, keys, a.run)
            print(json.dumps([s.model_dump(mode="json") for s in steps], indent=2) if a.json
                  else format_steps(steps))
            return 0
        if a.cmd == "facts":
            facts = await ledger.get_facts(r, keys, a.run)
            if a.json:
                print(json.dumps(facts, indent=2))
            else:
                for k in sorted(facts, key=_fact_sort):
                    print(f"{k:<16} {json.dumps(facts[k], separators=(',', ':'))}")
            return 0
        if a.cmd == "release":
            print(" ".join(await ledger.release_dependents(r, keys, a.run, actor=ACTOR)) or "nothing to release")
            return 0
        if a.cmd == "runs":
            for run in await ledger.list_runs(r, keys):
                print(f"{run.id}  {run.status.value:<24} {_ts(run.created_at)}  {run.goal}")
            return 0
    finally:
        await r.aclose()
    return 1


def _fact_sort(k: str) -> tuple:
    head, _, tail = k.partition(":")
    return (head, int(tail) if tail.isdigit() else 0, k)


def main() -> None:
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
