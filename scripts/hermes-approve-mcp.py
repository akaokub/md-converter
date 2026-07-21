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
from dataclasses import dataclass

# ID generator: 8 hex chars (4 bytes). Plenty of entropy for single-user scale.
_CALLBACK_RE = re.compile(r"^ap:([0-9a-f]{6,12}):(allow|deny)$")

# Validation limits for request_approval inputs.
VALID_RISKS = frozenset({"low", "moderate", "destructive"})
ACTION_MAX = 200
SUMMARY_MIN = 20
SUMMARY_MAX = 1000
TIMEOUT_MIN = 60
TIMEOUT_MAX = 1800


def gen_id() -> str:
    """Generate a short hex ID for an approval request."""
    return secrets.token_hex(4)  # 8 hex chars


def parse_callback(data: str) -> tuple[str, str] | None:
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
) -> ValidationError | None:
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
