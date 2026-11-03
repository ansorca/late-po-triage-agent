import io
import json

from helpers import build
from po_agent.logging import configure_logging
from po_agent.models import TriageRequest


async def test_logs_are_json_lines_with_request_and_trace_ids():
    buf = io.StringIO()
    configure_logging("INFO", buf)
    _, _, runner, _ = build()
    r = await runner.handle(TriageRequest("req-log", "p", "q"))
    lines = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert lines, "expected log output"
    agent_lines = [x for x in lines if x["logger"] != "po_agent.runner"]
    assert all(x["request_id"] == "req-log" for x in lines)
    assert all(x["trace_id"] == r.trace_id for x in agent_lines)
    assert {"ts", "level", "logger", "event"} <= lines[0].keys()
    assert any(x["event"] == "session_finished" and x["status"] == "ok" for x in lines)
