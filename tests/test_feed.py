"""Публичный режим: база, кнопочное меню, подписки по ссылкам, оплаты."""
import json
import sqlite3
import time

import pytest

import olx_feed
import olx_watcher
from feed_db import DAY, FeedDB

OWNER = "12345"
CONFIG = {"feed": {"enabled": True, "trial_days": 3, "max_watches": 2, "price_kzt": 5000,
                   "period_days": 30, "min_samples": 3, "seed_pages": 1, "url_delay_seconds": 0}}
SEARCH = "https://www.olx.kz/d/elektronika/telefony/alma-ata/q-iphone-13/?search%5Bfilter_float_price%3Ato%5D=200000"


@pytest.fixture
def bot(tmp_path, monkeypatch):
    calls = []

    def fake(token, method, **kw):
        calls.append((method, kw))
        return {"result": {"message_id": len(calls)}}

    monkeypatch.setattr(olx_watcher, "telegram_request", fake)
    storage = olx_watcher.Storage(tmp_path / "feed.sqlite3")
    db = FeedDB(storage.connection)
    yield storage, db, calls
    storage.close()


def say(storage, chat_id, text="", config=CONFIG, **extra):
    message = {"message_id": 7, "chat": {"id": int(chat_id), "username": f"u{chat_id}", "first_name": "Имя"},
               "text": text, **extra}
    olx_watcher.handle_update("token", storage, {"update_id": 1, "message": message}, OWNER, config)


def press(storage, chat_id, data, config=CONFIG, message_text="меню"):
    callback = {"id": "cb", "data": data, "from": {"id": int(chat_id)},
                "message": {"message_id": 99, "chat": {"id": int(chat_id)}, "text": message_text}}
    olx_watcher.handle_update("token", storage, {"update_id": 2, "callback_query": callback}, OWNER, config)


def texts(calls):
    return [kw.get("text", "") for method, kw in calls if method in {"sendMessage", "editMessageText"}]


def buttons(calls):
    """Все кнопки последнего экрана: {текст: callback_data или url}."""
    for method, kw in reversed(calls):
        if "reply_markup" in kw:
            rows = json.loads(kw["reply_markup"])["inline_keyboard"]
            return {b["text"]: b.get("callback_data") or b.get("url") for row in rows for b in row}
    return {}


def answers(calls):
    return [kw.get("text") for method, kw in calls if method == "answerCallbackQuery"]


# --- Ссылки -----------------------------------------------------------------
def test_normalize_forces_newest_first_and_drops_page():
    url = olx_feed.normalize_search_url(
        "смотри https://m.olx.kz/d/transport/alma-ata/?page=3&utm_source=tg&search%5Bdist%5D=30 ага")
    assert url.startswith("https://www.olx.kz/d/transport/alma-ata/?")
    assert "page=" not in url and "utm_" not in url
    assert "search%5Border%5D=created_at%3Adesc" in url


def test_same_search_with_different_param_order_is_one_url():
    assert (olx_feed.normalize_search_url("https://www.olx.kz/d/x/?a=1&b=2")
            == olx_feed.normalize_search_url("https://www.olx.kz/d/x?b=2&a=1"))


@pytest.mark.parametrize("text", [
    "https://www.olx.kz/d/obyavlenie/iphone-13-IDqRnup.html",
    "https://kolesa.kz/cars/",
    "просто текст",
])
def test_not_a_search_url(text):
    assert olx_feed.normalize_search_url(text) is None


def test_watch_title_is_readable():
    title = olx_feed.watch_title(olx_feed.normalize_search_url(SEARCH))
    assert title == "«iphone 13» · telefony · Алматы · до 200 000 ₸"


def test_search_builder():
    url = olx_feed.build_search_url("iPhone 13 Pro", "alma-ata", None, 200000)
    assert url.startswith("https://www.olx.kz/alma-ata/q-iphone-13-pro/?")
    assert "filter_float_price%3Ato%5D=200000" in url and "created_at%3Adesc" in url
    assert olx_feed.build_search_url("", "alma-ata") is None
    assert olx_feed.build_search_url("iphone", "нет-такого") is None


def test_links_without_d_prefix_are_accepted():
    """OLX теперь перенаправляет /d/alma-ata/… на /alma-ata/…"""
    assert olx_feed.normalize_search_url("https://www.olx.kz/alma-ata/q-iphone/")
    assert olx_feed.normalize_search_url("https://www.olx.kz/") is None
    assert olx_feed.normalize_search_url("https://www.olx.kz/myaccount/") is None


# --- Оценка рынка -----------------------------------------------------------
@pytest.mark.parametrize("price, marker", [
    (70000, "🔥 На 30%"), (98000, "✅ Около рынка"), (130000, "💸 На 30%"), (20000, "⚠️"),
])
def test_market_line(price, marker):
    assert olx_feed.market_line(price, 100000, 10, 8).startswith(marker)


def test_market_line_needs_enough_samples():
    assert "мало данных" in olx_feed.market_line(70000, 100000, 3, 8)


def test_median_ignores_typo_outlier(bot):
    _, db, _ = bot
    db.store_cards("s", [olx_watcher.Listing(f"ID{i}", "u", "t", p) for i, p in
                         enumerate([100000, 110000, 90000, 105000, 256560000])])
    assert db.market("s", 30) == (105000, 5)


