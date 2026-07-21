"""Tests for TelegramPoller — daemon thread, callback dispatch, state.json."""
import json
import time
from datetime import UTC, datetime

import httpx


def _make_req(ham, store, **kw):
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
    req = ham.ApprovalRequest(**base)
    store.add(req)
    return req


def _config(ham, tmp_path):
    return ham.Config(
        bot_token="123:abc",
        allowed_user_ids={111},
        state_dir=tmp_path,
        bot_api_base="https://example.test",
    )


def test_poller_resolves_allow(ham, tmp_path):
    """Poller sees a callback_query for a pending request and resolves it allow."""
    store = ham.PendingStore(tmp_path)
    cfg = _config(ham, tmp_path)
    _make_req(ham, store)
    event = store._events["abc12345"]  # access internal for test

    batches = iter([
        [{"update_id": 1, "callback_query": {
            "id": "cb1", "from": {"id": 111}, "data": "ap:abc12345:allow",
            "message": {"message_id": 42, "chat": {"id": 111}},
        }}],
        [],
    ])

    def handler(req):
        if "getUpdates" in req.url.path:
            try:
                return httpx.Response(200, json={"ok": True, "result": next(batches)})
            except StopIteration:
                return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = ham.TelegramClient(token="123:abc", transport=httpx.MockTransport(handler))

    poller = ham.TelegramPoller(client=client, store=store, config=cfg)
    poller.start()
    try:
        assert event.wait(timeout=3.0), "event should be set within 3s"
    finally:
        poller.stop()
        poller.join(timeout=5.0)

    req = store.get("abc12345")
    assert req.status == "resolved"
    assert req.decision == "allow"


def test_poller_ignores_unauthorized_user(ham, tmp_path):
    store = ham.PendingStore(tmp_path)
    cfg = _config(ham, tmp_path)  # allowlist = {111}
    _make_req(ham, store)
    event = store._events["abc12345"]

    batches = iter([
        [{"update_id": 1, "callback_query": {
            "id": "cb1", "from": {"id": 999}, "data": "ap:abc12345:allow",
            "message": {"message_id": 42, "chat": {"id": 111}},
        }}],
        [],
    ])

    def handler(req):
        if "getUpdates" in req.url.path:
            try:
                return httpx.Response(200, json={"ok": True, "result": next(batches)})
            except StopIteration:
                return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": True})

    client = ham.TelegramClient(token="123:abc", transport=httpx.MockTransport(handler))

    poller = ham.TelegramPoller(client=client, store=store, config=cfg)
    poller.start()
    try:
        # Event should NOT be set — unauthorized user
        assert not event.wait(timeout=1.0)
    finally:
        poller.stop()
        poller.join(timeout=5.0)
    assert store.is_pending("abc12345")  # still pending


def test_poller_drops_stale_callback_unknown_id(ham, tmp_path):
    """callback for an id not in store → ignored silently, offset still advances."""
    store = ham.PendingStore(tmp_path)
    cfg = _config(ham, tmp_path)

    batches = iter([
        [{"update_id": 1, "callback_query": {
            "id": "cb1", "from": {"id": 111}, "data": "ap:nonexistent:allow",
            "message": {"message_id": 42, "chat": {"id": 111}},
        }}],
        [],
    ])

    def handler(req):
        if "getUpdates" in req.url.path:
            try:
                return httpx.Response(200, json={"ok": True, "result": next(batches)})
            except StopIteration:
                return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": True})

    client = ham.TelegramClient(token="123:abc", transport=httpx.MockTransport(handler))

    poller = ham.TelegramPoller(client=client, store=store, config=cfg)
    poller.start()
    try:
        time.sleep(1.0)  # let one batch process
    finally:
        poller.stop()
        poller.join(timeout=5.0)

    state_file = tmp_path / "state.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert state["last_update_id"] == 1  # offset advanced even though we dropped


def test_poller_persists_offset_across_restart(ham, tmp_path):
    """state.json's last_update_id is used as initial offset on next start."""
    store = ham.PendingStore(tmp_path)
    cfg = _config(ham, tmp_path)
    # Write a stale state.json as if a previous run committed offset=99
    (tmp_path / "state.json").write_text(json.dumps({"last_update_id": 99}))

    captured_offsets = []

    def handler(req):
        if "getUpdates" in req.url.path:
            body = json.loads(req.content)
            captured_offsets.append(body.get("offset"))
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": True})

    client = ham.TelegramClient(token="123:abc", transport=httpx.MockTransport(handler))

    poller = ham.TelegramPoller(client=client, store=store, config=cfg)
    poller.start()
    try:
        time.sleep(0.5)
    finally:
        poller.stop()
        poller.join(timeout=5.0)
    assert captured_offsets[0] == 100  # last_update_id + 1
