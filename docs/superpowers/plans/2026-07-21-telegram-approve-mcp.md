# Telegram Approve MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a custom stdio MCP server (`hermes-approve`) that gives the ZCode agent an opt-in tool to request human approval via a dedicated Telegram bot — blocking until the user taps Allow/Deny or the request times out (auto-deny).

**Architecture:** Single FastMCP stdio process. A background daemon thread long-polls Telegram `getUpdates`. The `request_approval` tool writes a pending request + `threading.Event`, sends a Telegram message with inline buttons, and blocks on the event. The poller resolves events when it sees matching `callback_query` updates. State is in-process; audit log is append-only JSONL on disk.

**Tech Stack:**
- Python 3.11.15 (via `C:\Users\Bew\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe`)
- `mcp` (FastMCP) — already installed in hermes-agent venv
- `httpx` 0.28.1 — for Telegram Bot API calls
- `python-dotenv` — for loading `.secrets/approve-bot.env`
- `pytest` — install in dev step (not yet present in hermes-agent venv)
- Telegram Bot API (HTML parse_mode, inline keyboards, callback queries)

---

## Global Constraints

(Copied verbatim from `docs/superpowers/specs/2026-07-21-telegram-approve-mcp-design.md`. Every task implicitly includes these.)

- **Python interpreter**: `C:\Users\Bew\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe` (only env with `mcp` + `httpx` installed). ZCode config will point `command` at this path.
- **Risk enum**: `{"low", "moderate", "destructive"}` — exact strings, no others.
- **Reason vocabulary**: `user_allowed`, `user_denied`, `timeout` (auto). Decisions: `allow`, `deny`.
- **callback_data format**: `ap:<id>:<decision>` where `id` is 6–12 char hex; total ≤ 64 bytes.
- **ID generator**: `secrets.token_hex(4)` → 8 hex chars (fits 6–12 range, no collision concern at single-user scale).
- **Timeout bounds**: `timeout_seconds ∈ [60, 1800]`. Default 900 (15 min).
- **Validation**: `action` ≤ 200 chars; `summary` ≥ 20 chars (forced detail) and ≤ 1000 chars.
- **Tool never raises**: every code path (including errors) returns a JSON string. Mirrors Phase 1D-2 hook's "must not block" rule.
- **Monotonic clock for deadlines**: use `time.monotonic()` for `expires_at` arithmetic; `datetime.now(timezone.utc)` only for display/audit.
- **Logging discipline**: MCP logging API (stdout) for lifecycle INFO only; stderr for DEBUG; `.approve/approve.log` for file audit. Never pollute stdout with non-JSON-RPC output.
- **HTML parse_mode**: every user-controlled field (`action`, `summary`) must pass through `html.escape()` before going into a Telegram message.
- **Allowlist**: single Telegram user ID (from `TELEGRAM_ALLOWED_USERS` env). Any callback_query from a different `from.id` → toast "⛔ Unauthorized" + log warning + no resolution.
- **No bot token in code or git history**: token lives only in `.secrets/approve-bot.env` (gitignored).
- **Branch**: `phase-2/telegram-approve-mcp` (created from `main` after Phase 1D-2 merges, or stacked on top of `phase-1d-2/session-memory-hook`).

---

## File Structure

```
C:\Users\Bew\ZCodeProject\
├── scripts\
│   └── hermes-approve-mcp.py        ← Main MCP server (~350 lines)
├── .secrets\
│   └── approve-bot.env              ← Gitignored. Loaded by server. Format:
│                                       APPROVE_BOT_TOKEN=123:abc
│                                       TELEGRAM_ALLOWED_USERS=5967541638
├── .approve\                        ← Runtime state (gitignored)
│   ├── pending.jsonl                ← Append-only audit log
│   ├── state.json                   ← {last_update_id, last_heartbeat_at}
│   └── approve.log                  ← Rotated at 1MB, keep last 5
├── tests\
│   ├── __init__.py
│   ├── conftest.py                  ← pytest fixtures (mock_telegram, fast_store)
│   ├── unit\
│   │   ├── __init__.py
│   │   ├── test_validate.py         ← Pure-function validation tests
│   │   ├── test_format.py           ← Message formatting tests
│   │   ├── test_parse_callback.py   ← callback_data parser tests
│   │   └── test_gen_id.py           ← ID generator tests
│   └── integration\
│       ├── __init__.py
│       ├── test_request_approval.py ← Happy/deny/timeout paths
│       ├── test_race_conditions.py  ← Double-click, timeout-vs-click
│       └── test_poller.py           ← Stale callback, unauthorized user
├── AGENTS.md                        ← New: tells ZCode agent when to call tool
├── pyproject.toml                   ← New: declares deps + pytest/ruff config
├── ruff.toml                        ← New: lint config (line length 100)
└── .gitignore                       ← New: .secrets/, .approve/, __pycache__, etc.
```

**Responsibilities of each module inside `hermes-approve-mcp.py`** (single file for simplicity; matches `n8n/server.py` pattern at ~250 lines):

| Section | Responsibility | Lines (est.) |
|---|---|---|
| `Config` dataclass + `load_config()` | Parse env, validate bot token/UID present | ~30 |
| `ApprovalRequest` dataclass + `gen_id()` | Immutable request record + ID generator | ~25 |
| `validate()` | Pure input validation → returns `Optional[ValidationError]` | ~30 |
| `format_request()` / `format_resolved()` | HTML-escaped message templates | ~40 |
| `parse_callback()` | `ap:<id>:<decision>` parser → `Optional[(id, decision)]` | ~10 |
| `TelegramClient` | httpx wrapper: `send_message`, `edit_message_text`, `answer_callback_query`, `get_updates`. 1 retry on transient errors. | ~80 |
| `PendingStore` | In-mem dict + lock + `threading.Event` per id; `add()`, `set_resolution()`, `append_audit()` | ~70 |
| `TelegramPoller` | Daemon thread; long-poll `get_updates`; dispatch callbacks; persist `state.json` | ~80 |
| `request_approval` / `ping` tools | FastMCP-decorated functions; orchestrate the above | ~50 |
| `__main__` | `mcp.run()` | ~5 |

Single-file rationale: ~350 lines is holdable in context; the components are tightly coupled (share `Config`, `PendingStore`); matches the established `n8n/server.py` precedent the user already runs. If the file grows past ~500 lines in Phase 3, split then.

---

## Task Decomposition (Overview)

| # | Task | What it delivers | Files |
|---|---|---|---|
| 1 | Repo scaffolding + tooling | `pyproject.toml`, `ruff.toml`, `.gitignore`, test dirs | 4 files |
| 2 | Pure helpers: `gen_id`, `parse_callback` | TDD unit-tested, no deps | `scripts/hermes-approve-mcp.py` (partial), `tests/unit/test_*.py` |
| 3 | `validate()` pure function | All input rules, TDD | same files |
| 4 | `format_request()` / `format_resolved()` | HTML-safe templates, TDD | same files |
| 5 | `Config` + `load_config()` | Env parsing, fail-fast on misconfig | same |
| 6 | `PendingStore` | In-mem + audit log + threading primitives, TDD | same + `tests/integration/test_store.py` |
| 7 | `TelegramClient` (mocked) | httpx wrapper with retry, TDD via `MockTransport` | same + `tests/integration/test_telegram_client.py` |
| 8 | `TelegramPoller` (mocked) | Daemon thread, callback dispatch, state.json persistence | same + `tests/integration/test_poller.py` |
| 9 | `request_approval` + `ping` tools | FastMCP wiring, end-to-end via mocked Telegram | same + `tests/integration/test_request_approval.py` |
| 10 | Race condition tests | Double-click, timeout-vs-click, concurrent requests | `tests/integration/test_race_conditions.py` |
| 11 | `AGENTS.md` + `.secrets/approve-bot.env` template | Agent guidance + secret scaffold | 2 files |
| 12 | Wire into ZCode config + manual E2E | Real bot via @BotFather, scenarios M1/M2/M3 | `~/.zcode/cli/config.json` + MEMORY.md update |

Tasks 1–10 are pure-TDD with mocked Telegram. Task 11 is docs. Task 12 is the only one requiring a real bot and manual steps — it must be done by the human (with agent assistance for config edits).

---

## Task 1: Repo Scaffolding + Tooling

**Files:**
- Create: `C:\Users\Bew\ZCodeProject\pyproject.toml`
- Create: `C:\Users\Bew\ZCodeProject\ruff.toml`
- Create: `C:\Users\Bew\ZCodeProject\.gitignore`
- Create: `C:\Users\Bew\ZCodeProject\tests\__init__.py` (empty)
- Create: `C:\Users\Bew\ZCodeProject\tests\unit\__init__.py` (empty)
- Create: `C:\Users\Bew\ZCodeProject\tests\integration\__init__.py` (empty)
- Create: `C:\Users\Bew\ZCodeProject\scripts\hermes-approve-mcp.py` (empty placeholder)

**Interfaces:**
- Consumes: nothing
- Produces: project skeleton that `pytest` can discover; `ruff check` runs clean on an empty file

- [ ] **Step 1: Create branch**

```bash
cd C:/Users/Bew/ZCodeProject
git checkout main 2>/dev/null || git checkout phase-1d-2/session-memory-hook
git checkout -b phase-2/telegram-approve-mcp
```

