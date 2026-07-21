"""Tests for format_request / format_resolved — HTML-escaped Telegram messages."""
from datetime import UTC, datetime


def _make_req(ham, **kw):
    """Build a minimal ApprovalRequest with sensible defaults."""
    base = dict(
        id="abc12345",
        action="git push",
        risk="destructive",
        summary="Force-push 5 new commits to overwrite yesterday's history.",
        created_at=datetime(2026, 7, 21, 12, 42, 0, tzinfo=UTC),
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
        resolved_at=datetime(2026, 7, 21, 12, 44, 12, tzinfo=UTC),
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
        resolved_at=datetime(2026, 7, 21, 12, 44, 0, tzinfo=UTC),
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
        resolved_at=datetime(2026, 7, 21, 12, 57, 0, tzinfo=UTC),
        timeout_seconds=900,
    )
    out = ham.format_resolved(req)
    assert "⌛ AUTO-DENIED" in out
    assert "15:00" in out
