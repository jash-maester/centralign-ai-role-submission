"""Playbook loading and sectioning (plans/01-architecture.md §7 layer 2, B6, E5).

A playbook is markdown with YAML front matter (run-config defaults, version)
split into sections by `## ` headings. Workers receive only the sections
relevant to their step kind (SECTIONS_BY_KIND). Run records pin
`playbook_hash` (sha256 of the file) so replays can tell when it changed.

SECTIONS_BY_KIND (B6). Titles are matched case-insensitively; a title the
playbook lacks is skipped. `sections_for(None)` / "all" returns everything
(the orchestrator's understand/plan calls).

| Step kind            | Sections                                         |
|----------------------|--------------------------------------------------|
| file.parse           | Dedupe rules                                     |
| crm.search_contact   | Dedupe rules                                     |
| crm.create_contact   | Dedupe rules, Owner routing                      |
| crm.update_contact   | Dedupe rules, Owner routing                      |
| crm.create_task      | Follow-up policy, Owner routing                  |
| email.draft          | Follow-up policy                                 |
| email.send           | Approval policy, Follow-up policy                |
| review.ambiguity     | Dedupe rules, Owner routing, Escalation rules    |
| review.approval      | Approval policy, Follow-up policy, Escalation rules |
| human.decide         | Escalation rules                                 |
| run.verify           | Definitions of done                              |

"Save as rule" (E5) appends a bullet to a section with append_rule(); the
version in the front matter is bumped and the hash changes.
"""

from __future__ import annotations

import fcntl
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import RunConfig
from .protocol import StepKind
from .settings import get_settings

DEFAULT_PLAYBOOK = "event-leads.md"

SECTIONS_BY_KIND: dict[str, tuple[str, ...]] = {
    StepKind.FILE_PARSE: ("Dedupe rules",),
    StepKind.CRM_SEARCH_CONTACT: ("Dedupe rules",),
    StepKind.CRM_CREATE_CONTACT: ("Dedupe rules", "Owner routing"),
    StepKind.CRM_UPDATE_CONTACT: ("Dedupe rules", "Owner routing"),
    StepKind.CRM_CREATE_TASK: ("Follow-up policy", "Owner routing"),
    StepKind.EMAIL_DRAFT: ("Follow-up policy",),
    StepKind.EMAIL_SEND: ("Approval policy", "Follow-up policy"),
    StepKind.REVIEW_AMBIGUITY: ("Dedupe rules", "Owner routing", "Escalation rules"),
    StepKind.REVIEW_APPROVAL: ("Approval policy", "Follow-up policy", "Escalation rules"),
    StepKind.HUMAN_DECIDE: ("Escalation rules",),
    StepKind.RUN_VERIFY: ("Definitions of done",),
}
ALL = "all"


class PlaybookError(ValueError):
    pass


@dataclass(frozen=True)
class Section:
    title: str
    body: str  # text under the heading, without the heading line

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")

    def markdown(self) -> str:
        return f"## {self.title}\n\n{self.body}".rstrip() + "\n"


@dataclass(frozen=True)
class Playbook:
    name: str
    front_matter: dict[str, Any]
    preamble: str  # text between front matter and the first "## " heading
    sections: tuple[Section, ...]
    content_hash: str
    path: Path | None = None
    text: str = field(default="", repr=False)

    @property
    def version(self) -> int | str | None:
        return self.front_matter.get("version")

    @property
    def short_hash(self) -> str:
        return self.content_hash[:4]

    @property
    def titles(self) -> list[str]:
        return [s.title for s in self.sections]

    def section(self, title: str) -> Section | None:
        want = title.strip().lower()
        return next((s for s in self.sections if s.title.lower() == want), None)

    def sections_for(self, step_kind: str | None) -> list[Section]:
        if step_kind is None or step_kind == ALL:
            return list(self.sections)
        titles = SECTIONS_BY_KIND.get(step_kind)  # StepKind is a StrEnum: str keys match
        if titles is None:
            raise PlaybookError(f"no playbook section mapping for step kind {step_kind!r}")
        return [s for t in titles if (s := self.section(t)) is not None]

    def run_defaults(self) -> RunConfig:
        """Front matter over env defaults (run_config.defaults)."""
        from .run_config import defaults

        return defaults(self)


