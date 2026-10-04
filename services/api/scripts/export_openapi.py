"""Write the API's OpenAPI schema (make openapi -> web/src/api/schema.json)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.main import app


def main(out: str) -> None:
    schema = app.openapi()
    Path(out).write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}: {len(schema['paths'])} paths")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "web/src/api/schema.json")
