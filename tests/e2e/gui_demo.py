"""Drive the Ledger demo from the browser against the real API (Track L).

Runs inside the browser image (Playwright + Chromium) on the compose network:

    docker compose run --rm --no-deps -v ./tests/e2e:/e2e:ro -v ./web/screenshots/real:/out \
        worker-browser-1 python /e2e/gui_demo.py http://web /out

Needs the full stack up and seeded with LLM_BACKEND=scripted. Steps, each with
a screenshot: submit the goal in the Builder, watch Dashboard progress, inject
a false claim from Run controls, answer the Sam Ito escalation (save as rule),
open the Report and export markdown. Fails on console/page errors and on any
failed /api request.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://web").rstrip("/")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/out")
TIMEOUT_S = float(sys.argv[3]) if len(sys.argv) > 3 else 420.0
OUT.mkdir(parents=True, exist_ok=True)

GOAL = "Add the leads from yesterday's event to the CRM and set up follow-ups."
errors: list[str] = []
api_calls: list[str] = []
T0 = time.time()


def log(msg: str) -> None:
    print(f"[{time.time() - T0:6.1f}s] {msg}", flush=True)


def shot(page: Page, name: str, full: bool = False) -> None:
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=full)
    log(f"saved {name}.png")


def api_json(page: Page, path: str):
    return page.evaluate("async (p) => { const r = await fetch('/api' + p); return r.ok ? r.json() : null; }", path)


def run_state(page: Page, run_id: str) -> dict:
    return api_json(page, f"/runs/{run_id}") or {}


def wait_until(fn, timeout: float, every: float = 1.0, what: str = "condition"):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}")


def on_response(resp) -> None:
    url = resp.url
    if "/api/" not in url:
        return
    path = url.split("/api", 1)[1]
    api_calls.append(f"{resp.request.method} {path.split('?')[0]} {resp.status}")
    # 404 on a just-created run's report/evidence is fine; every other error is a defect
    if resp.status >= 400 and not (resp.status == 404 and "/evidence/" in path):
        errors.append(f"api: {resp.request.method} {path} -> {resp.status}")


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1, accept_downloads=True)
        page = ctx.new_page()
        page.on("console", lambda m: m.type == "error" and errors.append(f"console: {m.text}"))
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("response", on_response)

        # 1. Builder: submit the goal --------------------------------------------------
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.wait_for_selector("[data-testid=dashboard]", timeout=20000)
        page.locator("[data-testid=nav-builder]:visible").click()
        page.wait_for_selector("[data-testid=node-ledger]")
        page.fill("[data-testid=builder-goal]", GOAL)
        time.sleep(0.5)
        shot(page, "01-builder-goal")
        with page.expect_response(lambda r: r.url.endswith("/api/runs") and r.request.method == "POST") as resp_info:
            page.click("[data-testid=builder-run]")
        resp = resp_info.value
        assert resp.status == 201, f"POST /runs -> {resp.status}"
        run_id = resp.json()["id"]
        log(f"POST /runs -> 201 {run_id}")
        wait_until(lambda: (run_state(page, run_id).get("steps_total") or 0) > 3, 90, what="the plan")
        time.sleep(4)
        shot(page, "02-builder-running")

        # 2. Dashboard progress -----------------------------------------------------------
        page.locator("[data-testid=nav-dashboard]:visible").click()
        page.wait_for_selector("[data-testid=run-strip]")
        assert run_id in page.url, f"dashboard should follow the new run ({page.url})"
        wait_until(lambda: (run_state(page, run_id).get("steps_committed") or 0) >= 6, 180, what="progress")
        time.sleep(1.5)
        st = run_state(page, run_id)
        log(f"progress: {st.get('steps_committed')}/{st.get('steps_total')} committed, status {st.get('status')}")
        shot(page, "03-dashboard-progress")

        # 3. Inject a false claim from Run controls ------------------------------------------
        btn = page.locator("[data-testid=fault-false_claim]")
        btn.scroll_into_view_if_needed()
        with page.expect_response(lambda r: "/api/chaos/false_claim" in r.url) as fr:
            btn.click()
        log(f"POST /chaos/false_claim -> {fr.value.status}")
        assert fr.value.status < 300

        def rejected() -> bool:
            evs = (api_json(page, f"/runs/{run_id}/events?type=step.rejected&limit=1000") or {}).get("events", [])
            return bool(evs)

        wait_until(rejected, 240, every=2, what="the verifier to reject the false claim")
        log("verifier rejected the false claim")
        time.sleep(2)
        page.locator("[data-testid=live-ledger]").scroll_into_view_if_needed()
        shot(page, "04-false-claim-rejected")

        # 4. Answer the Sam Ito escalation ---------------------------------------------------------
        page.evaluate("window.scrollTo(0, 0)")
        esc = page.locator("[data-testid=escalation]", has_text="Sam")
        esc.first.wait_for(timeout=int(TIMEOUT_S * 1000))
        time.sleep(1)
        shot(page, "05-escalation-sam-ito")
        card = esc.first
        radios = card.locator("button[role=radio]")
        labels = [radios.nth(i).inner_text() for i in range(radios.count())]
        log(f"Sam Ito options: {labels}")
        pick = next((i for i, t in enumerate(labels) if re.search(r"link|Harbor", t, re.I)), 0)
        radios.nth(pick).click()
        rule = card.locator("input[type=checkbox]")
        if rule.count():
            rule.first.check()
        with page.expect_response(lambda r: "/api/escalations/" in r.url and r.request.method == "POST") as er:
            card.locator("button", has_text="Commit").click()
        log(f"POST /escalations/{{id}} -> {er.value.status} ({labels[pick]!r}, save as rule)")
        assert er.value.status < 300
        page.wait_for_selector("[data-testid=escalation] >> text=Sam", state="detached", timeout=20000)
        time.sleep(1.5)
        shot(page, "06-escalation-answered")

        # 5. Run settles; Report ----------------------------------------------------------------
        def settled():
            s = run_state(page, run_id)
            if s.get("status") in ("completed", "completed_pending_input", "failed"):
                return s
            # approvals may need a human: answer them so the run can finish
            for e in api_json(page, f"/escalations?run_id={run_id}&status=open") or []:
                card = page.locator("[data-testid=escalation]").first
                if card.count():
                    card.locator("button[role=radio]").first.click()
                    card.locator("button", has_text="Commit").click()
                    log(f"answered escalation {e.get('id')} ({e.get('question', '')[:60]})")
                    time.sleep(2)
            return None

        st = wait_until(settled, TIMEOUT_S, every=3, what="the run to settle")
        log(f"run {run_id} settled: {st.get('status')} ({st.get('steps_committed')}/{st.get('steps_total')} committed)")
        time.sleep(2)
        page.set_viewport_size({"width": 1440, "height": 1600})
        shot(page, "07-dashboard-settled")

        page.locator("[data-testid=nav-report]:visible").click()
        page.wait_for_selector("[data-testid=report]", timeout=20000)
        time.sleep(2)
        text = page.inner_text("[data-testid=report]")
        assert "Evidence report" in text, "report heading"
        shot(page, "08-report", full=True)
        md_btn = page.locator("[data-testid=report] button", has_text=re.compile("markdown|\\.md|Export", re.I))
        if md_btn.count():
            with page.expect_download(timeout=15000) as dl:
                md_btn.first.click()
            path = OUT / "report.md"
            dl.value.save_as(str(path))
            log(f"exported {path.name}: {path.stat().st_size} bytes")
        browser.close()

    seen = sorted({re.sub(r"(run|stp|esc|step)_[0-9a-zA-Z]+", r"\1_*", c) for c in api_calls})
    print("API endpoints hit:\n  " + "\n  ".join(seen))
    if errors:
        print("\n".join(dict.fromkeys(errors)))
        print(f"FAIL: {len(errors)} console/page/API errors")
        return 1
    print(f"OK: demo driven from the browser in {time.time() - T0:.0f}s, run {run_id}")
    (OUT / "result.json").write_text(json.dumps({"run_id": run_id, "status": st.get("status")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
