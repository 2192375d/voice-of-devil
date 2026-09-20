"""Existing model tests mock transport; keep their quota state isolated too."""
from pathlib import Path
import sys
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def offline_quota_factory(monkeypatch, request):
    if request.node.get_closest_marker("live"):
        return
    from gemini_input import QuotaGovernor
    from gemini_quota import QuotaLimits
    original = QuotaGovernor.from_env
    monkeypatch.setattr(QuotaGovernor, "from_env", staticmethod(lambda: SimpleNamespace(
        limits=QuotaLimits(), reserve=AsyncMock(return_value=1),
        success=AsyncMock(), failure=AsyncMock(return_value=None),
    )))
    return original
