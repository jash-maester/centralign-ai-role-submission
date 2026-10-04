"""Smoke-test a VITE_MOCK=1 build of the web GUI and save screenshots.

Runs inside the browser image (Playwright + Chromium):

    docker compose run --rm --no-deps -v ./web/scripts:/scripts -v ./web/screenshots:/out \
        worker-browser-1 python /scripts/screenshots.py http://host.docker.internal:20531

Loads Dashboard, Report and Builder, drives the main interactions (graph tab,
step drawer, agent sheet + shell, escalation answer, builder Run) and fails on
any console error or uncaught page error.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://host.docker.internal:20531"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/out")
OUT.mkdir(parents=True, exist_ok=True)

errors: list[str] = []


def shot(page, name: str) -> None:
    page.screenshot(path=str(OUT / f"{name}.png"))
    print(f"saved {name}.png")


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 3300}, device_scale_factor=1)
        page = ctx.new_page()
        page.on("console", lambda m: m.type == "error" and errors.append(f"console: {m.text}"))
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_selector("[data-testid=dashboard]")
        page.wait_for_selector("[data-testid=escalation]", timeout=15000)
        page.wait_for_selector("[data-testid=agents-table]")
        time.sleep(1.5)
        assert page.locator("[data-testid=needs-badge]").inner_text().strip().endswith("1"), "Needs you badge should count 1 escalation"
        shot(page, "01-dashboard")

        page.click("[data-testid=wf-steps]")
        page.wait_for_selector("[data-testid=steps-table]")
        page.click("[data-testid=wf-graph]")
        page.wait_for_selector(".react-flow__node")
        time.sleep(1)
        n_nodes = page.locator(".react-flow__node").count()
        assert n_nodes > 40, f"graph should render every step, got {n_nodes}"
        page.locator("[data-testid=step-graph]").screenshot(path=str(OUT / "02-graph.png"))
        print("saved 02-graph.png")

        page.set_viewport_size({"width": 1440, "height": 1000})
        page.locator('.react-flow__node[data-id="stp_51"]').click()
        page.wait_for_selector("[data-testid=step-drawer]")
        page.wait_for_selector("[data-testid=attempt]")
        time.sleep(0.5)
        assert page.locator("[data-testid=attempt]").count() >= 2, "takeover step should show both attempts"
        shot(page, "03-step-drawer")
        page.keyboard.press("Escape")

        page.click("[data-testid=wf-agents]")
        page.locator("[data-testid=agents-table] button", has_text="Prompt").first.click()
        page.wait_for_selector("[data-testid=agent-sheet]")
        page.wait_for_selector("textarea")
        time.sleep(0.5)
        shot(page, "04-agent-prompt")
        page.locator("[data-testid=agent-sheet] button", has_text="Shell").click()
        page.wait_for_selector(".xterm")
        time.sleep(0.8)
        page.locator(".xterm").click()
        page.keyboard.type("ps\n")
        time.sleep(0.6)
        shot(page, "05-agent-shell")
        page.keyboard.press("Escape")
        page.locator("body").click(position={"x": 5, "y": 300})
        page.keyboard.press("Escape")

        # Answer the escalation from Needs your attention.
        page.set_viewport_size({"width": 1440, "height": 3300})
        esc = page.locator("[data-testid=escalation]").first
        esc.locator("button[role=radio]").first.click()
        esc.locator("button", has_text="Commit decision").click()
        page.wait_for_selector("[data-testid=escalation]", state="detached", timeout=10000)
        time.sleep(1)

        page.locator("[data-testid=nav-report]:visible").click()
        page.wait_for_selector("[data-testid=report]", timeout=15000)
        time.sleep(1.5)
        assert "Evidence report" in page.inner_text("[data-testid=report]")
        page.set_viewport_size({"width": 1440, "height": 4200})
        time.sleep(0.5)
        shot(page, "06-report")

        page.set_viewport_size({"width": 1440, "height": 900})
        page.locator("[data-testid=nav-builder]:visible").click()
        page.wait_for_selector("[data-testid=node-ledger]")
        time.sleep(1.2)
        shot(page, "07-builder")
        page.click("[data-testid=builder-run]")
        time.sleep(3.5)
        shot(page, "08-builder-running")

        page.locator("[data-testid=nav-dashboard]:visible").click()
        page.wait_for_selector("[data-testid=dashboard]")
        page.locator("button", has_text="Dark").first.click()
        page.set_viewport_size({"width": 1440, "height": 1400})
        time.sleep(1)
        shot(page, "09-dashboard-dark")

        browser.close()

    if errors:
        print("\n".join(errors))
        print(f"FAIL: {len(errors)} console/page errors")
        return 1
    print("OK: Dashboard, Report and Builder rendered with no console errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
