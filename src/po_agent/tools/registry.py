from __future__ import annotations

import re
from typing import Any

from ..errors import ToolInputError
from ..models import Tool, ToolSpec

_PY_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
}


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {t.spec.name: t for t in tools}

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self._tools.values()]


def validate_arguments(spec: ToolSpec, args: dict[str, Any]) -> None:
    """Minimal JSON-schema check. The model can send junk, or text an attacker planted."""
    schema = spec.input_schema
    props = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in args:
            raise ToolInputError(f"missing required argument '{name}'")
    for name, value in args.items():
        if name not in props:
            if schema.get("additionalProperties") is False:
                raise ToolInputError(f"unexpected argument '{name}'")
            continue
        rule = props[name]
        expected = _PY_TYPES.get(rule.get("type", ""))
        if expected and (
            not isinstance(value, expected) or (expected is int and isinstance(value, bool))
        ):
            raise ToolInputError(f"argument '{name}' must be {rule['type']}")
        if isinstance(value, str):
            if len(value) > rule.get("maxLength", 10_000):
                raise ToolInputError(f"argument '{name}' is too long")
            if "pattern" in rule and not re.fullmatch(rule["pattern"], value):
                raise ToolInputError(f"argument '{name}' has an invalid format")
        if isinstance(value, int | float) and not isinstance(value, bool):
            if value < rule.get("minimum", value) or value > rule.get("maximum", value):
                raise ToolInputError(f"argument '{name}' is out of range")
