"""End-to-end tests for request_approval tool — mocks Telegram, exercises store + poller."""
import json
import threading
from datetime import UTC, datetime

import httpx


def _wire_module(ham, tmp_path, batches_iter, allowed_uids=None):
    """Inject test doubles into the module's globals; return (store, poller).

    The handler captures the real request id from each ``sendMessage`` payload
    (parsing it out of the inline-keyboard ``callback_data``) and rewrites the
    "DYNAMIC" placeholder in subsequent ``getUpdates`` batches so the simulated
    user click targets the id actually sent in the message.

    Race-free ordering: ``getUpdates`` returns ``[]`` until at least one
    ``sendMessage`` has been observed. This guarantees the request is already
    in the store (the implementation calls ``store.add`` before ``send_message``)
    and that ``captured_msg_ids`` has the real id, so the rewritten callback
    lands in a poll cycle where ``store.is_pending(req_id)`` is True. Without
    this gate the poller's first ``getUpdates`` can race ahead of
    ``request_approval``, observe a callback with a bogus id, and silently drop
    it — leaving the request blocked forever.
    """
    if allowed_uids is None:
        allowed_uids = {111}
    cfg = ham.Config(
        bot_token="123:abc",
        allowed_user_ids=set(allowed_uids),
        state_dir=tmp_path,
        bot_api_base="https://example.test",
    )
    store = ham.PendingStore(tmp_path)

    lock = threading.Lock()
    captured_msg_ids: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else {}
        if "sendMessage" in req.url.path:
            # Capture the request id embedded in callback_data so the test's
            # simulated user clicks the right button.
            markup = body.get("reply_markup") or {}
            for row in markup.get("inline_keyboard", []):
                for btn in row:
                    if "callback_data" in btn:
                        captured_msg_ids["id"] = btn["callback_data"].split(":")[1]
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"message_id": 42, "date": 0, "chat": {"id": 111}},
                },
            )
        if "getUpdates" in req.url.path:
            with lock:
                # Hold the callback batch back until sendMessage has been seen.
                # Until then every poll returns [] so the poller cannot observe
                # the callback before the request exists in the store.
                if "id" not in captured_msg_ids:
                    batch: list[dict] = []
                else:
                    try:
                        batch = next(batches_iter)
                    except StopIteration:
                        batch = []
                # Rewrite the placeholder id to the real captured id.
                rid = captured_msg_ids.get("id", "unknown")
                for upd in batch:
                    cq = upd.get("callback_query")
                    if cq and "data" in cq:
                        cq["data"] = cq["data"].replace("DYNAMIC", rid)
            return httpx.Response(200, json={"ok": True, "result": batch})
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = ham.TelegramClient(token="123:abc", transport=httpx.MockTransport(handler))
    poller = ham.TelegramPoller(client=client, store=store, config=cfg)

    # Monkeypatch module-level globals so request_approval sees them.
    ham._config = cfg
    ham._store = store
    ham._client = client
    ham._poller = poller
    return store, poller


def test_request_approval_happy_allow(ham, tmp_path):
    """Agent calls tool → message sent → user clicks Allow → tool returns allow JSON."""
    batches = iter([
        [{"update_id": 1, "callback_query": {
            "id": "cb1", "from": {"id": 111}, "data": "ap:DYNAMIC:allow",
            "message": {"message_id": 42, "chat": {"id": 111}},
        }}],
        [],
    ])
    _store, poller = _wire_module(ham, tmp_path, batches)
    poller.start()
    try:
        result_json = ham.request_approval(
            action="git push --force origin main",
            risk="destructive",
            summary="Overwrite remote history because a secret was pushed.",
            timeout_seconds=120,
        )
    finally:
        poller.stop()
        poller.join(timeout=5.0)

    result = json.loads(result_json)
    assert result["decision"] == "allow"
    assert result["reason"] == "user_allowed"
    assert result["responded_by"] == 111
    assert "id" in result


def test_request_approval_validation_short_summary(ham, tmp_path):
    """Tool returns validation error JSON, never sends to Telegram."""
    _wire_module(ham, tmp_path, iter([]))
    result_json = ham.request_approval(
        action="x",
        risk="low",
        summary="too short",
        timeout_seconds=60,
    )
    result = json.loads(result_json)
    assert result["error"] == "validation_failed"
    assert result["field"] == "summary"


def test_request_approval_validation_invalid_risk(ham, tmp_path):
    _wire_module(ham, tmp_path, iter([]))
    result = json.loads(
        ham.request_approval(
            action="x",
            risk="critical",
            summary="x" * 20,
            timeout_seconds=60,
        )
    )
    assert result["error"] == "validation_failed"
    assert result["field"] == "risk"


def test_request_approval_timeout_auto_deny(ham, tmp_path):
    """No callback ever arrives → tool returns auto-deny after timeout.

    Goes through ``_run_approval`` directly with a 1s timeout so the test
    bypasses the public-API rule (``timeout_seconds ≥ 60``).
    """
    _store, poller = _wire_module(ham, tmp_path, iter([]))
    poller.start()
    try:
        req = ham.ApprovalRequest(
            id=ham.gen_id(),
            action="x",
            risk="low",
            summary="x" * 20,
            created_at=datetime.now(UTC),
            timeout_seconds=1,  # private layer ignores the [60,1800] rule
            status="pending",
        )
        result_json = ham._run_approval(req)
    finally:
        poller.stop()
        poller.join(timeout=5.0)

    result = json.loads(result_json)
    assert result["decision"] == "deny"
    assert result["reason"] == "timeout"
    assert result["auto"] is True
