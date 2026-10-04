"""Track L acceptance: drive the demo from the browser against the real API.

Needs the full stack up and seeded (LLM_BACKEND=scripted) and runs inside the
browser image; `make test-gui` sets GUI_E2E_BASE. Excluded from `make test`
(marker e2e) and skipped anywhere GUI_E2E_BASE is unset.
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.e2e]

BASE = os.environ.get("GUI_E2E_BASE")


@pytest.mark.skipif(not BASE, reason="GUI_E2E_BASE not set (run `make test-gui` with the stack up)")
def test_gui_demo(tmp_path: Path) -> None:
    pytest.importorskip("playwright")
    out = os.environ.get("GUI_E2E_OUT") or str(tmp_path)
    argv = sys.argv
    sys.argv = ["gui_demo.py", BASE, out]
    try:
        mod = runpy.run_path(str(Path(__file__).with_name("gui_demo.py")), run_name="gui_demo")
        assert mod["main"]() == 0
    finally:
        sys.argv = argv
