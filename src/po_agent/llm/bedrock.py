"""AWS Bedrock adapter using the Converse API (one tool-calling format for all models).

Not exercised against real AWS in this repo (no account yet); tests use a fake boto client.
`boto3` is imported lazily so the rest of the package works without it installed.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from ..errors import PermanentError, TransientError
from ..models import LLMResponse, Message, ToolCall, ToolSpec

_TRANSIENT_CODES = {
    "ThrottlingException",
    "ServiceUnavailableException",
    "ModelTimeoutException",
    "InternalServerException",
    "ModelNotReadyException",
}


_BLOCKED_STOP_REASONS = {"guardrail_intervened", "content_filtered"}


def build_client(
    region: str | None,
    max_concurrency: int,
    connect_timeout_s: float,
    read_timeout_s: float,
) -> Any:
    """boto3 client tuned for use under our own retry/timeout/concurrency control.

    * total_max_attempts=1: boto3 must NOT retry on its own, or every one of our attempts would
      secretly become several (boto3's default retries throttling errors itself).
    * read_timeout slightly BELOW our asyncio timeout: cancelling an await does not stop the
      worker thread, so the socket timeout is what really frees the thread.
    * max_pool_connections: botocore's default pool is 10 connections; match our concurrency.
    """
    import boto3  # lazy: optional dependency
    from botocore.config import Config

    config = Config(
        retries={"total_max_attempts": 1, "mode": "standard"},
        connect_timeout=connect_timeout_s,
        read_timeout=read_timeout_s,
        max_pool_connections=max_concurrency,
    )
    return boto3.client("bedrock-runtime", region_name=region, config=config)


class BedrockLLM:
    def __init__(
        self,
        model_id: str,
        region: str | None = None,
        client: Any = None,
        max_tokens: int = 1024,
        max_concurrency: int = 32,
        connect_timeout_s: float = 3.0,
        read_timeout_s: float = 18.0,  # keep below Settings.llm_timeout_s (default 20)
    ) -> None:
        if client is None:
            client = build_client(region, max_concurrency, connect_timeout_s, read_timeout_s)
        self._client = client
        self._model_id = model_id
        self._max_tokens = max_tokens
        # Dedicated pool: asyncio's default executor is capped at ~min(32, cpus + 4) threads and
        # shared with everything else in the process. This one is sized for Bedrock calls.
        self._pool = ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="bedrock")

    async def complete(
        self, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse:
        request: dict[str, Any] = {
            "modelId": self._model_id,
            "system": [{"text": system}],
            "messages": [self._to_bedrock(m) for m in messages],
            "inferenceConfig": {"maxTokens": self._max_tokens, "temperature": 0},
        }
        if tools:
            request["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": t.name,
                            "description": t.description,
                            "inputSchema": {"json": t.input_schema},
                        }
                    }
                    for t in tools
                ]
            }
        try:
            # boto3 is synchronous; run it in our own worker pool so the event loop stays free.
            response = await asyncio.get_running_loop().run_in_executor(
                self._pool, partial(self._client.converse, **request)
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        return self._from_bedrock(response)

    @staticmethod
    def _to_bedrock(m: Message) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        if m.text:
            content.append({"text": m.text})
        for c in m.tool_calls:
            content.append({"toolUse": {"toolUseId": c.id, "name": c.name, "input": c.arguments}})
        for r in m.tool_results:
            content.append(
                {
                    "toolResult": {
                        "toolUseId": r.tool_call_id,
                        "content": [{"json": r.content}],
                        "status": "error" if r.is_error else "success",
                    }
                }
            )
        return {"role": m.role, "content": content}

    @staticmethod
    def _from_bedrock(response: dict[str, Any]) -> LLMResponse:
        stop_reason = response.get("stopReason", "end_turn")
        if stop_reason in _BLOCKED_STOP_REASONS:
            raise PermanentError(f"bedrock: response blocked ({stop_reason})")
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in response["output"]["message"]["content"]:
            if "text" in block:
                text_parts.append(block["text"])
            elif "toolUse" in block:
                tu = block["toolUse"]
                calls.append(ToolCall(tu["toolUseId"], tu["name"], tu.get("input", {})))
        truncated = stop_reason == "max_tokens"
        if truncated and calls:
            # A cut-off tool call has incomplete arguments: never execute it.
            raise PermanentError("bedrock: output truncated mid tool call (raise max_tokens)")
        usage = response.get("usage", {})
        return LLMResponse(
            "".join(text_parts) or None,
            tuple(calls),
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
            truncated=truncated,
        )

    @staticmethod
    def _classify(exc: Exception) -> Exception:
        # botocore's ClientError carries .response["Error"]["Code"]; duck-typed to avoid the import.
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code in _TRANSIENT_CODES or isinstance(exc, TimeoutError | ConnectionError):
            return TransientError(f"bedrock: {code or type(exc).__name__}")
        return PermanentError(f"bedrock: {code or exc}")
