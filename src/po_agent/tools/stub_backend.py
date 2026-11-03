"""Fake business systems (ERP / supplier portal / inventory) with controllable flakiness.

Replace this class with real clients later; nothing else needs to change.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from ..errors import PermanentError, TransientError

_ORDERS = [
    {
        "po_id": "PO-1001",
        "supplier_id": "SUP-A",
        "sku": "SKU-100",
        "qty_open": 500,
        "days_late": 12,
    },
    {"po_id": "PO-1002", "supplier_id": "SUP-B", "sku": "SKU-200", "qty_open": 100, "days_late": 3},
    {"po_id": "PO-1003", "supplier_id": "SUP-A", "sku": "SKU-300", "qty_open": 250, "days_late": 9},
    {"po_id": "PO-1004", "supplier_id": "SUP-C", "sku": "SKU-400", "qty_open": 60, "days_late": 15},
    {"po_id": "PO-1005", "supplier_id": "SUP-B", "sku": "SKU-500", "qty_open": 80, "days_late": 1},
]
_SUPPLIERS = {
    "SUP-A": {"supplier_id": "SUP-A", "name": "Acme Metals", "reliability": 0.72},
    "SUP-B": {"supplier_id": "SUP-B", "name": "Bolt & Co", "reliability": 0.95},
    "SUP-C": {"supplier_id": "SUP-C", "name": "Cobalt Ltd", "reliability": 0.88},
}
_INVENTORY = {
    "SKU-100": {"on_hand": 40, "daily_demand": 20},
    "SKU-200": {"on_hand": 900, "daily_demand": 30},
    "SKU-300": {"on_hand": 120, "daily_demand": 40},
    "SKU-400": {"on_hand": 300, "daily_demand": 10},
    "SKU-500": {"on_hand": 25, "daily_demand": 5},
}


class StubBackend:
    def __init__(
        self, failure_rate: float = 0.0, latency_s: float = 0.0, seed: int | None = None
    ) -> None:
        self.failure_rate = failure_rate
        self.latency_s = latency_s
        self._rng = random.Random(seed)
        self._expedite_ledger: dict[str, dict[str, Any]] = {}  # idempotency_key -> result
        self.expedite_executions = 0  # real side effects performed (tests assert on this)
        self.calls = 0

    async def _flaky(self, op: str) -> None:
        self.calls += 1
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        if self._rng.random() < self.failure_rate:
            raise TransientError(f"{op}: simulated 503 from backend")

    async def list_late_orders(self, min_days_late: int) -> dict[str, Any]:
        await self._flaky("list_late_orders")
        return {"orders": [o for o in _ORDERS if o["days_late"] >= min_days_late]}

    async def get_supplier(self, supplier_id: str) -> dict[str, Any]:
        await self._flaky("get_supplier")
        if supplier_id not in _SUPPLIERS:
            raise PermanentError(f"unknown supplier '{supplier_id}'")
        return _SUPPLIERS[supplier_id]

    async def get_inventory(self, sku: str) -> dict[str, Any]:
        await self._flaky("get_inventory")
        if sku not in _INVENTORY:
            raise PermanentError(f"unknown sku '{sku}'")
        return {"sku": sku, **_INVENTORY[sku]}

    async def expedite(self, po_id: str, idempotency_key: str) -> dict[str, Any]:
        """Write operation. Deduplicates on the key server-side, like a real API should."""
        if idempotency_key not in self._expedite_ledger:
            self.expedite_executions += 1
            self._expedite_ledger[idempotency_key] = {
                "po_id": po_id,
                "status": "expedite_requested",
            }
        # Fail AFTER committing: models the nasty "it worked but the reply got lost" case.
        await self._flaky("expedite")
        return self._expedite_ledger[idempotency_key]
