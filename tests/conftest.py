import pytest

from po_agent.metrics import METRICS


@pytest.fixture(autouse=True)
def _reset_metrics():
    METRICS.reset()
    yield
