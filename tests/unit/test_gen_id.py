"""Tests for gen_id — short hex ID generator."""
import re

HEX_RE = re.compile(r"^[0-9a-f]+$")


def test_gen_id_length(ham):
    assert len(ham.gen_id()) == 8


def test_gen_id_is_hex(ham):
    assert HEX_RE.match(ham.gen_id())


def test_gen_id_uniqueness(ham):
    ids = {ham.gen_id() for _ in range(10_000)}
    assert len(ids) == 10_000  # extremely unlikely to collide at 8 hex chars