If `main` does not yet contain the Phase 1D-2 merge, branch off `phase-1d-2/session-memory-hook` instead (so the spec commit from `de38846` rides along).

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "hermes-approve-mcp"
version = "0.1.0"
description = "Stdio MCP server for requesting human approval via Telegram"
requires-python = ">=3.10"
license = { text = "MIT" }
dependencies = [
  "mcp>=1.0.0",
  "httpx>=0.27.0",
  "python-dotenv>=1.0.0",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.0.0",
  "pytest-timeout>=2.3.0",
  "ruff>=0.5.0",
  "mypy>=1.10.0",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-v --tb=short"
markers = [
  "integration: tests that exercise the store/poller/Telegram client with mocks",
  "unit: pure-function tests with no I/O",
]

[tool.mypy]
python_version = "3.11"
strict = false
ignore_missing_imports = true
files = ["scripts/hermes-approve-mcp.py"]
```

- [ ] **Step 3: Write `ruff.toml`**

```toml
line-length = 100
target-version = "py311"

[lint]
select = ["E", "F", "W", "I", "UP", "B"]
ignore = ["E501"]  # line length handled by formatter

[lint.per-file-ignores]
"tests/**" = ["B011"]  # assert False OK in tests
```

- [ ] **Step 4: Write `.gitignore`**

```gitignore
# Secrets — never commit
.secrets/

# Runtime state
.approve/

# Python
__pycache__/
*.py[cod]
*.egg-info/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.venv/

# Existing repo artifacts (from earlier phases)
.backups/
*.sqlite
*.db
downloads/
tmp_test/
.playwright-mcp/
.rag/
rag_store/
```

- [ ] **Step 5: Create empty `__init__.py` files**

```bash
cd C:/Users/Bew/ZCodeProject
touch tests/__init__.py tests/unit/__init__.py tests/integration/__init__.py
touch scripts/hermes-approve-mcp.py
```

(Or use `Write` tool to create each with a single newline.)

- [ ] **Step 6: Install dev deps in hermes-agent venv**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pip install pytest pytest-timeout ruff mypy
```

Expected: all four packages install without error. (mcp + httpx already present.)

- [ ] **Step 7: Verify pytest discovers zero tests cleanly**

Run:
```bash
cd C:/Users/Bew/ZCodeProject
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest
```
Expected: `no tests ran in 0.0xs` (exit 5 is fine — no tests yet, just no collection errors).

- [ ] **Step 8: Verify ruff runs clean on empty placeholder**

Run:
```bash
cd C:/Users/Bew/ZCodeProject
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m ruff check scripts/hermes-approve-mcp.py
```
Expected: `All checks passed!` (or "0 errors" wording).

- [ ] **Step 9: Commit**

```bash
cd C:/Users/Bew/ZCodeProject
git add pyproject.toml ruff.toml .gitignore tests/ scripts/hermes-approve-mcp.py
git commit -m "chore(phase-2): scaffold project structure (pyproject, ruff, gitignore, test dirs)"
```

---

## Task 2: Pure Helpers — `gen_id()` and `parse_callback()`

**Files:**
- Modify: `C:\Users\Bew\ZCodeProject\scripts\hermes-approve-mcp.py` (add module header + 2 functions)
- Create: `C:\Users\Bew\ZCodeProject\tests\unit\test_gen_id.py`
- Create: `C:\Users\Bew\ZCodeProject\tests\unit\test_parse_callback.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `gen_id() -> str` — returns 8-char hex string (4 bytes from `secrets.token_hex(4)`)
  - `parse_callback(data: str) -> Optional[tuple[str, str]]` — parses `ap:<id>:<decision>`; returns `(id, decision)` or `None`

- [ ] **Step 1: Write the failing tests for `gen_id`**

File: `tests/unit/test_gen_id.py`

```python
"""Tests for gen_id — short hex ID generator."""
import re

from scripts.hermes_approve_mcp import gen_id

# hermes-approve-mcp.py has a hyphen in the filename, which isn't a valid
# Python module name. We import it via importlib in conftest.py instead —
# see tests/conftest.py for the `ham` fixture that returns the module.
#
# This file uses the fixture style: `def test_x(ham): ham.gen_id(...)`.

 HEX_RE = re.compile(r"^[0-9a-f]+$")


def test_gen_id_length(ham):
    assert len(ham.gen_id()) == 8


def test_gen_id_is_hex(ham):
    assert HEX_RE.match(ham.gen_id())


def test_gen_id_uniqueness(ham):
    ids = {ham.gen_id() for _ in range(10_000)}
    assert len(ids) == 10_000  # extremely unlikely to collide at 8 hex chars
```

(Note: the `ham` fixture is defined in Task 6's `conftest.py`. For this task to run in isolation, create a temporary `tests/conftest.py` with the fixture now — see Step 2. The fixture loads `scripts/hermes-approve-mcp.py` via `importlib.util` because the hyphen in the filename prevents a normal `import`.)

- [ ] **Step 2: Write the conftest with the `ham` fixture**

File: `tests/conftest.py`

```python
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
```

Also fix the syntax error in the Step 1 file (the stray indent before `HEX_RE = re.compile(...)`): remove the leading space so `HEX_RE` is module-level.

- [ ] **Step 3: Write the failing tests for `parse_callback`**

File: `tests/unit/test_parse_callback.py`

```python
"""Tests for parse_callback — callback_data parser."""


def test_parse_callback_allow(ham):
    assert ham.parse_callback("ap:8f3a2cab:allow") == ("8f3a2cab", "allow")


def test_parse_callback_deny(ham):
    assert ham.parse_callback("ap:8f3a2cab:deny") == ("8f3a2cab", "deny")


def test_parse_callback_short_id(ham):
    assert ham.parse_callback("ap:abc123:allow") == ("abc123", "allow")


def test_parse_callback_invalid_format_no_prefix(ham):
    assert ham.parse_callback("foo:bar:baz") is None


def test_parse_callback_invalid_decision(ham):
    assert ham.parse_callback("ap:8f3a2cab:yes") is None


def test_parse_callback_too_few_segments(ham):
    assert ham.parse_callback("ap:8f3a2cab") is None


def test_parse_callback_extra_segments(ham):
    assert ham.parse_callback("ap:8f3a2cab:allow:extra") is None


def test_parse_callback_non_hex_id(ham):
    assert ham.parse_callback("ap:ZZZZZZZZ:allow") is None


def test_parse_callback_empty(ham):
    assert ham.parse_callback("") is None
```

- [ ] **Step 4: Run tests to verify they fail**

Run:
```bash
cd C:/Users/Bew/ZCodeProject
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/unit/test_gen_id.py tests/unit/test_parse_callback.py
```
Expected: FAIL with `AttributeError: module 'hermes_approve_mcp' has no attribute 'gen_id'` (and same for `parse_callback`).

- [ ] **Step 5: Write minimal implementation**

File: `scripts/hermes-approve-mcp.py` — replace the empty file with:

```python
#!/usr/bin/env python3
"""hermes-approve MCP server.

Stdio MCP server that exposes a `request_approval` tool. When called, the tool
sends an inline-button message to a dedicated Telegram bot, then blocks until
the user taps Allow / Deny or the request times out (auto-deny).

Design spec: docs/superpowers/specs/2026-07-21-telegram-approve-mcp-design.md
"""
from __future__ import annotations

import re
import secrets
from typing import Optional


# ID generator: 8 hex chars (4 bytes). Plenty of entropy for single-user scale.
_ID_RE = re.compile(r"^[0-9a-f]{6,12}$")
_VALID_DECISIONS = frozenset({"allow", "deny"})
_CALLBACK_RE = re.compile(r"^ap:([0-9a-f]{6,12}):(allow|deny)$")


def gen_id() -> str:
    """Generate a short hex ID for an approval request."""
    return secrets.token_hex(4)  # 8 hex chars


def parse_callback(data: str) -> Optional[tuple[str, str]]:
    """Parse a Telegram callback_data string.

    Format: ``ap:<id>:<decision>`` where id is 6–12 hex chars and decision is
    one of {allow, deny}. Returns ``(id, decision)`` or ``None`` if malformed.
    """
    if not data:
        return None
    m = _CALLBACK_RE.match(data)
    if not m:
        return None
    return (m.group(1), m.group(2))
```

- [ ] **Step 6: Run tests to verify they pass**

Run:
```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/unit/test_gen_id.py tests/unit/test_parse_callback.py
```
Expected: all tests PASS.

- [ ] **Step 7: Run ruff + mypy**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m ruff check scripts/hermes-approve-mcp.py tests/
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m mypy scripts/hermes-approve-mcp.py
```
Expected: both clean.

- [ ] **Step 8: Commit**

```bash
cd C:/Users/Bew/ZCodeProject
git add scripts/hermes-approve-mcp.py tests/conftest.py tests/unit/test_gen_id.py tests/unit/test_parse_callback.py
git commit -m "feat(phase-2): gen_id + parse_callback pure helpers (TDD)"
```

---

## Task 3: `validate()` — Pure Input Validation

**Files:**
- Modify: `C:\Users\Bew\ZCodeProject\scripts\hermes-approve-mcp.py` (add `ValidationError` + `validate()`)
- Create: `C:\Users\Bew\ZCodeProject\tests\unit\test_validate.py`

**Interfaces:**
- Consumes: nothing new
- Produces:
  - `class ValidationError(Exception)` with `.field: str` and `.message: str`
  - `validate(action, risk, summary, timeout_seconds) -> Optional[ValidationError]` — returns `None` on success

- [ ] **Step 1: Write failing tests**

File: `tests/unit/test_validate.py`

```python
"""Tests for validate — input validation."""


def test_validate_action_empty(ham):
    err = ham.validate(action="", risk="low", summary="x" * 20, timeout_seconds=60)
    assert err is not None
    assert err.field == "action"


def test_validate_action_whitespace_only(ham):
    err = ham.validate(action="   ", risk="low", summary="x" * 20, timeout_seconds=60)
    assert err is not None
    assert err.field == "action"


def test_validate_action_too_long(ham):
    err = ham.validate(action="x" * 201, risk="low", summary="x" * 20, timeout_seconds=60)
    assert err is not None
    assert err.field == "action"


def test_validate_risk_invalid(ham):
    err = ham.validate(action="x", risk="critical", summary="x" * 20, timeout_seconds=60)
    assert err is not None
    assert err.field == "risk"


def test_validate_risk_each_valid_value(ham):
    for r in ("low", "moderate", "destructive"):
        err = ham.validate(action="x", risk=r, summary="x" * 20, timeout_seconds=60)
        assert err is None, f"risk={r} should be valid"


def test_validate_summary_too_short(ham):
    err = ham.validate(action="x", risk="low", summary="abc", timeout_seconds=60)
    assert err is not None
    assert err.field == "summary"


def test_validate_summary_whitespace_trimmed(ham):
    # 20 visible chars but padded with spaces → should still pass after trim
    err = ham.validate(action="x", risk="low", summary="  " + "x" * 20 + "  ", timeout_seconds=60)
    assert err is None


def test_validate_summary_too_long(ham):
    err = ham.validate(action="x", risk="low", summary="x" * 1001, timeout_seconds=60)
    assert err is not None
    assert err.field == "summary"


def test_validate_timeout_too_short(ham):
    err = ham.validate(action="x", risk="low", summary="x" * 20, timeout_seconds=30)
    assert err is not None
    assert err.field == "timeout_seconds"


def test_validate_timeout_too_long(ham):
    err = ham.validate(action="x", risk="low", summary="x" * 20, timeout_seconds=2000)
    assert err is not None
    assert err.field == "timeout_seconds"


def test_validate_timeout_boundary_60(ham):
    err = ham.validate(action="x", risk="low", summary="x" * 20, timeout_seconds=60)
    assert err is None


def test_validate_timeout_boundary_1800(ham):
    err = ham.validate(action="x", risk="low", summary="x" * 20, timeout_seconds=1800)
    assert err is None


def test_validate_happy_path(ham):
    err = ham.validate(
        action="git push --force origin main",
        risk="destructive",
        summary="Rewrite remote history because the previous push included a secret.",
        timeout_seconds=900,
    )
    assert err is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/unit/test_validate.py
```
Expected: FAIL with `AttributeError: module 'hermes_approve_mcp' has no attribute 'validate'`.

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/hermes-approve-mcp.py` (after the existing helpers):

```python
from dataclasses import dataclass


VALID_RISKS = frozenset({"low", "moderate", "destructive"})
ACTION_MAX = 200
SUMMARY_MIN = 20
SUMMARY_MAX = 1000
TIMEOUT_MIN = 60
TIMEOUT_MAX = 1800


@dataclass
class ValidationError:
    """Returned by validate() when input fails a rule."""
    field: str
    message: str


def validate(
    action: str,
    risk: str,
    summary: str,
    timeout_seconds: int,
) -> Optional[ValidationError]:
    """Validate request_approval inputs. Returns None on success."""
    if not action or not action.strip():
        return ValidationError("action", "action must not be empty")
    if len(action) > ACTION_MAX:
        return ValidationError("action", f"action must be ≤ {ACTION_MAX} chars")

    if risk not in VALID_RISKS:
        return ValidationError(
            "risk",
            f"risk must be one of: {', '.join(sorted(VALID_RISKS))}",
        )

    trimmed_summary = summary.strip()
    if len(trimmed_summary) < SUMMARY_MIN:
        return ValidationError(
            "summary",
            f"summary must be ≥ {SUMMARY_MIN} chars (explain WHY this needs approval)",
        )
    if len(summary) > SUMMARY_MAX:
        return ValidationError("summary", f"summary must be ≤ {SUMMARY_MAX} chars")

    if timeout_seconds < TIMEOUT_MIN:
        return ValidationError("timeout_seconds", f"min {TIMEOUT_MIN} seconds")
    if timeout_seconds > TIMEOUT_MAX:
        return ValidationError("timeout_seconds", f"max {TIMEOUT_MAX} seconds (30 min)")

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/unit/test_validate.py
```
Expected: all PASS.

- [ ] **Step 5: ruff + mypy clean**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m ruff check scripts/hermes-approve-mcp.py tests/
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m mypy scripts/hermes-approve-mcp.py
```

- [ ] **Step 6: Commit**

```bash
cd C:/Users/Bew/ZCodeProject
git add scripts/hermes-approve-mcp.py tests/unit/test_validate.py
git commit -m "feat(phase-2): validate() pure input validation (TDD)"
```

---

## Task 4: `format_request()` / `format_resolved()` — HTML-Safe Message Templates

**Files:**
- Modify: `C:\Users\Bew\ZCodeProject\scripts\hermes-approve-mcp.py` (add `ApprovalRequest` + format functions)
- Create: `C:\Users\Bew\ZCodeProject\tests\unit\test_format.py`

**Interfaces:**
- Consumes: `validate()` constants
- Produces:
  - `class RiskStyle(NamedTuple)`: `icon: str`, `label: str`
  - `RISK_STYLES: dict[str, RiskStyle]` — `{"low": 🟢 LOW, "moderate": 🟡 MODERATE, "destructive": 🟠 DESTRUCTIVE}`
  - `@dataclass class ApprovalRequest`: `id, action, risk, summary, created_at: datetime, timeout_seconds: int, status: str` (`"pending"`, `"resolved"`), `decision: Optional[str]`, `reason: Optional[str]`, `responded_by: Optional[int]`, `resolved_at: Optional[datetime]`
  - `format_request(req: ApprovalRequest) -> str` — pending-state message (HTML)
  - `format_resolved(req: ApprovalRequest) -> str` — resolved-state message (HTML)

- [ ] **Step 1: Write failing tests**

File: `tests/unit/test_format.py`

```python
"""Tests for format_request / format_resolved — HTML-escaped Telegram messages."""
from datetime import datetime, timezone

import pytest


def _make_req(ham, **kw):
    """Build a minimal ApprovalRequest with sensible defaults."""
    base = dict(
        id="abc12345",
        action="git push",
        risk="destructive",
        summary="Force-push 5 new commits to overwrite yesterday's history.",
        created_at=datetime(2026, 7, 21, 12, 42, 0, tzinfo=timezone.utc),
        timeout_seconds=900,
        status="pending",
        decision=None,
        reason=None,
        responded_by=None,
        resolved_at=None,
    )
    base.update(kw)
    return ham.ApprovalRequest(**base)


def test_format_request_destructive_emoji(ham):
    req = _make_req(ham)
    out = ham.format_request(req)
    assert out.startswith("🟠")


def test_format_request_risk_label_uppercase(ham):
    req = _make_req(ham, risk="moderate")
    out = ham.format_request(req)
    assert "MODERATE" in out


def test_format_request_low_risk_emoji(ham):
    req = _make_req(ham, risk="low")
    out = ham.format_request(req)
    assert out.startswith("🟢")


def test_format_request_html_escapes_action(ham):
    req = _make_req(ham, action="<script>alert(1)</script>")
    out = ham.format_request(req)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_format_request_html_escapes_summary(ham):
    req = _make_req(ham, summary="<b>not bold</b>" + "x" * 20)
    out = ham.format_request(req)
    assert "<b>not bold</b>" not in out
    assert "&lt;b&gt;not bold&lt;/b&gt;" in out


def test_format_request_contains_auto_deny_countdown(ham):
    req = _make_req(ham, timeout_seconds=900)
    out = ham.format_request(req)
    assert "Auto-deny" in out
    assert "15:00" in out  # 900s = 15:00


def test_format_request_shorter_timeout(ham):
    req = _make_req(ham, timeout_seconds=120)
    out = ham.format_request(req)
    assert "02:00" in out


def test_format_resolved_allow(ham):
    req = _make_req(
        ham,
        status="resolved",
        decision="allow",
        reason="user_allowed",
        responded_by=5967541638,
        resolved_at=datetime(2026, 7, 21, 12, 44, 12, tzinfo=timezone.utc),
    )
    out = ham.format_resolved(req)
    assert "✅ ALLOWED" in out
    assert "2m 12s" in out  # 132 seconds elapsed


def test_format_resolved_deny(ham):
    req = _make_req(
        ham,
        status="resolved",
        decision="deny",
        reason="user_denied",
        responded_by=5967541638,
        resolved_at=datetime(2026, 7, 21, 12, 44, 0, tzinfo=timezone.utc),
    )
    out = ham.format_resolved(req)
    assert "❌ DENIED" in out


def test_format_resolved_timeout(ham):
    req = _make_req(
        ham,
        status="resolved",
        decision="deny",
        reason="timeout",
        responded_by=None,
        resolved_at=datetime(2026, 7, 21, 12, 57, 0, tzinfo=timezone.utc),
        timeout_seconds=900,
    )
    out = ham.format_resolved(req)
    assert "⌛ AUTO-DENIED" in out
    assert "15:00" in out
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/unit/test_format.py
```
Expected: FAIL with `AttributeError: ... has no attribute 'ApprovalRequest'`.

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/hermes-approve-mcp.py`:

```python
import html
from datetime import datetime
from typing import NamedTuple, Optional


class RiskStyle(NamedTuple):
    icon: str
    label: str


RISK_STYLES: dict[str, RiskStyle] = {
    "low": RiskStyle("🟢", "LOW"),
    "moderate": RiskStyle("🟡", "MODERATE"),
    "destructive": RiskStyle("🟠", "DESTRUCTIVE"),
}


@dataclass
class ApprovalRequest:
    """A pending or resolved approval request."""
    id: str
    action: str
    risk: str
    summary: str
    created_at: datetime
    timeout_seconds: int
    status: str  # "pending" | "resolved"
    decision: Optional[str] = None        # "allow" | "deny" | None
    reason: Optional[str] = None          # "user_allowed" | "user_denied" | "timeout"
    responded_by: Optional[int] = None
    resolved_at: Optional[datetime] = None


def _fmt_countdown(total_seconds: int) -> str:
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def format_request(req: ApprovalRequest) -> str:
    """Render the pending-state message (HTML)."""
    style = RISK_STYLES[req.risk]
    # created_at assumed tz-aware UTC; convert to Bangkok for display only.
    bangkok = req.created_at.astimezone(_BANGKOK_TZ)
    countdown = _fmt_countdown(req.timeout_seconds)
    return (
        f"{style.icon} <b>Approval request</b>\n\n"
        f"<b>Action:</b> <code>{html.escape(req.action)}</code>\n"
        f"<b>Risk:</b> {style.label}\n"
        f"<b>Time:</b> {bangkok:%Y-%m-%d %H:%M} (Bangkok UTC+7)\n\n"
        f"{html.escape(req.summary)}\n\n"
        f"⏱ Auto-deny ใน {countdown}"
    )


def _fmt_elapsed(start: datetime, end: datetime) -> str:
    seconds = int((end - start).total_seconds())
    minutes, secs = divmod(seconds, 60)
    if minutes >= 1:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_resolved(req: ApprovalRequest) -> str:
    """Render the resolved-state message (HTML). Buttons already removed by caller."""
    style = RISK_STYLES[req.risk]
    bangkok = req.created_at.astimezone(_BANGKOK_TZ)
    base = (
        f"{style.icon} <b>Approval request</b>\n\n"
        f"<b>Action:</b> <code>{html.escape(req.action)}</code>\n"
        f"<b>Risk:</b> {style.label}\n"
        f"<b>Time:</b> {bangkok:%Y-%m-%d %H:%M} (Bangkok UTC+7)\n\n"
        f"{html.escape(req.summary)}\n\n"
    )
    if req.decision == "allow":
        elapsed = _fmt_elapsed(req.created_at, req.resolved_at or req.created_at)
        return base + f"✅ ALLOWED by {req.responded_by} at " \
                      f"{(req.resolved_at or req.created_at).astimezone(_BANGKOK_TZ):%H:%M} " \
                      f"(after {elapsed})"
    if req.decision == "deny" and req.reason == "timeout":
        countdown = _fmt_countdown(req.timeout_seconds)
        return base + f"⌛ AUTO-DENIED (timeout {countdown})"
    # Explicit user deny
    return base + f"❌ DENIED by {req.responded_by} at " \
                  f"{(req.resolved_at or req.created_at).astimezone(_BANGKOK_TZ):%H:%M}"
```

Also add `_BANGKOK_TZ` near the top of the file (after imports):

```python
from datetime import timezone, timedelta

_BANGKOK_TZ = timezone(timedelta(hours=7))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/unit/test_format.py
```
Expected: all PASS.

- [ ] **Step 5: ruff + mypy clean**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m ruff check scripts/hermes-approve-mcp.py tests/
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m mypy scripts/hermes-approve-mcp.py
```

- [ ] **Step 6: Commit**

```bash
cd C:/Users/Bew/ZCodeProject
git add scripts/hermes-approve-mcp.py tests/unit/test_format.py
git commit -m "feat(phase-2): ApprovalRequest + format_request/format_resolved HTML templates (TDD)"
```

---

## Task 5: `Config` + `load_config()` — Env Parsing

**Files:**
- Modify: `C:\Users\Bew\ZCodeProject\scripts\hermes-approve-mcp.py` (add `Config` dataclass + `load_config()`)
- Create: `C:\Users\Bew\ZCodeProject\tests\unit\test_config.py`

**Interfaces:**
- Consumes: `os.environ`, optional env file path from `APPROVE_BOT_ENV`
- Produces:
  - `@dataclass class Config`: `bot_token: str`, `allowed_user_ids: set[int]`, `state_dir: Path`, `bot_api_base: str`
  - `class ConfigError(Exception)` with `.message`
  - `load_config() -> Config` — reads env, raises `ConfigError` if token/UID missing or malformed

- [ ] **Step 1: Write failing tests**

File: `tests/unit/test_config.py`

```python
"""Tests for load_config — env parsing."""
import os
from pathlib import Path

import pytest


def test_load_config_happy(monkeypatch, tmp_path, ham):
    env_file = tmp_path / "approve-bot.env"
    env_file.write_text(
        "APPROVE_BOT_TOKEN=123:abcdef\nTELEGRAM_ALLOWED_USERS=5967541638\n"
    )
    monkeypatch.setenv("APPROVE_BOT_ENV", str(env_file))
    monkeypatch.setenv("APPROVE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("APPROVE_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)

    cfg = ham.load_config()
    assert cfg.bot_token == "123:abcdef"
    assert cfg.allowed_user_ids == {5967541638}
    assert cfg.state_dir == tmp_path / "state"
    assert cfg.bot_api_base == "https://api.telegram.org"


def test_load_config_multiple_allowed_users(monkeypatch, tmp_path, ham):
    env_file = tmp_path / "approve-bot.env"
    env_file.write_text(
        "APPROVE_BOT_TOKEN=123:abcdef\nTELEGRAM_ALLOWED_USERS=111,222,333\n"
    )
    monkeypatch.setenv("APPROVE_BOT_ENV", str(env_file))
    monkeypatch.setenv("APPROVE_STATE_DIR", str(tmp_path / "state"))

    cfg = ham.load_config()
    assert cfg.allowed_user_ids == {111, 222, 333}


def test_load_config_missing_token(monkeypatch, tmp_path, ham):
    monkeypatch.delenv("APPROVE_BOT_ENV", raising=False)
    monkeypatch.delenv("APPROVE_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    with pytest.raises(ham.ConfigError) as exc:
        ham.load_config()
    assert "APPROVE_BOT_TOKEN" in str(exc.value)


def test_load_config_missing_allowed_users(monkeypatch, tmp_path, ham):
    monkeypatch.setenv("APPROVE_BOT_TOKEN", "123:abc")
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    with pytest.raises(ham.ConfigError) as exc:
        ham.load_config()
    assert "TELEGRAM_ALLOWED_USERS" in str(exc.value)


def test_load_config_invalid_uid(monkeypatch, tmp_path, ham):
    monkeypatch.setenv("APPROVE_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "not_a_number")
    with pytest.raises(ham.ConfigError):
        ham.load_config()


def test_load_config_state_dir_default(monkeypatch, tmp_path, ham):
    monkeypatch.setenv("APPROVE_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "111")
    monkeypatch.delenv("APPROVE_STATE_DIR", raising=False)
    cfg = ham.load_config()
    # Default is repo-rooted .approve/
    assert cfg.state_dir.name == ".approve"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/unit/test_config.py
```
Expected: FAIL with `AttributeError: ... has no attribute 'load_config'`.

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/hermes-approve-mcp.py`:

```python
from dotenv import load_dotenv


DEFAULT_STATE_DIR = Path(__file__).resolve().parent.parent / ".approve"
DEFAULT_BOT_API_BASE = "https://api.telegram.org"


@dataclass
class Config:
    """Runtime config parsed from env."""
    bot_token: str
    allowed_user_ids: set[int]
    state_dir: Path
    bot_api_base: str = DEFAULT_BOT_API_BASE


class ConfigError(Exception):
    """Raised when env is missing or malformed."""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def load_config() -> Config:
    """Read env (optionally from APPROVE_BOT_ENV file), validate, return Config."""
    env_path = os.getenv("APPROVE_BOT_ENV")
    if env_path:
        load_dotenv(env_path, override=True)

    token = os.getenv("APPROVE_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "APPROVE_BOT_TOKEN is missing. Set it in the environment or in "
            "the file pointed to by APPROVE_BOT_ENV."
        )

    uids_raw = os.getenv("TELEGRAM_ALLOWED_USERS", "").strip()
    if not uids_raw:
        raise ConfigError(
            "TELEGRAM_ALLOWED_USERS is missing. Set it to a comma-separated "
            "list of Telegram user IDs allowed to approve."
        )
    allowed: set[int] = set()
    for chunk in uids_raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            allowed.add(int(chunk))
        except ValueError as exc:
            raise ConfigError(
                f"TELEGRAM_ALLOWED_USERS contains non-integer value: {chunk!r}"
            ) from exc
    if not allowed:
        raise ConfigError("TELEGRAM_ALLOWED_USERS parsed to empty set")

    state_dir_str = os.getenv("APPROVE_STATE_DIR", "").strip()
    state_dir = Path(state_dir_str) if state_dir_str else DEFAULT_STATE_DIR

    return Config(
        bot_token=token,
        allowed_user_ids=allowed,
        state_dir=state_dir,
        bot_api_base=os.getenv("APPROVE_BOT_API_BASE", DEFAULT_BOT_API_BASE),
    )
```

Also add `import os` and `from pathlib import Path` at the top if not already imported.

- [ ] **Step 4: Run tests to verify they pass**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/unit/test_config.py
```

- [ ] **Step 5: ruff + mypy clean**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m ruff check scripts/hermes-approve-mcp.py tests/
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m mypy scripts/hermes-approve-mcp.py
```

- [ ] **Step 6: Commit**

```bash
cd C:/Users/Bew/ZCodeProject
git add scripts/hermes-approve-mcp.py tests/unit/test_config.py
git commit -m "feat(phase-2): Config + load_config env parsing (TDD)"
```

---

## Task 6: `PendingStore` — In-Mem State + Audit Log

**Files:**
- Modify: `C:\Users\Bew\ZCodeProject\scripts\hermes-approve-mcp.py` (add `PendingStore`)
- Create: `C:\Users\Bew\ZCodeProject\tests\integration\test_store.py`

**Interfaces:**
- Consumes: `ApprovalRequest` (from Task 4), `Config.state_dir` (from Task 5)
- Produces:
  - `class PendingStore`:
    - `__init__(self, state_dir: Path)`
    - `add(req: ApprovalRequest) -> threading.Event` — also writes audit `created` event
    - `get(req_id: str) -> Optional[ApprovalRequest]`
    - `is_pending(req_id: str) -> bool`
    - `set_resolution(req_id: str, decision: str, reason: str, responded_by: Optional[int] = None) -> bool` — thread-safe; writes audit `resolved` event; sets the event from `add()`. Returns `True` if this caller won the race.
    - `pending_ids() -> list[str]`
    - `mark_sent(req_id: str, telegram_message_id: int) -> None` — writes audit `sent` event
    - All methods safe to call from multiple threads.

- [ ] **Step 1: Write failing tests**

File: `tests/integration/test_store.py`

```python
"""Tests for PendingStore — thread-safe pending requests + audit log."""
import json
import threading
from datetime import datetime, timezone

import pytest


def _make_req(ham, **kw):
    base = dict(
        id="abc12345",
        action="x",
        risk="low",
        summary="x" * 20,
        created_at=datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc),
        timeout_seconds=900,
        status="pending",
    )
    base.update(kw)
    return ham.ApprovalRequest(**base)


