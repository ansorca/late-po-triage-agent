"""Offline demo: `python -m po_agent --sessions 6 --failure-rate 0.3 --seed 7`."""

from __future__ import annotations

import argparse
import asyncio
import json

from .agent import TriageAgent
from .config import Settings
from .idempotency import InMemoryIdempotencyStore
from .llm.fake import FakeLLM
from .logging import configure_logging
from .metrics import METRICS
from .models import TriageRequest
from .runner import SessionRunner
from .tools import ToolExecutor
from .tools.po_tools import build_registry
from .tools.stub_backend import StubBackend


async def demo(args: argparse.Namespace) -> None:
    settings = Settings(max_concurrent_sessions=args.concurrency, max_queue_depth=args.queue)
    backend = StubBackend(args.failure_rate, latency_s=0.02, seed=args.seed)
    executor = ToolExecutor(build_registry(backend), InMemoryIdempotencyStore(), settings)
    agent = TriageAgent(FakeLLM(), executor, settings)
    runner = SessionRunner(agent, settings)

    requests = [
        TriageRequest(f"req-{i}", f"planner-{i}", "Which late POs need attention?")
        for i in range(args.sessions)
    ]
    if args.duplicate:  # same request submitted twice at the same instant
        requests.append(requests[0])

    responses = await runner.run_many(requests)

    for r in responses:
        print(f"--- {r.request_id} status={r.status} turns={r.turns} trace={r.trace_id}")
        print(r.answer)
    print("\n=== summary ===")
    print(f"sessions submitted : {len(requests)}")
    print(f"expedite side effects actually performed: {backend.expedite_executions}")
    print(json.dumps(METRICS.snapshot(), indent=2))


def main() -> int:
    p = argparse.ArgumentParser(prog="po_agent")
    p.add_argument("--sessions", type=int, default=6)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--queue", type=int, default=32)
    p.add_argument("--failure-rate", type=float, default=0.0)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--duplicate", action="store_true", help="submit req-0 twice concurrently")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()
    configure_logging(args.log_level)
    asyncio.run(demo(args))
    return 0
