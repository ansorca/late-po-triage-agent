"""Shared builders so each test reads as a scenario, not as wiring."""

from __future__ import annotations

import random
from dataclasses import replace

from po_agent.agent import TriageAgent
from po_agent.config import Settings
from po_agent.idempotency import InMemoryIdempotencyStore
from po_agent.llm.fake import FakeLLM
from po_agent.retry import RetryPolicy
from po_agent.runner import SessionRunner
from po_agent.tools import ToolExecutor
from po_agent.tools.po_tools import build_registry
from po_agent.tools.stub_backend import StubBackend

FAST_RETRY = RetryPolicy(max_attempts=4, base_delay_s=0.001, max_delay_s=0.005)


def fast_settings(**overrides) -> Settings:
    return replace(
        Settings(retry=FAST_RETRY, llm_requests_per_s=10_000, llm_burst=10_000), **overrides
    )


def build(backend: StubBackend | None = None, llm=None, **settings_overrides):
    settings = fast_settings(**settings_overrides)
    backend = backend or StubBackend(seed=1)
    executor = ToolExecutor(build_registry(backend), InMemoryIdempotencyStore(), settings)
    agent = TriageAgent(llm or FakeLLM(), executor, settings)
    return backend, agent, SessionRunner(agent, settings), settings


class RecordingSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def seeded(seed: int = 0) -> random.Random:
    return random.Random(seed)