# --- База ---------------------------------------------------------------------
def test_old_database_is_migrated(tmp_path):
    """База первой версии публичного режима открывается без потерь."""
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE users (chat_id TEXT PRIMARY KEY, username TEXT, first_name TEXT, joined_at INTEGER NOT NULL,
          paid_until INTEGER NOT NULL, min_discount INTEGER NOT NULL DEFAULT 0,
          blocked INTEGER NOT NULL DEFAULT 0, expired_notified INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE watches (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
          search_url TEXT NOT NULL, title TEXT NOT NULL, created_at INTEGER NOT NULL, UNIQUE(chat_id, search_url));
        INSERT INTO users VALUES ('7','old','Старый',1,9999999999,15,0,0);
        INSERT INTO watches(chat_id,search_url,title,created_at) VALUES ('7','https://www.olx.kz/d/x/','x',1);
    """)
    db = FeedDB(connection)
    user = db.user("7")
    assert user["min_discount"] == 15 and user["plan"] == "trial" and user["state"] == ""
    assert db.watches("7")[0]["paused"] == 0


def test_grant_records_payment_and_marks_paid(bot):
    _, db, _ = bot
    db.touch_user({"id": 7}, 3)
    before = db.user("7")["paid_until"]
    user = db.grant("7", 30, by=OWNER, amount_kzt=5000)
    assert user["paid_until"] - before == 30 * DAY and user["plan"] == "paid"
    assert db.stats()["revenue_total"] == 5000
    bonus = db.grant("8", 3)
    assert bonus is None  # неизвестный пользователь


def test_bonus_days_do_not_make_user_paying(bot):
    _, db, _ = bot
    db.touch_user({"id": 7}, 3)
    assert db.grant("7", 3)["plan"] == "trial"


# --- Меню ---------------------------------------------------------------------
def test_start_shows_welcome_with_trial_and_menu(bot):
    storage, db, calls = bot
    say(storage, 777, "/start")
    assert "Пробный доступ до" in texts(calls)[-1]
    assert {"➕ Добавить поиск", "📋 Мои поиски", "⭐ Избранное", "💳 Подписка"} <= set(buttons(calls))
    assert "👑 Админка" not in buttons(calls)
    assert db.user("777")["first_name"] == "Имя"


def test_owner_menu_has_admin(bot):
    storage, _, calls = bot
    say(storage, OWNER, "/start")
    assert "👑 Админка" in buttons(calls) and "🎮 Видеокарты" in buttons(calls)


def test_menu_navigation_edits_same_message(bot):
    storage, _, calls = bot
    say(storage, 777, "/start")
    calls.clear()
    press(storage, 777, "u:add")
    assert calls[0][0] == "editMessageText" and calls[0][1]["message_id"] == 99
    assert buttons(calls)["🌐 Открыть OLX"].startswith("https://www.olx.kz")


def test_stranger_gets_trial_and_can_add_search(bot):
    storage, db, calls = bot
    say(storage, 777, "/start")
    say(storage, 777, SEARCH)
    assert "Слежу" in texts(calls)[-1]
    [watch] = db.watches("777")
    assert "created_at%3Adesc" in watch["search_url"]
    assert buttons(calls)["🔍 Открыть поиск"] == f"u:w:{watch['id']}"


def test_stranger_never_reaches_owner_commands(bot):
    storage, db, calls = bot
    say(storage, 777, "/favorites")
    say(storage, 777, "/grant 777 365")
    press(storage, 777, "a:g:777:365:0")
    assert texts(calls) == []
    user = db.user("777")
    assert user is None or user["paid_until"] < time.time() + 4 * DAY


def test_public_mode_off_keeps_strangers_out(bot):
    storage, _, calls = bot
    say(storage, 777, SEARCH, config={})
    assert calls == []


def test_expired_user_is_sent_to_subscription(bot):
    storage, db, calls = bot
    say(storage, 777, "/start")
    db.set_user("777", paid_until=int(time.time()) - 1)
    say(storage, 777, SEARCH)
    assert "Доступ закончился" in texts(calls)[-1]
    assert "📸 Я оплатил по Kaspi — отправить чек" in buttons(calls)
    assert "⭐ Оплатить 100 Stars" in buttons(calls)
    assert db.watches("777") == []


def test_watch_limit(bot):
    storage, _, calls = bot
    for path in ("a", "b", "c"):
        say(storage, 777, f"https://www.olx.kz/d/{path}/")
    assert "максимум за 2" in texts(calls)[-1]


def test_owner_has_no_limit(bot):
    storage, db, _ = bot
    for path in ("a", "b", "c"):
        say(storage, OWNER, f"https://www.olx.kz/d/{path}/")
    assert len(db.watches(OWNER)) == 3


def test_watch_pause_and_delete_with_confirmation(bot):
    storage, db, calls = bot
    say(storage, 777, SEARCH)
    watch_id = db.watches("777")[0]["id"]
    press(storage, 777, f"u:wp:{watch_id}")
    assert db.watch(watch_id)["paused"] == 1 and "▶️ Возобновить" in buttons(calls)
    assert db.polled_urls() == []  # на паузе OLX не опрашиваем
    press(storage, 777, f"u:wd:{watch_id}")
    assert db.watch(watch_id) is not None  # только вопрос «точно удалить?»
    press(storage, 777, f"u:wD:{watch_id}")
    assert db.watch(watch_id) is None


def test_foreign_watch_cannot_be_touched(bot):
    storage, db, _ = bot
    say(storage, 777, SEARCH)
    watch_id = db.watches("777")[0]["id"]
    press(storage, 888, f"u:wD:{watch_id}")
    press(storage, 888, f"f:del:{watch_id}")
    press(storage, 888, f"u:wp:{watch_id}")
    assert db.watch(watch_id)["paused"] == 0


def test_filter_buttons(bot):
    storage, db, calls = bot
    say(storage, 777, "/start")
    press(storage, 777, "u:f:20")
    assert db.user("777")["min_discount"] == 20
    assert "✓ от 20%" in buttons(calls)


def test_owner_note_with_link_stays_a_note(bot):
    storage, db, _ = bot
    storage.set_setting(f"pending_note:{OWNER}", "ID1")
    say(storage, OWNER, "продавец дал ссылку https://www.olx.kz/d/x/")
    assert db.watches(OWNER) == []


# --- Оплата -------------------------------------------------------------------
def test_receipt_goes_to_owner_and_approve_extends_access(bot):
    storage, db, calls = bot
    say(storage, 777, "/start")
    press(storage, 777, "u:paid")
    assert db.user("777")["state"] == "receipt"
    calls.clear()
    say(storage, 777, photo=[{"file_id": "small"}, {"file_id": "big"}])
    forwarded = [kw for method, kw in calls if method == "forwardMessage"]
    assert forwarded and forwarded[0]["chat_id"] == OWNER
    [payment] = db.pending_payments()
    assert payment["receipt_file"] == "big"
    before = db.user("777")["paid_until"]
    press(storage, OWNER, f"a:ok:{payment['id']}")
    user = db.user("777")
    assert user["paid_until"] - before == 30 * DAY and user["plan"] == "paid"
    assert db.payment(payment["id"])["status"] == "approved"
    assert any("Доступ продлён" in t for t in texts(calls))
    # Повторное нажатие не продлевает второй раз.
    press(storage, OWNER, f"a:ok:{payment['id']}")
    assert db.user("777")["paid_until"] == user["paid_until"]
    assert "уже обработан" in answers(calls)[-1]


def test_rejected_receipt(bot):
    storage, db, calls = bot
    say(storage, 777, "/start")
    say(storage, 777, photo=[{"file_id": "x"}])
    payment_id = db.pending_payments()[0]["id"]
    press(storage, OWNER, f"a:no:{payment_id}")
    assert db.payment(payment_id)["status"] == "rejected"
    assert any("не подтверждена" in t for t in texts(calls))


def test_admin_grant_buttons_and_stats(bot):
    storage, db, calls = bot
    say(storage, 777, "/start")
    press(storage, OWNER, "a:g:777:30:1")
    assert db.user("777")["plan"] == "paid"
    press(storage, OWNER, "a:home")
    assert "5 000 ₸" in texts(calls)[-1]  # выручка


def test_text_grant_command_still_works(bot):
    storage, db, calls = bot
    say(storage, 777, "/start")
    before = db.user("777")["paid_until"]
    say(storage, OWNER, "/grant @u777 30")
    assert db.user("777")["paid_until"] - before == 30 * DAY
    assert any("Доступ продлён" in t for t in texts(calls))


def test_broadcast_needs_confirmation(bot):
    storage, db, calls = bot
    say(storage, 777, "/start")
    press(storage, OWNER, "a:bc")
    say(storage, OWNER, "Новая функция!")
    assert db.user(OWNER)["state"] == "broadcast_confirm"
    assert not any(kw.get("chat_id") == "777" and "Новая функция" in kw.get("text", "")
                   for _, kw in calls)
    press(storage, OWNER, "a:bcgo")
    assert any(kw.get("chat_id") == "777" and kw.get("text") == "Новая функция!" for _, kw in calls)


# --- Опрос и уведомления ------------------------------------------------------
def poll_setup(storage, db, monkeypatch):
    say(storage, 777, SEARCH)
    say(storage, 778, SEARCH)
    db.set_user("778", min_discount=20)
    url = db.watches("777")[0]["search_url"]
    pages = [[olx_watcher.Listing(f"OLD{i}", "u", "старое", 100000) for i in range(5)]]
    monkeypatch.setattr(olx_feed, "fetch_cards", lambda session, search_url, page=1: pages[-1])
    return url, pages, olx_feed.settings(CONFIG)


def test_poll_sends_only_new_cards_with_market_and_discount_filter(bot, monkeypatch):
    storage, db, calls = bot
    url, pages, cfg = poll_setup(storage, db, monkeypatch)
    assert olx_feed.poll_url(db, None, "token", url, cfg, {OWNER}) == 0   # базовый снимок
    calls.clear()
    pages.append(pages[0] + [olx_watcher.Listing("NEW1", "https://olx.kz/n1", "iPhone 13", 90000),
                             olx_watcher.Listing("NEW2", "https://olx.kz/n2", "iPhone 13 дёшево", 70000)])
    assert olx_feed.poll_url(db, None, "token", url, cfg, {OWNER}) == 3
    recipients = sorted(kw["chat_id"] for method, kw in calls if method == "sendMessage")
    assert recipients == ["777", "777", "778"]  # 778 — только то, что на 30% дешевле
    assert any("🔥 На 30%" in kw["text"] for _, kw in calls)
    assert db.watches("777")[0]["sent_count"] == 2
    calls.clear()
    assert olx_feed.poll_url(db, None, "token", url, cfg, {OWNER}) == 0


def test_favorite_and_pause_from_notification(bot, monkeypatch):
    storage, db, calls = bot
    url, pages, cfg = poll_setup(storage, db, monkeypatch)
    olx_feed.poll_url(db, None, "token", url, cfg, {OWNER})
    pages.append(pages[0] + [olx_watcher.Listing("NEW1", "https://olx.kz/n1", "iPhone 13", 90000)])
    olx_feed.poll_url(db, None, "token", url, cfg, {OWNER})
    delivery_id = int(buttons(calls)["⭐ В избранное"].rsplit(":", 1)[1])
    press(storage, 777, f"u:fav:{delivery_id}")
    assert [f["ad_id"] for f in db.favorites("777")] == ["NEW1"]
    assert "★ В избранном" in buttons(calls)
    press(storage, 777, f"u:np:{delivery_id}")
    assert db.watches("777")[0]["paused"] == 1
    press(storage, 777, "u:favs")
    assert "iPhone 13" in texts(calls)[-1]
    # Чужое нажатие на ту же кнопку ничего не меняет.
    press(storage, 888, f"u:fav:{delivery_id}")
    assert db.favorites("888") == []


def test_blocked_user_is_marked(bot, monkeypatch):
    storage, db, _ = bot
    say(storage, 777, SEARCH)

    def forbidden(*a, **kw):
        raise olx_watcher.requests.RequestException("403: Forbidden: bot was blocked by the user")

    monkeypatch.setattr(olx_watcher, "telegram_request", forbidden)
    user = db.subscribers(db.watches("777")[0]["search_url"])[0]
    assert olx_feed.send_card(db, "t", user, olx_watcher.Listing("ID1", "u", "t", 1), "") is False
    assert db.user("777")["blocked"] == 1
    assert db.count("deliveries", "777") == 0


def test_expiry_reminders_are_sent_once(bot):
    storage, db, calls = bot
    say(storage, 777, SEARCH)
    cfg = olx_feed.settings(CONFIG)
    db.set_user("777", paid_until=int(time.time()) + 3600)
    olx_feed.notify_access(db, "t", cfg, {OWNER})
    olx_feed.notify_access(db, "t", cfg, {OWNER})
    assert sum("закончится" in t for t in texts(calls)) == 1
    db.set_user("777", paid_until=int(time.time()) - 1)
    olx_feed.notify_access(db, "t", cfg, {OWNER})
    assert any("Доступ закончился" in t for t in texts(calls))


def test_card_price_not_glued_to_title():
    """«iPhone 17 Pro Max 256» + «560 000 тг.» склеивалось в 256 560 000 ₸."""
    page = ('<div data-cy="l-card"><a href="/d/obyavlenie/iphone-IDr8UhN.html">'
            '<div data-testid="ad-card-title"><h4>Iphone 17 pro max 256</h4>'
            '<p data-testid="ad-price">560 000 тг.</p></div></a></div>')
    [card] = olx_watcher.extract_listings("https://www.olx.kz/d/x/", page, 60)
    assert card.price_kzt == 560000


def test_payment_text_from_env(monkeypatch):
    monkeypatch.setenv("FEED_PAYMENT_TEXT", "Kaspi: +7 700\\nПришлите чек")
    assert olx_feed.settings(CONFIG)["payment_text"] == "Kaspi: +7 700\nПришлите чек"
    monkeypatch.delenv("FEED_PAYMENT_TEXT")
    assert "платная" in olx_feed.settings(CONFIG)["payment_text"]


def test_all_callback_data_fits_telegram_limit(bot):
    storage, db, calls = bot
    say(storage, 777, "/start")
    for data in ("u:home", "u:add", "u:list", "u:favs", "u:filter", "u:sub", "u:help"):
        press(storage, OWNER, data)
    press(storage, OWNER, "a:home")
    press(storage, OWNER, "a:ul:0")
    press(storage, OWNER, "a:u:777")
    for method, kw in calls:
        if "reply_markup" in kw:
            for row in json.loads(kw["reply_markup"])["inline_keyboard"]:
                for b in row:
                    assert len(b.get("callback_data", "").encode()) <= 64


@pytest.mark.parametrize("entry", olx_feed.PUBLIC_COMMANDS)
def test_public_commands_match_telegram_rules(entry):
    import re
    assert re.fullmatch(r"[a-z0-9_]{1,32}", entry["command"])


# --- Безопасность -------------------------------------------------------------
def test_flood_is_ignored_after_limit(bot):
    storage, _, calls = bot
    for _ in range(olx_feed.FLOOD_LIMIT + 5):
        say(storage, 777, "привет")
    assert len(texts(calls)) == olx_feed.FLOOD_LIMIT


def test_owner_is_never_rate_limited(bot):
    storage, _, calls = bot
    for _ in range(olx_feed.FLOOD_LIMIT + 5):
        say(storage, OWNER, "/start")
    assert len(texts(calls)) == olx_feed.FLOOD_LIMIT + 5


def test_second_receipt_while_pending_is_not_forwarded(bot):
    storage, db, calls = bot
    say(storage, 777, "/start")
    say(storage, 777, photo=[{"file_id": "a"}])
    say(storage, 777, photo=[{"file_id": "b"}])
    assert sum(method == "forwardMessage" for method, _ in calls) == 1
    assert "уже на проверке" in texts(calls)[-1]
    assert len(db.pending_payments()) == 1


def test_receipts_per_day_cap(bot):
    storage, db, calls = bot
    say(storage, 777, "/start")
    for _ in range(olx_feed.RECEIPTS_PER_DAY + 1):
        say(storage, 777, photo=[{"file_id": "x"}])
        for p in db.pending_payments():
            db.reject_payment(p["id"], OWNER)
    assert sum(method == "forwardMessage" for method, _ in calls) == olx_feed.RECEIPTS_PER_DAY


def test_delete_my_data(bot):
    storage, db, calls = bot
    say(storage, 777, SEARCH)
    say(storage, 888, SEARCH)
    say(storage, 777, photo=[{"file_id": "x"}])
    press(storage, OWNER, f"a:ok:{db.pending_payments()[0]['id']}")
    say(storage, 777, "/deleteme")
    assert "Удалить мои данные" in texts(calls)[-1]
    press(storage, 777, "u:delok")
    assert db.user("777") is None and db.watches("777") == []
    assert len(db.watches("888")) == 1                      # чужое не тронуто
    assert db.stats()["revenue_total"] == 5000  # выручка осталась (цена из CONFIG теста)
    assert db.c.execute("SELECT COUNT(*) FROM payments WHERE chat_id='777'").fetchone()[0] == 0
    # Вернулся — пробного периода второй раз нет.
    say(storage, 777, "/start")
    assert not olx_feed.is_active(db.user("777"), {OWNER})


def test_telegram_429_is_retried_once(monkeypatch):
    responses = [
        {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 3}},
        {"ok": True, "result": {"message_id": 5}},
    ]
    slept = []

    class Response:
        def __init__(self, body):
            self.body, self.status_code = body, 200 if body["ok"] else 429

        def json(self):
            return self.body

    monkeypatch.setattr(olx_watcher.requests, "post", lambda *a, **kw: Response(responses.pop(0)))
    monkeypatch.setattr(olx_watcher.time, "sleep", slept.append)
    assert olx_watcher.telegram_request("t", "sendMessage", chat_id=1, text="x")["result"]["message_id"] == 5
    assert slept == [3]


def test_olx_block_backoff():
    assert [olx_feed.next_backoff(b, True) for b in (0, 2, 4, 32, 60)] == [2, 4, 8, 60, 60]
    assert olx_feed.next_backoff(16, False) == 0
    assert olx_feed.is_olx_block(Exception("HTTP Error 403: Forbidden"))
    assert not olx_feed.is_olx_block(Exception("Connection reset"))


def test_healthcheck_feed_staleness(bot):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))
    import healthcheck

    storage, db, _ = bot
    assert healthcheck.feed_problems(db.c, {OWNER}) == []
    say(storage, 777, SEARCH)
    url = db.watches("777")[0]["search_url"]
    db.mark_polled(url)
    assert healthcheck.feed_problems(db.c, {OWNER}) == []
    db.c.execute("UPDATE feed_urls SET last_polled=?", (int(time.time()) - 3600,))
    assert "не опрашивался" in healthcheck.feed_problems(db.c, {OWNER})[0]
    db.set_user("777", paid_until=1)  # у всех истёк доступ — опрос и не должен идти
    assert healthcheck.feed_problems(db.c, {OWNER}) == []


def test_healthcheck_alert_is_escaped_and_marked_only_when_sent(tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))
    import healthcheck

    sent = []
    monkeypatch.setattr(healthcheck, "STATE_PATH", tmp_path / "h.sqlite3")
    monkeypatch.setattr(healthcheck, "load_dotenv", lambda path: None)
    monkeypatch.setattr(healthcheck, "load_config", lambda: {})
    monkeypatch.setenv("TELEGRAM_CHAT_ID", OWNER)
    monkeypatch.setattr(healthcheck, "unit_problems", lambda: [])
    monkeypatch.setattr(healthcheck, "app_problems", lambda: ["приложение не отвечает: <urlopen error>"])

    def fail(*a, **kw):
        raise olx_watcher.requests.RequestException("400")

    monkeypatch.setattr(healthcheck, "telegram_request", fail)
    healthcheck.main()
    connection = sqlite3.connect(tmp_path / "h.sqlite3")
    assert healthcheck.meta(connection, "monitor_alert_at") == ""  # не дошло — повторим в следующий раз
    monkeypatch.setattr(healthcheck, "telegram_request", lambda *a, **kw: sent.append(kw))
    healthcheck.main()
    assert "&lt;urlopen error&gt;" in sent[0]["text"]
    assert healthcheck.meta(connection, "monitor_alert_at") != ""


# --- Слова-исключения ---------------------------------------------------------
def test_exclude_words_normalized_and_matched_in_title_only():
    from feed_db import is_excluded, normalize_words
    words = normalize_words("Чехол;  стекло\nДисплей, чехол,,")
    assert words == "чехол, стекло, дисплей"
    assert is_excluded("Дисплей на айфон 13", words)
    assert not is_excluded("iPhone 13 128GB", words)
    assert not is_excluded("iPhone 13", "")


def test_excluded_cards_are_not_sent(bot, monkeypatch):
    storage, db, calls = bot
    url, pages, cfg = poll_setup(storage, db, monkeypatch)
    db.set_watch_exclude(db.watches("777")[0]["id"], "777", "дисплей")
    olx_feed.poll_url(db, None, "token", url, cfg, {OWNER})
    calls.clear()
    pages.append(pages[0] + [olx_watcher.Listing("NEW1", "u", "Дисплей на айфон 13", 20000),
                             olx_watcher.Listing("NEW2", "u", "iPhone 13 128GB", 70000)])
    olx_feed.poll_url(db, None, "token", url, cfg, {OWNER})
    to_777 = [kw["text"] for method, kw in calls if method == "sendMessage" and kw["chat_id"] == "777"]
    assert len(to_777) == 1 and "iPhone 13 128GB" in to_777[0]


def test_exclude_words_via_bot(bot):
    storage, db, calls = bot
    say(storage, 777, SEARCH)
    watch_id = db.watches("777")[0]["id"]
    press(storage, 777, f"u:wx:{watch_id}")
    say(storage, 777, "Чехол, стекло")
    assert db.watch(watch_id)["exclude_words"] == "чехол, стекло"
    assert "Исключено: чехол, стекло" in texts(calls)[-1]
    press(storage, 777, f"u:wxa:{watch_id}")
    assert "дисплей" in db.watch(watch_id)["exclude_words"]
    press(storage, 777, f"u:wxc:{watch_id}")
    assert db.watch(watch_id)["exclude_words"] == ""


# --- Снижение цены в избранном ------------------------------------------------
def favorite_setup(storage, db, monkeypatch):
    url, pages, cfg = poll_setup(storage, db, monkeypatch)
    olx_feed.poll_url(db, None, "token", url, cfg, {OWNER})
    pages.append(pages[0] + [olx_watcher.Listing("NEW1", "https://olx.kz/n1", "iPhone 13", 90000)])
    olx_feed.poll_url(db, None, "token", url, cfg, {OWNER})
    delivery = db.recent_deliveries("777")[0]
    db.add_favorite(delivery)
    return delivery


def test_price_drop_in_favorites_is_notified_once(bot, monkeypatch):
    storage, db, calls = bot
    favorite_setup(storage, db, monkeypatch)
    prices = [80000]
    monkeypatch.setattr(olx_watcher, "fetch_details", lambda session, listing: listing.__class__(
        listing.ad_id, listing.url, listing.title, prices[0]))
    monkeypatch.setattr(olx_feed.time, "sleep", lambda s: None)
    calls.clear()
    assert olx_feed.check_favorites(db, None, "t") == 1
    assert "90 000 ₸</s> → <b>80 000 ₸" in texts(calls)[-1]
    # Сразу снова не проверяем (раз в 6 часов), а при той же цене — тишина.
    assert olx_feed.check_favorites(db, None, "t") == 0
    db.c.execute("UPDATE favorites SET checked_at=0"); db.c.commit()
    assert olx_feed.check_favorites(db, None, "t") == 0
    prices[0] = 95000  # подорожало — молчим, но запоминаем
    db.c.execute("UPDATE favorites SET checked_at=0"); db.c.commit()
    assert olx_feed.check_favorites(db, None, "t") == 0
    assert db.favorites("777")[0]["current_price"] == 95000


def test_removed_favorite_is_marked(bot, monkeypatch):
    storage, db, calls = bot
    favorite_setup(storage, db, monkeypatch)

    class Gone(olx_watcher.requests.RequestException):
        def __init__(self):
            super().__init__("410 Gone")
            self.response = type("R", (), {"status_code": 410})()

    def gone(session, listing):
        raise Gone()

    monkeypatch.setattr(olx_watcher, "fetch_details", gone)
    monkeypatch.setattr(olx_feed.time, "sleep", lambda s: None)
    assert olx_feed.check_favorites(db, None, "t") == 1
    assert db.favorites("777")[0]["gone"] == 1
    assert "Снято с публикации" in texts(calls)[-1]
    press(storage, 777, "u:favs")
    assert "снято" in texts(calls)[-1]


# --- Рефералы -----------------------------------------------------------------
def test_referral_reward_only_on_first_payment(bot, monkeypatch):
    storage, db, calls = bot
    monkeypatch.setattr(olx_feed, "_bot_username", {"token": "radar_bot"})
    say(storage, 777, "/start")
    say(storage, 888, "/start ref_777")
    assert db.user("888")["referrer"] == "777"
    before = db.user("777")["paid_until"]
    press(storage, OWNER, "a:g:888:3:0")                      # бонус — не оплата
    assert db.user("777")["paid_until"] == before
    press(storage, OWNER, "a:g:888:30:1")                     # первая оплата
    assert db.user("777")["paid_until"] - before == 7 * DAY
    assert any("друг оформил подписку" in t for t in texts(calls))
    press(storage, OWNER, "a:g:888:30:1")                     # вторая — без награды
    assert db.user("777")["paid_until"] - before == 7 * DAY
    assert db.referral_stats("777") == (1, 1)


def test_referral_rules(bot):
    storage, db, _ = bot
    say(storage, 777, "/start ref_777")                      # сам себя
    assert db.user("777")["referrer"] is None
    say(storage, 888, "/start ref_999")                      # несуществующий
    assert db.user("888")["referrer"] is None
    say(storage, 888, "/start ref_777")                      # уже зарегистрирован
    assert db.user("888")["referrer"] is None


def test_invite_screen(bot, monkeypatch):
    storage, _, calls = bot
    monkeypatch.setattr(olx_feed, "_bot_username", {"token": "radar_bot"})
    say(storage, 777, "/start")
    press(storage, 777, "u:ref")
    assert "https://t.me/radar_bot?start=ref_777" in texts(calls)[-1]
    assert buttons(calls)["📤 Поделиться ссылкой"].startswith("https://t.me/share/url?url=")


# --- Telegram Stars -----------------------------------------------------------
def pre_checkout(storage, user_id, payload, amount=100, currency="XTR"):
    update = {"update_id": 5, "pre_checkout_query": {"id": "q1", "from": {"id": user_id}, "currency": currency,
                                                      "total_amount": amount, "invoice_payload": payload}}
    olx_watcher.handle_update("token", storage, update, OWNER, CONFIG)


def paid(storage, user_id, payload, charge="ch1"):
    message = {"message_id": 3, "chat": {"id": user_id}, "from": {"id": user_id},
               "successful_payment": {"currency": "XTR", "total_amount": 100, "invoice_payload": payload,
                                      "telegram_payment_charge_id": charge}}
    olx_watcher.handle_update("token", storage, {"update_id": 6, "message": message}, OWNER, CONFIG)


def last_precheckout(calls):
    return [kw for method, kw in calls if method == "answerPreCheckoutQuery"][-1]


def test_stars_invoice_button(bot):
    storage, _, calls = bot
    say(storage, 777, "/start")
    press(storage, 777, "u:stars")
    invoice = [kw for method, kw in calls if method == "sendInvoice"][0]
    assert invoice["currency"] == "XTR" and invoice["provider_token"] == ""
    assert json.loads(invoice["prices"])[0]["amount"] == 100
    assert invoice["payload"] == "sub:777:30:100"


@pytest.mark.parametrize("user_id, payload, amount, ok", [
    (777, "sub:777:30:100", 100, "true"),
    (777, "sub:888:30:100", 100, "false"),     # чужой счёт
    (777, "sub:777:30:100", 50, "false"),      # цена поменялась
    (777, "мусор", 100, "false"),
])
def test_pre_checkout_validation(bot, user_id, payload, amount, ok):
    storage, _, calls = bot
    say(storage, 777, "/start")
    pre_checkout(storage, user_id, payload, amount)
    assert last_precheckout(calls)["ok"] == ok


def test_successful_payment_extends_once(bot):
    storage, db, calls = bot
    say(storage, 777, "/start")
    before = db.user("777")["paid_until"]
    paid(storage, 777, "sub:777:30:100")
    paid(storage, 777, "sub:777:30:100")        # Telegram прислал повторно
    user = db.user("777")
    assert user["paid_until"] - before == 30 * DAY and user["plan"] == "paid"
    assert db.payment_by_reference("ch1")["method"] == "stars"
    assert any("Оплата Stars" in t for t in texts(calls))  # владельцу
    assert any("Доступ продлён" in t for t in texts(calls))


def test_refund_command(bot):
    storage, db, calls = bot
    say(storage, 777, "/start")
    paid(storage, 777, "sub:777:30:100", charge="ch9")
    paid_until = db.user("777")["paid_until"]
    say(storage, OWNER, "/refund ch9")
    assert any(method == "refundStarPayment" for method, _ in calls)
    assert db.payment_by_reference("ch9")["status"] == "refunded"
    assert paid_until - db.user("777")["paid_until"] == 30 * DAY


def test_same_ad_from_two_searches_is_sent_once(bot, monkeypatch):
    storage, db, calls = bot
    say(storage, 777, "https://www.olx.kz/alma-ata/q-gtx-970/")
    say(storage, 777, "https://www.olx.kz/alma-ata/q-videokarta/")
    urls = [w["search_url"] for w in db.watches("777")]
    old = [olx_watcher.Listing("OLD", "u", "старое", 30000)]
    pages = {u: old for u in urls}
    monkeypatch.setattr(olx_feed, "fetch_cards", lambda session, search_url, page=1: pages[search_url])
    cfg = olx_feed.settings(CONFIG)
    for u in urls:
        olx_feed.poll_url(db, None, "t", u, cfg, {OWNER})
    calls.clear()
    card = olx_watcher.Listing("GTX", "https://olx.kz/g", "Видеокарта gtx 970", 35000)
    for u in urls:
        pages[u] = old + [card]
        olx_feed.poll_url(db, None, "t", u, cfg, {OWNER})
    assert sum(kw.get("chat_id") == "777" for method, kw in calls if method == "sendMessage") == 1


@pytest.mark.parametrize("url, expected", [
    ("https://frankfurt.apollo.olxcdn.com:443/v1/files/x-KZ/image;s=216x152;q=50",
     "https://frankfurt.apollo.olxcdn.com:443/v1/files/x-KZ/image;s=1000x750;q=85"),
    ("https://frankfurt.apollo.olxcdn.com/v1/files/x-KZ/image", "https://frankfurt.apollo.olxcdn.com/v1/files/x-KZ/image;s=1000x750;q=85"),
    ("https://example.com/a.jpg", "https://example.com/a.jpg"),
    (None, None),
])
def test_hd_image(url, expected):
    assert olx_watcher.hd_image(url) == expected


def test_seller_repost_with_new_id_is_sent_once(bot, monkeypatch):
    """Реальный случай: «Видеокарта б/у gtx 970 4gb» — IDrc3nv и IDrc3ph с разницей в секунду."""
    storage, db, calls = bot
    url, pages, cfg = poll_setup(storage, db, monkeypatch)
    olx_feed.poll_url(db, None, "t", url, cfg, {OWNER})
    calls.clear()
    pages.append(pages[0] + [olx_watcher.Listing("IDrc3nv", "u1", "Видеокарта б/у gtx 970 4gb", 35000),
                             olx_watcher.Listing("IDrc3ph", "u2", "Видеокарта  б/у GTX 970 4gb", 35000),
                             olx_watcher.Listing("IDother", "u3", "Видеокарта б/у gtx 970 4gb", 30000)])
    olx_feed.poll_url(db, None, "t", url, cfg, {OWNER})
    to_777 = [kw for method, kw in calls if method == "sendMessage" and kw["chat_id"] == "777"]
    assert len(to_777) == 2  # копия отсеяна, та же карта по другой цене — пришла


# --- Рекомендации OLX вместо результатов ----------------------------------------
def page(total, results, recommended):
    card = lambda i, title, price: (f'<div data-cy="l-card"><a href="/d/obyavlenie/x-ID{i}.html"><h4>{title}</h4>'
                                    f'<p data-testid="ad-price">{price} тг.</p></a></div>')
    grid = lambda cards: '<div data-testid="listing-grid">' + "".join(cards) + "</div>"
    return (f'<span data-testid="total-count">Мы нашли {total} объявлений</span>'
            + grid(card(f"r{i}", t, p) for i, (t, p) in enumerate(results))
            + grid(card(f"x{i}", t, p) for i, (t, p) in enumerate(recommended)))


def test_zero_results_page_gives_no_cards():
    """Опечатка «1070 it»: OLX нашёл 0 и показал рекомендации — котят и шины."""
    html_page = page(0, [], [("Котёнок в хорошие руки", "500"), ("Шины 195/65/15", "20 000")])
    assert olx_watcher.extract_listings("https://www.olx.kz/x/", html_page, 60) == []


def test_recommendations_after_results_are_ignored():
    html_page = page(2, [("GTX 1070 Ti", "60 000"), ("Palit 1070 Ti", "55 000")], [("Квадроцикл", "450 000")])
    titles = [c.title for c in olx_watcher.extract_listings("https://www.olx.kz/x/", html_page, 60)]
    assert titles == ["GTX 1070 Ti", "Palit 1070 Ti"]


def test_empty_search_warns_user_once(bot, monkeypatch):
    storage, db, calls = bot
    say(storage, 777, "https://www.olx.kz/alma-ata/q-1070-it/")
    url = db.watches("777")[0]["search_url"]
    monkeypatch.setattr(olx_feed, "fetch_cards", lambda session, search_url, page=1: [])
    cfg = olx_feed.settings(CONFIG)
    olx_feed.poll_url(db, None, "t", url, cfg, {OWNER})
    olx_feed.poll_url(db, None, "t", url, cfg, {OWNER})
    assert sum("нет ни одного объявления" in t for t in texts(calls)) == 1


# --- Тонкая настройка поиска ----------------------------------------------------
def test_apply_filters_keeps_category_and_replaces_filters():
    url = olx_feed.apply_filters("https://www.olx.kz/elektronika/alma-ata/q-1070-ti/?search%5Bfilter_float_price%3Ato%5D=50000",
                                 10000, 200000, "used")
    assert url.startswith("https://www.olx.kz/elektronika/alma-ata/q-1070-ti/?")
    assert "filter_float_price%3Afrom%5D=10000" in url and "filter_float_price%3Ato%5D=200000" in url
    assert "filter_enum_state%5D%5B0%5D=used" in url and "50000" not in url
    assert "filter_enum_state" not in olx_feed.apply_filters(url, None, None, "")


def test_title_shows_condition():
    url = olx_feed.build_search_url("iphone 13", "alma-ata", None, 200000, "new")
    assert olx_feed.watch_title(url) == "«iphone 13» · Алматы · новое · до 200 000 ₸"


def test_job_cards_without_d_prefix_are_parsed():
    """У вакансий ссылки /obyavlenie/rabota/… — без /d/, и раньше их не видно было вовсе."""
    html_page = ('<span data-testid="total-count">Мы нашли 1 объявление</span><div data-testid="listing-grid">'
                 '<div data-cy="l-card"><a href="/obyavlenie/rabota/elektronschik-IDraBal.html"><h4>Электронщик</h4></a></div></div>')
    [card] = olx_watcher.extract_listings("https://www.olx.kz/rabota/", html_page, 60)
    assert card.ad_id == "IDraBal"


def test_preview_parses_total_categories_and_exclusions(monkeypatch):
    html_page = ('<span data-testid="total-count">Мы нашли 2 объявления</span>'
                 '<div data-testid="category-count-links"><a href="/elektronika/alma-ata/q-1070-ti/">Электроника 2</a></div>'
                 '<div data-testid="listing-grid">'
                 '<div data-cy="l-card"><a href="/d/obyavlenie/a-IDa.html"><h4>GTX 1070 Ti</h4><p data-testid="ad-price">60 000 тг.</p></a></div>'
                 '<div data-cy="l-card"><a href="/d/obyavlenie/b-IDb.html"><h4>Компьютер с 1070 Ti</h4><p data-testid="ad-price">180 000 тг.</p></a></div>'
                 '</div>')

    class Session:
        def get(self, url, timeout):
            return type("R", (), {"text": html_page, "raise_for_status": lambda self: None})()

    url = olx_feed.build_search_url("1070 ti", "alma-ata", None, 200000)
    p = olx_feed.preview(Session(), url, "компьютер")
    assert p["total"] == 2 and p["median"] == 180000
    assert p["categories"][0]["name"] == "Электроника" and p["categories"][0]["count"] == 2
    assert "/elektronika/alma-ata/q-1070-ti/" in p["categories"][0]["url"]
    assert "filter_float_price%3Ato%5D=200000" in p["categories"][0]["url"]  # фильтры сохранились
    assert [i["excluded"] for i in p["items"]] == [False, True]


def test_parse_search_roundtrip():
    url = olx_feed.apply_filters("https://www.olx.kz/elektronika/alma-ata/q-1070-ti/", 10000, 200000, "used")
    form = olx_feed.parse_search(url, "«1070 ti» · Электроника · Алматы · б/у · 10 000 ₸–200 000 ₸")
    assert form["mode"] == "builder" and form["query"] == "1070 ti" and form["city"] == "alma-ata"
    assert (form["low"], form["high"], form["state"]) == ("10000", "200000", "used")
    assert form["crumbs"] == [{"name": "Электроника"}] and form["base"].endswith("/elektronika/alma-ata/q-1070-ti/")
    # Пересборка из формы даёт тот же поиск.
    assert olx_feed.apply_filters(form["base"], 10000, 200000, "used") == url
    link = olx_feed.parse_search("https://www.olx.kz/transport/legkovye-avtomobili/bmw/?search%5Border%5D=created_at%3Adesc")
    assert link["mode"] == "link" and link["link"] == "https://www.olx.kz/transport/legkovye-avtomobili/bmw/"


def test_category_names_from_olx_page():
    from bs4 import BeautifulSoup
    page = ('<div data-testid="breadcrumbs"><a href="/">Главная</a><a href="/elektronika/">Электроника</a>'
            '<a href="/elektronika/kompyutery-i-komplektuyuschie/">Компьютеры и комплектующие</a>'
            '<a href="/elektronika/kompyutery-i-komplektuyuschie/alm/">Компьютеры и комплектующие Алматинская область</a></div>'
            '<h1>Компьютеры - Алматинская область - cmp 30hx</h1>')
    url = "https://www.olx.kz/elektronika/kompyutery-i-komplektuyuschie/alm/q-cmp-30hx/"
    names = olx_feed.category_names(BeautifulSoup(page, "html.parser"), url)
    assert olx_feed.watch_title(url, names) == "«cmp 30hx» · Компьютеры и комплектующие · Алматинская область"
    leaf = ('<div data-testid="breadcrumbs"><a href="/transport/">Транспорт</a>'
            '<a href="/transport/legkovye-avtomobili/">Легковые автомобили</a></div><h1>Продажа BMW (БМВ)</h1>')
    url = "https://www.olx.kz/transport/legkovye-avtomobili/bmw/"
    assert olx_feed.watch_title(url, olx_feed.category_names(BeautifulSoup(leaf, "html.parser"), url)) == \
        "Легковые автомобили · Продажа BMW (БМВ)"


def test_fetch_title_falls_back_when_olx_fails():
    class Broken:
        def get(self, *a, **kw):
            raise olx_watcher.requests.RequestException("down")
    assert olx_feed.fetch_title(Broken(), "https://www.olx.kz/transport/legkovye-avtomobili/bmw/") == "legkovye-avtomobili · bmw"
