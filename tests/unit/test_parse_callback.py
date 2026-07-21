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
