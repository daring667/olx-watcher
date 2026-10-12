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
    assert "📸 Я оплатил — отправить чек" in buttons(calls)
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
