"""Shared pytest fixtures.

The main module lives at scripts/hermes-approve-mcp.py — the hyphen makes it
un-importable via normal `import`. We load it via importlib and expose it as
the `ham` fixture (short for "hermes-approve-mcp").
"""
import importlib.util
from pathlib import Path

import pytest

_SERVER_PATH = Path(__file__).resolve().parent.parent / "scripts" / "hermes-approve-mcp.py"


@pytest.fixture(scope="session")
def ham():
    """Load scripts/hermes-approve-mcp.py as a module."""
    spec = importlib.util.spec_from_file_location("hermes_approve_mcp", _SERVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
