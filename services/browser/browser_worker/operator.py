"""The browser operator: one Chromium context per agent, logged into EspoCRM.

Owns the session (login, storage-state reuse, re-authentication whenever the
login page shows up), navigation, evidence and the recovery loop. The CRM
skills themselves live in `skills.py`; the methods at the bottom of
`Operator` are thin wrappers so callers can write `await op.create_contact(...)`.

    async with Operator("worker-browser-1") as op:
        res = await op.create_contact("Priya", "Raman", "priya@northwind.com", ...)
        res.as_dict()  # {acted, record_id, observations, screenshots, ...}

No LLM calls here. Recovery decisions go through `recovery_chooser`
(deterministic by default; Track I may inject an LLM-backed one).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright, Response, async_playwright
from playwright.async_api import Error as PlaywrightError

from ledger_core.protocol import now_ms
from ledger_core.settings import get_settings

from . import selectors as sel
from .evidence import Evidence, SkillResult
from .formats import DEFAULT_DATE_FORMAT
from .recovery import GiveUp, PageState, RecoveryAction, RecoveryChooser, default_chooser

log = logging.getLogger(__name__)

DEFAULT_STATE_DIR = "/tmp/ledger-browser"


class SessionLost(Exception):
    """The CRM answered 401 or showed the login page in the middle of a skill."""


class UIError(Exception):
    """The page did not look the way a skill expected (recoverable by the loop)."""


FORM_ROUTE = re.compile(r"#[A-Za-z]+/(create|edit/)")

SkillBody = Callable[[SkillResult], Awaitable[None]]
Checkpoint = Callable[["Operator", str], Awaitable[None] | None]


class Operator:
    def __init__(
        self,
        agent_id: str | None = None,
        *,
        base_url: str | None = None,
        user: str | None = None,
        password: str | None = None,
        evidence_dir: str | None = None,
        state_dir: str | None = None,
        headless: bool = True,
        timeout_ms: int = 15_000,
        max_recoveries: int = 4,
        recovery_chooser: RecoveryChooser | None = None,
    ) -> None:
        s = get_settings()
        self.agent_id = agent_id or os.environ.get("AGENT_ID") or s.agent_id
        self.base_url = (base_url or s.crm_internal_url).rstrip("/")
        self.user = user or s.espo_operator_user
        self.password = password if password is not None else s.espo_operator_password
        self.evidence = Evidence(evidence_dir or s.evidence_dir)
        self.state_path = Path(state_dir or DEFAULT_STATE_DIR) / f"{self.agent_id}.storage.json"
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.max_recoveries = max_recoveries
        self.recovery_chooser: RecoveryChooser = recovery_chooser or default_chooser
        # name -> callbacks run at that point of a skill (fault injection, tests).
        self.checkpoints: dict[str, list[Checkpoint]] = {}
        self.app_user: dict[str, Any] = {}  # last /api/v1/App/user payload seen by the UI
        self.login_count = 0
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    # ------------------------------------------------------------------ lifecycle
    async def __aenter__(self) -> Operator:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._page is not None:
            return
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        storage = str(self.state_path) if self.state_path.exists() else None
        try:
            self._context = await self._browser.new_context(
                viewport={"width": 1400, "height": 900}, storage_state=storage
            )
        except PlaywrightError as e:  # corrupt state file: start clean
            log.warning("ignoring storage state %s: %s", storage, e)
            self._context = await self._browser.new_context(viewport={"width": 1400, "height": 900})
        self._context.set_default_timeout(self.timeout_ms)
        self._page = await self._context.new_page()
        self._page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        self._page.on("response", self._on_response)

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except PlaywrightError:
                    pass
        if self._pw is not None:
            await self._pw.stop()
        self._pw = self._browser = self._context = self._page = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Operator.start() first")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("Operator.start() first")
        return self._context

    async def _on_response(self, resp: Response) -> None:
        if resp.request.method == "GET" and "/api/v1/App/user" in resp.url and resp.status == 200:
            try:
                self.app_user = await resp.json()
            except Exception:  # noqa: BLE001 - body may be gone after navigation
                pass

    # ------------------------------------------------------------------ app facts
    @property
    def date_format(self) -> str:
        prefs = self.app_user.get("preferences") or {}
        settings = self.app_user.get("settings") or {}
        return prefs.get("dateFormat") or settings.get("dateFormat") or DEFAULT_DATE_FORMAT

    @property
    def user_id(self) -> str | None:
        return (self.app_user.get("user") or {}).get("id")

    def url(self, route: str) -> str:
        return f"{self.base_url}/{route}"

    # ------------------------------------------------------------------ session
    async def page_state(self) -> PageState:
        page = self.page
        try:
            if await sel.login_button(page).is_visible():
                return PageState.LOGIN
            if await sel.error_notice(page).first.is_visible():
                return PageState.ERROR
            if await sel.open_modal(page).count():
                return PageState.MODAL
            if await sel.app_navbar(page).first.is_visible():
                return PageState.APP
        except PlaywrightError:
            pass
        return PageState.UNKNOWN

    async def _wait_shell(self, timeout_ms: int | None = None) -> PageState:
        """Wait until either the logged-in shell or the login form is shown."""
        page = self.page
        either = sel.app_navbar(page).first.or_(sel.login_button(page))
        await either.first.wait_for(state="visible", timeout=timeout_ms or self.timeout_ms)
        return PageState.LOGIN if await sel.login_button(page).is_visible() else PageState.APP

    async def login(self, result: SkillResult | None = None, reason: str = "login") -> None:
        page = self.page
        if not page.url.startswith(self.base_url):
            await page.goto(self.url(""))
        state = await self._wait_shell()
        if state is PageState.APP:
            return
        if result:
            result.observe("login", f"login form shown ({reason})")
        await sel.login_username(page).fill(self.user)
        await sel.login_password(page).fill(self.password)
        await sel.login_button(page).click()
        try:
            await sel.app_navbar(page).first.wait_for(state="visible", timeout=20_000)
        except PlaywrightError as e:
            if await sel.login_button(page).is_visible():
                raise GiveUp(f"login rejected for {self.user}") from e
            raise
        self.login_count += 1
        await self.save_session()
        if result:
            result.observe("login", f"logged in as {self.user}", reason=reason)
        log.info("%s logged in to %s (%s)", self.agent_id, self.base_url, reason)

    async def save_session(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            await self.context.storage_state(path=str(self.state_path))
        except (OSError, PlaywrightError) as e:
            log.warning("could not save storage state: %s", e)

    async def ensure_logged_in(self, result: SkillResult | None = None) -> None:
        await self.start()
        if not self.page.url.startswith(self.base_url):
            await self.page.goto(self.url(""))
        if await self._wait_shell() is PageState.LOGIN:
            await self.login(result, reason="no session" if self.login_count == 0 else "session expired")

    async def expire_session(self) -> None:
        """F3 fault: drop the CRM session cookies; the next request sees the login page."""
        await self.context.clear_cookies()
        log.info("%s: session cookies cleared", self.agent_id)

    # ------------------------------------------------------------------ navigation
    async def goto(self, route: str, result: SkillResult | None = None, wait_api: str | None = None) -> None:
        """Open a hash route; re-authenticate if the login page appears, then retry once."""
        for _ in range(2):
            await self._navigate(route, wait_api)
            if await self._wait_shell() is PageState.APP:
                return
            await self.login(result, reason="session expired")
        raise SessionLost(f"login page still shown after re-login opening {route}")

    async def _navigate(self, route: str, wait_api: str | None) -> None:
        page = self.page
        target = self.url(route)
        same = page.url == target
        leaving_form = bool(FORM_ROUTE.search(page.url)) or await sel.open_modal(page).count() > 0

        async def go() -> None:
            if same:
                await page.reload()
            else:
                await page.goto(target)
            if leaving_form:
                await self.confirm_leave_form()

        if not wait_api:
            await go()
            return
        try:
            async with page.expect_response(
                lambda r: wait_api in r.url and r.request.method == "GET", timeout=self.timeout_ms
            ):
                await go()
        except PlaywrightError:
            pass  # the login page or a cached view; callers wait for their own elements

    async def confirm_leave_form(self, timeout_ms: int = 1500) -> bool:
        """Answer Espo's "leave the form?" confirm (shown after an abandoned edit)."""
        try:
            await sel.leave_form_yes(self.page).wait_for(state="visible", timeout=timeout_ms)
        except PlaywrightError:
            return False
        await sel.leave_form_yes(self.page).click()
        return True

    async def discard_forms(self) -> None:
        """Best effort after a failed skill: close an open modal form without saving."""
        try:
            if await sel.modal_cancel(self.page).count():
                await sel.modal_cancel(self.page).first.click()
                await self.confirm_leave_form()
        except PlaywrightError:
            pass

    async def wait_for(self, locator: Any, timeout_ms: int | None = None) -> None:
        """Wait for `locator`, but fail fast with SessionLost if the login form shows instead."""
        login = sel.login_button(self.page)
        await locator.first.or_(login).first.wait_for(state="visible", timeout=timeout_ms or self.timeout_ms)
        if await login.is_visible():
            raise SessionLost("login page shown")

    # ------------------------------------------------------------------ hooks
    def on_checkpoint(self, name: str, fn: Checkpoint) -> None:
        self.checkpoints.setdefault(name, []).append(fn)

    async def checkpoint(self, name: str) -> None:
        for fn in list(self.checkpoints.get(name, [])):
            out = fn(self, name)
            if inspect.isawaitable(out):
                await out

    # ------------------------------------------------------------------ skill runner
    async def shot(self, result: SkillResult, phase: str) -> None:
        label = result.data.get("label") or result.skill
        rel = self.evidence.path_for(label, phase).name
        if rel in result.screenshots:
            result.screenshots.remove(rel)  # retake after a retry, keep one entry
        await self.evidence.shot(self.page, label, phase, result)

    async def run_skill(self, skill: str, label: str | None, body: SkillBody) -> SkillResult:
        """Run `body` with session checks, before/after evidence and bounded recovery.

        Bodies must be check-then-act, so re-running one after a recovery is safe.
        """
        result = SkillResult(skill=skill)
        result.data["label"] = label or f"{self.agent_id}_{skill}_{now_ms()}"
        attempt = 0
        while True:
            try:
                await self.ensure_logged_in(result)
                if attempt == 0:
                    # Default "before"; skills retake it at their act/no-act decision point.
                    await self.shot(result, "before")
                await body(result)
                break
            except GiveUp as e:
                result.ok, result.reason = False, e.reason
                result.observe("give_up", e.reason)
                break
            except (PlaywrightError, SessionLost, UIError) as e:
                attempt += 1
                problem = f"{type(e).__name__}: {str(e).strip().splitlines()[0][:200] if str(e).strip() else ''}"
                state = await self.page_state()
                log.warning("%s: %s page during %s at %s: %s", self.agent_id, state.value, skill, self.page.url, problem)
                result.observe(state.value, f"unexpected page state during {skill}", problem=problem)
                await self.shot(result, f"recovery-{attempt}")
                action = await self._choose(state, attempt, problem)
                result.observe("recovery", f"chose {action.value}", action=action.value, attempt=attempt)
                if action is RecoveryAction.GIVE_UP:
                    result.ok, result.reason = False, f"gave up after {attempt} recoveries: {problem}"
                    break
                try:
                    await self._apply(action, result)
                except GiveUp as g:
                    result.ok, result.reason = False, g.reason
                    break
                except (PlaywrightError, SessionLost) as e2:
                    result.observe("recovery", f"{action.value} failed", problem=str(e2)[:200])
        await self.shot(result, "after")
        if not result.ok:
            await self.discard_forms()
        result.data.setdefault("recoveries", attempt)
        return result

    async def _choose(self, state: PageState, attempt: int, problem: str) -> RecoveryAction:
        if attempt > self.max_recoveries:
            return RecoveryAction.GIVE_UP
        try:
            out = self.recovery_chooser(state, attempt, problem)
            if inspect.isawaitable(out):
                out = await out
            return RecoveryAction(out)
        except Exception as e:  # noqa: BLE001 - a broken chooser must not wedge the worker
            log.warning("recovery chooser failed (%s); using default", e)
            return default_chooser(state, attempt, problem)

    async def _apply(self, action: RecoveryAction, result: SkillResult) -> None:
        page = self.page
        if action is RecoveryAction.RELOAD:
            await page.reload()
            await self._wait_shell()
        elif action is RecoveryAction.RELOGIN:
            if await self.page_state() is not PageState.LOGIN:
                await page.goto(self.url(""))
            await self.login(result, reason="recovery")
        elif action is RecoveryAction.HOME:
            await page.goto(self.url(sel.ROUTE_HOME))
            await self._wait_shell()
        elif action is RecoveryAction.GIVE_UP:
            raise GiveUp("recovery chose give_up")

    # ------------------------------------------------------------------ skills
    async def search_contact(self, email: str, label: str | None = None) -> SkillResult:
        from . import skills

        return await skills.search_contact(self, email, label=label)

    async def search_by_name_company(self, name: str, company: str | None, label: str | None = None) -> SkillResult:
        from . import skills

        return await skills.search_by_name_company(self, name, company, label=label)

    async def create_contact(self, first: str, last: str, email: str | None, **kw: Any) -> SkillResult:
        from . import skills

        return await skills.create_contact(self, first, last, email, **kw)

    async def update_contact(self, contact_id: str, **kw: Any) -> SkillResult:
        from . import skills

        return await skills.update_contact(self, contact_id, **kw)

    async def create_task(
        self, contact_id: str, subject: str, due_date: str | date, owner_user_name: str | None = None, **kw: Any
    ) -> SkillResult:
        from . import skills

        return await skills.create_task(self, contact_id, subject, due_date, owner_user_name, **kw)


