"""Export the protocol models as JSON Schema for the web client (make schema)."""

import json
import sys
from pathlib import Path

from pydantic import TypeAdapter

from ledger_core import protocol as p
from ledger_core.config import RunConfig

MODELS = [p.Run, p.Step, p.Event, p.Fact, p.AgentCard, p.Escalation, p.ReviewDecision, p.Envelope, RunConfig]
ENUMS = [p.StepStatus, p.RunStatus, p.EventType, p.Skill, p.StepKind]

out = {
    "models": {m.__name__: m.model_json_schema(by_alias=True) for m in MODELS},
    "enums": {e.__name__: [v.value for v in e] for e in ENUMS},
    "legal_transitions": {k.value: sorted(v.value for v in vs) for k, vs in p.LEGAL_TRANSITIONS.items()},
}
dest = Path(sys.argv[1])
dest.parent.mkdir(parents=True, exist_ok=True)
dest.write_text(json.dumps(out, indent=2, sort_keys=True))
print(f"wrote {dest}")
