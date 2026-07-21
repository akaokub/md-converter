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
