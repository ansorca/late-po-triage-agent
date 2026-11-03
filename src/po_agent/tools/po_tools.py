"""Tool definitions the LLM sees: a name, a description, a JSON schema, and a handler."""

from __future__ import annotations

from typing import Any

from ..models import Tool, ToolContext, ToolSpec
from .registry import ToolRegistry
from .stub_backend import StubBackend

_ID = {"type": "string", "maxLength": 32, "pattern": "[A-Za-z0-9_-]+"}


_DAYS = {"type": "integer", "minimum": 0, "maximum": 365}


def build_registry(backend: StubBackend) -> ToolRegistry:
    async def list_late_orders(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return await backend.list_late_orders(args.get("min_days_late", 1))

    async def get_supplier_info(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return await backend.get_supplier(args["supplier_id"])

    async def get_inventory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return await backend.get_inventory(args["sku"])

    async def propose_expedite(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        assert ctx.idempotency_key, "side-effecting tools always get a key"
        return await backend.expedite(args["po_id"], ctx.idempotency_key)

    return ToolRegistry(
        [
            Tool(
                ToolSpec(
                    "list_late_orders",
                    "List open purchase orders that are late by at least min_days_late days.",
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "min_days_late": {"type": "integer", "minimum": 0, "maximum": 365}
                        },
                        "required": [],
                    },
                    cache_ttl_s=5,
                ),
                list_late_orders,
            ),
            Tool(
                ToolSpec(
                    "get_supplier_info",
                    "Get a supplier's name and historical on-time reliability (0..1).",
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"supplier_id": _ID},
                        "required": ["supplier_id"],
                    },
                    cache_ttl_s=300,
                ),
                get_supplier_info,
            ),
            Tool(
                ToolSpec(
                    "get_inventory",
                    "Get on-hand stock and daily demand for a SKU.",
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"sku": _ID},
                        "required": ["sku"],
                    },
                    cache_ttl_s=10,
                ),
                get_inventory,
            ),
            Tool(
                ToolSpec(
                    "propose_expedite",
                    "Ask the supplier to expedite a purchase order. Changes real state.",
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"po_id": _ID},
                        "required": ["po_id"],
                    },
                    side_effecting=True,
                ),
                propose_expedite,
            ),
        ]
    )
