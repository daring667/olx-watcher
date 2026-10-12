"""Публичный режим: любой пользователь присылает боту ссылку на выдачу OLX.

Бот раз в пару минут читает первую страницу каждой такой выдачи (отсортированной
«сначала новые») и присылает подписчикам только что появившиеся объявления с
оценкой «дешевле / дороже рынка». Рынок — медиана цен этой же выдачи за
последние 30 дней: раз пользователь сам выставил фильтры, объявления в ней
сопоставимы между собой, и отдельная модель товара для каждой категории не нужна.

Одинаковые ссылки разных пользователей читаются одним запросом — нагрузка на
OLX растёт с числом разных поисков, а не с числом подписчиков.

Режим видеокарт владельца это не трогает: свои таблицы, свой поток.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
import statistics
import threading
import time
from datetime import datetime
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import requests
from curl_cffi import requests as impersonated

# Через модуль, а не `from … import`: тесты подменяют olx_watcher.telegram_request,
# и подмена должна действовать и здесь.
import olx_watcher as w

DAY = 86400

DEFAULTS = {
    "enabled": False,
    "poll_seconds": 120,
    "url_delay_seconds": 2,
    "seed_pages": 3,
    "trial_days": 3,
    "max_watches": 3,
    "min_samples": 8,
    "window_days": 30,
    "proxy": "",
    "payment_text": "Подписка платная. Напишите владельцу бота, чтобы продлить доступ.",
}

# Сортировка «сначала новые» обязательна: читаем только первую страницу, и без
# неё свежие объявления оказались бы где-то в глубине выдачи.
ORDER_KEY, ORDER_VALUE = "search[order]", "created_at:desc"
OLX_URL = re.compile(r"https?://(?:www\.|m\.)?olx\.kz/[^\s<>\"']+", re.I)

PUBLIC_COMMANDS = [
    {"command": "start", "description": "Как пользоваться ботом"},
    {"command": "list", "description": "Мои поиски"},
    {"command": "discount", "description": "Только дешевле рынка: /discount 15"},
    {"command": "status", "description": "Подписка и оплата"},
    {"command": "help", "description": "Помощь"},
]

HELP_TEXT = (
    "<b>Как пользоваться</b>\n"
    "1. Откройте OLX, выберите категорию, город и фильтры (цена, состояние…).\n"
    "2. Скопируйте ссылку из адресной строки и пришлите её сюда.\n"
    "3. Новые объявления по этому поиску будут приходить в течение 1–2 минут "
    "после публикации — с оценкой, насколько цена ниже или выше рынка.\n"
    "Чем точнее поиск («iPhone 13 128GB», а не просто «iPhone»), тем точнее оценка.\n\n"
    "/list — мои поиски и их отключение\n"
    "/discount 15 — присылать только объявления на 15% и больше дешевле рынка "
    "(<code>/discount 0</code> — все подряд)\n"
    "/status — подписка и оплата"
)


def settings(config: dict | None) -> dict:
    cfg = {**DEFAULTS, **((config or {}).get("feed") or {})}
    # Реквизиты — в .env, а не в config.yaml: репозиторий публичный, и номер
    # Kaspi лежал бы на GitHub. .env построчный, поэтому перенос строки
    # записывается как \n.
    payment = os.environ.get("FEED_PAYMENT_TEXT", "").strip()
    if payment:
        cfg["payment_text"] = payment.replace("\\n", "\n")
    return cfg


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          chat_id TEXT PRIMARY KEY, username TEXT, first_name TEXT,
          joined_at INTEGER NOT NULL, paid_until INTEGER NOT NULL,
          min_discount INTEGER NOT NULL DEFAULT 0,
          blocked INTEGER NOT NULL DEFAULT 0, expired_notified INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS watches (
          id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
          search_url TEXT NOT NULL, title TEXT NOT NULL, created_at INTEGER NOT NULL,
          UNIQUE(chat_id, search_url)
        );
        CREATE TABLE IF NOT EXISTS feed_ads (
          search_url TEXT NOT NULL, ad_id TEXT NOT NULL, title TEXT, price_kzt INTEGER,
          url TEXT, first_seen INTEGER NOT NULL, PRIMARY KEY(search_url, ad_id)
        );
        CREATE TABLE IF NOT EXISTS feed_urls (
          search_url TEXT PRIMARY KEY, baseline_done INTEGER NOT NULL DEFAULT 0,
          last_polled INTEGER
        );
        CREATE INDEX IF NOT EXISTS feed_ads_market ON feed_ads(search_url, first_seen);
    """)
    connection.commit()


