"""Tests for load_config — env parsing."""

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


def test_load_config_message_attr(monkeypatch, tmp_path, ham):
    """ConfigError exposes a `.message` attribute."""
    monkeypatch.delenv("APPROVE_BOT_ENV", raising=False)
    monkeypatch.delenv("APPROVE_BOT_TOKEN", raising=False)
    with pytest.raises(ham.ConfigError) as exc:
        ham.load_config()
    assert exc.value.message == str(exc.value)