def test_add_returns_event_and_stores_request(ham, tmp_path):
    store = ham.PendingStore(tmp_path)
    req = _make_req(ham)
    event = store.add(req)
    assert isinstance(event, threading.Event)
    assert store.is_pending("abc12345")
    assert store.get("abc12345").id == "abc12345"


def test_add_writes_created_audit(ham, tmp_path):
    store = ham.PendingStore(tmp_path)
    store.add(_make_req(ham))
    pending_file = tmp_path / "pending.jsonl"
    assert pending_file.exists()
    lines = pending_file.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["event"] == "created"
    assert entry["id"] == "abc12345"


def test_mark_sent_appends_sent_event(ham, tmp_path):
    store = ham.PendingStore(tmp_path)
    store.add(_make_req(ham))
    store.mark_sent("abc12345", telegram_message_id=42)
    lines = (tmp_path / "pending.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1]) == {
        "event": "sent",
        "id": "abc12345",
        "telegram_message_id": 42,
        "sent_at": json.loads(lines[1])["sent_at"],  # don't pin timestamp
    }


def test_set_resolution_returns_true_first_time(ham, tmp_path):
    store = ham.PendingStore(tmp_path)
    store.add(_make_req(ham))
    ok = store.set_resolution("abc12345", "allow", "user_allowed", responded_by=111)
    assert ok is True


