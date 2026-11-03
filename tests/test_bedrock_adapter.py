"""Verifies the Bedrock adapter with a fake boto client: no AWS account needed."""

import pytest

from po_agent.errors import PermanentError, TransientError
from po_agent.llm.bedrock import BedrockLLM
from po_agent.models import Message, ToolCall, ToolResult, ToolSpec


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.request = response, error, None

    def converse(self, **kwargs):
        self.request = kwargs
        if self.error:
            raise self.error
        return self.response


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


SPEC = ToolSpec("get_inventory", "d", {"type": "object", "properties": {}})


async def test_maps_request_and_response():
    client = FakeClient(
        {
            "output": {
                "message": {
                    "content": [
                        {"text": "checking"},
                        {
                            "toolUse": {
                                "toolUseId": "t1",
                                "name": "get_inventory",
                                "input": {"sku": "A"},
                            }
                        },
                    ]
                }
            },
            "usage": {"inputTokens": 11, "outputTokens": 7},
        }
    )
    llm = BedrockLLM("model-x", client=client)
    history = [
        Message("user", text="hi"),
        Message("assistant", tool_calls=(ToolCall("t0", "get_inventory", {"sku": "B"}),)),
        Message("user", tool_results=(ToolResult("t0", "get_inventory", {"error": "x"}, True),)),
    ]
    out = await llm.complete("sys", history, [SPEC])
    assert out.text == "checking" and out.tool_calls[0].arguments == {"sku": "A"}
    assert (out.input_tokens, out.output_tokens) == (11, 7)
    sent = client.request
    assert (
        sent["modelId"] == "model-x"
        and sent["toolConfig"]["tools"][0]["toolSpec"]["name"] == "get_inventory"
    )
    assert sent["messages"][2]["content"][0]["toolResult"]["status"] == "error"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("ThrottlingException", TransientError),
        ("ServiceUnavailableException", TransientError),
        ("AccessDeniedException", PermanentError),
        ("ValidationException", PermanentError),
    ],
)
async def test_error_classification(code, expected):
    llm = BedrockLLM("m", client=FakeClient(error=FakeClientError(code)))
    with pytest.raises(expected):
        await llm.complete("s", [Message("user", text="x")], [])


def _resp(content, stop="end_turn"):
    return {"output": {"message": {"content": content}}, "stopReason": stop}


async def test_truncated_tool_call_is_never_executed():
    tool_use = {"toolUse": {"toolUseId": "t", "name": "get_inventory", "input": {"sk": "A"}}}
    llm = BedrockLLM("m", client=FakeClient(_resp([tool_use], "max_tokens")))
    with pytest.raises(PermanentError, match="truncated"):
        await llm.complete("s", [Message("user", text="x")], [SPEC])


async def test_truncated_text_is_flagged_not_hidden():
    llm = BedrockLLM("m", client=FakeClient(_resp([{"text": "partial..."}], "max_tokens")))
    out = await llm.complete("s", [Message("user", text="x")], [])
    assert out.truncated and out.text == "partial..."


@pytest.mark.parametrize("stop", ["guardrail_intervened", "content_filtered"])
async def test_blocked_responses_are_permanent_errors(stop):
    llm = BedrockLLM("m", client=FakeClient(_resp([{"text": "x"}], stop)))
    with pytest.raises(PermanentError, match="blocked"):
        await llm.complete("s", [Message("user", text="x")], [])


def test_real_client_is_configured_not_to_retry_on_its_own():
    pytest.importorskip("boto3")
    from po_agent.llm.bedrock import build_client

    client = build_client("us-east-1", max_concurrency=40, connect_timeout_s=3, read_timeout_s=15)
    cfg = client.meta.config
    assert cfg.retries["total_max_attempts"] == 1  # our retry layer is the only one
    assert cfg.read_timeout == 15 and cfg.connect_timeout == 3
    assert cfg.max_pool_connections == 40


async def test_blocking_calls_are_bounded_by_the_dedicated_pool():
    import asyncio
    import threading
    import time

    running, peak, lock = 0, 0, threading.Lock()

    class SlowClient:
        def converse(self, **kwargs):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            time.sleep(0.05)
            with lock:
                running -= 1
            return _resp([{"text": "ok"}])

    llm = BedrockLLM("m", client=SlowClient(), max_concurrency=3)
    await asyncio.gather(*(llm.complete("s", [Message("user", text="x")], []) for _ in range(9)))
    assert peak == 3
