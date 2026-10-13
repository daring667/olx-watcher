"""Панель: оба раздела открываются, действия работают и защищены."""
import time

import pytest

import dashboard
import olx_feed
import olx_watcher
from feed_db import FeedDB


@pytest.fixture
def panel(tmp_path, monkeypatch):
    path = tmp_path / "dash.sqlite3"
    storage = olx_watcher.Storage(path)
    db = FeedDB(storage.connection)
    db.touch_user({"id": 777, "first_name": "Покупатель", "username": "buyer"}, 3)
    db.touch_user({"id": 888, "first_name": "Друг"}, 3)
    db.set_referrer("888", "777")
    watch = db.add_watch("777", olx_feed.build_search_url("1070 ti", "alma-ata"), "«1070 ti» · Алматы")
    db.set_watch_exclude(watch, "777", "чехол")
    db.record_delivery("777", watch, olx_watcher.Listing("ID1", "https://olx.kz/1", "GTX 1070 Ti", 55000), 31)
    db.grant("777", 30, amount_kzt=1000)
    db.create_receipt("888", "file")
    storage.close()
    sent = []
    monkeypatch.setattr(dashboard, "DB", path)
    monkeypatch.setattr(dashboard, "owner_ids", lambda: {"1"})
    monkeypatch.setattr(olx_watcher, "load_config", lambda: {"feed": {"price_kzt": 1000, "period_days": 30}})
    monkeypatch.setattr(olx_watcher, "telegram_request", lambda *a, **kw: sent.append(kw) or {"result": {"message_id": 1}})
    client = dashboard.app.test_client()
    client.sent, client.path = sent, path
    return client


@pytest.mark.parametrize("url", ["/", "/users", "/users?f=paid", "/users?q=buy", "/users/777", "/payments",
                                 "/payments?m=receipt", "/searches", "/deliveries", "/events", "/events?k=grant",
                                 "/gpu", "/gpu/passed", "/gpu/today", "/gpu/mining", "/gpu/all", "/gpu/market"])
def test_every_page_opens(panel, url):
    response = panel.get(url)
    assert response.status_code == 200, url
    assert "Радар объявлений" in response.get_data(as_text=True)


def test_overview_shows_money_and_pending(panel):
    html = panel.get("/").get_data(as_text=True)
    assert "1 000 ₸" in html and "Чеки на проверке" in html and "1070 ti" in html


def test_approve_receipt_from_panel(panel):
    db = FeedDB(__import__("sqlite3").connect(panel.path))
    [payment] = db.pending_payments()
    response = panel.post(f"/payments/{payment['id']}/approve", headers={"Origin": "http://localhost"})
    assert response.status_code == 302 and "ok=" in response.location
    assert db.payment(payment["id"])["status"] == "approved"
    assert any("Доступ продлён" in kw.get("text", "") for kw in panel.sent)
    # Повторное нажатие ничего не меняет.
    panel.post(f"/payments/{payment['id']}/approve", headers={"Origin": "http://localhost"})
    assert len(db.c.execute("SELECT * FROM payments WHERE status='approved' AND chat_id='888'").fetchall()) == 1


def test_grant_and_revoke_from_panel(panel):
    db = FeedDB(__import__("sqlite3").connect(panel.path))
    before = db.user("888")["paid_until"]
    panel.post("/users/888/grant", data={"preset": "7:0"}, headers={"Origin": "http://localhost"})
    assert db.user("888")["paid_until"] - before == 7 * 86400
    panel.post("/users/888/grant", data={"preset": "revoke"}, headers={"Origin": "http://localhost"})
    assert db.user("888")["paid_until"] <= time.time()


def test_cross_site_post_is_refused(panel):
    response = panel.post("/users/888/grant", data={"preset": "30:1"}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_old_gpu_addresses_redirect(panel):
    assert panel.get("/passed").location.endswith("/gpu/passed")
    assert panel.get("/price/ID1").location.endswith("/gpu/price/ID1")


def test_journal_details_are_links(panel):
    import sqlite3
    db = FeedDB(sqlite3.connect(panel.path))
    watch = db.watches("777")[0]
    db.log("777", "watch_pause", str(watch["id"]))
    db.add_favorite(db.recent_deliveries("777")[0])
    db.log("777", "watch_delete", "999")
    html = panel.get("/users/777").get_data(as_text=True)
    assert f"href='{watch['search_url']}'" in html and "«1070 ti» · Алматы</a>" in html   # поиск ссылкой с названием
    assert "href='https://olx.kz/1'" in html and "GTX 1070 Ti</a>" in html                 # избранное — объявление
    assert "поиск #999 (удалён)" in html
    assert "пришёл в бота</span>" not in html or "пришёл в бота ·" not in html            # без висящей точки
    events_page = panel.get("/events").get_data(as_text=True)
    assert f"href='{watch['search_url']}'" in events_page
    assert "href='/users/888'" in panel.get("/users/777").get_data(as_text=True)          # друг — карточкой
