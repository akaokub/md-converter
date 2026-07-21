"""Shared pytest fixtures.

The main module lives at scripts/hermes-approve-mcp.py — the hyphen makes it
un-importable via normal `import`. We load it via importlib and expose it as
the `ham` fixture (short for "hermes-approve-mcp").
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_SERVER_PATH = Path(__file__).resolve().parent.parent / "scripts" / "hermes-approve-mcp.py"


@pytest.fixture(scope="session")
def ham():
    """Load scripts/hermes-approve-mcp.py as a module."""
    spec = importlib.util.spec_from_file_location("hermes_approve_mcp", _SERVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules so decorators that introspect __module__
    # (e.g. @dataclass on Python 3.11+) can resolve the module namespace.
    sys.modules["hermes_approve_mcp"] = module
    spec.loader.exec_module(module)
    return module