def test_set_resolution_returns_false_on_second_call(ham, tmp_path):
    store = ham.PendingStore(tmp_path)
    store.add(_make_req(ham))
    store.set_resolution("abc12345", "allow", "user_allowed", responded_by=111)
    ok = store.set_resolution("abc12345", "deny", "user_denied", responded_by=111)
    assert ok is False


def test_set_resolution_sets_event(ham, tmp_path):
    store = ham.PendingStore(tmp_path)
    event = store.add(_make_req(ham))
    assert not event.is_set()
    store.set_resolution("abc12345", "allow", "user_allowed", responded_by=111)
    assert event.is_set()


def test_set_resolution_unknown_id_returns_false(ham, tmp_path):
    store = ham.PendingStore(tmp_path)
    ok = store.set_resolution("nonexistent", "allow", "user_allowed")
    assert ok is False


def test_set_resolution_appends_resolved_audit(ham, tmp_path):
    store = ham.PendingStore(tmp_path)
    store.add(_make_req(ham))
    store.set_resolution("abc12345", "deny", "timeout", responded_by=None)
    lines = (tmp_path / "pending.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    entry = json.loads(lines[1])
    assert entry["event"] == "resolved"
    assert entry["decision"] == "deny"
    assert entry["reason"] == "timeout"


def test_concurrent_resolution_only_one_wins(ham, tmp_path):
    """10 threads race to resolve the same id; exactly one returns True."""
    store = ham.PendingStore(tmp_path)
    store.add(_make_req(ham))
    results: list[bool] = []
    barrier = threading.Barrier(10)

    def race():
        barrier.wait()
        ok = store.set_resolution("abc12345", "allow", "user_allowed", responded_by=111)
        results.append(ok)

    threads = [threading.Thread(target=race) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 1  # exactly one winner
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/integration/test_store.py
```
Expected: FAIL with `AttributeError: ... has no attribute 'PendingStore'`.

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/hermes-approve-mcp.py`:

```python
import threading
from typing import Any


class PendingStore:
    """Thread-safe store of pending approval requests + audit log writer."""

    def __init__(self, state_dir: Path):
        self._state_dir = state_dir
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._pending_file = state_dir / "pending.jsonl"
        self._lock = threading.Lock()
        self._requests: dict[str, ApprovalRequest] = {}
        self._events: dict[str, threading.Event] = {}
        self._message_ids: dict[str, int] = {}

    def add(self, req: ApprovalRequest) -> threading.Event:
        """Register a new pending request. Returns the Event that will be set on resolution."""
        with self._lock:
            if req.id in self._requests:
                raise ValueError(f"duplicate request id: {req.id}")
            event = threading.Event()
            self._requests[req.id] = req
            self._events[req.id] = event
        self._append_audit({
            "event": "created",
            "id": req.id,
            "action": req.action,
            "risk": req.risk,
            "summary": req.summary,
            "created_at": _iso(req.created_at),
            "expires_at": _iso(req.created_at, req.timeout_seconds),
        })
        return event

    def get(self, req_id: str) -> Optional[ApprovalRequest]:
        with self._lock:
            return self._requests.get(req_id)

    def is_pending(self, req_id: str) -> bool:
        with self._lock:
            return req_id in self._requests and self._requests[req_id].status == "pending"

    def pending_ids(self) -> list[str]:
        with self._lock:
            return [rid for rid, r in self._requests.items() if r.status == "pending"]

    def mark_sent(self, req_id: str, telegram_message_id: int) -> None:
        with self._lock:
            self._message_ids[req_id] = telegram_message_id
        self._append_audit({
            "event": "sent",
            "id": req_id,
            "telegram_message_id": telegram_message_id,
            "sent_at": _iso_now(),
        })

    def set_resolution(
        self,
        req_id: str,
        decision: str,
        reason: str,
        responded_by: Optional[int] = None,
    ) -> bool:
        """Resolve a request. Returns True if this caller won the race, False otherwise."""
        with self._lock:
            req = self._requests.get(req_id)
            if req is None or req.status == "resolved":
                return False
            req.status = "resolved"
            req.decision = decision
            req.reason = reason
            req.responded_by = responded_by
            req.resolved_at = datetime.now(timezone.utc)
            event = self._events.get(req_id)
        if event is not None:
            event.set()
        self._append_audit({
            "event": "resolved",
            "id": req_id,
            "decision": decision,
            "reason": reason,
            "responded_by": responded_by,
            "resolved_at": _iso_now(),
        })
        return True

    def _append_audit(self, entry: dict[str, Any]) -> None:
        with self._lock:
            with self._pending_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _iso(dt: datetime, add_seconds: int = 0) -> str:
    """ISO 8601 UTC string. dt may be tz-aware; add_seconds shifts the expiry."""
    from datetime import timedelta
    if add_seconds:
        dt = dt + timedelta(seconds=add_seconds)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _iso_now() -> str:
    return _iso(datetime.now(timezone.utc))
```

Also ensure `import json` is at the top.

- [ ] **Step 4: Run tests to verify they pass**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/integration/test_store.py
```

- [ ] **Step 5: ruff + mypy clean**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m ruff check scripts/hermes-approve-mcp.py tests/
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m mypy scripts/hermes-approve-mcp.py
```

- [ ] **Step 6: Commit**

```bash
cd C:/Users/Bew/ZCodeProject
git add scripts/hermes-approve-mcp.py tests/integration/test_store.py
git commit -m "feat(phase-2): PendingStore thread-safe state + audit log (TDD)"
```

---

## Task 7: `TelegramClient` — httpx Wrapper with Retry

**Files:**
- Modify: `C:\Users\Bew\ZCodeProject\scripts\hermes-approve-mcp.py` (add `TelegramClient`, `TelegramError`)
- Create: `C:\Users\Bew\ZCodeProject\tests\integration\test_telegram_client.py`

**Interfaces:**
- Consumes: `Config.bot_token`, `Config.bot_api_base`
- Produces:
  - `class TelegramError(Exception)` with `.status: Optional[int]`, `.method: str`, `.detail: str`
  - `class TelegramClient`:
    - `__init__(self, bot_token: str, bot_api_base: str = "https://api.telegram.org", transport: Optional[httpx.BaseTransport] = None)` — `transport` arg is for tests
    - `send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None) -> int` — returns `message_id`
    - `edit_message_text(chat_id: int, message_id: int, text: str, reply_markup: Optional[dict] = None) -> None`
    - `answer_callback_query(callback_query_id: str, text: str, show_alert: bool = False) -> None`
    - `get_updates(offset: int = 0, timeout: int = 30) -> list[dict]`
    - All methods retry once on transient errors (network / 5xx / 429). Non-retryable: 400, 401, 403.

- [ ] **Step 1: Write failing tests**

File: `tests/integration/test_telegram_client.py`

```python
"""Tests for TelegramClient — httpx wrapper, mocked transport."""
import json

import httpx
import pytest


def _client_with_handler(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, timeout=10)


def test_send_message_returns_message_id(ham):
    def handler(req: httpx.Request) -> httpx.Response:
        assert "sendMessage" in req.url.path
        return httpx.Response(200, json={
            "ok": True,
            "result": {"message_id": 42, "date": 0, "chat": {"id": 1}},
        })
    client = ham.TelegramClient(token="123:abc", transport=httpx.MockTransport(handler))
    msg_id = client.send_message(chat_id=1, text="hello")
    assert msg_id == 42


def test_send_message_includes_reply_markup(ham):
    captured = {}
    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={
            "ok": True,
            "result": {"message_id": 1, "date": 0, "chat": {"id": 1}},
        })
    client = ham.TelegramClient(token="123:abc", transport=httpx.MockTransport(handler))
    markup = {"inline_keyboard": [[{"text": "Allow", "callback_data": "ap:1:allow"}]]}
    client.send_message(chat_id=1, text="hi", reply_markup=markup)
    assert captured["body"]["reply_markup"] == markup
    assert captured["body"]["parse_mode"] == "HTML"


def test_send_message_400_raises_no_retry(ham):
    calls = {"n": 0}
    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"ok": False, "description": "chat not found"})
    client = ham.TelegramClient(token="123:abc", transport=httpx.MockTransport(handler))
    with pytest.raises(ham.TelegramError):
        client.send_message(chat_id=1, text="hi")
    assert calls["n"] == 1  # 400 → no retry


def test_send_message_500_retries_once(ham):
    calls = {"n": 0}
    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"ok": False, "description": "server error"})
        return httpx.Response(200, json={
            "ok": True,
            "result": {"message_id": 7, "date": 0, "chat": {"id": 1}},
        })
    client = ham.TelegramClient(token="123:abc", transport=httpx.MockTransport(handler))
    msg_id = client.send_message(chat_id=1, text="hi")
    assert calls["n"] == 2
    assert msg_id == 7


def test_send_message_500_twice_raises(ham):
    calls = {"n": 0}
    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"ok": False, "description": "down"})
    client = ham.TelegramClient(token="123:abc", transport=httpx.MockTransport(handler))
    with pytest.raises(ham.TelegramError):
        client.send_message(chat_id=1, text="hi")
    assert calls["n"] == 2  # initial + 1 retry


def test_get_updates_returns_result_list(ham):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": True,
            "result": [
                {"update_id": 100, "callback_query": {"id": "cb1", "data": "ap:1:allow"}},
            ],
        })
    client = ham.TelegramClient(token="123:abc", transport=httpx.MockTransport(handler))
    updates = client.get_updates(offset=101, timeout=1)
    assert len(updates) == 1
    assert updates[0]["update_id"] == 100


def test_edit_message_text_succeeds(ham):
    calls = []
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "result": {}})
    client = ham.TelegramClient(token="123:abc", transport=httpx.MockTransport(handler))
    client.edit_message_text(chat_id=1, message_id=42, text="resolved")
    assert calls[0]["message_id"] == 42
    assert calls[0]["text"] == "resolved"


def test_answer_callback_query_succeeds(ham):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["callback_query_id"] == "cb1"
        assert body["text"] == "✅ Approved"
        return httpx.Response(200, json={"ok": True, "result": True})
    client = ham.TelegramClient(token="123:abc", transport=httpx.MockTransport(handler))
    client.answer_callback_query("cb1", "✅ Approved")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/integration/test_telegram_client.py
```
Expected: FAIL with `AttributeError: ... has no attribute 'TelegramClient'`.

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/hermes-approve-mcp.py`:

```python
import httpx


class TelegramError(Exception):
    """Raised when a Telegram API call fails permanently."""
    def __init__(self, method: str, detail: str, status: Optional[int] = None):
        super().__init__(f"{method} failed (status={status}): {detail}")
        self.method = method
        self.detail = detail
        self.status = status


# Status codes we never retry.
_FATAL_STATUSES = frozenset({400, 401, 403})


class TelegramClient:
    """Thin httpx wrapper for the small subset of Bot API methods we use."""

    def __init__(
        self,
        token: str,
        bot_api_base: str = "https://api.telegram.org",
        transport: Optional[httpx.BaseTransport] = None,
        timeout: float = 10.0,
    ):
        self._base = f"{bot_api_base.rstrip('/')}/bot{token}"
        self._client = httpx.Client(transport=transport, timeout=timeout)

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        """Call a Bot API method with 1 retry on transient errors."""
        url = f"{self._base}/{method}"
        last_exc: Optional[TelegramError] = None
        for attempt in range(2):  # initial + 1 retry
            try:
                r = self._client.post(url, json=payload)
            except httpx.RequestError as e:
                last_exc = TelegramError(method, f"network: {e}")
                if attempt == 0:
                    time.sleep(1)
                    continue
                raise last_exc

            if r.status_code == 200:
                body = r.json()
                if body.get("ok"):
                    return body["result"]
                # Telegram-style error: 200 with ok=False
                last_exc = TelegramError(method, body.get("description", "unknown"))
                if attempt == 0 and "retry_after" not in body:
                    time.sleep(1)
                    continue
                if "retry_after" in body:
                    time.sleep(float(body["retry_after"]))
                    continue
                raise last_exc

            if r.status_code in _FATAL_STATUSES:
                raise TelegramError(method, f"HTTP {r.status_code}: {r.text}", r.status_code)

            # 5xx / 429 → retry
            last_exc = TelegramError(method, f"HTTP {r.status_code}: {r.text}", r.status_code)
            if attempt == 0:
                time.sleep(1)
                continue
            raise last_exc
        # Should not reach here
        raise last_exc if last_exc else TelegramError(method, "unreachable")

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Optional[dict] = None,
    ) -> int:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = self._call("sendMessage", payload)
        return int(result["message_id"])

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Optional[dict] = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self._call("editMessageText", payload)

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: str,
        show_alert: bool = False,
    ) -> None:
        self._call("answerCallbackQuery", {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        })

    def get_updates(self, offset: int = 0, timeout: int = 30) -> list[dict]:
        result = self._call("getUpdates", {"offset": offset, "timeout": timeout})
        return list(result) if result else []
