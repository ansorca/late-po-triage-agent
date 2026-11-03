"""A deterministic stand-in for the model, so everything runs offline with no AWS account.

It is stateless: it decides the next step purely by reading the conversation so far, exactly
like a real model would. It has a tiny "policy": look up late POs, gather inventory + supplier
data in parallel, expedite the critical ones, then summarise.
"""

from __future__ import annotations

from ..models import LLMResponse, Message, ToolCall, ToolResult, ToolSpec

COVER_DAYS_CRITICAL = 5  # fewer days of stock than this => critical


class FakeLLM:
    def __init__(self, transient_failures: int = 0) -> None:
        self._failures_left = transient_failures  # for tests: fail N times then work

    async def complete(
        self, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse:
        response = await self._decide(messages)
        used_in = sum(len(str(m)) for m in messages) // 4  # crude ~4 chars/token estimate
        used_out = len(str(response.text)) // 4 + 20 * len(response.tool_calls)
        return LLMResponse(response.text, response.tool_calls, used_in, used_out)

    async def _decide(self, messages: list[Message]) -> LLMResponse:
        from ..errors import TransientError

        if self._failures_left > 0:
            self._failures_left -= 1
            raise TransientError("fake llm: simulated throttling")

        issued = [c for m in messages for c in m.tool_calls]
        issued_names = {c.name for c in issued}
        results = [r for m in messages for r in m.tool_results]
        by_name: dict[str, list[ToolResult]] = {}
        for r in results:
            by_name.setdefault(r.name, []).append(r)

        def call(name: str, **args: object) -> ToolCall:
            return ToolCall(id=f"call_{len(issued) + 1}", name=name, arguments=dict(args))

        def numbered(calls: list[tuple[str, dict]]) -> tuple[ToolCall, ...]:
            return tuple(
                ToolCall(f"call_{len(issued) + i + 1}", n, a) for i, (n, a) in enumerate(calls)
            )

        if "list_late_orders" not in issued_names:
            return LLMResponse(None, (call("list_late_orders", min_days_late=1),))

        listing = by_name["list_late_orders"][0]
        if listing.is_error:
            return LLMResponse("I couldn't read the late purchase orders right now. Please retry.")
        orders = listing.content["orders"]

        if "get_inventory" not in issued_names and orders:
            calls = [("get_inventory", {"sku": o["sku"]}) for o in orders]
            calls += [
                ("get_supplier_info", {"supplier_id": s})
                for s in {o["supplier_id"] for o in orders}
            ]
            return LLMResponse(None, numbered(calls))

        inventory = {
            r.content["sku"]: r.content for r in by_name.get("get_inventory", []) if not r.is_error
        }
        suppliers = {
            r.content["supplier_id"]: r.content
            for r in by_name.get("get_supplier_info", [])
            if not r.is_error
        }

        def cover(order: dict) -> float | None:
            inv = inventory.get(order["sku"])
            return None if inv is None else inv["on_hand"] / inv["daily_demand"]

        critical = [o for o in orders if (c := cover(o)) is not None and c < COVER_DAYS_CRITICAL]

        if "propose_expedite" not in issued_names and critical:
            return LLMResponse(
                None, numbered([("propose_expedite", {"po_id": o["po_id"]}) for o in critical])
            )

        expedited = {
            r.content["po_id"] for r in by_name.get("propose_expedite", []) if not r.is_error
        }
        lines = []
        for o in sorted(orders, key=lambda o: -o["days_late"]):
            c = cover(o)
            sup = suppliers.get(o["supplier_id"], {})
            if c is None:
                tier, why = "UNKNOWN", "inventory data unavailable"
            elif c < COVER_DAYS_CRITICAL:
                tier, why = "CRITICAL", f"only {c:.1f} days of stock"
            elif o["days_late"] >= 7:
                tier, why = "WARNING", f"{o['days_late']} days late, stock ok ({c:.0f} days)"
            else:
                tier, why = "LOW", f"{o['days_late']} days late, stock ok ({c:.0f} days)"
            action = " -> expedite requested" if o["po_id"] in expedited else ""
            supplier = sup.get("name", o["supplier_id"])
            lines.append(f"[{tier}] {o['po_id']} ({supplier}): {why}{action}")
        return LLMResponse("Late PO triage:\n" + "\n".join(lines) if lines else "No late POs.")
