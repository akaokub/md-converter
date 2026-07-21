"""Tests for TelegramClient — httpx wrapper, mocked transport."""
import json

import httpx
import pytest


def _client_with_handler(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, timeout=10)


def test_send_message_returns_message_id(ham):
    def handler(req: httpx.Request) -> httpx.Response:
        assert "sendMessage" in req.url.path
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 42, "date": 0, "chat": {"id": 1}},
            },
        )

    client = ham.TelegramClient(
        token="123:abc", transport=httpx.MockTransport(handler)
    )
    msg_id = client.send_message(chat_id=1, text="hello")
    assert msg_id == 42


def test_send_message_includes_reply_markup(ham):
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 1, "date": 0, "chat": {"id": 1}},
            },
        )

    client = ham.TelegramClient(
        token="123:abc", transport=httpx.MockTransport(handler)
    )
    markup = {"inline_keyboard": [[{"text": "Allow", "callback_data": "ap:1:allow"}]]}
    client.send_message(chat_id=1, text="hi", reply_markup=markup)
    assert captured["body"]["reply_markup"] == markup
    assert captured["body"]["parse_mode"] == "HTML"


def test_send_message_400_raises_no_retry(ham):
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"ok": False, "description": "chat not found"})

    client = ham.TelegramClient(
        token="123:abc", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ham.TelegramError):
        client.send_message(chat_id=1, text="hi")
    assert calls["n"] == 1  # 400 → no retry


def test_send_message_500_retries_once(ham):
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"ok": False, "description": "server error"})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 7, "date": 0, "chat": {"id": 1}},
            },
        )

    client = ham.TelegramClient(
        token="123:abc", transport=httpx.MockTransport(handler)
    )
    msg_id = client.send_message(chat_id=1, text="hi")
    assert calls["n"] == 2
    assert msg_id == 7


def test_send_message_500_twice_raises(ham):
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"ok": False, "description": "down"})

    client = ham.TelegramClient(
        token="123:abc", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ham.TelegramError):
        client.send_message(chat_id=1, text="hi")
    assert calls["n"] == 2  # initial + 1 retry


def test_get_updates_returns_result_list(ham):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 100,
                        "callback_query": {"id": "cb1", "data": "ap:1:allow"},
                    },
                ],
            },
        )

    client = ham.TelegramClient(
        token="123:abc", transport=httpx.MockTransport(handler)
    )
    updates = client.get_updates(offset=101, timeout=1)
    assert len(updates) == 1
    assert updates[0]["update_id"] == 100


def test_edit_message_text_succeeds(ham):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = ham.TelegramClient(
        token="123:abc", transport=httpx.MockTransport(handler)
    )
    client.edit_message_text(chat_id=1, message_id=42, text="resolved")
    assert calls[0]["message_id"] == 42
    assert calls[0]["text"] == "resolved"


def test_answer_callback_query_succeeds(ham):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["callback_query_id"] == "cb1"
        assert body["text"] == "✅ Approved"
        return httpx.Response(200, json={"ok": True, "result": True})

    client = ham.TelegramClient(
        token="123:abc", transport=httpx.MockTransport(handler)
    )
    client.answer_callback_query("cb1", "✅ Approved")