```

Also ensure `import time` is present.

- [ ] **Step 4: Run tests to verify they pass**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/integration/test_telegram_client.py
```

- [ ] **Step 5: ruff + mypy clean**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m ruff check scripts/hermes-approve-mcp.py tests/
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m mypy scripts/hermes-approve-mcp.py
```

- [ ] **Step 6: Commit**

```bash
cd C:/Users/Bew/ZCodeProject
git add scripts/hermes-approve-mcp.py tests/integration/test_telegram_client.py
git commit -m "feat(phase-2): TelegramClient httpx wrapper with 1 retry (TDD)"
```

---

## Task 8: `TelegramPoller` — Daemon Thread + State Persistence

**Files:**
- Modify: `C:\Users\Bew\ZCodeProject\scripts\hermes-approve-mcp.py` (add `TelegramPoller`)
- Create: `C:\Users\Bew\ZCodeProject\tests\integration\test_poller.py`

**Interfaces:**
- Consumes: `Config`, `PendingStore`, `TelegramClient`, `parse_callback`, `format_resolved`
- Produces:
  - `class TelegramPoller(threading.Thread)` (daemon=True):
    - `__init__(self, client: TelegramClient, store: PendingStore, config: Config)`
    - `run() -> None` — loop forever: `get_updates` → dispatch each → commit offset
    - `stop() -> None` — sets internal flag, returns immediately
    - `is_alive()` (inherited)
    - `last_call_at: Optional[datetime]`, `last_error: Optional[str]` — for `ping()` introspection
  - Internal dispatch logic (private):
    - For each update: if `callback_query` → check allowlist → check store → set_resolution → answerCallbackQuery → editMessageText
    - For other updates (text messages, etc.): ignore
  - State persistence: writes `state_dir / "state.json"` after each batch with `{"last_update_id": N, "last_heartbeat_at": iso}`
  - On boot, reads `state.json`; first `get_updates(offset=last+1)`

- [ ] **Step 1: Write failing tests**

File: `tests/integration/test_poller.py`

```python
"""Tests for TelegramPoller — daemon thread, callback dispatch, state.json."""
import json
import threading
import time
from datetime import datetime, timezone

import httpx


def _make_req(ham, store, **kw):
    base = dict(
        id="abc12345",
        action="x",
        risk="low",
        summary="x" * 20,
        created_at=datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc),
        timeout_seconds=900,
        status="pending",
    )
    base.update(kw)
    req = ham.ApprovalRequest(**base)
    store.add(req)
    return req


def _config(ham, tmp_path):
    return ham.Config(
        bot_token="123:abc",
        allowed_user_ids={111},
        state_dir=tmp_path,
        bot_api_base="https://example.test",
    )


def test_poller_resolves_allow(ham, tmp_path):
    """Poller sees a callback_query for a pending request and resolves it allow."""
    store = ham.PendingStore(tmp_path)
    cfg = _config(ham, tmp_path)
    _make_req(ham, store)
    event = store._events["abc12345"]  # access internal for test

    batches = iter([
        [{"update_id": 1, "callback_query": {
            "id": "cb1", "from": {"id": 111}, "data": "ap:abc12345:allow",
            "message": {"message_id": 42, "chat": {"id": 111}},
        }}],
        [],
    ])
    def handler(req):
        body = json.loads(req.content)
        if "getUpdates" in req.url.path:
            try:
                return httpx.Response(200, json={"ok": True, "result": next(batches)})
            except StopIteration:
                return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": {}})
    client = ham.TelegramClient(token="123:abc", transport=httpx.MockTransport(handler))

    poller = ham.TelegramPoller(client=client, store=store, config=cfg)
    poller.start()
    try:
        assert event.wait(timeout=3.0), "event should be set within 3s"
    finally:
        poller.stop()
        poller.join(timeout=5.0)

    req = store.get("abc12345")
    assert req.status == "resolved"
    assert req.decision == "allow"


def test_poller_ignores_unauthorized_user(ham, tmp_path):
    store = ham.PendingStore(tmp_path)
    cfg = _config(ham, tmp_path)  # allowlist = {111}
    _make_req(ham, store)
    event = store._events["abc12345"]

    batches = iter([
        [{"update_id": 1, "callback_query": {
            "id": "cb1", "from": {"id": 999}, "data": "ap:abc12345:allow",
            "message": {"message_id": 42, "chat": {"id": 111}},
        }}],
        [],
    ])
    def handler(req):
        if "getUpdates" in req.url.path:
            try:
                return httpx.Response(200, json={"ok": True, "result": next(batches)})
            except StopIteration:
                return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": True})
    client = ham.TelegramClient(token="123:abc", transport=httpx.MockTransport(handler))

    poller = ham.TelegramPoller(client=client, store=store, config=cfg)
    poller.start()
    try:
        # Event should NOT be set — unauthorized user
        assert not event.wait(timeout=1.0)
    finally:
        poller.stop()
        poller.join(timeout=5.0)
    assert store.is_pending("abc12345")  # still pending


def test_poller_drops_stale_callback_unknown_id(ham, tmp_path):
    """callback for an id not in store → ignored silently, offset still advances."""
    store = ham.PendingStore(tmp_path)
    cfg = _config(ham, tmp_path)

    batches = iter([
        [{"update_id": 1, "callback_query": {
            "id": "cb1", "from": {"id": 111}, "data": "ap:nonexistent:allow",
            "message": {"message_id": 42, "chat": {"id": 111}},
        }}],
        [],
    ])
    def handler(req):
        if "getUpdates" in req.url.path:
            try:
                return httpx.Response(200, json={"ok": True, "result": next(batches)})
            except StopIteration:
                return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": True})
    client = ham.TelegramClient(token="123:abc", transport=httpx.MockTransport(handler))

    poller = ham.TelegramPoller(client=client, store=store, config=cfg)
    poller.start()
    try:
        time.sleep(1.0)  # let one batch process
    finally:
        poller.stop()
        poller.join(timeout=5.0)

    state_file = tmp_path / "state.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert state["last_update_id"] == 1  # offset advanced even though we dropped


def test_poller_persists_offset_across_restart(ham, tmp_path):
    """state.json's last_update_id is used as initial offset on next start."""
    store = ham.PendingStore(tmp_path)
    cfg = _config(ham, tmp_path)
    # Write a stale state.json as if a previous run committed offset=99
    (tmp_path / "state.json").write_text(json.dumps({"last_update_id": 99}))

    captured_offsets = []
    def handler(req):
        if "getUpdates" in req.url.path:
            body = json.loads(req.content)
            captured_offsets.append(body.get("offset"))
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": True})
    client = ham.TelegramClient(token="123:abc", transport=httpx.MockTransport(handler))

    poller = ham.TelegramPoller(client=client, store=store, config=cfg)
    poller.start()
    try:
        time.sleep(0.5)
    finally:
        poller.stop()
        poller.join(timeout=5.0)
    assert captured_offsets[0] == 100  # last_update_id + 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/integration/test_poller.py
```
Expected: FAIL with `AttributeError: ... has no attribute 'TelegramPoller'`.

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/hermes-approve-mcp.py`:

```python
class TelegramPoller(threading.Thread):
    """Daemon thread: long-polls getUpdates and dispatches callback_query events."""

    def __init__(self, client: TelegramClient, store: PendingStore, config: Config):
        super().__init__(daemon=True, name="telegram-poller")
        self._client = client
        self._store = store
        self._config = config
        self._stop_flag = threading.Event()
        self._state_file = config.state_dir / "state.json"
        self._last_update_id = self._load_last_update_id()
        self.last_call_at: Optional[datetime] = None
        self.last_error: Optional[str] = None

    def _load_last_update_id(self) -> int:
        if not self._state_file.exists():
            return 0
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            return int(data.get("last_update_id", 0))
        except (json.JSONDecodeError, ValueError, OSError):
            return 0

    def _save_state(self, last_update_id: int) -> None:
        tmp = self._state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "last_update_id": last_update_id,
            "last_heartbeat_at": _iso_now(),
        }), encoding="utf-8")
        tmp.replace(self._state_file)

    def stop(self) -> None:
        self._stop_flag.set()

    def run(self) -> None:
        while not self._stop_flag.is_set():
            try:
                self.last_call_at = datetime.now(timezone.utc)
                updates = self._client.get_updates(
                    offset=self._last_update_id + 1,
                    timeout=30,
                )
                for update in updates:
                    self._handle_update(update)
                    self._last_update_id = max(
                        self._last_update_id,
                        int(update.get("update_id", self._last_update_id)),
                    )
                if updates:
                    self._save_state(self._last_update_id)
                self.last_error = None
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                # Don't tight-loop on persistent errors
                self._stop_flag.wait(5.0)

    def _handle_update(self, update: dict) -> None:
        cq = update.get("callback_query")
        if not cq:
            return  # ignore non-callback updates (text messages, etc.)
        parsed = parse_callback(cq.get("data", ""))
        cq_id = cq.get("id", "")
        from_id = (cq.get("from") or {}).get("id")
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        message_id = msg.get("message_id")

        if from_id not in self._config.allowed_user_ids:
            self._safe_answer(cq_id, "⛔ Unauthorized")
            return

        if parsed is None:
            return  # not our callback format; ignore

        req_id, decision = parsed
        if not self._store.is_pending(req_id):
            self._safe_answer(cq_id, "⌛ Expired")
            return

        reason = "user_allowed" if decision == "allow" else "user_denied"
        ok = self._store.set_resolution(req_id, decision, reason, responded_by=from_id)
        if not ok:
            self._safe_answer(cq_id, "⚠️ Already resolved")
            return

        self._safe_answer(cq_id, "✅ Approved" if decision == "allow" else "❌ Denied")
        if chat_id is not None and message_id is not None:
            req = self._store.get(req_id)
            if req is not None:
                try:
                    self._client.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=format_resolved(req),
                        reply_markup=None,  # remove buttons
                    )
                except TelegramError:
                    pass  # user may have deleted the message; not fatal

    def _safe_answer(self, cq_id: str, text: str) -> None:
        try:
            self._client.answer_callback_query(cq_id, text)
        except TelegramError:
            pass
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/integration/test_poller.py
```

