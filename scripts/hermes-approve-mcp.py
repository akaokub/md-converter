#!/usr/bin/env python3
"""hermes-approve MCP server.

Stdio MCP server that exposes a `request_approval` tool. When called, the tool
sends an inline-button message to a dedicated Telegram bot, then blocks until
the user taps Allow / Deny or the request times out (auto-deny).

Design spec: docs/superpowers/specs/2026-07-21-telegram-approve-mcp-design.md
"""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

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


# Default paths/base URLs. STATE_DIR is repo-rooted: scripts/ -> parent = repo.
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

    def __init__(self, message: str) -> None:
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


def _iso(dt: datetime, add_seconds: int = 0) -> str:
    """ISO 8601 UTC string. ``dt`` may be tz-aware or naive (assumed UTC).

    If ``add_seconds`` is given, the result is shifted forward by that amount
    (used to compute ``expires_at`` from ``created_at`` + ``timeout_seconds``).
    """
    if add_seconds:
        dt = dt + timedelta(seconds=add_seconds)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _iso_now() -> str:
    """Current UTC time as an ISO 8601 string."""
    return _iso(datetime.now(UTC))


class PendingStore:
    """Thread-safe store of pending approval requests + audit log writer.

    Backs the poll-loop's view of which approvals are still open. Resolution
    is atomic across threads: exactly one caller wins per request id.
    """

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._pending_file = state_dir / "pending.jsonl"
        self._lock = threading.Lock()
        self._requests: dict[str, ApprovalRequest] = {}
        self._events: dict[str, threading.Event] = {}
        self._message_ids: dict[str, int] = {}

    def add(self, req: ApprovalRequest) -> threading.Event:
        """Register a new pending request.

        Returns the :class:`threading.Event` that will be set when the request
        is resolved. Raises ``ValueError`` on a duplicate request id.
        """
        with self._lock:
            if req.id in self._requests:
                raise ValueError(f"duplicate request id: {req.id}")
            event = threading.Event()
            self._requests[req.id] = req
            self._events[req.id] = event
        self._append_audit(
            {
                "event": "created",
                "id": req.id,
                "action": req.action,
                "risk": req.risk,
                "summary": req.summary,
                "created_at": _iso(req.created_at),
                "expires_at": _iso(req.created_at, req.timeout_seconds),
            }
        )
        return event

    def get(self, req_id: str) -> ApprovalRequest | None:
        with self._lock:
            return self._requests.get(req_id)

    def is_pending(self, req_id: str) -> bool:
        with self._lock:
            return (
                req_id in self._requests
                and self._requests[req_id].status == "pending"
            )

    def pending_ids(self) -> list[str]:
        with self._lock:
            return [rid for rid, r in self._requests.items() if r.status == "pending"]

    def mark_sent(self, req_id: str, telegram_message_id: int) -> None:
        """Record the Telegram message id for a sent request + write ``sent`` audit."""
        with self._lock:
            self._message_ids[req_id] = telegram_message_id
        self._append_audit(
            {
                "event": "sent",
                "id": req_id,
                "telegram_message_id": telegram_message_id,
                "sent_at": _iso_now(),
            }
        )

    def set_resolution(
        self,
        req_id: str,
        decision: str,
        reason: str,
        responded_by: int | None = None,
    ) -> bool:
        """Resolve a pending request.

        Returns ``True`` if this caller won the race (i.e. the request existed
        and was still pending), ``False`` otherwise. The check-and-mutate is
        lock-guarded so concurrent callers on the same id produce exactly one
        winner. The Event is set *after* the lock is released so we don't hold
        the lock while waking a blocked thread.
        """
        with self._lock:
            req = self._requests.get(req_id)
            if req is None or req.status == "resolved":
                return False
            req.status = "resolved"
            req.decision = decision
            req.reason = reason
            req.responded_by = responded_by
            req.resolved_at = datetime.now(UTC)
            event = self._events.get(req_id)
        if event is not None:
            event.set()
        self._append_audit(
            {
                "event": "resolved",
                "id": req_id,
                "decision": decision,
                "reason": reason,
                "responded_by": responded_by,
                "resolved_at": _iso_now(),
            }
        )
        return True

    def _append_audit(self, entry: dict[str, Any]) -> None:
        """Append one JSON-line audit entry to ``pending.jsonl`` (lock-guarded).

        File append is not atomic on Windows, so we serialize writes under the
        same lock used for the in-memory state.
        """
        with self._lock:
            with self._pending_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class TelegramError(Exception):
    """Raised when a Telegram API call fails permanently."""

    def __init__(self, method: str, detail: str, status: int | None = None) -> None:
        super().__init__(f"{method} failed (status={status}): {detail}")
        self.method = method
        self.detail = detail
        self.status = status


