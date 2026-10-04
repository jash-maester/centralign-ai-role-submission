"""Helpers for the e2e suite (Track M): drive the running compose stack from the
`test` container over its own network, the way a user or the GUI would.

    API      http://api:8000      (runs, chaos, escalations, report)
    CRM      http://espocrm       (admin REST, for resets and duplicate checks)
    Mailpit  http://mailpit:8025  (the outbox)
    Docker   /var/run/docker.sock (kill / restart compose services; tests/e2e/compose.e2e.yml)

Every scenario starts from a "clean" world: CRM reset to exactly data/crm_seed.json
(asserted, not assumed), Mailpit empty, no armed faults, every agent alive.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import httpx

from ledger_core.settings import get_settings

REPO = Path(os.environ.get("REPO_DIR", "/repo"))
API_URL = os.environ.get("E2E_API_URL", "http://api:8000")
DOCKER_SOCK = "/var/run/docker.sock"
GOAL = "Add the leads from yesterday's event to the CRM and set up follow-ups."
TASK_SUBJECT = "Follow up: Signal Summit"
SETTLED = ("completed", "completed_pending_input", "failed")
AGENTS = ("orchestrator", "verifier", "meta-reviewer", "worker-api", "worker-parser", "worker-drafter",
          "worker-mailer", "worker-browser-1", "worker-browser-2")

# plans/02 "Demo data": row -> (outcome, owner, email)
EXPECTED = {
    1: ("created", "a.chen", "priya@northwind.com"),
    2: ("updated", "r.silva", "marcus.lee@acme.com"),
    3: ("created", "r.silva", "lena@kestrel-labs.io"),
    4: ("created", "a.chen", "dana@helixbio.com"),
    5: ("created", "r.silva", "tom.becker@orbitalfreight.com"),
    6: ("skipped", None, "lena@kestrel-labs.io"),
    7: ("updated", "a.chen", "ben.ortiz@gmail.com"),
    8: ("updated", "a.chen", "hannah@fieldstone.dev"),
    9: ("waiting", None, "sam@lumen.io"),
    10: ("created", "a.chen", "omar@brightline.co"),
    11: ("skipped", None, None),
    12: ("created", "r.silva", "grace.wu@tallgrass.com"),
}
CREATED = {n: v for n, v in EXPECTED.items() if v[0] == "created"}
EMAIL_ROWS = (1, 2, 3, 4, 5, 8, 10, 12)
EMAIL_TO = {EXPECTED[n][2] for n in EMAIL_ROWS}
LUMEN_INC = "Lumen Inc"  # the human answer for Sam Ito (EMEA, owner r.silva)


def log(msg: str) -> None:
    print(f"[e2e {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wait_for(pred: Callable[[], Any], timeout: float, what: str, every: float = 2.0) -> Any:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            got = pred()
            if got:
                return got
        except (httpx.HTTPError, OSError) as exc:  # stack restarting: keep polling
            last_exc = exc
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}"
                         + (f" (last error: {last_exc})" if last_exc else ""))


# ---- API ------------------------------------------------------------------------------------


class Api:
    def __init__(self, base: str = API_URL) -> None:
        self.http = httpx.Client(base_url=base, timeout=30)

    def get(self, path: str, **params: Any) -> Any:
        resp = self.http.get(path, params=params or None)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        resp = self.http.post(path, json=body or {})
        resp.raise_for_status()
        return resp.json()

    # runs
    def submit(self, config: dict[str, Any] | None = None) -> str:
        run = self.post("/runs", {"goal": GOAL, "input_file": "event_attendees.csv", "config": config or {}})
        log(f"submitted {run['id']} config={config or {}}")
        return run["id"]

    def run(self, run_id: str) -> dict[str, Any]:
        return self.get(f"/runs/{run_id}")

    def wait_settled(self, run_id: str, timeout: float = 420, statuses: tuple[str, ...] = SETTLED) -> dict[str, Any]:
        t0 = time.monotonic()
        run = wait_for(lambda: (lambda x: x if x["status"] in statuses else None)(self.run(run_id)),
                       timeout, f"run {run_id} to reach {statuses}", every=3)
        log(f"{run_id} {run['status']} after {time.monotonic() - t0:.0f}s; steps {run.get('step_counts')}")
        return run

    def steps(self, run_id: str) -> list[dict[str, Any]]:
        return self.get(f"/runs/{run_id}/steps")

    def facts(self, run_id: str) -> dict[str, Any]:
        return {k: v.get("value") for k, v in self.get(f"/runs/{run_id}/facts").items()}

    def fact_records(self, run_id: str) -> dict[str, Any]:
        return self.get(f"/runs/{run_id}/facts")

    def events(self, run_id: str, *types: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        after = None
        while True:
            params: dict[str, Any] = {"limit": 1000}
            if after:
                params["after"] = after
            if types:
                params["type"] = list(types)
            page = self.get(f"/runs/{run_id}/events", **params)
            out += page["events"]
            after = page.get("next")
            if not after:
                return out

    def report(self, run_id: str) -> dict[str, Any]:
        return self.get(f"/runs/{run_id}/report")

    def escalations(self, run_id: str, status: str = "open") -> list[dict[str, Any]]:
        return self.get("/escalations", run_id=run_id, status=status)

    def answer(self, esc_id: str, answer: str, by: str = "e2e") -> dict[str, Any]:
        return self.post(f"/escalations/{esc_id}", {"answer": answer, "by": by, "save_as_rule": False})

    def inject(self, fault: str, **body: Any) -> dict[str, Any]:
        out = self.post(f"/chaos/{fault}", body)
        log(f"injected {fault}: {json.dumps({k: v for k, v in out.items() if k != 'kill'})}")
        return out

    def clear_faults(self) -> None:
        for fault in self.get("/chaos")["faults"]:
            self.http.delete(f"/chaos/{fault}").raise_for_status()

    def agents(self) -> list[dict[str, Any]]:
        return self.get("/agents")

    def wait_agents(self, names: tuple[str, ...] = AGENTS, timeout: float = 120) -> None:
        def ok() -> bool:
            alive = {a["id"] for a in self.agents() if a.get("alive")}
            return set(names) <= alive
        wait_for(ok, timeout, f"agents alive: {', '.join(names)}")

    def healthy(self) -> bool:
        try:
            return self.http.get("/health", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False


# ---- CRM (admin REST) --------------------------------------------------------------------------


class Crm:
    def __init__(self) -> None:
        s = get_settings()
        self.http = httpx.Client(base_url=f"{s.crm_internal_url}/api/v1",
                                 auth=(s.espo_admin_user, s.espo_admin_password), timeout=30)
        self.seed = json.loads((REPO / "data/crm_seed.json").read_text())

    def _list(self, entity: str, **params: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            resp = self.http.get(f"/{entity}", params={"maxSize": 200, "offset": offset, **params})
            resp.raise_for_status()
            page = resp.json().get("list", [])
            out += page
            if len(page) < 200:
                return out
            offset += 200

    def contacts(self) -> list[dict[str, Any]]:
        """Every contact with all its email addresses (lowercased) and owner."""
        out = []
        for c in self._list("Contact", select="id,firstName,lastName,emailAddress,assignedUserName,accountName"):
            full = self.http.get(f"/Contact/{c['id']}").json()
            emails = sorted({(e.get("emailAddress") or "").lower() for e in full.get("emailAddressData") or []}
                            | ({full["emailAddress"].lower()} if full.get("emailAddress") else set()))
            out.append({**c, "emails": emails, "assignedUserName": full.get("assignedUserName"),
                        "assignedUserId": full.get("assignedUserId"), "accountName": full.get("accountName"),
                        "phoneNumber": full.get("phoneNumber")})
        return out

    def contacts_with(self, email: str) -> list[dict[str, Any]]:
        params = {"where[0][type]": "equals", "where[0][attribute]": "emailAddress", "where[0][value]": email}
        return self._list("Contact", **params)

    def tasks(self, contact_id: str | None = None) -> list[dict[str, Any]]:
        if contact_id is None:
            return self._list("Task")
        params = {"where[0][type]": "equals", "where[0][attribute]": "parentId", "where[0][value]": contact_id}
        return [{**t, **self.http.get(f"/Task/{t['id']}").json()} for t in self._list("Task", **params)]

    def users(self) -> dict[str, str]:
        return {u["userName"]: u["id"] for u in self._list("User", select="id,userName")}

    # reset to exactly the seed --------------------------------------------------------------

    def reset(self) -> None:
        seed_emails = {c["emailAddress"].lower(): c for c in self.seed["contacts"]}
        seed_accounts = {a["name"]: a for a in self.seed["accounts"]}
        seed_opps = {o["name"] for o in self.seed["opportunities"]}
        users = self.users()
        for t in self._list("Task", select="id"):
            self.http.delete(f"/Task/{t['id']}")
        for o in self._list("Opportunity", select="id,name"):
            if o["name"] not in seed_opps:
                self.http.delete(f"/Opportunity/{o['id']}")
        accounts = {a["name"]: a["id"] for a in self._list("Account", select="id,name")}
        for name, aid in accounts.items():
            if name not in seed_accounts:
                self.http.delete(f"/Account/{aid}")
        for c in self.contacts():
            mine = [e for e in c["emails"] if e in seed_emails]
            if not mine:
                self.http.delete(f"/Contact/{c['id']}")
                continue
            spec = seed_emails[mine[0]]
            data = {k: v for k, v in spec.items() if k not in ("account", "assignedUser")}
            data["emailAddressData"] = [{"emailAddress": spec["emailAddress"], "primary": True, "optOut": False,
                                         "invalid": False}]
            data["phoneNumberData"] = ([{"phoneNumber": spec["phoneNumber"], "primary": True, "type": "Mobile",
                                         "optOut": False, "invalid": False}] if spec.get("phoneNumber") else [])
            data["phoneNumber"] = spec.get("phoneNumber")
            data["accountId"] = accounts.get(seed_accounts_name(self.seed, spec["account"]))
            data["assignedUserId"] = users[spec["assignedUser"]]
            self.http.put(f"/Contact/{c['id']}", json=data).raise_for_status()

    def state(self) -> dict[str, Any]:
        """Canonical CRM state for the 'clean' assertion."""
        return {
            "contacts": sorted((tuple(c["emails"]), c.get("firstName"), c.get("lastName"), c.get("assignedUserName"),
                                c.get("accountName")) for c in self.contacts()),
            "accounts": sorted(a["name"] for a in self._list("Account", select="id,name")),
            "tasks": len(self._list("Task", select="id")),
            "opportunities": sorted(o["name"] for o in self._list("Opportunity", select="id,name")),
        }

    def seed_state(self) -> dict[str, Any]:
        names = {u["userName"]: f"{u['firstName']} {u['lastName']}" for u in self.seed["users"]}
        return {
            "contacts": sorted(((c["emailAddress"].lower(),), c["firstName"], c["lastName"], names[c["assignedUser"]],
                                seed_accounts_name(self.seed, c["account"])) for c in self.seed["contacts"]),
            "accounts": sorted(a["name"] for a in self.seed["accounts"]),
            "tasks": 0,
            "opportunities": sorted(o["name"] for o in self.seed["opportunities"]),
        }

    def duplicates(self) -> dict[str, int]:
        """email -> number of contacts carrying it, for every email on more than one contact."""
        n = Counter(e for c in self.contacts() for e in c["emails"])
        return {e: k for e, k in n.items() if k > 1}


def seed_accounts_name(seed: dict[str, Any], key: str) -> str:
    return next(a["name"] for a in seed["accounts"] if a["key"] == key)


# ---- Mailpit ---------------------------------------------------------------------------------------


class Mailpit:
    def __init__(self) -> None:
        self.http = httpx.Client(base_url=f"{get_settings().mailpit_api_url}/api/v1", timeout=15)

    def clear(self) -> None:
        self.http.delete("/messages").raise_for_status()

    def messages(self) -> list[dict[str, Any]]:
        return self.http.get("/messages", params={"limit": 500}).json().get("messages", [])

    def recipients(self) -> list[str]:
        return sorted(t["Address"].lower() for m in self.messages() for t in m.get("To") or [])


# ---- Docker engine (socket from tests/e2e/compose.e2e.yml) ------------------------------------------


class Docker:
    def __init__(self) -> None:
        self.http = httpx.Client(transport=httpx.HTTPTransport(uds=DOCKER_SOCK), base_url="http://docker",
                                 timeout=120)
        self.project = os.environ.get("COMPOSE_PROJECT_NAME") or "ledger"

    @staticmethod
    def available() -> bool:
        return Path(DOCKER_SOCK).exists()

    def containers(self, service: str | None = None) -> list[dict[str, Any]]:
        labels = [f"com.docker.compose.project={self.project}", "com.docker.compose.oneoff=False"]
        if service:
            labels.append(f"com.docker.compose.service={service}")
        resp = self.http.get("/containers/json", params={"all": "1", "filters": json.dumps({"label": labels})})
        resp.raise_for_status()
        return resp.json()

    def _one(self, service: str) -> str:
        found = self.containers(service)
        assert found, f"no container for {service} in project {self.project}"
        return found[0]["Id"]

    def restart(self, *services: str) -> None:
        for svc in services:
            self.http.post(f"/containers/{self._one(svc)}/restart", params={"t": "5"}).raise_for_status()

    def start(self, service: str) -> None:
        resp = self.http.post(f"/containers/{self._one(service)}/start")
        assert resp.status_code in (204, 304), resp.text

    def running(self, service: str) -> bool:
        return bool(self.containers(service)) and self.containers(service)[0]["State"] == "running"


# ---- the world ------------------------------------------------------------------------------------


def reset_world(api: Api, crm: Crm, mail: Mailpit) -> None:
    """'From clean': CRM == data/crm_seed.json, empty outbox, no faults, all agents up,
    no other run still moving."""
    api.wait_agents()
    for run in api.get("/runs", limit=20):
        if run["status"] not in SETTLED:
            api.wait_settled(run["id"], timeout=300)
    api.clear_faults()
    crm.reset()
    mail.clear()
    assert crm.state() == crm.seed_state(), "CRM reset did not reproduce the seed"
    assert mail.messages() == []


def outcomes(report: dict[str, Any]) -> dict[int, str]:
    return {lead["n"]: lead["outcome"] for lead in report["leads"]}


def normalized_plan(api: Api, run_id: str) -> dict[str, Any]:
    """The run's plan without volatile values (ids, timestamps): run.understood,
    plan.created, the fan-out, and the full step graph (kind, lane, skill, title,
    inputs keys, dependencies by title)."""
    evs = {e["type"]: e for e in reversed(api.events(run_id, "run.understood", "plan.created"))}
    steps = api.steps(run_id)
    by_id = {s["id"]: s for s in steps}

    def strip_steps(rows: list[dict[str, Any]]) -> list[tuple]:
        return [(s["kind"], s.get("lane"), s.get("skill"), s.get("title"),
                 tuple(sorted(by_id.get(d, {}).get("title", d) for d in s.get("depends_on") or []))) for s in rows]

    created = dict(evs["plan.created"]["payload"])
    created["steps"] = strip_steps(created.get("steps") or [])
    understood = {k: v for k, v in evs["run.understood"]["payload"].items() if k not in ("playbook_hash",)}
    graph = sorted((s["kind"], s.get("lane") or "", s["skill"], s["title"],
                    tuple(sorted(by_id[d]["title"] for d in s.get("depends_on") or [] if d in by_id)),
                    tuple(sorted((s.get("inputs") or {}).keys())), s["postcondition"]["check"]) for s in steps)
    return {"understood": understood, "plan_created": created, "graph": graph}