# --- Ссылки -----------------------------------------------------------------
def normalize_search_url(raw: str) -> str | None:
    """Приводит ссылку выдачи к одному виду; None — если это не выдача.

    Порядок параметров сортируется, номер страницы и якорь отбрасываются:
    иначе две одинаковые выдачи разных пользователей читались бы дважды.
    """
    match = OLX_URL.search(raw or "")
    if not match:
        return None
    parts = urlsplit(match.group(0).rstrip(".,;)"))
    if "/obyavlenie/" in parts.path or "/d/" not in parts.path + "/":
        return None
    query = {k: v for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k != "page" and not k.startswith("utm_")}
    query[ORDER_KEY] = ORDER_VALUE
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    return urlunsplit(("https", "www.olx.kz", path, urlencode(sorted(query.items())), ""))


def watch_title(search_url: str) -> str:
    """Короткое имя поиска для списка: «q: iphone 13 · alma-ata»."""
    parts = urlsplit(search_url)
    segments = [unquote(s) for s in parts.path.split("/") if s and s != "d"]
    words = [s[2:].replace("-", " ") for s in segments if s.startswith("q-")]
    rest = [s for s in segments if not s.startswith("q-")]
    label = " · ".join((["«" + words[0] + "»"] if words else []) + rest[-2:])
    query = dict(parse_qsl(parts.query))
    low, high = query.get("search[filter_float_price:from]"), query.get("search[filter_float_price:to]")
    if low or high:
        label += f" · {low or 0}–{high or '∞'} ₸"
    return label or "поиск OLX"


# --- Пользователи и доступ --------------------------------------------------
def register_user(connection: sqlite3.Connection, chat: dict, trial_days: int) -> sqlite3.Row:
    chat_id = str(chat.get("id"))
    now = int(time.time())
    connection.execute(
        "INSERT OR IGNORE INTO users(chat_id,username,first_name,joined_at,paid_until) VALUES(?,?,?,?,?)",
        (chat_id, chat.get("username"), chat.get("first_name"), now, now + trial_days * DAY))
    # Пользователь, заблокировавший и снова запустивший бота, должен снова
    # получать уведомления.
    connection.execute("UPDATE users SET blocked=0, username=COALESCE(?,username) WHERE chat_id=?",
                       (chat.get("username"), chat_id))
    connection.commit()
    return connection.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()


def is_active(user: sqlite3.Row | None, owner_ids: set[str]) -> bool:
    if user is None:
        return False
    return user["chat_id"] in owner_ids or user["paid_until"] > time.time()


def grant(connection: sqlite3.Connection, target: str, days: int) -> sqlite3.Row | None:
    """Продлевает доступ от более поздней из дат: сейчас или текущий конец."""
    target = target.strip()
    column = "username" if target.startswith("@") else "chat_id"
    user = connection.execute(f"SELECT * FROM users WHERE {column}=?", (target.lstrip("@"),)).fetchone()
    if not user:
        return None
    until = max(int(time.time()), user["paid_until"]) + days * DAY
    connection.execute("UPDATE users SET paid_until=?, expired_notified=0 WHERE chat_id=?",
                       (until, user["chat_id"]))
    connection.commit()
    return connection.execute("SELECT * FROM users WHERE chat_id=?", (user["chat_id"],)).fetchone()