Note: tests use `pytest-timeout` implicit via small `event.wait()` timeouts. If any test hangs, the bug is in the poller's stop/join logic.

- [ ] **Step 5: ruff + mypy clean**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m ruff check scripts/hermes-approve-mcp.py tests/
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m mypy scripts/hermes-approve-mcp.py
```

- [ ] **Step 6: Commit**

```bash
cd C:/Users/Bew/ZCodeProject
git add scripts/hermes-approve-mcp.py tests/integration/test_poller.py
git commit -m "feat(phase-2): TelegramPoller daemon thread + state.json persistence (TDD)"
```

---

## Task 9: `request_approval` + `ping` MCP Tools (FastMCP Wiring)

**Files:**
- Modify: `C:\Users\Bew\ZCodeProject\scripts\hermes-approve-mcp.py` (add module-level globals + tool functions + `__main__`)
- Create: `C:\Users\Bew\ZCodeProject\tests\integration\test_request_approval.py`

**Interfaces:**
- Consumes: All previous components
- Produces:
  - Module-level globals `_config`, `_store`, `_client`, `_poller` (initialized in `_initialize()`)
  - `_initialize() -> None` — idempotent; called at import time + on demand if `_config is None`
  - `@mcp.tool() def request_approval(action, risk, summary, timeout_seconds=900) -> str`
  - `@mcp.tool() def ping() -> str`
  - `def main() -> None` — initializes + starts poller + `mcp.run()`
  - `if __name__ == "__main__": main()`

**Design note**: We do NOT instantiate `FastMCP("hermes-approve")` at import time inside a test context because the tests import the module via importlib and we don't want to start the poller during collection. Solution: lazy init via `_initialize()` that reads env. Tests monkeypatch `_config`, `_store`, `_client`, `_poller` directly rather than going through env.

- [ ] **Step 1: Write failing tests**

File: `tests/integration/test_request_approval.py`

```python
"""End-to-end tests for request_approval tool — mocks Telegram, exercises store + poller."""
import json
import threading
import time
from datetime import datetime, timezone

import httpx
import pytest


def _wire_module(ham, tmp_path, batches_iter, allowed_uids=None):
    """Inject test doubles into the module's globals; return (store, poller, batches_handler)."""
    if allowed_uids is None:
        allowed_uids = {111}
    cfg = ham.Config(
        bot_token="123:abc",
        allowed_user_ids=set(allowed_uids),
        state_dir=tmp_path,
        bot_api_base="https://example.test",
    )
    store = ham.PendingStore(tmp_path)

    # Telegram handler that yields from batches_iter for getUpdates and acks everything else.
    lock = threading.Lock()
    def handler(req: httpx.Request) -> httpx.Response:
        if "getUpdates" in req.url.path:
            with lock:
                try:
                    batch = next(batches_iter)
                except StopIteration:
                    batch = []
            return httpx.Response(200, json={"ok": True, "result": batch})
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = ham.TelegramClient(token="123:abc", transport=httpx.MockTransport(handler))
    poller = ham.TelegramPoller(client=client, store=store, config=cfg)

    # Monkeypatch module-level globals so request_approval sees them.
    ham._config = cfg
    ham._store = store
    ham._client = client
    ham._poller = poller
    return store, poller


def test_request_approval_happy_allow(ham, tmp_path):
    """Agent calls tool → message sent → user clicks Allow → tool returns allow JSON."""
    batches = iter([
        [{"update_id": 1, "callback_query": {
            "id": "cb1", "from": {"id": 111}, "data": "ap:DYNAMIC:allow",
            "message": {"message_id": 42, "chat": {"id": 111}},
        }}],
        [],
    ])
    store, poller = _wire_module(ham, tmp_path, batches)
    poller.start()
    try:
        result_json = ham.request_approval(
            action="git push --force origin main",
            risk="destructive",
            summary="Overwrite remote history because a secret was pushed.",
            timeout_seconds=120,
        )
    finally:
        poller.stop()
        poller.join(timeout=5.0)

    result = json.loads(result_json)
    assert result["decision"] == "allow"
    assert result["reason"] == "user_allowed"
    assert result["responded_by"] == 111
    assert "id" in result


def test_request_approval_validation_short_summary(ham, tmp_path):
    """Tool returns validation error JSON, never sends to Telegram."""
    batches = iter([])
    _wire_module(ham, tmp_path, batches)
    result_json = ham.request_approval(
        action="x",
        risk="low",
        summary="too short",
        timeout_seconds=60,
    )
    result = json.loads(result_json)
    assert result["error"] == "validation_failed"
    assert result["field"] == "summary"


def test_request_approval_validation_invalid_risk(ham, tmp_path):
    _wire_module(ham, tmp_path, iter([]))
    result = json.loads(ham.request_approval(
        action="x",
        risk="critical",
        summary="x" * 20,
        timeout_seconds=60,
    ))
    assert result["error"] == "validation_failed"
    assert result["field"] == "risk"


def test_request_approval_timeout_auto_deny(ham, tmp_path):
    """No callback ever arrives → tool returns auto-deny after timeout_seconds."""
    batches = iter([])  # never any updates
    store, poller = _wire_module(ham, tmp_path, batches)
    poller.start()
    try:
        result_json = ham.request_approval(
            action="x",
            risk="low",
            summary="x" * 20,
            timeout_seconds=1,  # 1s — within [60,1800] violation but TEST ONLY override
        )
    finally:
        poller.stop()
        poller.join(timeout=5.0)

    result = json.loads(result_json)
    assert result["decision"] == "deny"
    assert result["reason"] == "timeout"
    assert result["auto"] is True
```

⚠️ **Note on the timeout test**: The spec mandates `timeout_seconds ≥ 60`. To avoid a 60s unit test, the implementation must **separate validation from enforcement** — `validate()` rejects `timeout_seconds < 60`, but the test passes `timeout_seconds=1` directly into the internal flow. Resolution: split into two layers:

- `request_approval()` (the public tool) calls `validate()` and rejects `< 60`.
- A private `_run_approval(req: ApprovalRequest) -> str` takes a fully-constructed request and uses `req.timeout_seconds` as-is. Tests call `_run_approval` directly, bypassing validation.

Update the test to call `ham._run_approval(req)` instead of `ham.request_approval(...)` for the timeout case:

```python
def test_request_approval_timeout_auto_deny(ham, tmp_path):
    """No callback ever arrives → tool returns auto-deny after timeout."""
    batches = iter([])
    store, poller = _wire_module(ham, tmp_path, batches)
    poller.start()
    try:
        req = ham.ApprovalRequest(
            id=ham.gen_id(),
            action="x",
            risk="low",
            summary="x" * 20,
            created_at=datetime.now(timezone.utc),
            timeout_seconds=1,  # private layer ignores the [60,1800] rule
            status="pending",
        )
        result_json = ham._run_approval(req)
    finally:
        poller.stop()
        poller.join(timeout=5.0)
    result = json.loads(result_json)
    assert result["decision"] == "deny"
    assert result["reason"] == "timeout"
    assert result["auto"] is True
```

Also note `_wire_module`'s `batches` for the happy-path test hardcodes `"ap:DYNAMIC:allow"` — that ID won't match. Fix by capturing the actual id when `sendMessage` is called. Update handler in `_wire_module`:

```python
captured_msg_ids = {}

def handler(req: httpx.Request) -> httpx.Response:
    body = json.loads(req.content) if req.content else {}
    if "sendMessage" in req.url.path:
        # Capture the request id embedded in callback_data so the test's
        # simulated user clicks the right button.
        markup = body.get("reply_markup") or {}
        for row in markup.get("inline_keyboard", []):
            for btn in row:
                if "callback_data" in btn:
                    captured_msg_ids["id"] = btn["callback_data"].split(":")[1]
        return httpx.Response(200, json={
            "ok": True,
            "result": {"message_id": 42, "date": 0, "chat": {"id": 111}},
        })
    if "getUpdates" in req.url.path:
        with lock:
            try:
                batch = next(batches_iter)
            except StopIteration:
                batch = []
        # Rewrite the placeholder id to the real captured id.
        rid = captured_msg_ids.get("id", "unknown")
        for upd in batch:
            cq = upd.get("callback_query")
            if cq and "data" in cq:
                cq["data"] = cq["data"].replace("DYNAMIC", rid)
        return httpx.Response(200, json={"ok": True, "result": batch})
    return httpx.Response(200, json={"ok": True, "result": {}})
```

And the test's batch uses `"ap:DYNAMIC:allow"` — the handler rewrites it post-capture.

- [ ] **Step 2: Run tests to verify they fail**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/integration/test_request_approval.py
```
Expected: FAIL with `AttributeError: ... has no attribute 'request_approval'`.

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/hermes-approve-mcp.py`:

```python
from mcp.server.fastmcp import FastMCP


mcp = FastMCP("hermes-approve")

# Module-level singletons, initialized lazily.
_config: Optional[Config] = None
_store: Optional[PendingStore] = None
_client: Optional[TelegramClient] = None
_poller: Optional[TelegramPoller] = None


def _initialize() -> None:
    """Initialize global config/store/client/poller. Idempotent. Safe to call multiple times."""
    global _config, _store, _client, _poller
    if _config is not None:
        return
    _config = load_config()
    _store = PendingStore(_config.state_dir)
    _client = TelegramClient(token=_config.bot_token, bot_api_base=_config.bot_api_base)
    _poller = TelegramPoller(client=_client, store=_store, config=_config)


def _inline_keyboard(req_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Allow", "callback_data": f"ap:{req_id}:allow"},
            {"text": "❌ Deny",  "callback_data": f"ap:{req_id}:deny"},
        ]]
    }


@mcp.tool()
def request_approval(
    action: str,
    risk: str,
    summary: str,
    timeout_seconds: int = 900,
) -> str:
    """Request human approval via Telegram. Blocks until decision or timeout.

    Returns JSON with 'decision' field. Always returns JSON — never raises.
    """
    err = validate(action, risk, summary, timeout_seconds)
    if err is not None:
        return json.dumps({
            "error": "validation_failed",
            "field": err.field,
            "message": err.message,
        })

    if _config is None or _store is None or _client is None:
        try:
            _initialize()
        except ConfigError as e:
            return json.dumps({"error": "server_misconfigured", "message": e.message})

    req = ApprovalRequest(
        id=gen_id(),
        action=action,
        risk=risk,
        summary=summary,
        created_at=datetime.now(timezone.utc),
        timeout_seconds=timeout_seconds,
        status="pending",
    )
    return _run_approval(req)


def _run_approval(req: ApprovalRequest) -> str:
    """Internal: send req to Telegram, block until resolved or timeout. No validation."""
    assert _store is not None and _client is not None and _config is not None
    event = _store.add(req)

    # Send the message + buttons to every allowed user (typically 1).
    sent_to: list[int] = []
    try:
        for uid in _config.allowed_user_ids:
            msg_id = _client.send_message(
                chat_id=uid,
                text=format_request(req),
                reply_markup=_inline_keyboard(req.id),
            )
            _store.mark_sent(req.id, msg_id)
            sent_to.append(uid)
    except TelegramError as e:
        return json.dumps({
            "error": "telegram_send_failed",
            "message": str(e),
        })

    # Block until resolved or timeout. Use monotonic clock for the deadline.
    deadline = time.monotonic() + req.timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Auto-deny on timeout
            _store.set_resolution(req.id, "deny", "timeout", responded_by=None)
            break
        if event.wait(timeout=remaining):
            break  # resolved by poller

    final = _store.get(req.id)
    if final is None or final.decision is None:
        # Should not happen — set_resolution always sets decision
        return json.dumps({"error": "internal", "message": "request vanished"})

    return _format_result(final)


