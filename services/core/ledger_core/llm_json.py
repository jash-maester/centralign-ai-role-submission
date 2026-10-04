"""JSON helpers for LLM output: strict schemas, instructions, lenient parsing.

Used by every LLM backend so a model without `response_format` support can
still return a validated pydantic object (plans/01-architecture.md §6):
"JSON in a fenced block + pydantic validate + one re-ask".
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from pydantic import BaseModel

_FENCE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def schema_name(schema: type[BaseModel]) -> str:
    """OpenAI-style schema name: [a-zA-Z0-9_-], at most 64 chars."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", schema.__name__)[:64] or "Output"


def json_schema(schema: type[BaseModel]) -> dict[str, Any]:
    return schema.model_json_schema()


def strict_schema(schema: dict[str, Any]) -> dict[str, Any] | None:
    """Return a copy usable with `strict: true`, or None if it can't be strict.

    Strict mode needs every object closed (additionalProperties false) with all
    properties required. Free-form dicts (dict[str, Any]) can't be expressed,
    so such schemas are sent non-strict.
    """
    out = copy.deepcopy(schema)

    def walk(node: Any) -> bool:
        if isinstance(node, list):
            return all(walk(n) for n in node)
        if not isinstance(node, dict):
            return True
        node.pop("default", None)
        is_object = node.get("type") == "object" or "properties" in node
        if is_object:
            props = node.get("properties")
            if not props:
                return False  # free-form mapping
            extra = node.get("additionalProperties", False)
            if extra not in (False, None):
                return False
            node["additionalProperties"] = False
            node["required"] = list(props.keys())
        for key in ("properties", "$defs", "definitions"):
            if isinstance(node.get(key), dict) and not all(walk(v) for v in node[key].values()):
                return False
        for key in ("items", "anyOf", "allOf", "oneOf", "prefixItems"):
            if key in node and not walk(node[key]):
                return False
        return True

    return out if walk(out) else None


def instruction(schema: dict[str, Any]) -> str:
    """Prompt text asking for bare JSON (for models without response_format)."""
    return (
        "Respond with a single JSON object that matches this JSON Schema. "
        "Output only the JSON, no prose. A ```json fenced block is acceptable.\n"
        + json.dumps(schema, sort_keys=True, separators=(",", ":"))
    )


def extract_json(text: str) -> Any:
    """Parse bare JSON, a fenced ```json block, or the first {...} span."""
    if text is None:
        raise ValueError("empty response")
    cleaned = _THINK.sub("", text).strip()
    if not cleaned:
        raise ValueError("empty response")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    for block in _FENCE.findall(cleaned):
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError:
            continue
    span = _first_object(cleaned)
    if span is not None:
        try:
            return json.loads(span)
        except json.JSONDecodeError:
            pass
    raise ValueError("no JSON object found in the response")


def _first_object(text: str) -> str | None:
    """The first balanced {...} span, honouring strings and escapes."""
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        start = text.find("{", start + 1)
    return None


def parse_as[T: BaseModel](schema: type[T], text: str) -> T:
    """extract_json + pydantic validation. Raises ValueError on either failure."""
    data = extract_json(text)
    try:
        return schema.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError is a ValueError subclass
        raise ValueError(str(exc)) from exc
