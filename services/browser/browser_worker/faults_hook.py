"""Browser-side fault hooks (F3 expire_session, F5 ui_changed), read through
ledger_core.faults so every fault switch has a single reader/writer module.

For the browser worker loop (Track I), around each leased step:

    fired = await faults_hook.before_step(op, r, keys, step, agent_id=AGENT_ID)
    try:
        res = await dispatch.execute(op, step.kind, inputs, label=...)
    finally:
        faults_hook.after_step(fired)

Switch fields consulted, most specific first: "<fault>:<agent_id>",
"<fault>:browser.espocrm", "<fault>". Each shot consumed appends
fault.injected (phase=consumed) for the step.
"""

from __future__ import annotations

from typing import Any

from ledger_core import faults
from ledger_core.protocol import FaultName, Skill

from . import selectors


async def before_step(op: Any, r: Any, keys: Any, step: Any, *, agent_id: str) -> list[str]:
    """Apply armed browser faults to this step; returns the faults that fired."""
    fired: list[str] = []
    scopes = [agent_id, Skill.BROWSER_ESPOCRM.value]
    if await faults.consume_and_record(
        r, keys, FaultName.EXPIRE_SESSION, scope=scopes, actor=agent_id, run_id=step.run_id, step_id=step.id,
        effect="CRM session cookies cleared before the skill; the operator must log in again",
    ):
        await op.expire_session()
        fired.append(FaultName.EXPIRE_SESSION.value)
    if await faults.consume_and_record(
        r, keys, FaultName.UI_CHANGED, scope=scopes, actor=agent_id, run_id=step.run_id, step_id=step.id,
        effect="Save selector broken for this step; the skill gives up after bounded recovery",
    ):
        selectors.set_ui_changed(True)
        fired.append(FaultName.UI_CHANGED.value)
    return fired


def after_step(fired: list[str]) -> None:
    """Undo per-step fault effects (the broken selector lasts one step per shot)."""
    if FaultName.UI_CHANGED.value in fired:
        selectors.set_ui_changed(False)
