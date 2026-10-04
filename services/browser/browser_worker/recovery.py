"""Unexpected page states and the small, fixed set of recovery actions.

Navigation is coded; when a skill meets a page it did not expect, the operator
classifies the page and picks ONE of `RecoveryAction`. The default chooser is
deterministic. Track I may inject an LLM-backed chooser with the same
signature (plans/03 Phase 4: "recovering from unexpected page states by
choosing among a small set of recovery actions"); its answer is validated
against the enum, so it can never invent an action.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum


class PageState(StrEnum):
    LOGIN = "login"  # login form visible: session expired / never logged in
    APP = "app"  # logged-in shell with the main navigation
    MODAL = "modal"  # an unexpected dialog is open over the app
    ERROR = "error"  # Espo error notice (4xx/5xx) visible
    UNKNOWN = "unknown"  # blank, still loading, or something else


class RecoveryAction(StrEnum):
    RELOAD = "reload"
    RELOGIN = "relogin"
    HOME = "home"
    GIVE_UP = "give_up"


class GiveUp(Exception):
    """Raised when recovery is exhausted; carries a human-readable reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# chooser(state, attempt, problem) -> action. attempt counts from 1.
RecoveryChooser = Callable[[PageState, int, str], RecoveryAction | Awaitable[RecoveryAction]]


def default_chooser(state: PageState, attempt: int, problem: str) -> RecoveryAction:
    """Login page -> re-login; otherwise reload, then go home, then give up."""
    if state is PageState.LOGIN:
        return RecoveryAction.RELOGIN if attempt <= 3 else RecoveryAction.GIVE_UP
    if attempt == 1:
        return RecoveryAction.RELOAD
    if attempt == 2:
        return RecoveryAction.HOME
    return RecoveryAction.GIVE_UP