def date_text(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


# --- Оценка рынка -----------------------------------------------------------
def market(connection: sqlite3.Connection, search_url: str, window_days: int) -> tuple[int | None, int]:
    """Медиана цен выдачи за окно и число объявлений, по которым она посчитана.

    Медиана, а не среднее: одно «iPhone за 1 ₸» или опечатка с лишним нулём
    сдвинули бы среднее на десятки процентов, медиану — почти нет.
    """
    since = int(time.time()) - window_days * DAY
    prices = [row[0] for row in connection.execute(
        "SELECT price_kzt FROM feed_ads WHERE search_url=? AND first_seen>=? AND price_kzt>0",
        (search_url, since))]
    return (int(statistics.median(prices)) if prices else None), len(prices)


def discount_percent(price: int | None, median: int | None) -> int | None:
    if not price or not median:
        return None
    return round((median - price) / median * 100)


def market_line(price: int | None, median: int | None, count: int, min_samples: int) -> str:
    if price is None:
        return ""
    if median is None or count < min_samples:
        return f"📊 Пока мало данных для оценки рынка ({count} объявл.)"
    pct = discount_percent(price, median)
    basis = f"медиана {median:,} ₸ по {count} объявл."
    if pct >= 60:
        # Слишком хорошая цена чаще всего приманка мошенника или опечатка.
        return f"⚠️ На {pct}% дешевле рынка — подозрительно, проверьте продавца ({basis})"
    if pct >= 10:
        return f"🔥 На {pct}% дешевле рынка ({basis})"
    if pct <= -10:
        return f"💸 На {-pct}% дороже рынка ({basis})"
    return f"✅ Около рынка ({basis})"


# --- Опрос OLX --------------------------------------------------------------
def make_session(proxy: str = "") -> impersonated.Session:
    session = impersonated.Session(impersonate=w.IMPERSONATE,
                                   proxies={"http": proxy, "https": proxy} if proxy else None)
    session.headers.update(w.HEADERS)
    return session


def fetch_cards(session, search_url: str, page: int = 1) -> list[w.Listing]:
    response = session.get(w.page_url(search_url, page), timeout=25)
    response.raise_for_status()
    return w.extract_listings(search_url, response.text, 60)


def store_cards(connection: sqlite3.Connection, search_url: str, cards: list[w.Listing]) -> None:
    now = int(time.time())
    connection.executemany(
        "INSERT OR IGNORE INTO feed_ads(search_url,ad_id,title,price_kzt,url,first_seen) VALUES(?,?,?,?,?,?)",
        [(search_url, c.ad_id, c.title, c.price_kzt, c.url, now) for c in cards])
    connection.commit()


def active_subscribers(connection: sqlite3.Connection, search_url: str, owner_ids: set[str]) -> list[sqlite3.Row]:
    rows = connection.execute(
        "SELECT w.id watch_id, u.* FROM watches w JOIN users u ON u.chat_id=w.chat_id "
        "WHERE w.search_url=? AND u.blocked=0", (search_url,)).fetchall()
    return [row for row in rows if is_active(row, owner_ids)]


def poll_url(connection: sqlite3.Connection, session, token: str, search_url: str,
             cfg: dict, owner_ids: set[str]) -> int:
    """Читает одну выдачу; возвращает число разосланных уведомлений."""
    state = connection.execute("SELECT * FROM feed_urls WHERE search_url=?", (search_url,)).fetchone()
    cards = fetch_cards(session, search_url)
    connection.execute("INSERT OR IGNORE INTO feed_urls(search_url) VALUES(?)", (search_url,))
    connection.execute("UPDATE feed_urls SET last_polled=? WHERE search_url=?", (int(time.time()), search_url))
    if not state or not state["baseline_done"]:
        # Первый проход ничего не рассылает: всё на странице уже опубликовано
        # раньше. Заодно берём ещё несколько страниц, чтобы оценка рынка
        # работала сразу, а не через неделю накопления.
        store_cards(connection, search_url, cards)
        for page in range(2, int(cfg["seed_pages"]) + 1):
            try:
                more = fetch_cards(session, search_url, page)
            except w.NETWORK_ERRORS:
                break
            if not more:
                break
            store_cards(connection, search_url, more)
            time.sleep(float(cfg["url_delay_seconds"]))
        connection.execute("UPDATE feed_urls SET baseline_done=1 WHERE search_url=?", (search_url,))
        connection.commit()
        logging.info("Публичный поиск: базовый снимок %s", search_url)
        return 0
    known = {row[0] for row in connection.execute(
        "SELECT ad_id FROM feed_ads WHERE search_url=? AND ad_id IN (%s)" % ",".join("?" * len(cards)),
        [search_url, *[c.ad_id for c in cards]])} if cards else set()
    fresh = [c for c in cards if c.ad_id not in known]
    if not fresh:
        connection.commit()
        return 0
    # Рынок считаем до добавления новинок: иначе объявление сравнивалось бы
    # само с собой.
    median, count = market(connection, search_url, int(cfg["window_days"]))
    store_cards(connection, search_url, fresh)
    subscribers = active_subscribers(connection, search_url, owner_ids)
    sent = 0
    for card in fresh:
        pct = discount_percent(card.price_kzt, median) if count >= int(cfg["min_samples"]) else None
        line = market_line(card.price_kzt, median, count, int(cfg["min_samples"]))
        for user in subscribers:
            if user["min_discount"] > 0 and (pct is None or pct < user["min_discount"]):
                continue
            if send_card(connection, token, user, card, line):
                sent += 1
    return sent


def card_caption(card: w.Listing, line: str) -> str:
    price = f"{card.price_kzt:,} ₸" if card.price_kzt is not None else "не указана"
    return (f"🆕 <b>{html.escape(card.title or 'Новое объявление')}</b>\n"
            f"Цена: <b>{price}</b>\n" + (f"{line}\n" if line else "") +
            f'\n<a href="{html.escape(card.url, quote=True)}">Открыть на OLX</a>')


def send_card(connection: sqlite3.Connection, token: str, user: sqlite3.Row, card: w.Listing, line: str) -> bool:
    keyboard = json.dumps({"inline_keyboard": [[
        {"text": "🔕 Отключить этот поиск", "callback_data": f"f:del:{user['watch_id']}"}]]}, ensure_ascii=False)
    caption = card_caption(card, line)
    try:
        try:
            if not card.image_url:
                raise requests.RequestException("нет фото")
            w.telegram_request(token, "sendPhoto", chat_id=user["chat_id"], photo=card.image_url,
                               caption=caption, parse_mode="HTML", reply_markup=keyboard)
        except requests.RequestException as error:
            if is_blocked_error(error):
                raise
            w.telegram_request(token, "sendMessage", chat_id=user["chat_id"], text=caption,
                               parse_mode="HTML", reply_markup=keyboard)
        return True
    except requests.RequestException as error:
        if is_blocked_error(error):
            # Пользователь заблокировал бота: дальше слать бессмысленно, а
            # Telegram за упорные попытки начинает ограничивать весь бот.
            connection.execute("UPDATE users SET blocked=1 WHERE chat_id=?", (user["chat_id"],))
            connection.commit()
        logging.warning("Публичный поиск: не отправлено %s: %s", user["chat_id"], w.safe_error(error))
        return False


def is_blocked_error(error: Exception) -> bool:
    text = str(error).lower()
    return "blocked" in text or "deactivated" in text or "chat not found" in text or "403" in text


def notify_expired(connection: sqlite3.Connection, token: str, cfg: dict, owner_ids: set[str]) -> None:
    """Один раз сообщает об окончании доступа — с реквизитами для оплаты."""
    rows = connection.execute(
        "SELECT * FROM users WHERE paid_until<? AND expired_notified=0 AND blocked=0 "
        "AND chat_id IN (SELECT chat_id FROM watches)", (int(time.time()),)).fetchall()
    for user in rows:
        if user["chat_id"] in owner_ids:
            continue
        try:
            w.telegram_request(token, "sendMessage", chat_id=user["chat_id"], parse_mode="HTML",
                               text="⏳ <b>Доступ закончился</b> — новые объявления больше не приходят.\n\n"
                                    + cfg["payment_text"])
        except requests.RequestException as error:
            logging.warning("Не удалось сообщить об окончании доступа: %s", w.safe_error(error))
        connection.execute("UPDATE users SET expired_notified=1 WHERE chat_id=?", (user["chat_id"],))
        connection.commit()


def feed_loop(token: str, config: dict, owner_ids: set[str], stop: threading.Event) -> None:
    cfg = settings(config)
    storage = w.Storage()
    connection = storage.connection
    ensure_schema(connection)
    session = make_session(cfg["proxy"])
    try:
        while not stop.is_set():
            started = time.monotonic()
            urls = [row[0] for row in connection.execute("SELECT DISTINCT search_url FROM watches")]
            sent = 0
            for search_url in urls:
                if stop.is_set():
                    break
                if not active_subscribers(connection, search_url, owner_ids):
                    continue  # у всех подписчиков истёк доступ — OLX зря не дёргаем
                try:
                    sent += poll_url(connection, session, token, search_url, cfg, owner_ids)
                except w.NETWORK_ERRORS as error:
                    storage.record_error("Публичный поиск OLX", error)
                    logging.warning("Публичный поиск: %s — %s", search_url, error)
                except Exception as error:
                    storage.record_error("Публичный поиск", error)
                    logging.exception("Публичный поиск: ошибка %s", search_url)
                stop.wait(float(cfg["url_delay_seconds"]))
            notify_expired(connection, token, cfg, owner_ids)
            if urls:
                logging.info("Публичный поиск: выдач %d, отправлено %d, %.1f сек.",
                             len(urls), sent, time.monotonic() - started)
            stop.wait(max(5.0, float(cfg["poll_seconds"]) - (time.monotonic() - started)))
    finally:
        storage.close()


# --- Telegram ---------------------------------------------------------------
def send(token: str, chat_id, text: str, **extra) -> None:
    w.telegram_request(token, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                       disable_web_page_preview=True, **extra)


def status_text(user: sqlite3.Row, cfg: dict, owner_ids: set[str]) -> str:
    if user["chat_id"] in owner_ids:
        return "Вы владелец бота — доступ без ограничений."
    if is_active(user, owner_ids):
        return f"✅ Доступ активен до <b>{date_text(user['paid_until'])}</b>.\n\n{cfg['payment_text']}"
    return f"⏳ Доступ закончился {date_text(user['paid_until'])}.\n\n{cfg['payment_text']}"


def list_reply(connection: sqlite3.Connection, chat_id: str) -> tuple[str, dict]:
    rows = connection.execute("SELECT * FROM watches WHERE chat_id=? ORDER BY id", (chat_id,)).fetchall()
    if not rows:
        return "Поисков пока нет. Пришлите ссылку на выдачу OLX — и я начну следить.", {"inline_keyboard": []}
    lines = ["<b>Ваши поиски</b>"]
    buttons = []
    for index, row in enumerate(rows, 1):
        lines.append(f'{index}. <a href="{html.escape(row["search_url"], quote=True)}">'
                     f'{html.escape(row["title"])}</a>')
        buttons.append([{"text": f"🗑 Удалить {index}", "callback_data": f"f:del:{row['id']}"}])
    return "\n".join(lines), {"inline_keyboard": buttons}


def add_watch(token: str, connection: sqlite3.Connection, user: sqlite3.Row, text: str,
              cfg: dict, owner_ids: set[str]) -> None:
    chat_id = user["chat_id"]
    if "/obyavlenie/" in text:
        send(token, chat_id, "Это ссылка на одно объявление. Нужна ссылка на <b>выдачу</b>: "
                             "откройте категорию с фильтрами и скопируйте адрес страницы со списком.")
        return
    search_url = normalize_search_url(text)
    if not search_url:
        send(token, chat_id, "Не похоже на ссылку выдачи OLX.kz. Откройте категорию на olx.kz, "
                             "выставьте фильтры и пришлите адрес страницы.")
        return
    if not is_active(user, owner_ids):
        send(token, chat_id, status_text(user, cfg, owner_ids))
        return
    count = connection.execute("SELECT COUNT(*) FROM watches WHERE chat_id=?", (chat_id,)).fetchone()[0]
    limit = int(cfg["max_watches"])
    if chat_id not in owner_ids and count >= limit:
        send(token, chat_id, f"Можно следить максимум за {limit} поисками. Удалите лишний в /list.")
        return
    title = watch_title(search_url)
    added = connection.execute(
        "INSERT OR IGNORE INTO watches(chat_id,search_url,title,created_at) VALUES(?,?,?,?)",
        (chat_id, search_url, title, int(time.time()))).rowcount
    connection.commit()
    if not added:
        send(token, chat_id, "За этим поиском я уже слежу.")
        return
    send(token, chat_id,
         f"✅ Слежу: <b>{html.escape(title)}</b>\n\n"
         "Сначала запомню текущую выдачу, затем буду присылать только новые объявления — "
         f"обычно в течение {max(1, int(cfg['poll_seconds']) // 60)}–{max(2, int(cfg['poll_seconds']) // 30)} мин. "
         "после публикации.")


def handle_public_update(token: str, connection: sqlite3.Connection, update: dict,
                         config: dict | None, owner_ids: set[str]) -> bool:
    """Обрабатывает то, что относится к публичному режиму. True — обработано.

    Команды владельца (/favorites, /search…) для чужих чатов не существуют:
    на них бот молчит, чтобы не намекать на закрытые данные.
    """
    cfg = settings(config)
    if not cfg["enabled"]:
        return False
    ensure_schema(connection)
    callback = update.get("callback_query")
    if callback:
        action, _, payload = callback.get("data", "").partition(":")
        if action != "f":
            return False
        chat_id = str(callback["message"]["chat"]["id"])
        verb, _, watch_id = payload.partition(":")
        if verb == "del" and watch_id.isdigit():
            removed = connection.execute("DELETE FROM watches WHERE id=? AND chat_id=?",
                                         (int(watch_id), chat_id)).rowcount
            connection.commit()
            w.telegram_request(token, "answerCallbackQuery", callback_query_id=callback["id"],
                               text="Поиск отключён" if removed else "Этот поиск уже удалён")
            if callback["message"].get("text", "").startswith("Ваши поиски"):
                text, keyboard = list_reply(connection, chat_id)
                w.telegram_request(token, "editMessageText", chat_id=chat_id,
                                   message_id=callback["message"]["message_id"], text=text,
                                   parse_mode="HTML", disable_web_page_preview=True,
                                   reply_markup=json.dumps(keyboard, ensure_ascii=False))
            else:
                w.telegram_request(token, "editMessageReplyMarkup", chat_id=chat_id,
                                   message_id=callback["message"]["message_id"],
                                   reply_markup=json.dumps({"inline_keyboard": []}))
        return True
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    if not chat.get("id"):
        return False
    chat_id = str(chat["id"])
    text = (message.get("text") or message.get("caption") or "").strip()
    command = text.split()[0].split("@", 1)[0].lower() if text.startswith("/") else ""
    is_owner = chat_id in owner_ids
    if command == "/start" and not is_owner:
        user = register_user(connection, chat, int(cfg["trial_days"]))
        send(token, chat_id, f"👋 Я слежу за OLX и присылаю новые объявления раньше, чем их увидят другие.\n\n"
                             f"{HELP_TEXT}\n\n🎁 Пробный доступ до <b>{date_text(user['paid_until'])}</b>.")
        return True
    if command == "/help" and not is_owner:
        send(token, chat_id, HELP_TEXT)
        return True
    if OLX_URL.search(text) and not command:
        add_watch(token, connection, register_user(connection, chat, int(cfg["trial_days"])),
                  text, cfg, owner_ids)
        return True
    if command in {"/list", "/status", "/discount"}:
        user = register_user(connection, chat, int(cfg["trial_days"]))
        if command == "/list":
            reply, keyboard = list_reply(connection, chat_id)
            send(token, chat_id, reply, reply_markup=json.dumps(keyboard, ensure_ascii=False))
        elif command == "/status":
            send(token, chat_id, status_text(user, cfg, owner_ids))
        else:
            argument = text[len("/discount"):].strip().rstrip("%")
            if not argument.isdigit() or int(argument) > 90:
                send(token, chat_id, f"Сейчас: <b>{user['min_discount']}%</b>. "
                                     "Пример: <code>/discount 15</code> — только на 15%+ дешевле рынка, "
                                     "<code>/discount 0</code> — все новые объявления.")
            else:
                connection.execute("UPDATE users SET min_discount=? WHERE chat_id=?", (int(argument), chat_id))
                connection.commit()
                send(token, chat_id, "Буду присылать все новые объявления." if argument == "0" else
                     f"Буду присылать только объявления на {argument}% и больше дешевле рынка.")
        return True
    if is_owner:
        return False  # остальное — обычные команды владельца
    if message.get("photo") or message.get("document"):
        # Чек об оплате: пересылаем владельцу с готовой командой продления.
        for owner in owner_ids:
            try:
                w.telegram_request(token, "forwardMessage", chat_id=owner, from_chat_id=chat_id,
                                   message_id=message["message_id"])
                send(token, owner, f"От {html.escape(chat.get('first_name') or '')} "
                                   f"@{html.escape(chat.get('username') or '—')}: "
                                   f"<code>/grant {chat_id} 30</code>")
            except requests.RequestException as error:
                logging.warning("Не удалось переслать чек владельцу: %s", w.safe_error(error))
        send(token, chat_id, "Спасибо! Передал владельцу — доступ продлят после проверки.")
        return True
    if text and not command:
        send(token, chat_id, "Пришлите ссылку на выдачу OLX — я начну следить за новыми объявлениями. /help")
        return True
    return True  # неизвестные и владельческие команды — молча


# --- Команды владельца ------------------------------------------------------
ADMIN_COMMANDS = [
    {"command": "users", "description": "Пользователи публичного режима"},
    {"command": "grant", "description": "Продлить доступ: /grant ID 30"},
]


def handle_admin_command(token: str, connection: sqlite3.Connection, message: dict) -> bool:
    text = (message.get("text") or "").strip()
    command = text.split()[0].split("@", 1)[0].lower() if text.startswith("/") else ""
    chat_id = message["chat"]["id"]
    if command not in {"/grant", "/users"}:
        return False
    ensure_schema(connection)
    if command == "/grant":
        parts = text.split()[1:]
        if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
            send(token, chat_id, "Использование: <code>/grant 123456789 30</code> или "
                                 "<code>/grant @username 30</code> (дни; отрицательное число — убавить).")
            return True
        user = grant(connection, parts[0], int(parts[1]))
        if not user:
            send(token, chat_id, "Такого пользователя нет — он должен хотя бы раз написать боту /start.")
            return True
        send(token, chat_id, f"✅ {html.escape(parts[0])}: доступ до {date_text(user['paid_until'])}")
        try:
            send(token, user["chat_id"], f"✅ Доступ продлён до <b>{date_text(user['paid_until'])}</b>. Спасибо!")
        except requests.RequestException as error:
            logging.warning("Не удалось уведомить пользователя о продлении: %s", w.safe_error(error))
        return True
    now = int(time.time())
    total = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    active = connection.execute("SELECT COUNT(*) FROM users WHERE paid_until>?", (now,)).fetchone()[0]
    urls = connection.execute("SELECT COUNT(DISTINCT search_url) FROM watches").fetchone()[0]
    rows = connection.execute(
        "SELECT u.*, (SELECT COUNT(*) FROM watches w WHERE w.chat_id=u.chat_id) n "
        "FROM users u ORDER BY joined_at DESC LIMIT 20").fetchall()
    lines = [f"<b>Пользователи:</b> {total}, с доступом {active}; разных выдач: {urls}"]
    for row in rows:
        mark = "✅" if row["paid_until"] > now else "⏳"
        blocked = " 🚫" if row["blocked"] else ""
        lines.append(f"{mark} <code>{row['chat_id']}</code> @{html.escape(row['username'] or '—')} · "
                     f"до {date_text(row['paid_until'])} · поисков {row['n']}{blocked}")
    send(token, chat_id, "\n".join(lines))
    return True
