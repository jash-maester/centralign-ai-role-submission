"""file.parsed_rows: verify a parse claim against the source file (plans/01 §8).

The check re-reads the source file itself, with its own minimal CSV and
header logic (it does not import the parser), and checks:
- the required canonical columns exist in the file header (default name, email)
- the claim's sha256 (if given) matches the file
- row accounting: every non-empty data row 1..N appears exactly once, either as
  a usable row or as a flagged row
- each usable row's email is the source row's email trimmed + lowercased, and
  its name matches the source name ignoring case/whitespace
- no two usable rows share an email
- every flagged row's reason agrees with the source (phone_only rows really have
  no email; duplicate_in_file rows really repeat an earlier usable email)
- optional expect: {"total_rows": n, "usable": n, "flagged": n}

args:   {"file": "<name under DATA_DIR or absolute path>"}
expect: {"required_columns": [...], "total_rows": n, "usable": n, "flagged": n}

Facts on commit (kind file.parse): "lead:<row>" per usable row (the normalised
record incl. region) and "parse.summary" (source, sha256, counts, usable row
numbers, flagged rows with reasons and records, stats).
"""

from __future__ import annotations

import csv
import hashlib
import re
from pathlib import Path
from typing import Any

from ..postconditions import CheckContext, CheckResult, register
from ..protocol import Claim, Step, StepKind
from ..verifier import register_facts


def _canon(header: str) -> str | None:
    t = re.sub(r"[^a-z]", "", header.lower())
    if "mail" in t:
        return "email"
    if t.startswith(("company", "org", "account")):
        return "company"
    if "phone" in t or "mobile" in t or t.startswith("tel"):
        return "phone"
    if "name" in t:
        return "name"
    if t in ("title", "jobtitle", "role", "position"):
        return "title"
    if t.startswith("country"):
        return "country"
    return None


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).casefold()


def _read_source(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    header = rows[0] if rows else []
    cols: dict[str, int] = {}
    for i, h in enumerate(header):
        c = _canon(h)
        if c and c not in cols:
            cols[c] = i
    data = [r for r in rows[1:] if any(cell.strip() for cell in r)]
    return header, [{c: (r[i] if i < len(r) else "") for c, i in cols.items()} for r in data]


def _resolve(file: str, data_dir: str) -> Path:
    p = Path(file)
    return p if p.is_absolute() else Path(data_dir) / p


@register("file.parsed_rows")
async def parsed_rows(args: dict[str, Any], expect: dict[str, Any], ctx: CheckContext) -> CheckResult:
    file = args.get("file") or (ctx.extra.get("inputs") or {}).get("file")
    if not file:
        return CheckResult(False, "postcondition has no source file (args.file)")
    path = _resolve(file, ctx.data_dir)
    if not path.is_file():
        return CheckResult(False, f"source file {file} not found by the verifier", {"path": str(path)})
    header, src = _read_source(path)
    n = len(src)
    observed: dict[str, Any] = {"file": path.name, "file_rows": n, "header": header}

    required = expect.get("required_columns") or ["name", "email"]
    present = {c for c in (_canon(h) for h in header) if c}
    missing_cols = [c for c in required if c not in present]
    if missing_cols:
        return CheckResult(False, f"source file lacks required columns: {', '.join(missing_cols)}", observed)

    claim = ctx.claim or {}
    rows, flagged = claim.get("rows"), claim.get("flagged")
    if not isinstance(rows, list) or not isinstance(flagged, list):
        return CheckResult(False, f"claim accounts for 0 of {n} rows in {path.name}: "
                                  "no parsed rows or flagged rows in the claim", observed)
    observed.update(claimed_usable=len(rows), claimed_flagged=len(flagged))
    problems: list[str] = []

    sha = claim.get("sha256")
    if sha and sha != hashlib.sha256(path.read_bytes()).hexdigest():
        problems.append("claimed sha256 does not match the source file")

    numbers = [x.get("row") for x in rows + flagged if isinstance(x, dict)]
    if sorted(n_ for n_ in numbers if isinstance(n_, int)) != list(range(1, n + 1)) or len(numbers) != n:
        problems.append(f"row accounting: claim covers rows {sorted(set(map(str, numbers)))} "
                        f"but the file has rows 1..{n}")

    emails_seen: dict[str, int] = {}
    for rec in rows:
        i = rec.get("row")
        if not isinstance(i, int) or not 1 <= i <= n:
            continue
        s = src[i - 1]
        want = (s.get("email") or "").strip().lower()
        email = rec.get("email") or ""
        if not email or "@" not in email:
            problems.append(f"row {i}: usable row without a valid email")
        elif email != want:
            problems.append(f"row {i}: email {email!r} != source {want!r}")
        if _norm(rec.get("name")) != _norm(s.get("name")):
            problems.append(f"row {i}: name {rec.get('name')!r} != source {s.get('name', '').strip()!r}")
        if email in emails_seen:
            problems.append(f"row {i}: email {email!r} already used by row {emails_seen[email]}")
        emails_seen.setdefault(email, i)

    for fl in flagged:
        i, reason = fl.get("row"), fl.get("reason")
        if not isinstance(i, int) or not 1 <= i <= n:
            continue
        if not reason:
            problems.append(f"row {i}: flagged without a reason")
            continue
        src_email = (src[i - 1].get("email") or "").strip().lower()
        if reason == "phone_only" and src_email:
            problems.append(f"row {i}: flagged phone_only but the source has email {src_email!r}")
        if reason == "duplicate_in_file":
            first = fl.get("duplicate_of")
            if emails_seen.get(src_email) != first or not src_email:
                problems.append(f"row {i}: flagged duplicate of row {first} but that row is not a usable "
                                f"row with the same email")

    for k in ("total_rows", "usable", "flagged"):
        if k in expect:
            got = {"total_rows": n, "usable": len(rows), "flagged": len(flagged)}[k]
            if got != expect[k]:
                problems.append(f"expected {k}={expect[k]}, got {got}")

    if problems:
        return CheckResult(False, "; ".join(problems[:8]) + (" ..." if len(problems) > 8 else ""),
                           {**observed, "problems": problems})
    return CheckResult(True, f"{n} source rows accounted for: {len(rows)} usable, {len(flagged)} flagged; "
                             "emails and names match the source", observed)


@register_facts(StepKind.FILE_PARSE)
def parse_facts(step: Step, claim: Claim, result: CheckResult) -> dict[str, Any]:
    data = claim.data
    facts: dict[str, Any] = {f"lead:{rec['row']}": rec for rec in data.get("rows", [])}
    facts["parse.summary"] = {
        "source": data.get("source"), "sha256": data.get("sha256"), "total_rows": data.get("total_rows"),
        "usable_rows": [rec["row"] for rec in data.get("rows", [])],
        "flagged": data.get("flagged", []), "stats": data.get("stats", {}),
    }
    return facts
