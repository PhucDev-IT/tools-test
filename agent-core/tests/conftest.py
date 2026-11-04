import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Isolated %LOCALAPPDATA%\\SaraeAgent for each test."""
    root = tmp_path / "SaraeAgent"
    monkeypatch.setenv("SARAE_AGENT_HOME", str(root))
    return root


@pytest.fixture()
def events():
    captured: list[tuple[str, dict]] = []

    def emit(name, data):
        captured.append((name, data))

    emit.captured = captured  # type: ignore[attr-defined]
    return emit


windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows-only API")
