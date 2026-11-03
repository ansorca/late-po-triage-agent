"""Error taxonomy. The retry layer only needs to know one thing: is it worth trying again?"""


class AgentError(Exception):
    """Base class for all errors raised on purpose by this package."""


class TransientError(AgentError):
    """Might work if tried again (throttling, timeout, 503). Retried with backoff."""


class PermanentError(AgentError):
    """Will never work as-is (bad input, unknown id, access denied). Never retried."""


class ToolInputError(PermanentError):
    """The model sent arguments that do not match the tool's schema."""


class CircuitOpenError(TransientError):
    """Fast-fail: the dependency is known to be unhealthy. Never retried in the same call."""


class PermissionDenied(PermanentError):
    """The authenticated principal is not allowed to do this. Never retried."""
