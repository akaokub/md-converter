"""Tests for PendingStore — thread-safe pending requests + audit log."""
import json
import threading
from datetime import UTC, datetime


def _make_req(ham, **kw):
    base = dict(
        id="abc12345",
        action="x",
        risk="low",
        summary="x" * 20,
        created_at=datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC),
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