# Status codes we never retry. 400 = bad request, 401 = unauthorized,
# 403 = forbidden — retrying won't fix the cause.
_FATAL_STATUSES = frozenset({400, 401, 403})


class TelegramClient:
    """Thin httpx wrapper for the small subset of Bot API methods we use.

    All public methods retry once on transient errors (network failures,
    5xx, 429). Status codes in :data:`_FATAL_STATUSES` raise immediately
    with no retry.
    """

    def __init__(
        self,
        token: str,
        bot_api_base: str = DEFAULT_BOT_API_BASE,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._base = f"{bot_api_base.rstrip('/')}/bot{token}"
        self._client = httpx.Client(transport=transport, timeout=timeout)

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        """Call a Bot API method with 1 retry on transient errors.

        Raises :class:`TelegramError` if the call ultimately fails.
        """
        url = f"{self._base}/{method}"
        last_exc: TelegramError | None = None
        for attempt in range(2):  # initial + 1 retry
            try:
                r = self._client.post(url, json=payload)
            except httpx.RequestError as e:
                last_exc = TelegramError(method, f"network: {e}")
                if attempt == 0:
                    time.sleep(1)
                    continue
                raise last_exc from e

            if r.status_code == 200:
                body = r.json()
                if body.get("ok"):
                    return body["result"]
                # Telegram-style error: HTTP 200 with ok=False.
                last_exc = TelegramError(
                    method, body.get("description", "unknown")
                )
                if attempt == 0:
                    if "retry_after" in body:
                        time.sleep(float(body["retry_after"]))
                    else:
                        time.sleep(1)
                    continue
                raise last_exc

            if r.status_code in _FATAL_STATUSES:
                # Non-retryable: raise immediately.
                raise TelegramError(
                    method, f"HTTP {r.status_code}: {r.text}", r.status_code
                )

            # 5xx / 429 → retry once.
            last_exc = TelegramError(
                method, f"HTTP {r.status_code}: {r.text}", r.status_code
            )
            if attempt == 0:
                time.sleep(1)
                continue
            raise last_exc
        # Should be unreachable: loop exits only via return or raise.
        raise last_exc if last_exc else TelegramError(method, "unreachable")

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> int:
        """Send an HTML-formatted message. Returns the new ``message_id``."""
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
        reply_markup: dict | None = None,
    ) -> None:
        """Edit the text of an existing message (HTML)."""
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
        """Acknowledge a callback query (the loading spinner disappears)."""
        self._call(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_query_id,
                "text": text,
                "show_alert": show_alert,
            },
        )

    def get_updates(self, offset: int = 0, timeout: int = 30) -> list[dict]:
        """Long-poll for updates. Returns ``[]`` if no updates."""
        result = self._call("getUpdates", {"offset": offset, "timeout": timeout})
        return list(result) if result else []


