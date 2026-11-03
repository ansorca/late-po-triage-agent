"""Regression eval set: fixed scenarios with machine-checkable expectations.

Today it scores the offline FakeLLM policy + the whole reliability stack. With real Bedrock,
run the same cases on a schedule (and on every prompt/model change) and track the pass rate.
"""

import json
import pathlib

import pytest

from helpers import build
from po_agent.models import TriageRequest
from po_agent.tools.stub_backend import StubBackend

CASES = json.loads((pathlib.Path(__file__).parent / "evals" / "cases.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
async def test_eval_case(case):
    backend = StubBackend(**case["backend"])
    _, _, runner, _ = build(backend, breaker_failure_threshold=1000)
    r = await runner.handle(TriageRequest(case["name"], "eval", case["question"]))
    assert r.status == case["expect_status"]
    for text in case.get("must_contain", []):
        assert text in r.answer
    for text in case.get("must_not_contain", []):
        assert text not in r.answer
    if "expedite_executions" in case:
        assert backend.expedite_executions == case["expedite_executions"]
    if "max_expedite_executions" in case:
        assert backend.expedite_executions <= case["max_expedite_executions"]