def _format_result(req: ApprovalRequest) -> str:
    elapsed = 0
    if req.resolved_at is not None:
        elapsed = int((req.resolved_at - req.created_at).total_seconds())
    return json.dumps({
        "decision": req.decision,
        "reason": req.reason,
        "id": req.id,
        "responded_by": req.responded_by,
        "elapsed_seconds": elapsed,
        "auto": req.reason == "timeout",
    })


@mcp.tool()
def ping() -> str:
    """Health check: server status, poller state, pending count."""
    alive = _poller.is_alive() if _poller is not None else False
    return json.dumps({
        "status": "ok",
        "bot_username": "bew_approve_bot",  # static label for now
        "poller_alive": alive,
        "pending_count": len(_store.pending_ids()) if _store is not None else 0,
        "last_getupdate_at": _iso(_poller.last_call_at) if _poller and _poller.last_call_at else None,
        "last_error": _poller.last_error if _poller else None,
    }, indent=2)


def main() -> None:
    """Entry point: init globals, start poller, run MCP server."""
    try:
        _initialize()
        if _poller is not None:
            _poller.start()
    except ConfigError as e:
        # Don't crash — log to stderr and run anyway so ping() can report the issue.
        import sys
        print(f"[hermes-approve] config error: {e.message}", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/integration/test_request_approval.py
```
Expected: all PASS. The timeout test should complete in ~1s.

- [ ] **Step 5: Run full test suite**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest
```
Expected: all tests across unit/ and integration/ pass.

- [ ] **Step 6: ruff + mypy clean**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m ruff check scripts/hermes-approve-mcp.py tests/
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m mypy scripts/hermes-approve-mcp.py
```

- [ ] **Step 7: Commit**

```bash
cd C:/Users/Bew/ZCodeProject
git add scripts/hermes-approve-mcp.py tests/integration/test_request_approval.py
git commit -m "feat(phase-2): request_approval + ping MCP tools + main() entry (TDD)"
```

---

## Task 10: Race Condition Tests

**Files:**
- Create: `C:\Users\Bew\ZCodeProject\tests\integration\test_race_conditions.py`

**Interfaces:**
- Consumes: All public APIs from Tasks 6–9
- Produces: confidence that double-clicks, timeout-vs-click races, and concurrent pending requests behave per spec

No new implementation — these are characterization tests of existing behavior. If any fails, the bug is in `PendingStore.set_resolution` or `_run_approval`.

- [ ] **Step 1: Write the tests**

File: `tests/integration/test_race_conditions.py`

```python
"""Race-condition tests — double-click, timeout-vs-click, concurrent pending."""
import json
import threading
import time
from datetime import datetime, timezone

import httpx


def _wire(ham, tmp_path, batches_iter):
    cfg = ham.Config(
        bot_token="123:abc",
        allowed_user_ids={111},
        state_dir=tmp_path,
        bot_api_base="https://example.test",
    )
    store = ham.PendingStore(tmp_path)
    lock = threading.Lock()
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else {}
        if "sendMessage" in req.url.path:
            markup = body.get("reply_markup") or {}
            for row in markup.get("inline_keyboard", []):
                for btn in row:
                    if "callback_data" in btn:
                        captured.setdefault("ids", []).append(btn["callback_data"].split(":")[1])
            return httpx.Response(200, json={
                "ok": True, "result": {"message_id": 1, "date": 0, "chat": {"id": 111}},
            })
        if "getUpdates" in req.url.path:
            with lock:
                try:
                    batch = next(batches_iter)
                except StopIteration:
                    batch = []
            for upd in batch:
                cq = upd.get("callback_query")
                if cq and "data" in cq:
                    rid = captured["ids"][-1] if captured.get("ids") else "unknown"
                    cq["data"] = cq["data"].replace("DYNAMIC", rid)
            return httpx.Response(200, json={"ok": True, "result": batch})
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = ham.TelegramClient(token="123:abc", transport=httpx.MockTransport(handler))
    poller = ham.TelegramPoller(client=client, store=store, config=cfg)
    ham._config, ham._store, ham._client, ham._poller = cfg, store, client, poller
    return store, poller


def test_double_click_allow_then_deny(ham, tmp_path):
    """Two callback_queries in the same batch for the same id — first wins."""
    batches = iter([
        [
            {"update_id": 1, "callback_query": {
                "id": "cb1", "from": {"id": 111}, "data": "ap:DYNAMIC:allow",
                "message": {"message_id": 1, "chat": {"id": 111}},
            }},
            {"update_id": 2, "callback_query": {
                "id": "cb2", "from": {"id": 111}, "data": "ap:DYNAMIC:deny",
                "message": {"message_id": 1, "chat": {"id": 111}},
            }},
        ],
        [],
    ])
    store, poller = _wire(ham, tmp_path, batches)
    poller.start()
    try:
        result = json.loads(ham.request_approval(
            action="x", risk="low", summary="x" * 20, timeout_seconds=10,
        ))
    finally:
        poller.stop()
        poller.join(timeout=5.0)

    # First-processed wins (Allow); the second is rejected as Already resolved.
    assert result["decision"] == "allow"


def test_concurrent_pending_requests_independent(ham, tmp_path):
    """5 simultaneous request_approval calls each get their own resolution."""
    # We won't simulate clicks for all 5; instead just verify they all pend
    # independently and timeout auto-deny works for each.
    batches = iter([])
    store, poller = _wire(ham, tmp_path, batches)
    poller.start()
    try:
        results = [None] * 5

        def call(i):
            req = ham.ApprovalRequest(
                id=ham.gen_id(),
                action=f"action-{i}",
                risk="low",
                summary=f"some summary longer than twenty chars {i}",
                created_at=datetime.now(timezone.utc),
                timeout_seconds=1,
                status="pending",
            )
            results[i] = json.loads(ham._run_approval(req))

        threads = [threading.Thread(target=call, args=(i,)) for i in range(5)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10.0)
    finally:
        poller.stop()
        poller.join(timeout=5.0)

    assert len(results) == 5
    assert all(r["decision"] == "deny" and r["reason"] == "timeout" for r in results)
    ids = {r["id"] for r in results}
    assert len(ids) == 5  # all distinct


def test_timeout_vs_click_race_lock_wins(ham, tmp_path):
    """If click arrives microseconds before timeout, exactly one wins — no double-resolve."""
    batches = iter([
        [{"update_id": 1, "callback_query": {
            "id": "cb1", "from": {"id": 111}, "data": "ap:DYNAMIC:allow",
            "message": {"message_id": 1, "chat": {"id": 111}},
        }}],
        [],
    ])
    store, poller = _wire(ham, tmp_path, batches)
    poller.start()
    try:
        req = ham.ApprovalRequest(
            id=ham.gen_id(),
            action="x", risk="low", summary="x" * 20,
            created_at=datetime.now(timezone.utc),
            timeout_seconds=1,
            status="pending",
        )
        result = json.loads(ham._run_approval(req))
    finally:
        poller.stop()
        poller.join(timeout=5.0)

    final = store.get(req.id)
    # Whatever won, the audit log should have exactly one resolved event.
    audit_lines = (tmp_path / "pending.jsonl").read_text().strip().splitlines()
    resolved = [json.loads(l) for l in audit_lines if json.loads(l)["event"] == "resolved"]
    assert len(resolved) == 1
```

- [ ] **Step 2: Run tests**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest tests/integration/test_race_conditions.py -v
```
Expected: all PASS.

- [ ] **Step 3: Run full test suite one more time**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest
```

- [ ] **Step 4: ruff + mypy clean**

```bash
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m ruff check scripts/hermes-approve-mcp.py tests/
"C:/Users/Bew/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m mypy scripts/hermes-approve-mcp.py
```

- [ ] **Step 5: Commit**

```bash
cd C:/Users/Bew/ZCodeProject
git add tests/integration/test_race_conditions.py
git commit -m "test(phase-2): race conditions — double-click, concurrent, timeout-vs-click"
```

---

## Task 11: `AGENTS.md` + `.secrets/approve-bot.env` Template

**Files:**
- Create: `C:\Users\Bew\ZCodeProject\AGENTS.md`
- Create: `C:\Users\Bew\ZCodeProject\.secrets\approve-bot.env.example` (note `.example` suffix — the real `.env` is gitignored)

**Interfaces:** none (documentation + secret scaffold)

- [ ] **Step 1: Write `AGENTS.md`**

File: `AGENTS.md`

```markdown
# AGENTS.md — guidance for AI coding agents in this repo

This file gives ZCode (and other AI coding agents) the rules of engagement.

## When to request approval via hermes-approve

The `hermes-approve` MCP server exposes a `request_approval` tool. Calling it
sends a message to a dedicated Telegram bot that the human carries in their
pocket, with [✅ Allow] / [❌ Deny] buttons. The call blocks until the human
responds or 15 minutes elapse (auto-deny on timeout).

**Always call `request_approval()` BEFORE doing any of the following:**

- `git push --force` / `git rebase` that rewrites history already pushed to a remote
- `rm -rf` or recursive delete outside `C:/Users/Bew/ZCodeProject`
- Editing `.env` files, secret files, or credentials
- `docker compose restart | kill | down` against services currently in use
  (e.g. n8n, augment)
- Sending data to external services when the data was not previously public
  (publishing APIs, webhooks, posting to chat)

**You MAY request approval (use your judgment):**

- Deleting non-temp files (anything outside `tmp_test/`)
- Installing new top-level packages (npm/pip at repo root)
- Editing Hermes or ZCode config files

**Do NOT request approval for:**

- Normal code edits inside the repo
- Running tests, linters, type checkers
- Creating/editing files under `tmp_test/`
- Reading files (always safe)

When in doubt, request approval. The human would rather click "Deny" than
discover afterwards that you did something irreversible.

## How to call the tool

```python
# Python-ish pseudocode for any MCP-aware agent
result = request_approval(
    action="git push --force origin phase-1d-2/session-memory-hook",  # ≤ 200 chars
    risk="destructive",                                                # low|moderate|destructive
    summary="Overwrite remote because the previous push contained a secret.",  # ≥ 20 chars
    timeout_seconds=900,                                               # 60–1800, default 900
)
# result is JSON. Check result.decision: "allow" | "deny"
# If "deny" (including timeout auto-deny), DO NOT proceed — propose an alternative.
```

## Other repo conventions

- Branch naming: `<phase>-<n>/<short-slug>` (e.g. `phase-2/telegram-approve-mcp`)
- Commit style: `type(scope): summary` — see `git log --oneline` for examples
- Don't commit secrets. The `.secrets/` directory is gitignored.
- Don't commit runtime state. The `.approve/` directory is gitignored.
- Match surrounding code style: `ruff` and `mypy` configs are at repo root.
```

- [ ] **Step 2: Write `.secrets/approve-bot.env.example`**

Create the directory + file:

```bash
mkdir -p C:/Users/Bew/ZCodeProject/.secrets
```

File: `.secrets/approve-bot.env.example`

```
# Copy this file to .secrets/approve-bot.env and fill in real values.
# The .env file is gitignored.
#
# Get APPROVE_BOT_TOKEN from @BotFather (create a NEW bot, e.g. @bew_approve_bot).
# Get TELEGRAM_ALLOWED_USERS from your own Telegram user ID — message @userinfobot
# to learn yours. Comma-separate to allow multiple users.

APPROVE_BOT_TOKEN=1234567890:AAA...
TELEGRAM_ALLOWED_USERS=5967541638
```

- [ ] **Step 3: Verify `.gitignore` covers `.secrets/`**

```bash
cd C:/Users/Bew/ZCodeProject
git check-ignore -v .secrets/approve-bot.env
```
Expected: prints `.gitignore:2:.secrets/` or similar (i.e. the path IS ignored).

Also verify the `.example` is NOT ignored:

```bash
git check-ignore -v .secrets/approve-bot.env.example; echo "exit=$?"
```
Expected: exit code 1 (the .example file is tracked normally).

If the `.example` IS ignored (because `.secrets/` catches everything), refine `.gitignore`:

```gitignore
.secrets/*
!.secrets/*.example
```

- [ ] **Step 4: Commit**

```bash
cd C:/Users/Bew/ZCodeProject
git add AGENTS.md .secrets/approve-bot.env.example .gitignore
git commit -m "docs(phase-2): AGENTS.md approval guidance + .env.example template"
```

---

## Task 12: Wire into ZCode + Manual E2E

This is the **only task requiring real Telegram and human interaction**. The agent does config edits + restart guidance; the human creates the bot, runs the E2E scenarios, and updates `MEMORY.md`.

**Files:**
- Modify: `C:\Users\Bew\ZCodeProject\MEMORY.md` (mark Phase 2 done)
- Modify: `~/.zcode/cli/config.json` (add MCP server entry)

**Interfaces:** none

### Sub-step 12.1: Human creates the bot (manual, ~3 min)

- [ ] In Telegram, message [@BotFather](https://t.me/BotFather)
- [ ] Send `/newbot`
- [ ] Choose a name (e.g. `Bew Approve`)
- [ ] Choose a username ending in `bot` (e.g. `bew_approve_bot`)
- [ ] Copy the bot token (format `1234567890:AAA...`)
- [ ] Message [@userinfobot](https://t.me/userinfobot) to get your Telegram user ID
- [ ] Send any message to your new bot (this is required so it can DM you first)

### Sub-step 12.2: Agent creates the real env file

- [ ] **Create `.secrets/approve-bot.env` with the real values**

```bash
mkdir -p C:/Users/Bew/ZCodeProject/.secrets
# (Agent should prompt user for the two values and write the file via Write tool.)
```

File content (agent fills in from user input):
```
APPROVE_BOT_TOKEN=<user-provided>
TELEGRAM_ALLOWED_USERS=<user-provided>
```

- [ ] **Verify the file is gitignored**

```bash
cd C:/Users/Bew/ZCodeProject
git check-ignore .secrets/approve-bot.env && echo "OK: ignored"
```

### Sub-step 12.3: Agent registers MCP server in ZCode

- [ ] **Read current `~/.zcode/cli/config.json`**

```bash
cat ~/.zcode/cli/config.json
```

- [ ] **Add the `mcp.servers.hermes-approve` entry**

If the file already has `mcp.servers`, merge into it. If not, add the top-level `mcp` key. Use the canonical shape from the spec:

```json
{
  "mcp": {
    "servers": {
      "hermes-approve": {
        "type": "stdio",
        "command": "C:\\Users\\Bew\\AppData\\Local\\hermes\\hermes-agent\\venv\\Scripts\\python.exe",
        "args": ["C:\\Users\\Bew\\ZCodeProject\\scripts\\hermes-approve-mcp.py"],
        "env": {
          "APPROVE_BOT_ENV": "C:/Users/Bew/ZCodeProject/.secrets/approve-bot.env",
          "APPROVE_STATE_DIR": "C:/Users/Bew/ZCodeProject/.approve"
        },
        "enabled": true,
        "timeoutMs": 600000
      }
    }
  }
}
```

⚠️ **Path note**: ZCode config does not expand `${...}` for MCP servers (only plugins). Use absolute paths. Forward slashes in env values are fine on Windows for Python's `Path`.

⚠️ **`command` choice**: We use the hermes-agent venv's python.exe because it has `mcp` + `httpx` installed. The Phase 1D-2 `C:/HermesHooks/python.exe` is a *copy* of base Python without these deps.

- [ ] **Restart ZCode**

The user must restart ZCode for the new MCP server to load. After restart, verify the tool is visible.

### Sub-step 12.4: Verify with `ping()`

- [ ] In a new ZCode session, ask the agent: "Call the `ping` tool from hermes-approve and report what it returns."

Expected: tool returns JSON with `"status":"ok"`, `"poller_alive":true`. If `poller_alive` is `false`, the bot token may be wrong or Telegram unreachable — check `.approve/approve.log`.

### Sub-step 12.5: E2E scenario M1 (happy path)

- [ ] Tell the agent: "I want you to test the approval flow. Call `request_approval` with action='test scenario M1', risk='low', summary='E2E test of the approve bot — please click Allow.'"

Expected:
1. Agent calls the tool
2. Telegram bot sends you a message with [✅ Allow] [❌ Deny]
3. You tap Allow
4. Agent receives `{"decision":"allow",...}` and reports it

### Sub-step 12.6: E2E scenario M2 (deny path)

- [ ] Same command but with summary mentioning "scenario M2 — please click Deny"

Expected: you tap Deny, agent reports `{"decision":"deny","reason":"user_denied"}`.

### Sub-step 12.7: E2E scenario M3 (timeout)

- [ ] Same command with `timeout_seconds=60` and summary "scenario M3 — do NOT click"

Expected: after 60s, agent reports `{"decision":"deny","reason":"timeout","auto":true}`. Telegram message updates to "⌛ AUTO-DENIED".

### Sub-step 12.8: Update MEMORY.md

- [ ] **Mark Phase 2 done in `MEMORY.md`**

Add a new section to `MEMORY.md`:

```markdown
## 🎯 Phase 2 — Telegram Approve MCP Server (DONE 2026-07-21)

### ไฟล์ที่เกี่ยวข้อง
| ไฟล์ | หน้าที่ | สถานะ |
|---|---|---|
| `scripts/hermes-approve-mcp.py` | MCP server (FastMCP, stdio) | ✅ Done |
| `.secrets/approve-bot.env` | bot token + allowlist (gitignored) | ✅ Done |
| `.approve/pending.jsonl` | audit log | ✅ (written at runtime) |
| `.approve/state.json` | getUpdates cursor | ✅ (written at runtime) |
| `~/.zcode/cli/config.json` | MCP server registration | ✅ Done |
| `AGENTS.md` | tells agent when to call `request_approval` | ✅ Done |

### สถานะปัจจุบัน
- bot: @bew_approve_bot (token อยู่ใน `.secrets/approve-bot.env` เท่านั้น)
- poller_alive: true (verified via `ping()`)
- E2E M1/M2/M3: all passed
- tool `hermes-approve_request_approval` visible in ZCode

### บทเรียน
1. ...
2. ...

### ยังไม่ได้ทำ
- [ ] Phase 3: auto-block guardrail (PreToolUse hook) — optional
- [ ] รวม PR Phase 1D-2 + Phase 2 → main
- [ ] revoke credentials 8 ตัว (ค้างจาก Phase 1)
```

- [ ] **Commit**

```bash
cd C:/Users/Bew/ZCodeProject
git add MEMORY.md
git commit -m "docs(memory): Phase 2 complete — Telegram Approve MCP server live"
```

- [ ] **Push branch**

```bash
cd C:/Users/Bew/ZCodeProject
git push -u origin phase-2/telegram-approve-mcp
```

---

## Acceptance Criteria Recap

All of the following must be true at the end of Task 12:

- [ ] All unit + integration tests pass (`pytest` exits 0)
- [ ] `ruff check` clean
- [ ] `mypy` clean
- [ ] Branch `phase-2/telegram-approve-mcp` pushed to origin
- [ ] `.gitignore` covers `.secrets/` and `.approve/` (verified via `git check-ignore`)
- [ ] Bot token exists ONLY in `.secrets/approve-bot.env` — not in code, not in git history
- [ ] `AGENTS.md` exists with the "when to call request_approval" section
- [ ] ZCode config has `hermes-approve` server entry, `enabled: true`
- [ ] After ZCode restart, `ping()` tool returns `"status":"ok"`, `"poller_alive":true`
- [ ] E2E scenarios M1 (Allow), M2 (Deny), M3 (timeout) all passed manually
- [ ] `MEMORY.md` updated with Phase 2 close-out section

---

## Self-Review

### Spec coverage check

Walk each spec section; map to tasks:

| Spec section | Covered by |
|---|---|
| §1 Goal | Tasks 1–12 (whole plan) |
| §2 Requirements (5 locked decisions) | §Architecture + Tasks 9, 11, 12 |
| §3.1 File layout | Task 1 (scaffold) + each subsequent task |
| §3.2 ZCode MCP registration | Task 12.3 |
| §3.3 Components (5) | Tasks 5 (Config), 6 (Store), 7 (Client), 8 (Poller), 9 (tools) |
| §3.4 Threading model | Tasks 8 (poller), 9 (`_run_approval` blocks on event) |
| §4.1–4.3 Sequences + state machine | Task 9 + race tests in Task 10 |
| §4.4–4.5 getUpdates cursor + stale handling | Task 8 (incl. `test_poller_drops_stale_callback_unknown_id`, `test_poller_persists_offset_across_restart`) |
| §4.6 Tool input/output contract | Task 9 (`_format_result`, validation branch) |
| §4.7 Audit log | Task 6 (`_append_audit` in PendingStore) |
| §5.1 callback_data ≤ 64 bytes | Task 2 (`_CALLBACK_RE` allows 6–12 hex; `ap:12hex:allow` = 21 bytes max) |
| §5.2 Inline keyboard | Task 9 (`_inline_keyboard`) |
| §5.3 Risk visual mapping | Task 4 (`RISK_STYLES`) |
| §5.4 Message format (4 states) | Tasks 4 + 8 (format_request pending; format_resolved used by poller) |
| §5.5 answerCallbackQuery toasts | Task 8 (`_safe_answer`) |
| §5.6 Routing & authorization | Task 8 (`_handle_update`) |
| §5.7 Race conditions | Task 10 |
| §6 Error handling (6 categories + 10 edge cases) | Tasks 7 (Telegram retry), 9 (validation/misconfig), 8 (stale/expired/unauthorized) |
| §6.5 Logging 3 channels | Defer to runtime — see Open Items |
| §6.6 Health-check tool | Task 9 (`ping`) |
| §7 AGENTS.md content | Task 11 |
| §8 Testing strategy | Tasks 2–10 |
| §9 Rollout (3 sub-phases) | Tasks 1–10 (2.1+2.2), 11 (bridge), 12 (2.3) |
| §9.2 Rollback | Covered in `AGENTS.md` + gitignore; no code needed |
| §9.3 Acceptance criteria | Recap at end of Task 12 |

### Placeholder scan

Searched the plan for: "TBD", "TODO", "FIXME", "implement later", "add appropriate", "handle edge cases" (without specifics). Found:

- `MEMORY.md` task has `### บทเรียน 1. ... 2. ...` placeholder lines — these are intentional templates for the human to fill in during E2E; not plan failures.
- `AGENTS.md` mentions "see git log --oneline for examples" — that's a runtime pointer, not a placeholder.

No other placeholders found. All code steps contain complete code; all command steps contain exact commands.

### Type consistency

Walked through cross-task references:

- `gen_id() -> str` — Task 2 produces, Task 6 + Task 9 consume ✓
- `parse_callback(data) -> Optional[tuple[str, str]]` — Task 2 produces, Task 8 consumes ✓
- `validate(...) -> Optional[ValidationError]` with `.field`, `.message` — Task 3 produces, Task 9 consumes ✓
- `ApprovalRequest` fields: `id, action, risk, summary, created_at, timeout_seconds, status, decision, reason, responded_by, resolved_at` — Task 4 defines, Tasks 6/8/9/10 consume. Field names match across all tasks ✓
- `Config.bot_token, .allowed_user_ids (set[int]), .state_dir (Path), .bot_api_base (str)` — Task 5 defines, Tasks 7/8/9 consume ✓
- `PendingStore.add() -> threading.Event` — Task 6 defines, Task 9 (`_run_approval`) consumes ✓
- `PendingStore.set_resolution(req_id, decision, reason, responded_by=None) -> bool` — Task 6 defines, Tasks 8/9 consume ✓
- `TelegramClient.send_message(...) -> int`, `.edit_message_text(...)`, `.answer_callback_query(...)`, `.get_updates(...) -> list[dict]` — Task 7 defines, Tasks 8/9 consume ✓
- `TelegramPoller(client, store, config)` constructor — Task 8 defines, Task 9 consumes ✓
- `_run_approval(req: ApprovalRequest) -> str` — Task 9 defines private, Tasks 9/10 (test) consume ✓

No mismatches found.

### Open Items (deferred — not plan blockers)

1. **File logging to `.approve/approve.log`**: spec §6.5 mentions 3-channel logging. The implementation uses MCP's logging API + stderr only. File logging is a Phase 3 enhancement; the audit log (`pending.jsonl`) already provides forensic trail.
2. **Log rotation at 1MB**: same — deferred.
3. **Decision toast copy verification**: `format_resolved` is unit-tested, but the exact emoji strings ("✅ ALLOWED") in production Telegram rendering are only verifiable via E2E — covered by M1/M2/M3.
4. **Telegram 429 `retry_after` field**: implemented in `TelegramClient._call`, but no explicit unit test for it (the spec mentioned it; covered indirectly by retry tests).

These are all "nice to have" polish items; none block the definition of done.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-21-telegram-approve-mcp.md`.

Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task (Tasks 1–11), review between tasks, fast iteration. Best for keeping context clean across the 11 implementation tasks.
2. **Inline Execution** — Execute tasks in this session using `executing-plans`, batch execution with checkpoints. Better if you want to watch each step live.

Task 12 (E2E with real bot) always requires your direct involvement regardless of execution mode.

**Which approach?**
