"""Публичный режим: подписки по ссылкам, оценка рынка, доступ."""
import time

import pytest

import olx_feed
import olx_watcher

OWNER = "12345"
CONFIG = {"feed": {"enabled": True, "trial_days": 3, "max_watches": 2,
                   "min_samples": 3, "seed_pages": 1, "url_delay_seconds": 0}}
SEARCH = "https://www.olx.kz/d/elektronika/telefony/alma-ata/q-iphone-13/?search%5Bfilter_float_price%3Ato%5D=200000"


@pytest.fixture
def bot(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(olx_watcher, "telegram_request",
                        lambda token, method, **kw: calls.append((method, kw)) or {"result": {"message_id": 1}})
    storage = olx_watcher.Storage(tmp_path / "feed.sqlite3")
    olx_feed.ensure_schema(storage.connection)
    yield storage, calls
    storage.close()


def say(storage, chat_id, text, config=CONFIG):
    update = {"update_id": 1, "message": {"message_id": 7, "chat": {"id": int(chat_id), "username": f"u{chat_id}"},
                                          "text": text}}
    olx_watcher.handle_update("token", storage, update, OWNER, config)


def texts(calls):
    return [kw.get("text", "") for method, kw in calls if method == "sendMessage"]


# --- Ссылки -----------------------------------------------------------------
def test_normalize_forces_newest_first_and_drops_page():
    url = olx_feed.normalize_search_url(
        "смотри https://m.olx.kz/d/transport/alma-ata/?page=3&utm_source=tg&search%5Bdist%5D=30 ага")
    assert url.startswith("https://www.olx.kz/d/transport/alma-ata/?")
    assert "page=" not in url and "utm_" not in url
    assert "search%5Border%5D=created_at%3Adesc" in url


def test_same_search_with_different_param_order_is_one_url():
    a = olx_feed.normalize_search_url("https://www.olx.kz/d/x/?a=1&b=2")
    b = olx_feed.normalize_search_url("https://www.olx.kz/d/x?b=2&a=1")
    assert a == b


@pytest.mark.parametrize("text", [
    "https://www.olx.kz/d/obyavlenie/iphone-13-IDqRnup.html",   # одно объявление
    "https://kolesa.kz/cars/",
    "просто текст",
])
def test_not_a_search_url(text):
    assert olx_feed.normalize_search_url(text) is None


def test_watch_title_is_readable():
    title = olx_feed.watch_title(olx_feed.normalize_search_url(SEARCH))
    assert "«iphone 13»" in title and "alma-ata" in title and "200000" in title


# --- Оценка рынка -----------------------------------------------------------
@pytest.mark.parametrize("price, marker", [
    (70000, "🔥 На 30%"), (98000, "✅ Около рынка"), (130000, "💸 На 30%"), (20000, "⚠️"),
])
def test_market_line(price, marker):
    assert olx_feed.market_line(price, 100000, 10, 8).startswith(marker)


def test_market_line_needs_enough_samples():
    assert "мало данных" in olx_feed.market_line(70000, 100000, 3, 8)


def test_median_ignores_typo_outlier(bot):
    storage, _ = bot
    cards = [olx_watcher.Listing(f"ID{i}", "u", "t", p) for i, p in
             enumerate([100000, 110000, 90000, 105000, 256560000])]
    olx_feed.store_cards(storage.connection, "s", cards)
    median, count = olx_feed.market(storage.connection, "s", 30)
    assert median == 105000 and count == 5


# --- Подписки и доступ ------------------------------------------------------
def test_stranger_gets_trial_and_can_add_search(bot):
    storage, calls = bot
    say(storage, 777, "/start")
    assert "Пробный доступ" in texts(calls)[-1]
    say(storage, 777, SEARCH)
    assert "Слежу" in texts(calls)[-1]
    rows = storage.connection.execute("SELECT * FROM watches WHERE chat_id='777'").fetchall()
    assert len(rows) == 1 and "created_at%3Adesc" in rows[0]["search_url"]


def test_stranger_never_reaches_owner_commands(bot):
    storage, calls = bot
    say(storage, 777, "/favorites")
    say(storage, 777, "/grant 777 365")
    assert calls == []
    user = storage.connection.execute("SELECT * FROM users WHERE chat_id='777'").fetchone()
    assert user is None


def test_public_mode_off_keeps_strangers_out(bot):
    storage, calls = bot
    say(storage, 777, SEARCH, config={})
    assert calls == []


def test_expired_user_cannot_add_search(bot):
    storage, calls = bot
    say(storage, 777, "/start")
    storage.connection.execute("UPDATE users SET paid_until=? WHERE chat_id='777'", (int(time.time()) - 1,))
    say(storage, 777, SEARCH)
    assert "закончился" in texts(calls)[-1]
    assert storage.connection.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 0


def test_watch_limit(bot):
    storage, calls = bot
    for path in ("a", "b", "c"):
        say(storage, 777, f"https://www.olx.kz/d/{path}/")
    assert "максимум за 2" in texts(calls)[-1]


def test_owner_has_no_limit_and_no_expiry(bot):
    storage, calls = bot
    for path in ("a", "b", "c"):
        say(storage, OWNER, f"https://www.olx.kz/d/{path}/")
    assert storage.connection.execute("SELECT COUNT(*) FROM watches WHERE chat_id=?", (OWNER,)).fetchone()[0] == 3


def test_grant_extends_access(bot):
    storage, calls = bot
    say(storage, 777, "/start")
    before = storage.connection.execute("SELECT paid_until FROM users WHERE chat_id='777'").fetchone()[0]
    say(storage, OWNER, "/grant @u777 30")
    after = storage.connection.execute("SELECT paid_until FROM users WHERE chat_id='777'").fetchone()[0]
    assert after - before == 30 * 86400
    assert any("продлён" in t for t in texts(calls))


def test_delete_button_only_removes_own_watch(bot):
    storage, calls = bot
    say(storage, 777, SEARCH)
    watch_id = storage.connection.execute("SELECT id FROM watches").fetchone()[0]
    stranger_click = {"update_id": 2, "callback_query": {
        "id": "c", "data": f"f:del:{watch_id}", "message": {"message_id": 1, "chat": {"id": 888}}}}
    olx_watcher.handle_update("token", storage, stranger_click, OWNER, CONFIG)
    assert storage.connection.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 1
    own_click = {**stranger_click, "callback_query": {**stranger_click["callback_query"],
                                                      "message": {"message_id": 1, "chat": {"id": 777}}}}
    olx_watcher.handle_update("token", storage, own_click, OWNER, CONFIG)
    assert storage.connection.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 0


def test_owner_note_with_link_stays_a_note(bot):
    """Заметка владельца со ссылкой не должна превращаться в подписку."""
    storage, _ = bot
    storage.set_setting(f"pending_note:{OWNER}", "ID1")
    say(storage, OWNER, "продавец дал ссылку https://www.olx.kz/d/x/")
    assert storage.connection.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 0


# --- Опрос ------------------------------------------------------------------
def test_poll_sends_only_new_cards_with_market_and_discount_filter(bot, monkeypatch):
    storage, calls = bot
    connection = storage.connection
    say(storage, 777, SEARCH)
    say(storage, 778, SEARCH)
    connection.execute("UPDATE users SET min_discount=20 WHERE chat_id='778'")
    url = connection.execute("SELECT search_url FROM watches").fetchone()[0]
    pages = [[olx_watcher.Listing(f"OLD{i}", "u", "старое", 100000) for i in range(5)]]
    monkeypatch.setattr(olx_feed, "fetch_cards", lambda session, search_url, page=1: pages[-1])
    cfg = olx_feed.settings(CONFIG)

    assert olx_feed.poll_url(connection, None, "token", url, cfg, {OWNER}) == 0   # базовый снимок
    calls.clear()
    pages.append(pages[0] + [olx_watcher.Listing("NEW1", "https://olx.kz/n1", "iPhone 13", 90000),
                             olx_watcher.Listing("NEW2", "https://olx.kz/n2", "iPhone 13 дёшево", 70000)])
    sent = olx_feed.poll_url(connection, None, "token", url, cfg, {OWNER})
    recipients = [kw["chat_id"] for method, kw in calls if method == "sendMessage"]
    # 777 получает обе, 778 — только ту, что на 30% дешевле рынка.
    assert sent == 3 and sorted(recipients) == ["777", "777", "778"]
    assert any("🔥 На 30%" in kw["text"] for _, kw in calls)
    # Повторный опрос той же страницы ничего не шлёт.
    calls.clear()
    assert olx_feed.poll_url(connection, None, "token", url, cfg, {OWNER}) == 0


def test_blocked_user_is_marked(bot, monkeypatch):
    storage, _ = bot
    say(storage, 777, SEARCH)

    def forbidden(*a, **kw):
        raise olx_watcher.requests.RequestException("403 Client Error: Forbidden")

    monkeypatch.setattr(olx_watcher, "telegram_request", forbidden)
    user = storage.connection.execute(
        "SELECT w.id watch_id, u.* FROM watches w JOIN users u USING(chat_id)").fetchone()
    assert olx_feed.send_card(storage.connection, "t", user,
                              olx_watcher.Listing("ID1", "u", "t", 1), "") is False
    assert storage.connection.execute("SELECT blocked FROM users").fetchone()[0] == 1


def test_card_price_not_glued_to_title():
    """«iPhone 17 Pro Max 256» + «560 000 тг.» склеивалось в 256 560 000 ₸."""
    page = ('<div data-cy="l-card"><a href="/d/obyavlenie/iphone-IDr8UhN.html">'
            '<div data-testid="ad-card-title"><h4>Iphone 17 pro max 256</h4>'
            '<p data-testid="ad-price">560 000 тг.</p></div></a></div>')
    [card] = olx_watcher.extract_listings("https://www.olx.kz/d/x/", page, 60)
    assert card.price_kzt == 560000


@pytest.mark.parametrize("entry", olx_feed.PUBLIC_COMMANDS)
def test_public_commands_match_telegram_rules(entry):
    import re
    assert re.fullmatch(r"[a-z0-9_]{1,32}", entry["command"])
