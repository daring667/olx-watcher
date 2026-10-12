"""API Mini App: подпись Telegram, доступ, поиски, админка."""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

import miniapp
import olx_watcher

TOKEN = "123:TEST"
OWNER = "1"
CONFIG = {"feed": {"enabled": True, "trial_days": 3, "max_watches": 2, "price_kzt": 5000, "period_days": 30}}


def init_data(user_id: int, token: str = TOKEN, age: int = 0, **user) -> str:
    fields = {"auth_date": str(int(time.time()) - age),
              "user": json.dumps({"id": user_id, "first_name": "Тест", **user}, ensure_ascii=False)}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.fixture
def client(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(olx_watcher, "STATE_PATH", tmp_path / "app.sqlite3")
    monkeypatch.setattr(miniapp, "context", lambda: (CONFIG, TOKEN, {OWNER}))
    monkeypatch.setattr(olx_watcher, "telegram_request",
                        lambda token, method, **kw: sent.append((method, kw)) or {"result": {"message_id": 1}})
    client = miniapp.app.test_client()
    client.sent = sent

    def call(method, path, user_id=777, json_body=None, **kw):
        headers = {"X-Telegram-Init-Data": init_data(user_id, **kw)}
        return client.open("/api/" + path, method=method, headers=headers, json=json_body)

    client.call = call
    return client


def test_page_is_served_under_prefix(client):
    assert client.get("/olx/").status_code == 200
    assert client.get("/").status_code == 200


@pytest.mark.parametrize("header", ["", "auth_date=1&user=%7B%22id%22%3A1%7D&hash=deadbeef"])
def test_api_rejects_missing_or_forged_signature(client, header):
    response = client.get("/api/me", headers={"X-Telegram-Init-Data": header})
    assert response.status_code == 401


def test_signature_from_another_bot_is_rejected(client):
    response = client.get("/api/me", headers={"X-Telegram-Init-Data": init_data(OWNER, token="999:OTHER")})
    assert response.status_code == 401


def test_stale_session_is_rejected(client):
    response = client.get("/api/me", headers={"X-Telegram-Init-Data": init_data(777, age=3 * 86400)})
    assert response.status_code == 401


def test_me_registers_user_with_trial(client):
    data = client.call("GET", "me", username="vasya").get_json()
    assert data["access"]["active"] and data["user"]["plan"] == "trial"
    assert data["user"]["username"] == "vasya" and data["max_watches"] == 2
    assert "alma-ata" in data["cities"]


def test_add_search_from_builder_and_link(client):
    r = client.call("POST", "watches", json_body={"query": "iPhone 13", "city": "alma-ata", "price_to": "200000"})
    assert r.status_code == 200 and "iphone 13" in r.get_json()["watch"]["title"]
    r = client.call("POST", "watches", json_body={"url": "https://www.olx.kz/astana/q-ps5/"})
    assert r.status_code == 200
    r = client.call("POST", "watches", json_body={"url": "https://www.olx.kz/shymkent/q-ps5/"})
    assert r.status_code == 400 and "максимум" in r.get_json()["error"]
    r = client.call("POST", "watches", json_body={"url": "https://www.olx.kz/d/obyavlenie/x-IDabc.html"})
    assert r.status_code == 400


def test_pause_and_delete_only_own_search(client):
    watch_id = client.call("POST", "watches", json_body={"query": "ps5", "city": "list"}).get_json()["watch"]["id"]
    assert client.call("PATCH", f"watches/{watch_id}", user_id=888, json_body={"paused": True}).status_code == 404
    assert client.call("DELETE", f"watches/{watch_id}", user_id=888).status_code == 404
    r = client.call("PATCH", f"watches/{watch_id}", json_body={"paused": True})
    assert r.get_json()["watch"]["paused"] is True
    assert client.call("DELETE", f"watches/{watch_id}").status_code == 200


def test_expired_user_cannot_add(client):
    client.call("GET", "me")
    db = miniapp.FeedDB(__import__("sqlite3").connect(olx_watcher.STATE_PATH))
    db.set_user("777", paid_until=1)
    r = client.call("POST", "watches", json_body={"query": "ps5", "city": "list"})
    assert r.status_code == 402


def test_filter_setting(client):
    assert client.call("POST", "settings", json_body={"min_discount": 20}).status_code == 200
    assert client.call("GET", "me").get_json()["user"]["min_discount"] == 20
    assert client.call("POST", "settings", json_body={"min_discount": 17}).status_code == 400


def test_paid_button_asks_for_receipt_in_chat(client):
    client.call("POST", "paid")
    assert any(m == "sendMessage" and kw["chat_id"] == "777" for m, kw in client.sent)


def test_admin_is_owner_only(client):
    assert client.call("GET", "admin/stats").status_code == 403
    assert client.call("POST", "admin/users/777/grant", json_body={"days": 365}).status_code == 403
    assert client.call("GET", "admin/stats", user_id=int(OWNER)).status_code == 200


def test_admin_grant_and_stats(client):
    client.call("GET", "me")
    r = client.call("POST", "admin/users/777/grant", user_id=int(OWNER), json_body={"days": 30, "paid": True})
    assert r.get_json()["user"]["plan"] == "paid"
    stats = client.call("GET", "admin/stats", user_id=int(OWNER)).get_json()["stats"]
    assert stats["revenue_total"] == 5000 and stats["paid"] == 1
    users = client.call("GET", "admin/users?q=777", user_id=int(OWNER)).get_json()["items"]
    assert [u["chat_id"] for u in users] == ["777"]


def test_closed_service_lets_only_owner_in(client, monkeypatch):
    monkeypatch.setattr(miniapp, "context", lambda: ({"feed": {"enabled": False}}, TOKEN, {OWNER}))
    assert client.call("GET", "me").status_code == 403
    assert client.call("GET", "me", user_id=int(OWNER)).status_code == 200


def test_delete_me(client):
    client.call("POST", "watches", json_body={"query": "ps5", "city": "list"})
    assert client.call("DELETE", "me").status_code == 200
    db = miniapp.FeedDB(__import__("sqlite3").connect(olx_watcher.STATE_PATH))
    assert db.watches("777") == []


def test_api_rate_limit(client):
    codes = [client.call("GET", "me").status_code for _ in range(95)]
    assert codes.count(429) == 5
    assert client.call("GET", "me", user_id=int(OWNER)).status_code == 200  # владельца не ограничиваем