class TelegramPoller(threading.Thread):
    """Daemon thread: long-polls getUpdates and dispatches callback_query events.

    On each iteration, calls ``get_updates(offset=last+1, timeout=30)``. For
    each update that contains a ``callback_query``, the dispatcher performs the
    allow/deny resolution flow:

    1. Authorization: ``from.id`` must be in ``config.allowed_user_ids``.
    2. Format check: ``parse_callback(data)`` must succeed.
    3. Liveness: ``store.is_pending(req_id)`` must be True.
    4. Atomic resolve via ``store.set_resolution(...)``.
    5. ``answerCallbackQuery`` toast + ``editMessageText`` to remove buttons.

    Offset persistence: after each non-empty batch, writes ``state_dir /
    state.json`` with ``{"last_update_id": N, "last_heartbeat_at": iso}``.
    On boot, reads this file (defaulting to 0 on any error) so the first poll
    is ``offset = last+1``.
    """

    def __init__(
        self,
        client: TelegramClient,
        store: PendingStore,
        config: Config,
    ) -> None:
        super().__init__(daemon=True, name="telegram-poller")
        self._client = client
        self._store = store
        self._config = config
        self._stop_flag = threading.Event()
        self._state_file = config.state_dir / "state.json"
        self._last_update_id = self._load_last_update_id()
        self.last_call_at: datetime | None = None
        self.last_error: str | None = None

    def _load_last_update_id(self) -> int:
        """Return persisted offset from state.json, or 0 on any error."""
        if not self._state_file.exists():
            return 0
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            return int(data.get("last_update_id", 0))
        except (json.JSONDecodeError, ValueError, OSError):
            return 0

    def _save_state(self, last_update_id: int) -> None:
        """Atomically write state.json (tmp + replace)."""
        tmp = self._state_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "last_update_id": last_update_id,
                    "last_heartbeat_at": _iso_now(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(self._state_file)

    def stop(self) -> None:
        """Request shutdown. Returns immediately; the loop exits on next tick."""
        self._stop_flag.set()

    def run(self) -> None:
        """Main loop: poll → dispatch → persist. Catches all exceptions."""
        while not self._stop_flag.is_set():
            try:
                self.last_call_at = datetime.now(UTC)
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
                # Don't tight-loop on persistent errors; interruptible by stop().
                self._stop_flag.wait(5.0)

    def _handle_update(self, update: dict) -> None:
        """Dispatch one update. Authorization happens before id lookup so an
        unauthorized user can't probe for live ids (no "Expired" toast)."""
        cq = update.get("callback_query")
        if not cq:
            return  # ignore non-callback updates (text messages, etc.)
        parsed = parse_callback(cq.get("data", ""))
        cq_id = cq.get("id", "")
        from_id = (cq.get("from") or {}).get("id")
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        message_id = msg.get("message_id")

        # Authorization FIRST: unauthorized users get a toast but no resolution.
        if from_id not in self._config.allowed_user_ids:
            self._safe_answer(cq_id, "⛔ Unauthorized")
            return

        if parsed is None:
            return  # not our callback format; ignore silently

        req_id, decision = parsed
        if not self._store.is_pending(req_id):
            self._safe_answer(cq_id, "⌛ Expired")
            return

        reason = "user_allowed" if decision == "allow" else "user_denied"
        ok = self._store.set_resolution(
            req_id, decision, reason, responded_by=from_id
        )
        if not ok:
            self._safe_answer(cq_id, "⚠️ Already resolved")
            return

        self._safe_answer(
            cq_id, "✅ Approved" if decision == "allow" else "❌ Denied"
        )
        if chat_id is not None and message_id is not None:
            req = self._store.get(req_id)
            if req is not None:
                try:
                    self._client.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=format_resolved(req),
                        reply_markup=None,  # remove inline buttons
                    )
                except TelegramError:
                    pass  # user may have deleted the message; not fatal

    def _safe_answer(self, cq_id: str, text: str) -> None:
        """answerCallbackQuery, swallowing TelegramError (best-effort ack)."""
        try:
            self._client.answer_callback_query(cq_id, text)
        except TelegramError:
            pass


# ---------------------------------------------------------------------------
# MCP server wiring
#
# FastMCP() itself doesn't open sockets or start threads at construction time,
# so instantiating it at import is safe. The poller, by contrast, is started
# only inside main() — and is also created lazily by _initialize() so test
# code that monkeypatches the module-level globals doesn't trigger env reads.
# ---------------------------------------------------------------------------
mcp = FastMCP("hermes-approve")

# Module-level singletons, populated by _initialize() (or monkeypatched by tests).
_config: Config | None = None
_store: PendingStore | None = None
_client: TelegramClient | None = None
_poller: TelegramPoller | None = None


def _initialize() -> None:
    """Populate the module-level config/store/client/poller.

    Idempotent — if ``_config`` is already set, this is a no-op. Reads env
    via :func:`load_config`; raises :class:`ConfigError` on missing/invalid env.
    """
    global _config, _store, _client, _poller
    if _config is not None:
        return
    _config = load_config()
    _store = PendingStore(_config.state_dir)
    _client = TelegramClient(
        token=_config.bot_token, bot_api_base=_config.bot_api_base
    )
    _poller = TelegramPoller(client=_client, store=_store, config=_config)