def parse(text: str, name: str = DEFAULT_PLAYBOOK, path: Path | None = None) -> Playbook:
    front: dict[str, Any] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("\n---", 1)
        if len(parts) != 2:
            raise PlaybookError(f"{name}: unterminated front matter")
        front = yaml.safe_load(parts[0][3:]) or {}
        body = parts[1].split("\n", 1)[1] if "\n" in parts[1] else ""
        if not isinstance(front, dict):
            raise PlaybookError(f"{name}: front matter must be a mapping")
    chunks = re.split(r"(?m)^## ", body)
    preamble = chunks[0].strip()
    sections = []
    for chunk in chunks[1:]:
        heading, _, rest = chunk.partition("\n")
        sections.append(Section(heading.strip(), rest.strip() + "\n"))
    return Playbook(
        name=name, front_matter=front, preamble=preamble, sections=tuple(sections),
        content_hash=hashlib.sha256(text.encode()).hexdigest(), path=path, text=text,
    )


def playbook_path(name: str = DEFAULT_PLAYBOOK, playbook_dir: str | Path | None = None) -> Path:
    base = Path(playbook_dir or get_settings().playbook_dir)
    if not name.endswith(".md"):
        name = f"{name}.md"
    path = (base / name).resolve()
    if base.resolve() not in path.parents:
        raise PlaybookError(f"playbook name escapes the playbook dir: {name!r}")
    return path


def load(name: str = DEFAULT_PLAYBOOK, playbook_dir: str | Path | None = None) -> Playbook:
    path = playbook_path(name, playbook_dir)
    if not path.is_file():
        raise PlaybookError(f"playbook not found: {path}")
    return parse(path.read_text(), path.name, path)


def append_rule(section: str, text: str, *, name: str = DEFAULT_PLAYBOOK, playbook_dir: str | Path | None = None,
                source: str | None = None) -> Playbook:
    """Append "- <text>" to the end of `section` (created if missing), bump version.

    Used for "save as playbook rule" on an escalation answer (E5). The file is
    locked while rewritten so concurrent saves don't lose rules. Returns the
    reloaded playbook (new version and hash). The caller emits the event.
    """
    rule = " ".join(text.split())
    if not rule:
        raise PlaybookError("empty rule")
    if source:
        rule += f" _(saved rule: {' '.join(source.split())})_"
    path = playbook_path(name, playbook_dir)
    with open(path, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            original = f.read()
            updated = _bump_version(_insert_rule(original, section, rule))
            f.seek(0)
            f.write(updated)
            f.truncate()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return parse(updated, path.name, path)


def _insert_rule(text: str, section: str, rule: str) -> str:
    lines = text.rstrip("\n").split("\n")
    want = section.strip().lower()
    start = next((i for i, line in enumerate(lines) if line.startswith("## ") and line[3:].strip().lower() == want), None)
    if start is None:
        return "\n".join(lines) + f"\n\n## {section.strip()}\n\n- {rule}\n"
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    last = end - 1
    while last > start and not lines[last].strip():
        last -= 1
    lines.insert(last + 1, f"- {rule}")
    return "\n".join(lines) + "\n"


def _bump_version(text: str) -> str:
    if not text.startswith("---"):
        return text
    head, sep, rest = text.partition("\n---")
    bumped, n = re.subn(r"(?m)^version:\s*(\d+)\s*$", lambda m: f"version: {int(m.group(1)) + 1}", head, count=1)
    return (bumped if n else head) + sep + rest
