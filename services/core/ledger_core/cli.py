"""Ledger CLI (python -m ledger_core.cli ...). Runs in the `test` container:

    docker compose run --rm test python -m ledger_core.cli submit tests/specs/parse_one_step.json
    docker compose run --rm test python -m ledger_core.cli submit --example parse --follow
    docker compose run --rm test python -m ledger_core.cli submit --goal "Process yesterday's leads" \
        --file event_attendees.csv [--crm-write-path api] [--set key=value] [--follow]
    docker compose run --rm test python -m ledger_core.cli demo [--crm-write-path api]   (= make demo)
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
moves dependency-free steps to ready. `release RUN` (or `submit --follow`)
moves planned steps whose dependencies are committed to ready (hand-written
specs only). `submit --goal` creates the run (with its RunConfig from the
playbook + overrides) for the orchestrator, which understands, plans and runs it.

`demo` submits the one-line goal with data/event_attendees.csv, starts
in-process stand-ins for any agent the run needs that is not alive
(orchestrator, parser, api.espocrm worker, verifier with the CRM reader), follows
the events until the run finishes, and prints criteria verdicts, step counts and
one line per lead lane. CRM_WRITE_PATH=api|browser|auto sets the route.

`approvals list [--run R] [--all] [--json]` shows human escalations (Track J);
`approvals answer ESC ANSWER [--save-as-rule] [--wait|--local]` answers one with
an option value (e.g. link_account:<id>, skip): the decision fact is committed,
input.answered emitted and that lane released.
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
    StepStatus,
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
    steps = build_steps(spec, run.id, cfg)
    if steps:
        # A hand-written plan is born `running` without criteria, so the
        # orchestrator never runs understand/plan (LLM) on it (plans/01 §5 W2 note).
        run.status = RunStatus.RUNNING
    await ledger.create_run(r, keys, run, actor=actor)
    if spec.get("config"):
        await ledger.set_run_config(r, keys, run.id, cfg, actor=actor)
    if steps:
        await ledger.create_steps(r, keys, steps, actor=actor, payload={"source": "hand-written spec"})
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


async def _run_done(r, keys: Keys, run_id: str) -> bool:
    run = await ledger.get_run(r, keys, run_id)
    return run is not None and run.status in RUN_DONE


async def cmd_tail(r, keys: Keys, run_id: str | None, *, from_start: bool, until_done: bool,
                   timeout: float | None, release: bool = False, until_run_done: bool = False,
                   quiet: bool = False) -> None:
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
            if not quiet or ev.type in RUN_EVENTS:
                print(format_event(ev), flush=True)
            if release and run_id and ev.type == EventType.STEP_COMMITTED:
                await ledger.release_dependents(r, keys, run_id, actor=ACTOR)
        elif until_done and run_id and await _all_terminal(r, keys, run_id):
            return  # only once caught up, so the last events are printed
        elif until_run_done and run_id and await _run_done(r, keys, run_id):
            return


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
    sub_submit = sub.choices["submit"]
    sub_submit.add_argument("--file", dest="input_file_alias", help="input file under DATA_DIR (with --goal)")
    sub_submit.add_argument("--crm-write-path", choices=["browser", "auto", "api"])
    sub_submit.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                            help="RunConfig override (repeatable), e.g. --set replan_after_rejections=1")
    p = sub.add_parser("demo", help="submit the one-line goal and follow the run to the end (Track G)")
    p.add_argument("--goal", default=DEMO_GOAL)
    p.add_argument("--file", default="event_attendees.csv")
    p.add_argument("--crm-write-path", choices=["browser", "auto", "api"],
                   default=os.environ.get("CRM_WRITE_PATH") or None)
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--timeout", type=float, default=float(os.environ.get("DEMO_TIMEOUT", "300")))
    p.add_argument("--no-local", action="store_true",
                   help="do not start in-process stand-ins for missing agents (orchestrator, parser, api worker, verifier)")
    p.add_argument("--quiet", action="store_true", help="do not print every event")
    p = sub.add_parser("approvals", help="human escalations (Track J): list | answer")
    asub = p.add_subparsers(dest="approvals_cmd", required=True)
    q = asub.add_parser("list", help="open escalations (all runs, or --run)")
    q.add_argument("--run")
    q.add_argument("--all", action="store_true", help="answered ones too")
    q.add_argument("--json", action="store_true")
    q = asub.add_parser("answer", help="answer an escalation with one of its option values (or labels)")
    q.add_argument("escalation")
    q.add_argument("answer")
    q.add_argument("--save-as-rule", action="store_true", help="append the answer as a playbook rule")
    q.add_argument("--by", default="cli")
    q.add_argument("--note")
    q.add_argument("--wait", action="store_true", help="follow the run until it settles again")
    q.add_argument("--local", action="store_true",
                   help="start in-process stand-ins for missing agents while waiting (implies --wait)")
    q.add_argument("--timeout", type=float, default=120)
    a = ap.parse_args(argv)

    if a.cmd == "demo":
        return await cmd_demo(a)
    if a.cmd == "approvals":
        return await cmd_approvals(a)
    if a.cmd == "example":
        print(json.dumps(EXAMPLES[a.name], indent=2))
        return 0

    r = connect()
    keys = Keys(get_settings().ledger_ns)
    try:
        if a.cmd == "submit":
            if a.goal:
                from .orchestrator import submit_goal

                run = await submit_goal(r, keys, a.goal, input_file=a.input_file or a.input_file_alias,
                                        config=_overrides(a), actor=ACTOR)
                print(f"run {run.id} created (status {run.status.value}); the orchestrator plans it")
                if a.follow:
                    await cmd_tail(r, keys, run.id, from_start=True, until_done=False, timeout=a.timeout,
                                   until_run_done=True)
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


DEMO_GOAL = "Add the leads from yesterday's event to the CRM and set up follow-ups."
RUN_DONE = frozenset({RunStatus.COMPLETED, RunStatus.COMPLETED_PENDING_INPUT, RunStatus.FAILED})
RUN_EVENTS = frozenset({EventType.RUN_CREATED, EventType.RUN_UNDERSTOOD, EventType.PLAN_CREATED,
                        EventType.PLAN_REVISED, EventType.RUN_STATUS, EventType.RUN_COMPLETED,
                        EventType.RUN_COMPLETED_PENDING_INPUT, EventType.RUN_FAILED, EventType.STEP_DEAD,
                        EventType.STEP_REJECTED})


def _overrides(a: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in getattr(a, "set", None) or []:
        k, _, v = item.partition("=")
        try:
            out[k.strip()] = json.loads(v)
        except ValueError:
            out[k.strip()] = v
    if getattr(a, "crm_write_path", None):
        out["crm_write_path"] = a.crm_write_path
    return out


def format_lanes(lanes: list[dict[str, Any]]) -> str:
    lines = [f"{'lane':<9} {'name':<16} {'email':<32} {'status':<12} {'action':<9} {'owner':<8} detail"]
    for x in lanes:
        detail = x.get("reason") or ""
        if x.get("task"):
            detail = (detail + " " if detail else "") + f"task due {x['task'].get('due')} [{x['task'].get('status')}]"
        lines.append(f"{x['lane']:<9} {(x.get('name') or '-')[:16]:<16} {(x.get('email') or '-')[:32]:<32} "
                     f"{x['status']:<12} {x.get('action') or '-':<9} {x.get('owner') or '-':<8} {detail}")
    return "\n".join(lines)


def format_summary(run: Run, steps: list[Step], lanes: list[dict[str, Any]]) -> str:
    out = [f"\nrun {run.id}: {run.status.value}", f"goal: {run.goal}", "", "success criteria:"]
    for c in run.criteria:
        out.append(f"  [{c.status:<8}] {c.id} {c.text}  ({c.check})")
        if c.evidence:
            out.append(f"             {c.evidence}")
    counts: dict[str, dict[str, int]] = {}
    for s in steps:
        counts.setdefault(s.kind.value, {}).setdefault(s.status.value, 0)
        counts[s.kind.value][s.status.value] += 1
    out += ["", f"steps ({len(steps)}):"]
    for kind, by in sorted(counts.items()):
        out.append(f"  {kind:<20} " + ", ".join(f"{n} {st}" for st, n in sorted(by.items())))
    skills = sorted({s.skill.value for s in steps})
    out += [f"  skills used: {', '.join(skills)}", "", "lanes:", format_lanes(lanes)]
    return "\n".join(out)


async def cmd_demo(a: argparse.Namespace) -> int:
    """Submit the one-line goal, make sure every needed agent is up (in-process
    stand-ins for missing ones), follow the run to the end, print the result."""
    from pathlib import Path as _P

    from . import postconditions
    from .orchestrator import submit_goal
    from .orchestrator_lanes import lane_outcomes
    from .orchestrator_local import LocalAgents, served

    s = get_settings()
    # test container: data and playbooks are mounted under /repo, not /app
    for env, have, alt in (("DATA_DIR", s.data_dir, "/repo/data"), ("PLAYBOOK_DIR", s.playbook_dir, "/repo/playbooks")):
        if not _P(have).is_dir() and _P(alt).is_dir():
            os.environ[env] = alt
    get_settings.cache_clear()
    postconditions.load_all()
    r = connect()
    keys = Keys(get_settings().ledger_ns)
    try:
        overrides = _overrides(a)
        path = overrides.get("crm_write_path") or RunConfig().crm_write_path
        crm_skill = {"api": "api.espocrm", "browser": "browser.espocrm", "auto": "browser.espocrm"}[path]
        need = ["orchestrator", "verifier", "file.parse", crm_skill, "review", "email.draft", "email.send"]
        if path == "auto":
            need.append("api.espocrm")
        live = await served(r, keys)
        print(f"llm backend: {os.environ.get('LLM_BACKEND') or get_settings().llm_backend}; crm_write_path: {path}")
        print("live agents: " + (", ".join(f"{k}={v}" for k, v in sorted(live.items())) or "none"))
        if a.no_local:
            need = []
        # An explicit LLM_BACKEND for the demo (e.g. scripted) must not be overridden by
        # a live orchestrator service using its own backend: run our own, pinned to this run.
        own = bool(os.environ.get("LLM_BACKEND")) and "orchestrator" in need
        async with LocalAgents(r, keys, need=need, own_orchestrator=own) as local:
            if local.started:
                print("in-process stand-ins: " + ", ".join(local.started))
            if crm_skill == "browser.espocrm" and not live.get(crm_skill):
                print("warning: no browser operator is alive; CRM steps will wait (use --crm-write-path api)")
            pin = local.orchestrator.agent_id if (own and local.orchestrator) else None
            run = await submit_goal(r, keys, a.goal, input_file=a.file, config=overrides, actor=ACTOR,
                                    orchestrator=pin)
            print(f"run {run.id} submitted: {a.goal!r} with {a.file}")
            await cmd_tail(r, keys, run.id, from_start=True, until_done=False, timeout=a.timeout,
                           until_run_done=True, quiet=a.quiet)
        run = await ledger.get_run(r, keys, run.id)
        steps = await ledger.list_steps(r, keys, run.id)
        facts = await ledger.get_facts(r, keys, run.id)
        print(format_summary(run, steps, lane_outcomes(steps, facts)))
        return 0 if run.status in (RunStatus.COMPLETED, RunStatus.COMPLETED_PENDING_INPUT) else 1
    finally:
        await r.aclose()


def format_escalation(e: Any) -> str:
    lines = [f"{e.id}  run {e.run_id}  {e.lane or e.step_id}  [{e.status}]  confidence {e.confidence:.2f} "
             f"< threshold {e.threshold:.2f}", f"  Q: {e.question}"]
    for o in e.options:
        lines.append(f"    - {o.value:<36} {o.label}" + (f"  ({o.detail})" if o.detail else ""))
    for t in e.tried[:8]:
        lines.append(f"  tried: {t}")
    if e.answer:
        lines.append(f"  answer: {e.answer}" + ("  (saved as playbook rule)" if e.save_as_rule else ""))
    return "\n".join(lines)


async def cmd_approvals(a: argparse.Namespace) -> int:
    from . import escalations

    r = connect()
    keys = Keys(get_settings().ledger_ns)
    try:
        if a.approvals_cmd == "list":
            items = await escalations.list_escalations(r, keys, run_id=a.run, status="all" if a.all else "open")
            if a.json:
                print(json.dumps([json.loads(e.model_dump_json()) for e in items], indent=2))
            elif not items:
                print("no open escalations" if not a.all else "no escalations")
            else:
                print("\n\n".join(format_escalation(e) for e in items))
            return 0
        try:
            out = await escalations.answer(r, keys, a.escalation, a.answer, by=a.by, save_as_rule=a.save_as_rule,
                                           note=a.note)
        except escalations.EscalationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        esc = out["escalation"]
        print(f"answered {esc['id']} ({esc.get('lane')}): {esc['answer']} -> fact {out['fact_key']}; "
              f"step {esc['step_id']} is {out['step_status']}")
        if out.get("rule"):
            print(f"saved playbook rule ({out['rule']['playbook']} v{out['rule']['version']}): {out['rule']['text']}")
        if a.wait or a.local:
            from contextlib import AsyncExitStack

            from .orchestrator_lanes import lane_outcomes
            from .orchestrator_local import LocalAgents

            run_id = esc["run_id"]
            async with AsyncExitStack() as stack:
                if a.local:
                    from . import postconditions

                    postconditions.load_all()
                    local = await stack.enter_async_context(LocalAgents(
                        r, keys, need=["orchestrator", "verifier", "review", "api.espocrm"]))
                    if local.started:
                        print("in-process stand-ins: " + ", ".join(local.started))
                await _wait_lane_settled(r, keys, run_id, esc.get("lane"), a.timeout)
            run = await ledger.get_run(r, keys, run_id)
            steps = await ledger.list_steps(r, keys, run_id)
            facts = await ledger.get_facts(r, keys, run_id)
            lanes = [x for x in lane_outcomes(steps, facts) if not esc.get("lane") or x["lane"] == esc.get("lane")]
            print(f"run {run_id}: {run.status.value}")
            print(format_lanes(lanes))
        return 0
    finally:
        await r.aclose()


async def _wait_lane_settled(r, keys: Keys, run_id: str, lane: str | None, timeout: float) -> None:
    """Poll until the lane has no step moving (all terminal or waiting on a human) and the
    run is in a done status again."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    moving = {StepStatus.READY, StepStatus.LEASED, StepStatus.CLAIMED_DONE, StepStatus.VERIFIED,
              StepStatus.REJECTED, StepStatus.LEASE_EXPIRED, StepStatus.PLANNED}
    await asyncio.sleep(1.0)
    while loop.time() < deadline:
        steps = [s for s in await ledger.list_steps(r, keys, run_id) if lane is None or s.lane == lane]
        run = await ledger.get_run(r, keys, run_id)
        if steps and not any(s.status in moving for s in steps) and run and run.status in RUN_DONE:
            return
        await asyncio.sleep(0.5)
    print(f"(timeout after {timeout:.0f}s; the lane is still moving)")


def _fact_sort(k: str) -> tuple:
    head, _, tail = k.partition(":")
    return (head, int(tail) if tail.isdigit() else 0, k)


def main() -> None:
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
