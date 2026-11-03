"""Tiny in-process metrics. In production, emit the same names to CloudWatch EMF / Prometheus."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any


class Metrics:
    def __init__(self) -> None:
        self.counters: dict[str, int] = defaultdict(int)
        self.timings: dict[str, list[float]] = defaultdict(list)

    def incr(self, name: str, n: int = 1) -> None:
        self.counters[name] += n

    def observe(self, name: str, value: float) -> None:
        self.timings[name].append(value)

    def reset(self) -> None:
        self.counters.clear()
        self.timings.clear()

    def snapshot(self) -> dict[str, Any]:
        timings = {}
        for name, values in self.timings.items():
            ordered = sorted(values)
            p95 = ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]
            timings[name] = {
                "count": len(ordered),
                "avg": round(sum(ordered) / len(ordered), 4),
                "p95": round(p95, 4),
                "max": round(ordered[-1], 4),
            }
        return {"counters": dict(sorted(self.counters.items())), "timings": timings}


METRICS = Metrics()
