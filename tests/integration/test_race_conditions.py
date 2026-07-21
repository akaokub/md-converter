"""Race-condition tests — double-click, timeout-vs-click, concurrent pending.

Characterization tests of existing behavior. No production code is exercised
that isn't already covered elsewhere; these tests exist to lock in the
atomicity guarantees in ``PendingStore.set_resolution`` and the
timeout/click race handling in ``_run_approval``.

The ``_wire`` helper mirrors ``tests/integration/test_request_approval.py``'s
``_wire_module``: it injects a ``MockTransport``-backed ``TelegramClient``
and monkeypatches the module-level globals so ``request_approval`` /
``_run_approval`` see them. Critically, the ``getUpdates`` handler is
**sendMessage-gated** — it returns ``[]`` until at least one ``sendMessage``
has been observed, so the simulated user click cannot race ahead of the
request being registered in the store.
"""
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest

# Per-test safety net: race tests are inherently probabilistic; a hang almost
# always means the _wire handler is missing the sendMessage-gate fix.
pytestmark = pytest.mark.timeout(30)


def _wire(ham, tmp_path, batches_iter: Iterator[list[dict]]):
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
    it — leaving the request blocked forever (this is exactly the hang Task 9
    hit before applying the gate).
    """
    cfg = ham.Config(
        bot_token="123:abc",
        allowed_user_ids={111},
        state_dir=tmp_path,
        bot_api_base="https://example.test",
    )
    store = ham.PendingStore(tmp_path)

    lock = threading.Lock()
    captured_msg_ids: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else {}
        if "sendMessage" in req.url.path:
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
                # Gate: hold the callback batch back until sendMessage has been
                # seen. Until then every poll returns [] so the poller cannot
                # observe the callback before the request exists in the store.
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

    ham._config = cfg
    ham._store = store
    ham._client = client
    ham._poller = poller
    return store, poller


def test_double_click_allow_then_deny(ham, tmp_path):
    """Two callback_queries in the same batch for the same id — first wins.

    Both updates carry the same target id (rewritten from "DYNAMIC" by the
    ``_wire`` handler). The poller processes them sequentially within a single
    ``_handle_update`` call per query. The first resolution (Allow) succeeds;
    the second sees ``set_resolution`` return False and surfaces an
    "Already resolved" toast. The tool returns ``decision=allow``.
    """
    batches = iter([
        [
            {"update_id": 1, "callback_query": {
                "id": "cb1", "from": {"id": 111}, "data": "ap:DYNAMIC:allow",
                "message": {"message_id": 42, "chat": {"id": 111}},
            }},
            {"update_id": 2, "callback_query": {
                "id": "cb2", "from": {"id": 111}, "data": "ap:DYNAMIC:deny",
                "message": {"message_id": 42, "chat": {"id": 111}},
            }},
        ],
        [],
    ])
    _store, poller = _wire(ham, tmp_path, batches)
    poller.start()
    try:
        # timeout_seconds=60 is the public-API minimum; the poller resolves
        # the request long before that via the double-click batch.
        result_json = ham.request_approval(
            action="git push --force origin main",
            risk="destructive",
            summary="Overwrite remote history because a secret was pushed.",
            timeout_seconds=60,
        )
    finally:
        poller.stop()
        poller.join(timeout=5.0)

    result = json.loads(result_json)
    assert result["decision"] == "allow"
    assert result["reason"] == "user_allowed"


def test_concurrent_pending_requests_independent(ham, tmp_path):
    """5 simultaneous _run_approval calls each get their own resolution.

    No callbacks are delivered (the batch iterator is empty), so each request
    must auto-deny on its own 1s timeout. Each call uses ``gen_id()`` so the
    ids must all be distinct; the store rejects duplicate ids, so distinctness
    also proves no id-collision occurred under concurrency.
    """
    _store, poller = _wire(ham, tmp_path, iter([]))
    poller.start()
    results: list[dict | None] = [None] * 5
    try:
        def call(i: int) -> None:
            req = ham.ApprovalRequest(
                id=ham.gen_id(),
                action=f"action-{i}",
                risk="low",
                summary=f"some summary longer than twenty chars {i}",
                created_at=datetime.now(UTC),
                timeout_seconds=1,
                status="pending",
            )
            results[i] = json.loads(ham._run_approval(req))

        threads = [threading.Thread(target=call, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
    finally:
        poller.stop()
        poller.join(timeout=5.0)

    assert all(r is not None for r in results), f"a thread did not finish: {results}"
    typed = [r for r in results if r is not None]
    assert all(r["decision"] == "deny" for r in typed)
    assert all(r["reason"] == "timeout" for r in typed)
    ids = {r["id"] for r in typed}
    assert len(ids) == 5, f"expected 5 distinct ids, got {len(ids)}: {ids}"


def test_timeout_vs_click_race_lock_wins(ham, tmp_path):
    """If a click arrives around the timeout boundary, exactly one wins.

    Single request with ``timeout_seconds=1``. One allow callback is queued.
    Whether the timeout fires first or the callback lands first, the audit
    log must contain exactly one ``resolved`` event for the request id —
    proving ``set_resolution``'s lock-guarded check-and-mutate is atomic.
    """
    batches = iter([
        [{"update_id": 1, "callback_query": {
            "id": "cb1", "from": {"id": 111}, "data": "ap:DYNAMIC:allow",
            "message": {"message_id": 42, "chat": {"id": 111}},
        }}],
        [],
    ])
    store, poller = _wire(ham, tmp_path, batches)
    poller.start()
    try:
        req = ham.ApprovalRequest(
            id=ham.gen_id(),
            action="x",
            risk="low",
            summary="x" * 20,
            created_at=datetime.now(UTC),
            timeout_seconds=1,
            status="pending",
        )
        _result_json = ham._run_approval(req)
    finally:
        poller.stop()
        poller.join(timeout=5.0)

    # The request must exist in the store and be marked resolved.
    final = store.get(req.id)
    assert final is not None
    assert final.status == "resolved"

    # The audit log must contain exactly one resolved event for this id.
    audit_lines = (tmp_path / "pending.jsonl").read_text(encoding="utf-8").splitlines()
    resolved = [
        json.loads(line)
        for line in audit_lines
        if line.strip()
        and json.loads(line).get("event") == "resolved"
        and json.loads(line).get("id") == req.id
    ]
    assert len(resolved) == 1, (
        f"expected exactly 1 resolved event for {req.id}, got {len(resolved)}: "
        f"{resolved}"
    )
