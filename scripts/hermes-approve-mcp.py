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

# ID generator: 8 hex chars (4 bytes). Plenty of entropy for single-user scale.
_CALLBACK_RE = re.compile(r"^ap:([0-9a-f]{6,12}):(allow|deny)$")


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
