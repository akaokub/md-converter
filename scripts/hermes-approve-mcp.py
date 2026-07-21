#!/usr/bin/env python3
"""hermes-approve MCP server.

Stdio MCP server that exposes a `request_approval` tool. When called, the tool
sends an inline-button message to a dedicated Telegram bot, then blocks until
the user taps Allow / Deny or the request times out (auto-deny).

Design spec: docs/superpowers/specs/2026-07-21-telegram-approve-mcp-design.md
"""
from __future__ import annotations

import html
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

# Bangkok timezone (UTC+7) — all display timestamps are shown in this tz.
_BANGKOK_TZ = timezone(timedelta(hours=7))

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


class RiskStyle(NamedTuple):
    """Icon + label for a risk level."""

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
    decision: str | None = None  # "allow" | "deny" | None
    reason: str | None = None  # "user_allowed" | "user_denied" | "timeout"
    responded_by: int | None = None
    resolved_at: datetime | None = None


def _fmt_countdown(total_seconds: int) -> str:
    """Format a duration as MM:SS (e.g. 900 -> '15:00')."""
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def format_request(req: ApprovalRequest) -> str:
    """Render the pending-state message (HTML).

    All user-controlled fields (action, summary) are HTML-escaped.
    Timestamps are displayed in Bangkok (UTC+7) time.
    """
    style = RISK_STYLES[req.risk]
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
    """Format an elapsed duration as '<M>m <SS>s' (≥1min) or '<S>s' (<1min)."""
    seconds = int((end - start).total_seconds())
    minutes, secs = divmod(seconds, 60)
    if minutes >= 1:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_resolved(req: ApprovalRequest) -> str:
    """Render the resolved-state message (HTML). Buttons already removed by caller.

    Adds a one-line terminal-status suffix to the pending header:
    - allow              -> ✅ ALLOWED by {responded_by} at {HH:MM} (after {elapsed})
    - timeout (auto-deny)-> ⌛ AUTO-DENIED (timeout {countdown})
    - explicit user deny -> ❌ DENIED by {responded_by} at {HH:MM}
    """
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
        end = req.resolved_at or req.created_at
        elapsed = _fmt_elapsed(req.created_at, end)
        return base + (
            f"✅ ALLOWED by {req.responded_by} at {end.astimezone(_BANGKOK_TZ):%H:%M} "
            f"(after {elapsed})"
        )
    if req.decision == "deny" and req.reason == "timeout":
        countdown = _fmt_countdown(req.timeout_seconds)
        return base + f"⌛ AUTO-DENIED (timeout {countdown})"
    # Explicit user deny
    end = req.resolved_at or req.created_at
    return base + (
        f"❌ DENIED by {req.responded_by} at {end.astimezone(_BANGKOK_TZ):%H:%M}"
    )