def _inline_keyboard(req_id: str) -> dict:
    """Build the 1×2 inline keyboard (Allow / Deny) for a pending request."""
    return {
        "inline_keyboard": [[
            {"text": "✅ Allow", "callback_data": f"ap:{req_id}:allow"},
            {"text": "❌ Deny", "callback_data": f"ap:{req_id}:deny"},
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

    Returns JSON with a ``decision`` field (``allow`` / ``deny``) plus audit
    metadata. Always returns JSON — never raises. On a validation failure
    returns ``{"error": "validation_failed", "field": ..., "message": ...}``;
    on a missing-config failure returns ``{"error": "server_misconfigured", ...}``.
    """
    err = validate(action, risk, summary, timeout_seconds)
    if err is not None:
        return json.dumps(
            {
                "error": "validation_failed",
                "field": err.field,
                "message": err.message,
            }
        )

    if _config is None or _store is None or _client is None:
        try:
            _initialize()
        except ConfigError as e:
            return json.dumps(
                {"error": "server_misconfigured", "message": e.message}
            )

    req = ApprovalRequest(
        id=gen_id(),
        action=action,
        risk=risk,
        summary=summary,
        created_at=datetime.now(UTC),
        timeout_seconds=timeout_seconds,
        status="pending",
    )
    return _run_approval(req)


def _run_approval(req: ApprovalRequest) -> str:
    """Send ``req`` to Telegram, block until resolved or timeout, return JSON.

    Private layer that skips input validation (the public ``request_approval``
    already did that). Tests use this directly to inject ``timeout_seconds``
    values below the public 60-second floor.
    """
    assert _store is not None and _client is not None and _config is not None
    event = _store.add(req)

    # Send the message + buttons to every allowed user (typically just one).
    try:
        for uid in _config.allowed_user_ids:
            msg_id = _client.send_message(
                chat_id=uid,
                text=format_request(req),
                reply_markup=_inline_keyboard(req.id),
            )
            _store.mark_sent(req.id, msg_id)
    except TelegramError as e:
        return json.dumps(
            {"error": "telegram_send_failed", "message": str(e)}
        )

    # Block until resolved or timeout. Use monotonic clock for the deadline.
    deadline = time.monotonic() + req.timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Auto-deny on timeout — exactly one caller wins via set_resolution.
            _store.set_resolution(req.id, "deny", "timeout", responded_by=None)
            break
        if event.wait(timeout=remaining):
            break  # resolved by poller

    final = _store.get(req.id)
    if final is None or final.decision is None:
        # Should not happen — set_resolution always sets decision.
        return json.dumps({"error": "internal", "message": "request vanished"})

    return _format_result(final)


def _format_result(req: ApprovalRequest) -> str:
    """Render the final-state JSON returned to the agent."""
    elapsed = 0
    if req.resolved_at is not None:
        elapsed = int((req.resolved_at - req.created_at).total_seconds())
    return json.dumps(
        {
            "decision": req.decision,
            "reason": req.reason,
            "id": req.id,
            "responded_by": req.responded_by,
            "elapsed_seconds": elapsed,
            "auto": req.reason == "timeout",
        }
    )


@mcp.tool()
def ping() -> str:
    """Health check: server status, poller state, pending count."""
    alive = _poller.is_alive() if _poller is not None else False
    return json.dumps(
        {
            "status": "ok",
            "bot_username": "bew_approve_bot",  # static label for now
            "poller_alive": alive,
            "pending_count": len(_store.pending_ids()) if _store is not None else 0,
            "last_getupdate_at": (
                _iso(_poller.last_call_at)
                if _poller is not None and _poller.last_call_at
                else None
            ),
            "last_error": _poller.last_error if _poller is not None else None,
        },
        indent=2,
    )


def main() -> None:
    """Entry point: init globals, start poller, run MCP server.

    A ``ConfigError`` from ``_initialize()`` is logged to stderr but does not
    crash — the server still runs so an operator can call ``ping()`` to see
    what's wrong.
    """
    import sys

    try:
        _initialize()
        if _poller is not None:
            _poller.start()
    except ConfigError as e:
        print(f"[hermes-approve] config error: {e.message}", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
