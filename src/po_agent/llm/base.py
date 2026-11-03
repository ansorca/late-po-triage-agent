from __future__ import annotations

from typing import Protocol

from ..models import LLMResponse, Message, ToolSpec


class LLMClient(Protocol):
    """Anything that can take a conversation + tool list and return text and/or tool calls.

    Implementations MUST raise TransientError (throttling, timeouts, 5xx) or PermanentError
    (bad request, access denied) so the retry layer can tell them apart.
    """

    async def complete(
        self, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse: ...
