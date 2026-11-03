"""Input validation and authorization hooks.

The agent trusts nothing it is given: client ids are checked for shape, tool arguments (which
the model chose, possibly after reading attacker-controlled text) are authorized per user.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Protocol

from .config import Settings
from .errors import PermissionDenied
from .models import TriageRequest

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")  # no ':' or whitespace, so composite keys cannot collide


def validate_request(request: TriageRequest, settings: Settings) -> str | None:
    """Return a reason string if the request must be rejected, else None."""
    if not _ID.fullmatch(request.request_id):
        return "invalid request_id"
    if not _ID.fullmatch(request.planner_id):
        return "invalid planner_id"
    if not request.question.strip():
        return "empty question"
    if len(request.question) > settings.max_question_chars:
        return "question too long"
    return None


class Authorizer(Protocol):
    """Decides whether `principal` may run `tool` with `args`. Raise PermissionDenied to refuse."""

    async def authorize(self, principal: str, tool: str, args: dict[str, Any]) -> None: ...


class AllowAll:
    """Skeleton default. Replace before exposing to real users (see THREAT_MODEL.md)."""

    async def authorize(self, principal: str, tool: str, args: dict[str, Any]) -> None:
        return None


class PoOwnershipAuthorizer:
    """Example policy: a principal may only act on purchase orders assigned to them."""

    def __init__(self, owned_pos: Mapping[str, frozenset[str]]) -> None:
        self._owned = owned_pos

    async def authorize(self, principal: str, tool: str, args: dict[str, Any]) -> None:
        po_id = args.get("po_id")
        if po_id is not None and po_id not in self._owned.get(principal, frozenset()):
            raise PermissionDenied(f"{principal} may not access {po_id}")
