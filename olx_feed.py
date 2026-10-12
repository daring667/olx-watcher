"""Публичный режим: любой пользователь присылает боту ссылку на выдачу OLX.

Бот раз в пару минут читает первую страницу каждой такой выдачи (отсортированной
«сначала новые») и присылает подписчикам только что появившиеся объявления с
оценкой «дешевле / дороже рынка». Рынок — медиана цен этой же выдачи за
последние 30 дней: раз пользователь сам выставил фильтры, объявления в ней
сопоставимы между собой, и отдельная модель товара для каждой категории не нужна.

Одинаковые ссылки разных пользователей читаются одним запросом — нагрузка на
OLX растёт с числом разных поисков, а не с числом подписчиков.

Управление — кнопками, а не командами: меню живёт в одном сообщении и
перерисовывается на месте, чат не засоряется. Команды остались как запасной
вход. Вся работа с базой — в feed_db.FeedDB.

Режим видеокарт владельца это не трогает: свои таблицы, свой поток.
"""
from __future__ import annotations

import html
import json
import logging
import math
import os
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import requests
from curl_cffi import requests as impersonated

# Через модуль, а не `from … import`: тесты подменяют olx_watcher.telegram_request,
# и подмена должна действовать и здесь.
import olx_watcher as w
from feed_db import DAY, FeedDB, is_excluded

DEFAULTS = {
    "enabled": False,
    "brand": "Радар объявлений",
    "poll_seconds": 120,
    "url_delay_seconds": 2,
    "seed_pages": 3,
    "trial_days": 3,
    "max_watches": 3,
    "min_samples": 8,
    "window_days": 30,
    "price_kzt": 5000,
    "period_days": 30,
    "proxy": "",
    # HTTPS-адрес Mini App. Пустой — кнопки приложения в боте нет.
    "miniapp_url": "",
    "payment_text": "Подписка платная. Напишите владельцу бота, чтобы продлить доступ.",
}

# Сортировка «сначала новые» обязательна: читаем только первую страницу, и без
# неё свежие объявления оказались бы где-то в глубине выдачи.
ORDER_KEY, ORDER_VALUE = "search[order]", "created_at:desc"
OLX_URL = re.compile(r"https?://(?:www\.|m\.)?olx\.kz/[^\s<>\"']+", re.I)
FILTER_STEPS = (0, 10, 20, 30, 40)
# Готовый набор для запросов вроде «iphone 13»: OLX отдаёт по нему и дисплеи, и чехлы.
ACCESSORY_WORDS = "чехол, стекло, плёнка, дисплей, экран, запчасти, корпус, коробка, зарядк, кабель, на запчасти"

# Города для конструктора поиска: адрес OLX → название. Каждый адрес проверен
# запросом к OLX; «list» — весь Казахстан.
CITIES = {
    "list": "Весь Казахстан", "alma-ata": "Алматы", "astana": "Астана", "shymkent": "Шымкент",
    "karaganda": "Караганда", "aktobe": "Актобе", "taraz": "Тараз", "pavlodar": "Павлодар",
    "ust-kamenogorsk": "Усть-Каменогорск", "semey": "Семей", "atyrau": "Атырау",
    "kostanay": "Костанай", "kyzylorda": "Кызылорда", "uralsk": "Уральск",
    "petropavlovsk": "Петропавловск", "aktau": "Актау", "turkestan": "Туркестан",
    "temirtau": "Темиртау", "kokshetau": "Кокшетау", "taldykorgan": "Талдыкорган",
    "ekibastuz": "Экибастуз",
}
# Разделы сайта, которые не являются выдачей объявлений.
NOT_LISTING = {"obyavlenie", "myaccount", "account", "adding", "favorites", "chat", "help",
               "oferta", "login", "register", "payments", "messages"}

# Защита от флуда: больше FLOOD_LIMIT сообщений и нажатий за FLOOD_WINDOW
# секунд от одного чата бот молча пропускает. Иначе один человек мог бы
# упереть бота в лимиты Telegram (429), и уведомления опоздали бы у всех.
FLOOD_LIMIT, FLOOD_WINDOW = 20, 60
_recent: dict[str, deque] = defaultdict(deque)


def flooding(chat_id: str, limit: int = FLOOD_LIMIT, window: int = FLOOD_WINDOW) -> bool:
    now_ts = time.monotonic()
    hits = _recent[chat_id]
    while hits and now_ts - hits[0] > window:
        hits.popleft()
    if len(hits) >= limit:
        return True
    hits.append(now_ts)
    return False


# Чеки: один на проверке за раз и не больше трёх в сутки — каждый чек
# пересылается владельцу, и без лимита им можно было заспамить личку.
RECEIPTS_PER_DAY = 3

PUBLIC_COMMANDS = [
    {"command": "start", "description": "Главное меню"},
    {"command": "list", "description": "Мои поиски"},
    {"command": "help", "description": "Как это работает"},
]


def settings(config: dict | None) -> dict:
    cfg = {**DEFAULTS, **((config or {}).get("feed") or {})}
    # Реквизиты — в .env, а не в config.yaml: репозиторий публичный, и номер
    # Kaspi лежал бы на GitHub. .env построчный, поэтому перенос строки
    # записывается как \n.
    payment = os.environ.get("FEED_PAYMENT_TEXT", "").strip()
    if payment:
        cfg["payment_text"] = payment.replace("\\n", "\n")
    return cfg


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
    # OLX отдаёт выдачу и как /d/alma-ata/…, и как /alma-ata/… (сейчас
    # перенаправляет на второй вариант), поэтому /d/ не требуем.
    segments = [s for s in parts.path.split("/") if s and s != "d"]
    if not segments or "/obyavlenie/" in parts.path or segments[0] in NOT_LISTING:
        return None
    query = {k: v for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k != "page" and not k.startswith("utm_")}
    query[ORDER_KEY] = ORDER_VALUE
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    return urlunsplit(("https", "www.olx.kz", path, urlencode(sorted(query.items())), ""))


def build_search_url(query: str, city: str = "list", price_from: int | None = None,
                     price_to: int | None = None) -> str | None:
    """Ссылка выдачи из конструктора: запрос, город и цена — без копирования с OLX."""
    words = re.sub(r"[^\w]+", "-", (query or "").strip().casefold()).strip("-")
    if not words or city not in CITIES:
        return None
    url = f"https://www.olx.kz/{city}/q-{words}/"
    query_params = []
    if price_from:
        query_params.append(f"search%5Bfilter_float_price%3Afrom%5D={int(price_from)}")
    if price_to:
        query_params.append(f"search%5Bfilter_float_price%3Ato%5D={int(price_to)}")
    return normalize_search_url(url + ("?" + "&".join(query_params) if query_params else ""))


def watch_title(search_url: str) -> str:
    """Короткое имя поиска для списка: «q: iphone 13 · Алматы»."""
    parts = urlsplit(search_url)
    segments = [unquote(s) for s in parts.path.split("/") if s and s not in {"d", "list"}]
    words = [s[2:].replace("-", " ") for s in segments if s.startswith("q-")]
    rest = [CITIES.get(s, s) for s in segments if not s.startswith("q-")]
    label = " · ".join((["«" + words[0] + "»"] if words else []) + rest[-2:])
    query = dict(parse_qsl(parts.query))
    low, high = query.get("search[filter_float_price:from]"), query.get("search[filter_float_price:to]")
    if low and high:
        label += f" · {money(int(float(low)))}–{money(int(float(high)))}"
    elif low or high:
        label += f" · {'от' if low else 'до'} {money(int(float(low or high)))}"
    return label or "поиск OLX"


# --- Доступ и даты ----------------------------------------------------------
def is_active(user, owner_ids: set[str]) -> bool:
    if user is None:
        return False
    return user["chat_id"] in owner_ids or user["paid_until"] > time.time()


def date_text(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


def left_text(ts: int) -> str:
    """«ещё 3 дн.» / «ещё 5 ч.» — сколько осталось до конца доступа."""
    seconds = ts - time.time()
    if seconds >= DAY:
        return f"ещё {math.ceil(seconds / DAY)} дн."
    return f"ещё {max(1, math.ceil(seconds / 3600))} ч."


def money(value: int | None) -> str:
    return f"{value:,} ₸".replace(",", " ") if value is not None else "—"


# --- Оценка рынка -----------------------------------------------------------
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
    basis = f"медиана {money(median)} по {count} объявл."
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


def poll_url(db: FeedDB, session, token: str, search_url: str, cfg: dict, owner_ids: set[str]) -> int:
    """Читает одну выдачу; возвращает число разосланных уведомлений."""
    cards = fetch_cards(session, search_url)
    if not db.url_baseline_done(search_url):
        # Первый проход ничего не рассылает: всё на странице уже опубликовано
        # раньше. Заодно берём ещё несколько страниц, чтобы оценка рынка
        # работала сразу, а не через неделю накопления.
        db.store_cards(search_url, cards)
        for page in range(2, int(cfg["seed_pages"]) + 1):
            try:
                more = fetch_cards(session, search_url, page)
            except w.NETWORK_ERRORS:
                break
            if not more:
                break
            db.store_cards(search_url, more)
            time.sleep(float(cfg["url_delay_seconds"]))
        db.mark_polled(search_url, baseline_done=True)
        logging.info("Публичный поиск: базовый снимок %s", search_url)
        return 0
    db.mark_polled(search_url)
    known = db.known_ads(search_url, [c.ad_id for c in cards])
    fresh = [c for c in cards if c.ad_id not in known]
    if not fresh:
        return 0
    # Рынок считаем до добавления новинок: иначе объявление сравнивалось бы
    # само с собой.
    median, count = db.market(search_url, int(cfg["window_days"]))
    db.store_cards(search_url, fresh)
    subscribers = [u for u in db.subscribers(search_url) if is_active(u, owner_ids)]
    min_samples = int(cfg["min_samples"])
    sent = 0
    for card in fresh:
        pct = discount_percent(card.price_kzt, median) if count >= min_samples else None
        line = market_line(card.price_kzt, median, count, min_samples)
        for user in subscribers:
            if user["min_discount"] > 0 and (pct is None or pct < user["min_discount"]):
                continue
            if is_excluded(card.title, user["exclude_words"]):
                continue
            if send_card(db, token, user, card, line, pct):
                sent += 1
    return sent


def card_caption(card: w.Listing, line: str) -> str:
    return (f"🆕 <b>{html.escape(card.title or 'Новое объявление')}</b>\n"
            f"💰 <b>{money(card.price_kzt) if card.price_kzt is not None else 'цена не указана'}</b>\n"
            + (f"{line}\n" if line else ""))


def card_markup(delivery_id: int, url: str, favorite: bool, paused: bool) -> str:
    return markup(
        [link("🔗 Открыть на OLX", url)],
        [button("★ В избранном" if favorite else "⭐ В избранное", f"u:fav:{delivery_id}"),
         button("▶️ Возобновить поиск" if paused else "⏸ Пауза поиска", f"u:np:{delivery_id}")],
    )


def send_card(db: FeedDB, token: str, user, card: w.Listing, line: str, pct: int | None = None) -> bool:
    delivery_id = db.record_delivery(user["chat_id"], user["watch_id"], card, pct)
    keyboard = card_markup(delivery_id, card.url, False, False)
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
                               parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)
        return True
    except requests.RequestException as error:
        db.c.execute("DELETE FROM deliveries WHERE id=?", (delivery_id,))
        db.c.execute("UPDATE watches SET sent_count=MAX(sent_count-1,0) WHERE id=?", (user["watch_id"],))
        db.c.commit()
        if is_blocked_error(error):
            # Пользователь заблокировал бота: дальше слать бессмысленно, а
            # Telegram за упорные попытки начинает ограничивать весь бот.
            db.set_user(user["chat_id"], blocked=1)
        logging.warning("Публичный поиск: не отправлено %s: %s", user["chat_id"], w.safe_error(error))
        return False


def is_blocked_error(error: Exception) -> bool:
    text = str(error).lower()
    return ("blocked" in text or "deactivated" in text or "chat not found" in text
            or text.startswith("403"))


def notify_access(db: FeedDB, token: str, cfg: dict, owner_ids: set[str]) -> None:
    """Напоминание за сутки до конца доступа и сообщение, когда он кончился."""
    renew = markup([button("💳 Продлить доступ", "u:sub")])
    for user in db.expiring_users(DAY):
        if user["chat_id"] in owner_ids:
            continue
        safe_send(db, token, user["chat_id"],
                  f"⏰ <b>Доступ закончится {date_text(user['paid_until'])}</b>\n"
                  "Продлите подписку, чтобы объявления продолжали приходить без перерыва.",
                  reply_markup=renew)
        db.set_user(user["chat_id"], expiring_notified=1)
    for user in db.expired_users():
        if user["chat_id"] in owner_ids:
            continue
        safe_send(db, token, user["chat_id"],
                  "⏳ <b>Доступ закончился</b> — новые объявления больше не приходят.\n"
                  "Ваши поиски и избранное сохранены: продлите подписку, и всё продолжит работать.",
                  reply_markup=renew)
        db.set_user(user["chat_id"], expired_notified=1)
        db.log(user["chat_id"], "expired")


def is_olx_block(error: Exception) -> bool:
    """403/429 от OLX — не сбой сети, а ограничение: долбить дальше нельзя."""
    status = getattr(getattr(error, "response", None), "status_code", None)
    return status in {403, 429} or any(code in str(error) for code in (" 403", " 429"))


def next_backoff(current: int, blocked: bool) -> int:
    """Пауза в минутах: 2 → 4 → 8 … до 60 при блокировках подряд, 0 после успеха."""
    return min(60, current * 2 if current else 2) if blocked else 0


def notify_owners(db: FeedDB, token: str, owner_ids: set[str], text: str) -> None:
    for owner in owner_ids:
        safe_send(db, token, owner, text)


def feed_loop(token: str, config: dict, owner_ids: set[str], stop: threading.Event) -> None:
    cfg = settings(config)
    storage = w.Storage()
    db = FeedDB(storage.connection)
    session = make_session(cfg["proxy"])
    backoff = 0  # минут паузы после блокировки OLX; 0 — работаем как обычно
    try:
        while not stop.is_set():
            started = time.monotonic()
            urls = db.polled_urls()
            sent = polled = 0
            blocked = False
            for search_url in urls:
                if stop.is_set():
                    break
                if not any(is_active(u, owner_ids) for u in db.subscribers(search_url)):
                    continue  # у всех подписчиков истёк доступ — OLX зря не дёргаем
                polled += 1
                try:
                    sent += poll_url(db, session, token, search_url, cfg, owner_ids)
                except w.NETWORK_ERRORS as error:
                    storage.record_error("Публичный поиск OLX", error)
                    logging.warning("Публичный поиск: %s — %s", search_url, error)
                    if is_olx_block(error):
                        blocked = True
                        break  # остальные выдачи сейчас тоже не откроются — не злим OLX дальше
                except Exception as error:
                    storage.record_error("Публичный поиск", error)
                    logging.exception("Публичный поиск: ошибка %s", search_url)
                stop.wait(float(cfg["url_delay_seconds"]))
            try:
                notify_access(db, token, cfg, owner_ids)
            except Exception:
                logging.exception("Публичный поиск: напоминания о доступе")
            if polled:
                logging.info("Публичный поиск: выдач %d, отправлено %d, %.1f сек.",
                             polled, sent, time.monotonic() - started)
            backoff = next_backoff(backoff, blocked)
            if blocked:
                logging.warning("OLX ограничил запросы — пауза %d мин.", backoff)
                if backoff == 2:  # первая блокировка подряд — сообщаем один раз
                    notify_owners(db, token, owner_ids,
                                  "⚠️ <b>OLX ограничил запросы</b> с сервера. Делаю паузу и повторяю всё реже "
                                  "(до часа). Если повторяется — увеличьте feed.poll_seconds или задайте feed.proxy.")
                stop.wait(backoff * 60)
                continue
            stop.wait(max(5.0, float(cfg["poll_seconds"]) - (time.monotonic() - started)))
    finally:
        storage.close()


# --- Кнопки и экраны --------------------------------------------------------
def button(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def link(text: str, url: str) -> dict:
    return {"text": text, "url": url}


def markup(*rows) -> str:
    return json.dumps({"inline_keyboard": [list(row) for row in rows if row]}, ensure_ascii=False)


HOME = button("🏠 Меню", "u:home")


def send(token: str, chat_id, text: str, **extra) -> dict:
    return w.telegram_request(token, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                              disable_web_page_preview=True, **extra)


def safe_send(db: FeedDB, token: str, chat_id, text: str, **extra) -> bool:
    try:
        send(token, chat_id, text, **extra)
        return True
    except requests.RequestException as error:
        if is_blocked_error(error):
            db.set_user(chat_id, blocked=1)
        logging.warning("Не отправлено %s: %s", chat_id, w.safe_error(error))
        return False


def show(token: str, chat_id, screen: tuple[str, str], message: dict | None = None) -> None:
    """Показывает экран меню: перерисовывает то же сообщение, если можно.

    Меню живёт в одном сообщении, и переходы не засоряют чат. Сообщение с
    фото (карточку объявления) в текст не превратить — тогда шлём новое.
    """
    text, keyboard = screen
    if message and message.get("text") is not None:
        try:
            w.telegram_request(token, "editMessageText", chat_id=chat_id, message_id=message["message_id"],
                               text=text, parse_mode="HTML", disable_web_page_preview=True,
                               reply_markup=keyboard)
            return
        except requests.RequestException as error:
            if "not modified" in str(error):
                return
    send(token, chat_id, text, reply_markup=keyboard)


def access_line(user, owner_ids: set[str]) -> str:
    if user["chat_id"] in owner_ids:
        return "👑 Владелец — без ограничений"
    if not is_active(user, owner_ids):
        return "⏳ <b>Доступ закончился</b> — новые объявления не приходят"
    kind = "✅ Подписка" if user["plan"] == "paid" else "🎁 Пробный доступ"
    return f"{kind} до {date_text(user['paid_until'])} ({left_text(user['paid_until'])})"


def filter_text(value: int) -> str:
    return "все новые объявления" if not value else f"только на {value}%+ дешевле рынка"


def home_screen(db: FeedDB, user, cfg: dict, owner_ids: set[str], greeting: str = "") -> tuple[str, str]:
    chat_id = user["chat_id"]
    watches = db.watches(chat_id)
    paused = sum(1 for row in watches if row["paused"])
    owner = chat_id in owner_ids
    limit = "∞" if owner else cfg["max_watches"]
    text = (greeting or f"🔎 <b>{html.escape(cfg['brand'])}</b>\n"
            "Новые объявления с OLX через 1–2 минуты после публикации.\n") + (
        f"\n{access_line(user, owner_ids)}\n"
        f"📋 Поиски: <b>{len(watches)}</b> из {limit}" + (f" (на паузе {paused})" if paused else "") + "\n"
        f"🎯 Присылаю: {filter_text(user['min_discount'])}\n"
        f"⭐ В избранном: {db.count('favorites', chat_id)}")
    rows = []
    if cfg["miniapp_url"]:
        rows.append([{"text": "📱 Открыть приложение", "web_app": {"url": cfg["miniapp_url"]}}])
    if not is_active(user, owner_ids):
        rows.append([button("💳 Продлить доступ", "u:sub")])
    rows += [
        [button("➕ Добавить поиск", "u:add"), button("📋 Мои поиски", "u:list")],
        [button("⭐ Избранное", "u:favs"), button("🎯 Фильтр выгоды", "u:filter")],
        [button("💳 Подписка", "u:sub"), button("❓ Как это работает", "u:help")],
    ]
    if owner:
        rows.append([button("👑 Админка", "a:home"), button("🎮 Видеокарты", "u:gpu")])
    return text, markup(*rows)


def add_screen(db: FeedDB, user, cfg: dict, owner_ids: set[str]) -> tuple[str, str]:
    used = db.count("watches", user["chat_id"])
    owner = user["chat_id"] in owner_ids
    free = "без ограничений" if owner else f"{max(0, int(cfg['max_watches']) - used)} из {cfg['max_watches']}"
    return ("➕ <b>Новый поиск</b>\n\n"
            "1️⃣ Откройте OLX, выберите категорию и город.\n"
            "2️⃣ Выставьте фильтры: цена, состояние, ключевые слова.\n"
            "3️⃣ Скопируйте ссылку из адресной строки и пришлите её сюда, в чат.\n\n"
            "💡 Чем точнее поиск («iPhone 13 128GB», а не просто «iPhone»), тем точнее оценка цены.\n\n"
            f"Свободно мест: {free}",
            markup([link("🌐 Открыть OLX", "https://www.olx.kz/")], [button("⬅️ Назад", "u:home")]))


def list_screen(db: FeedDB, user, cfg: dict, owner_ids: set[str]) -> tuple[str, str]:
    watches = db.watches(user["chat_id"])
    if not watches:
        return ("📋 <b>Мои поиски</b>\n\nПока пусто. Добавьте первый поиск — и я начну присылать "
                "новые объявления.", markup([button("➕ Добавить поиск", "u:add")], [HOME]))
    rows = [[button(f"{'⏸' if row['paused'] else '🟢'} {row['title'][:48]}", f"u:w:{row['id']}")]
            for row in watches]
    limit = "∞" if user["chat_id"] in owner_ids else cfg["max_watches"]
    return (f"📋 <b>Мои поиски</b> ({len(watches)} из {limit})\n\n🟢 — активен, ⏸ — на паузе.\n"
            "Нажмите на поиск, чтобы открыть его.",
            markup(*rows, [button("➕ Добавить", "u:add"), HOME]))


def watch_screen(db: FeedDB, watch, cfg: dict) -> tuple[str, str]:
    median, count = db.market(watch["search_url"], int(cfg["window_days"]))
    market = (f"медиана <b>{money(median)}</b> по {count} объявл. за {cfg['window_days']} дн."
              if median and count >= int(cfg["min_samples"]) else f"пока мало данных ({count} объявл.)")
    last = f"\n🕐 Последнее: {date_text(watch['last_sent_at'])}" if watch["last_sent_at"] else ""
    status = "⏸ на паузе" if watch["paused"] else "🟢 активен"
    toggle = button("▶️ Возобновить", f"u:wr:{watch['id']}") if watch["paused"] \
        else button("⏸ Пауза", f"u:wp:{watch['id']}")
    excluded = f"\n🚫 Исключено: {html.escape(watch['exclude_words'])}" if watch["exclude_words"] else ""
    return (f"🔍 <b>{html.escape(watch['title'])}</b>\n\n"
            f"Статус: {status}\n📨 Прислано объявлений: {watch['sent_count']}{last}\n"
            f"📊 Рынок: {market}{excluded}\n"
            f"📅 Добавлен: {date_text(watch['created_at'])}",
            markup([link("🌐 Открыть выдачу на OLX", watch["search_url"])],
                   [toggle, button("🗑 Удалить", f"u:wd:{watch['id']}")],
                   [button("🚫 Слова-исключения", f"u:wx:{watch['id']}")],
                   [button("⬅️ Мои поиски", "u:list"), HOME]))


def favorites_screen(db: FeedDB, user) -> tuple[str, str]:
    favorites = db.favorites(user["chat_id"])
    if not favorites:
        return ("⭐ <b>Избранное</b>\n\nПока пусто. Нажимайте «⭐ В избранное» под объявлениями, "
                "чтобы не потерять интересные варианты.", markup([HOME]))
    lines = [f"⭐ <b>Избранное</b> ({len(favorites)})\n"]
    for index, row in enumerate(favorites, 1):
        lines.append(f'{index}. <a href="{html.escape(row["url"] or "", quote=True)}">'
                     f'{html.escape((row["title"] or "—")[:60])}</a> — {money(row["price_kzt"])}')
    deletes = [button(f"🗑 {i}", f"u:fd:{row['id']}") for i, row in enumerate(favorites, 1)]
    rows = [deletes[i:i + 5] for i in range(0, len(deletes), 5)]
    return "\n".join(lines) + "\n\nКнопки 🗑 — убрать из избранного.", markup(*rows, [HOME])


def filter_screen(user) -> tuple[str, str]:
    current = user["min_discount"]
    options = [button(("✓ " if value == current else "") + ("Все" if not value else f"от {value}%"),
                      f"u:f:{value}") for value in FILTER_STEPS]
    return ("🎯 <b>Фильтр выгоды</b>\n\n"
            "Цену каждого нового объявления я сравниваю с медианой по вашему поиску за 30 дней.\n\n"
            "• <b>Все</b> — присылаю каждое новое объявление.\n"
            "• <b>от 20%</b> — только те, что минимум на 20% дешевле рынка.\n\n"
            f"Сейчас: <b>{filter_text(current)}</b>",
            markup(options, [HOME]))


def sub_screen(db: FeedDB, user, cfg: dict, owner_ids: set[str]) -> tuple[str, str]:
    if user["chat_id"] in owner_ids:
        return "💳 <b>Подписка</b>\n\n👑 Вы владелец бота — доступ без ограничений.", markup([HOME])
    pending = any(p["status"] == "pending" for p in db.payments_of(user["chat_id"]))
    text = (f"💳 <b>Подписка</b>\n\n{access_line(user, owner_ids)}\n\n"
            f"{cfg['payment_text']}\n\n"
            "<b>Как продлить:</b> переведите оплату, нажмите «📸 Я оплатил» и пришлите скриншот чека.")
    if pending:
        text += "\n\n⏳ Ваш чек уже на проверке — обычно это занимает до часа."
    return text, markup([button("📸 Я оплатил — отправить чек", "u:paid")], [HOME])


def delete_screen() -> tuple[str, str]:
    return ("🗑 <b>Удалить мои данные?</b>\n\nСотрутся поиски, избранное, история присланных объявлений "
            "и профиль. Оплаченный доступ не сохранится. Отменить это нельзя.",
            markup([button("🗑 Да, удалить всё", "u:delok")], [button("⬅️ Отмена", "u:home")]))


def help_screen(cfg: dict) -> tuple[str, str]:
    return (f"❓ <b>Как работает {html.escape(cfg['brand'])}</b>\n\n"
            "1. Вы присылаете ссылку на поиск OLX со своими фильтрами.\n"
            "2. Я проверяю его каждые пару минут и присылаю <b>только новые</b> объявления — "
            "раньше, чем их заметят другие.\n"
            "3. К каждому объявлению — оценка цены: 🔥 дешевле рынка, ✅ около рынка, "
            "💸 дороже, ⚠️ подозрительно дёшево (часто это мошенники).\n\n"
            "<b>Полезное</b>\n"
            "• ⭐ под объявлением — сохранить в избранное.\n"
            "• ⏸ под объявлением — поставить поиск на паузу.\n"
            "• 🎯 Фильтр выгоды — присылать только то, что заметно дешевле рынка.\n\n"
            "Независимый сервис, не связан с OLX. Свои данные можно удалить в любой момент.",
            markup([button("➕ Добавить поиск", "u:add")], [button("🗑 Удалить мои данные", "u:del"), HOME]))


# --- Действия пользователя ----------------------------------------------------
def add_watch(token: str, db: FeedDB, user, text: str, cfg: dict, owner_ids: set[str]) -> None:
    chat_id = user["chat_id"]
    retry = markup([button("❓ Как добавить поиск", "u:add")], [HOME])
    if "/obyavlenie/" in text:
        send(token, chat_id, "🤔 Это ссылка на одно объявление. Нужна ссылка на <b>список</b>: "
                             "откройте категорию с фильтрами и скопируйте адрес страницы.", reply_markup=retry)
        return
    search_url = normalize_search_url(text)
    if not search_url:
        send(token, chat_id, "🤔 Не похоже на ссылку поиска OLX.kz.", reply_markup=retry)
        return
    if not is_active(user, owner_ids):
        show(token, chat_id, sub_screen(db, user, cfg, owner_ids))
        return
    limit = int(cfg["max_watches"])
    if chat_id not in owner_ids and db.count("watches", chat_id) >= limit:
        send(token, chat_id, f"📋 Можно следить максимум за {limit} поисками. Удалите ненужный, "
                             "чтобы добавить новый.", reply_markup=markup([button("📋 Мои поиски", "u:list")], [HOME]))
        return
    title = watch_title(search_url)
    watch_id = db.add_watch(chat_id, search_url, title)
    if watch_id is None:
        existing = db.watch_by_url(chat_id, search_url)
        send(token, chat_id, "👌 За этим поиском я уже слежу.",
             reply_markup=markup([button("🔍 Открыть поиск", f"u:w:{existing['id']}")], [HOME]))
        return
    minutes = max(1, int(cfg["poll_seconds"]) // 60)
    send(token, chat_id,
         f"✅ <b>Слежу:</b> {html.escape(title)}\n\n"
         f"Сейчас запомню текущие объявления, а дальше буду присылать только новые — "
         f"примерно через {minutes}–{minutes * 2} мин. после публикации.",
         reply_markup=markup([button("🔍 Открыть поиск", f"u:w:{watch_id}"), HOME]))


def handle_receipt(token: str, db: FeedDB, user, chat: dict, message: dict, cfg: dict, owner_ids: set[str]) -> None:
    """Чек об оплате: в базу как ожидающий, владельцу — с кнопками решения."""
    if any(p["status"] == "pending" for p in db.payments_of(user["chat_id"])):
        send(token, user["chat_id"], "⏳ Ваш чек уже на проверке — я напишу, как только доступ продлят.",
             reply_markup=markup([HOME]))
        return
    if db.receipts_since(user["chat_id"], int(time.time()) - DAY) >= RECEIPTS_PER_DAY:
        send(token, user["chat_id"], "Слишком много чеков за сутки. Если что-то не так — попробуйте завтра.",
             reply_markup=markup([HOME]))
        return
    photo = (message.get("photo") or [{}])[-1].get("file_id") or (message.get("document") or {}).get("file_id")
    payment_id = db.create_receipt(user["chat_id"], photo)
    db.set_user(user["chat_id"], state="", state_data="")
    name = html.escape(" ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or "—")
    who = f"{name} @{html.escape(chat.get('username') or '—')} (<code>{user['chat_id']}</code>)"
    for owner in owner_ids:
        try:
            w.telegram_request(token, "forwardMessage", chat_id=owner, from_chat_id=user["chat_id"],
                               message_id=message["message_id"])
            send(token, owner, f"💳 <b>Чек #{payment_id}</b> от {who}\n{access_line(user, set())}",
                 reply_markup=markup(
                     [button(f"✅ +{cfg['period_days']} дн. · {money(int(cfg['price_kzt']))}", f"a:ok:{payment_id}"),
                      button("❌ Отклонить", f"a:no:{payment_id}")],
                     [button("👤 Пользователь", f"a:u:{user['chat_id']}")]))
        except requests.RequestException as error:
            logging.warning("Не удалось переслать чек владельцу: %s", w.safe_error(error))
    send(token, user["chat_id"], "✅ <b>Чек получен!</b>\nПроверю и включу доступ — обычно в течение часа. "
                                 "Я напишу, как только всё будет готово.", reply_markup=markup([HOME]))


def handle_user_callback(token: str, db: FeedDB, user, callback: dict, verb: str, arg: str,
                         cfg: dict, owner_ids: set[str], storage_setting) -> str:
    """Кнопки пользователя. Возвращает текст всплывающей подсказки."""
    chat_id, message = user["chat_id"], callback["message"]
    number = int(arg) if arg.lstrip("-").isdigit() else None
    if verb == "home":
        show(token, chat_id, home_screen(db, user, cfg, owner_ids), message)
    elif verb == "add":
        show(token, chat_id, add_screen(db, user, cfg, owner_ids), message)
    elif verb == "list":
        show(token, chat_id, list_screen(db, user, cfg, owner_ids), message)
    elif verb in {"w", "wp", "wr", "wd", "wD", "wx", "wxa", "wxc"}:
        watch = db.watch(number, chat_id) if number is not None else None
        if not watch:
            show(token, chat_id, list_screen(db, user, cfg, owner_ids), message)
            return "Этот поиск уже удалён"
        if verb in {"wp", "wr"}:
            db.set_watch_paused(watch["id"], chat_id, verb == "wp")
            db.log(chat_id, "watch_pause" if verb == "wp" else "watch_resume", str(watch["id"]))
            show(token, chat_id, watch_screen(db, db.watch(watch["id"]), cfg), message)
            return "Поиск на паузе" if verb == "wp" else "Поиск снова работает"
        if verb == "wx":
            db.set_user(chat_id, state=f"exclude:{watch['id']}")
            current = watch["exclude_words"] or "нет"
            show(token, chat_id, ("🚫 <b>Слова-исключения</b>\n\n"
                                  "Объявления, в заголовке которых есть эти слова, присылать не буду. "
                                  "Пришлите слова через запятую следующим сообщением.\n\n"
                                  f"Сейчас: <i>{html.escape(current)}</i>",
                                  markup([button("📱 Набор «аксессуары»", f"u:wxa:{watch['id']}")],
                                         [button("🧹 Очистить", f"u:wxc:{watch['id']}"),
                                          button("⬅️ Назад", f"u:w:{watch['id']}")])), message)
            return ""
        if verb in {"wxa", "wxc"}:
            words = "" if verb == "wxc" else ", ".join(filter(None, [watch["exclude_words"], ACCESSORY_WORDS]))
            db.set_watch_exclude(watch["id"], chat_id, words)
            db.set_user(chat_id, state="")
            show(token, chat_id, watch_screen(db, db.watch(watch["id"]), cfg), message)
            return "Исключения очищены" if verb == "wxc" else "Добавлен набор «аксессуары»"
        if verb == "wd":
            show(token, chat_id, (f"🗑 Удалить поиск <b>{html.escape(watch['title'])}</b>?\n\n"
                                  "Уведомления по нему прекратятся. Избранное останется.",
                                  markup([button("✅ Да, удалить", f"u:wD:{watch['id']}"),
                                          button("⬅️ Отмена", f"u:w:{watch['id']}")])), message)
        elif verb == "wD":
            db.delete_watch(watch["id"], chat_id)
            show(token, chat_id, list_screen(db, user, cfg, owner_ids), message)
            return "Поиск удалён"
        else:
            show(token, chat_id, watch_screen(db, watch, cfg), message)
    elif verb == "favs":
        show(token, chat_id, favorites_screen(db, user), message)
    elif verb == "fd" and number is not None:
        db.delete_favorite(number, chat_id)
        show(token, chat_id, favorites_screen(db, user), message)
        return "Убрано из избранного"
    elif verb in {"fav", "np"} and number is not None:
        # Кнопки под карточкой объявления: перерисовываем только кнопки.
        delivery = db.delivery(number, chat_id)
        if not delivery:
            return "Объявление не найдено"
        watch = db.watch(delivery["watch_id"], chat_id) if delivery["watch_id"] else None
        answer = ""
        if verb == "fav":
            if db.is_favorite(chat_id, delivery["ad_id"]):
                db.remove_favorite_ad(chat_id, delivery["ad_id"])
                answer = "Убрано из избранного"
            else:
                db.add_favorite(delivery)
                answer = "⭐ Сохранено в избранное"
        elif watch:
            db.set_watch_paused(watch["id"], chat_id, not watch["paused"])
            answer = "Поиск снова работает" if watch["paused"] else "Поиск на паузе — нажмите ещё раз, чтобы возобновить"
            watch = db.watch(watch["id"])
        else:
            answer = "Этот поиск уже удалён"
        w.telegram_request(token, "editMessageReplyMarkup", chat_id=chat_id, message_id=message["message_id"],
                           reply_markup=card_markup(number, delivery["url"] or "https://www.olx.kz/",
                                                    db.is_favorite(chat_id, delivery["ad_id"]),
                                                    bool(watch and watch["paused"])))
        return answer
    elif verb == "filter":
        show(token, chat_id, filter_screen(user), message)
    elif verb == "f" and number in FILTER_STEPS:
        if number == user["min_discount"]:
            return "Уже выбрано"
        db.set_user(chat_id, min_discount=number)
        db.log(chat_id, "filter", str(number))
        show(token, chat_id, filter_screen(db.user(chat_id)), message)
        return "Сохранено"
    elif verb == "sub":
        if user["state"] == "receipt":
            db.set_user(chat_id, state="")
        show(token, chat_id, sub_screen(db, user, cfg, owner_ids), message)
    elif verb == "paid":
        db.set_user(chat_id, state="receipt")
        show(token, chat_id, ("📸 <b>Пришлите скриншот или фото чека</b> следующим сообщением.\n\n"
                              "Я передам его на проверку и напишу, когда доступ будет продлён.",
                              markup([button("⬅️ Отмена", "u:sub")])), message)
    elif verb == "help":
        show(token, chat_id, help_screen(cfg), message)
    elif verb == "del":
        show(token, chat_id, delete_screen(), message)
    elif verb == "delok":
        db.delete_user(chat_id)
        show(token, chat_id, ("🗑 <b>Ваши данные удалены.</b>\nПоиски, избранное и история стёрты. "
                              "Если захотите вернуться — просто напишите /start.", markup()), message)
        return "Данные удалены"
    elif verb == "gpu" and chat_id in owner_ids:
        show(token, chat_id, ("🎮 <b>Видеокарты</b> — панель управления",
                              json.dumps(w.main_menu(storage_setting), ensure_ascii=False)), message)
    return ""


# --- Админка ----------------------------------------------------------------
def admin_screen(db: FeedDB, cfg: dict) -> tuple[str, str]:
    s = db.stats()
    text = ("👑 <b>Админка</b>\n\n"
            f"👥 Пользователей: <b>{s['users']}</b> (новых сегодня {s['new_today']}, за 7 дн. {s['new_7d']})\n"
            f"🟢 Заходили за 7 дн.: {s['active_7d']}\n"
            f"✅ С доступом: {s['with_access']} — платный {s['paid']}, пробный {s['trial']}\n"
            f"💰 Платили хоть раз: {s['ever_paid']}\n"
            f"🚫 Заблокировали бота: {s['blocked']}\n\n"
            f"📋 Поисков: {s['watches']}, разных выдач в опросе: {s['urls']}\n"
            f"📨 Отправлено: сегодня {s['sent_today']}, за 7 дн. {s['sent_7d']}\n"
            f"⭐ В избранном: {s['favorites']}\n\n"
            f"💵 Выручка за 30 дн.: <b>{money(s['revenue_30d'])}</b> (всего {money(s['revenue_total'])})")
    return text, markup(
        [button("👥 Пользователи", "a:ul:0"), button(f"💳 Чеки ({s['pending']})", "a:pay")],
        [button("📣 Рассылка", "a:bc"), button("🔄 Обновить", "a:home")],
        [HOME])


USERS_PER_PAGE = 8


def users_screen(db: FeedDB, page: int) -> tuple[str, str]:
    total = db.user_count()
    rows = db.users_page(page * USERS_PER_PAGE, USERS_PER_PAGE)
    now_ts = time.time()
    buttons = []
    for row in rows:
        mark = "🚫" if row["blocked"] else ("💰" if row["plan"] == "paid" and row["paid_until"] > now_ts
                                            else "🎁" if row["paid_until"] > now_ts else "⏳")
        name = f"@{row['username']}" if row["username"] else (row["first_name"] or row["chat_id"])
        buttons.append([button(f"{mark} {name[:24]} · до {datetime.fromtimestamp(row['paid_until']):%d.%m} · "
                               f"🔍{row['watches_n']}", f"a:u:{row['chat_id']}")])
    pages = max(1, math.ceil(total / USERS_PER_PAGE))
    nav = []
    if page > 0:
        nav.append(button("◀️", f"a:ul:{page - 1}"))
    if page + 1 < pages:
        nav.append(button("▶️", f"a:ul:{page + 1}"))
    return (f"👥 <b>Пользователи</b> ({total}) · стр. {page + 1}/{pages}\n\n"
            "💰 платный · 🎁 пробный · ⏳ истёк · 🚫 заблокировал бота",
            markup(*buttons, nav, [button("⬅️ Админка", "a:home")]))


def user_card(db: FeedDB, chat_id: str, cfg: dict) -> tuple[str, str]:
    user = db.user(chat_id)
    if not user:
        return "Пользователь не найден.", markup([button("⬅️ Пользователи", "a:ul:0")])
    name = html.escape(" ".join(filter(None, [user["first_name"], user["last_name"]])) or "—")
    watches = db.watches(chat_id)
    payments = db.payments_of(chat_id)
    watch_lines = "\n".join(f"  {'⏸' if row['paused'] else '🟢'} {html.escape(row['title'])} · 📨{row['sent_count']}"
                            for row in watches) or "  —"
    payment_lines = "\n".join(
        f"  {datetime.fromtimestamp(p['created_at']):%d.%m} · {p['method']} · {p['status']} · "
        f"{p['days']} дн. · {money(p['amount_kzt'])}" for p in payments) or "  —"
    seen = date_text(user["last_seen_at"]) if user["last_seen_at"] else "—"
    text = (f"👤 <b>{name}</b> @{html.escape(user['username'] or '—')}\n"
            f"ID: <code>{chat_id}</code>\n"
            f"С нами с {date_text(user['joined_at'])}, заходил {seen}\n"
            f"{access_line(user, set())}" + (" · 🚫 заблокировал бота" if user["blocked"] else "") + "\n"
            f"🎯 Фильтр: {filter_text(user['min_discount'])}\n"
            f"⭐ Избранное: {db.count('favorites', chat_id)} · 📨 получено: {db.count('deliveries', chat_id)}\n\n"
            f"<b>Поиски:</b>\n{watch_lines}\n\n<b>Оплаты:</b>\n{payment_lines}")
    period, price = int(cfg["period_days"]), int(cfg["price_kzt"])
    return text, markup(
        [button(f"💰 +{period} дн. (оплата {money(price)})", f"a:g:{chat_id}:{period}:1")],
        [button("🎁 +3 дня бонус", f"a:g:{chat_id}:3:0"), button("➖ −1 день", f"a:g:{chat_id}:-1:0")],
        [button("⬅️ Пользователи", "a:ul:0"), button("👑 Админка", "a:home")])


def notify_granted(db: FeedDB, token: str, user) -> None:
    safe_send(db, token, user["chat_id"],
              f"✅ <b>Доступ продлён до {date_text(user['paid_until'])}</b>\nСпасибо! Объявления снова приходят.",
              reply_markup=markup([HOME]))


def handle_admin_callback(token: str, db: FeedDB, owner, callback: dict, verb: str, arg: str,
                          cfg: dict) -> str:
    chat_id, message = owner["chat_id"], callback["message"]
    if verb == "home":
        show(token, chat_id, admin_screen(db, cfg), message)
    elif verb == "ul":
        show(token, chat_id, users_screen(db, int(arg) if arg.isdigit() else 0), message)
    elif verb == "u":
        show(token, chat_id, user_card(db, arg, cfg), message)
    elif verb == "g":
        target, _, rest = arg.partition(":")
        days, _, paid = rest.partition(":")
        if not days.lstrip("-").isdigit():
            return ""
        days_n = int(days)
        amount = round(int(cfg["price_kzt"]) * days_n / int(cfg["period_days"])) if paid == "1" else 0
        user = db.grant(target, days_n, by=chat_id, amount_kzt=amount)
        if not user:
            return "Пользователь не найден"
        if days_n > 0:
            notify_granted(db, token, user)
        show(token, chat_id, user_card(db, target, cfg), message)
        return f"Доступ до {date_text(user['paid_until'])}"
    elif verb in {"ok", "no"} and arg.isdigit():
        payment = db.payment(int(arg))
        if not payment or payment["status"] != "pending":
            return "Этот чек уже обработан"
        if verb == "ok":
            user = db.grant(payment["chat_id"], int(cfg["period_days"]), by=chat_id,
                            amount_kzt=int(cfg["price_kzt"]), method="receipt", payment_id=payment["id"])
            notify_granted(db, token, user)
            result = f"✅ Чек #{payment['id']} подтверждён: доступ до {date_text(user['paid_until'])}"
        else:
            db.reject_payment(payment["id"], chat_id)
            safe_send(db, token, payment["chat_id"],
                      "❌ <b>Оплата не подтверждена.</b>\nПроверьте сумму и реквизиты и отправьте чек ещё раз.",
                      reply_markup=markup([button("💳 Подписка", "u:sub")]))
            result = f"❌ Чек #{payment['id']} отклонён"
        show(token, chat_id, (result, markup([button("👤 Пользователь", f"a:u:{payment['chat_id']}"),
                                              button("💳 Чеки", "a:pay")])), message)
        return "Готово"
    elif verb == "pay":
        pending = db.pending_payments()
        rows = [[button(f"✅ #{p['id']} @{p['username'] or p['first_name'] or p['chat_id']}", f"a:ok:{p['id']}"),
                 button("❌", f"a:no:{p['id']}"), button("👤", f"a:u:{p['chat_id']}")] for p in pending[:10]]
        text = ("💳 <b>Чеки на проверке</b>\n\n" + ("Сами чеки — пересланные сообщения выше в чате."
                                                    if pending else "Нет чеков на проверке. 👌"))
        show(token, chat_id, (text, markup(*rows, [button("⬅️ Админка", "a:home")])), message)
    elif verb == "bc":
        db.set_user(chat_id, state="broadcast", state_data="")
        show(token, chat_id, (f"📣 <b>Рассылка</b>\n\nПришлите текст следующим сообщением — "
                              f"получат {len(db.broadcast_targets())} чел. Можно HTML-разметку Telegram.",
                              markup([button("❌ Отмена", "a:bcx")])), message)
    elif verb == "bcx":
        db.set_user(chat_id, state="", state_data="")
        show(token, chat_id, admin_screen(db, cfg), message)
        return "Рассылка отменена"
    elif verb == "bcgo":
        text = owner["state_data"]
        db.set_user(chat_id, state="", state_data="")
        if not text:
            return "Текст рассылки потерян — начните заново"
        targets = db.broadcast_targets()
        delivered = 0
        for target in targets:
            delivered += safe_send(db, token, target, text)
            time.sleep(0.05)  # Telegram ограничивает массовую отправку ~30 сообщениями в секунду
        db.log(chat_id, "broadcast", f"{delivered}/{len(targets)}")
        show(token, chat_id, (f"📣 Рассылка завершена: доставлено {delivered} из {len(targets)}.",
                              markup([button("⬅️ Админка", "a:home")])), message)
    return ""


def setup_menu_button(token: str, config: dict | None) -> None:
    """Кнопка «Приложение» слева от поля ввода — во всех чатах с ботом."""
    cfg = settings(config)
    if not cfg["enabled"] or not cfg["miniapp_url"]:
        return
    try:
        w.telegram_request(token, "setChatMenuButton", menu_button=json.dumps(
            {"type": "web_app", "text": "Приложение", "web_app": {"url": cfg["miniapp_url"]}},
            ensure_ascii=False))
    except requests.RequestException as error:
        logging.warning("Не удалось поставить кнопку приложения: %s", w.safe_error(error))


# --- Вход из Telegram -------------------------------------------------------
class _Settings:
    """Кусочек Storage для w.main_menu(): ему нужен только setting()."""
    def __init__(self, connection) -> None:
        self.connection = connection

    def setting(self, key: str, default: str = "") -> str:
        row = self.connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default


def handle_public_update(token: str, connection, update: dict, config: dict | None,
                         owner_ids: set[str]) -> bool:
    """Обрабатывает то, что относится к публичному режиму. True — обработано.

    Команды владельца (/favorites, /search…) для чужих чатов не существуют:
    на них бот молчит, чтобы не намекать на закрытые данные.
    """
    cfg = settings(config)
    if not cfg["enabled"]:
        return False
    db = FeedDB(connection)
    trial = int(cfg["trial_days"])
    callback = update.get("callback_query")
    if callback:
        prefix, _, payload = callback.get("data", "").partition(":")
        if prefix not in {"f", "u", "a"}:
            return False
        chat = callback["message"]["chat"]
        if str(chat["id"]) not in owner_ids and flooding(str(chat["id"])):
            return True
        user, _ = db.touch_user({**chat, **(callback.get("from") or {}), "id": chat["id"]}, trial)
        verb, _, arg = payload.partition(":")
        answer = ""
        if prefix == "f" and verb == "del" and arg.isdigit():
            # Кнопка из уведомлений первой версии: «Отключить этот поиск».
            removed = db.delete_watch(int(arg), user["chat_id"])
            answer = "Поиск отключён" if removed else "Этот поиск уже удалён"
            w.telegram_request(token, "editMessageReplyMarkup", chat_id=user["chat_id"],
                               message_id=callback["message"]["message_id"],
                               reply_markup=json.dumps({"inline_keyboard": []}))
        elif prefix == "u":
            answer = handle_user_callback(token, db, user, callback, verb, arg, cfg, owner_ids,
                                          _Settings(connection))
        elif prefix == "a" and user["chat_id"] in owner_ids:
            answer = handle_admin_callback(token, db, user, callback, verb, arg, cfg)
        try:
            w.telegram_request(token, "answerCallbackQuery", callback_query_id=callback["id"], text=answer)
        except requests.RequestException:
            pass  # нажатие устарело — подтверждать уже нечего
        return True

    message = update.get("message") or {}
    chat = message.get("chat") or {}
    if not chat.get("id"):
        return False
    chat_id = str(chat["id"])
    is_owner = chat_id in owner_ids
    if not is_owner and flooding(chat_id):
        return True
    text = (message.get("text") or message.get("caption") or "").strip()
    command = text.split()[0].split("@", 1)[0].lower() if text.startswith("/") else ""
    public = {"/start", "/menu", "/help", "/list", "/status", "/discount", "/deleteme"}
    if command and command not in public:
        # Владелец — к своим командам; посторонний — тишина, даже без записи в базу.
        return False if is_owner else True
    user, created = db.touch_user({**chat, **(message.get("from") or {}), "id": chat["id"]}, trial)

    if is_owner and user["state"] in {"broadcast", "broadcast_confirm"} and text and not command:
        targets = len(db.broadcast_targets())
        db.set_user(chat_id, state="broadcast_confirm", state_data=message.get("text") or "")
        send(token, chat_id, f"{message.get('text')}\n\n———\n📣 Отправить это {targets} пользователям?",
             reply_markup=markup([button("✅ Отправить", "a:bcgo"), button("❌ Отмена", "a:bcx")]))
        return True
    if command in {"/start", "/menu"}:
        greeting = ""
        if created and not is_owner:
            greeting = (f"👋 <b>Привет! Я {html.escape(cfg['brand'])}.</b>\n"
                        "Присылаю новые объявления с OLX через 1–2 минуты после публикации — раньше, "
                        "чем их увидят другие, — и показываю, насколько цена ниже рынка.\n")
        text_, keyboard = home_screen(db, user, cfg, owner_ids, greeting)
        if created and not is_owner:
            text_ += f"\n\n🎁 Пробный доступ до <b>{date_text(user['paid_until'])}</b> — начните с первого поиска 👇"
        send(token, chat_id, text_, reply_markup=keyboard)
        return True
    if command == "/help":
        show(token, chat_id, help_screen(cfg))
        return True
    if command == "/deleteme":
        show(token, chat_id, delete_screen())
        return True
    if command == "/list":
        show(token, chat_id, list_screen(db, user, cfg, owner_ids))
        return True
    if command == "/status":
        show(token, chat_id, sub_screen(db, user, cfg, owner_ids))
        return True
    if command == "/discount":
        argument = text[len("/discount"):].strip().rstrip("%")
        if argument.isdigit() and int(argument) <= 90:
            db.set_user(chat_id, min_discount=int(argument))
        show(token, chat_id, filter_screen(db.user(chat_id)))
        return True
    if user["state"].startswith("exclude:") and text and not command:
        watch_id = user["state"].split(":", 1)[1]
        db.set_user(chat_id, state="")
        watch = db.watch(int(watch_id), chat_id) if watch_id.isdigit() else None
        if watch:
            db.set_watch_exclude(watch["id"], chat_id, text)
            show(token, chat_id, watch_screen(db, db.watch(watch["id"]), cfg))
            return True
    if OLX_URL.search(text):
        add_watch(token, db, user, text, cfg, owner_ids)
        return True
    if is_owner:
        return False  # остальное — обычные сообщения владельца (заметки и т. п.)
    if message.get("photo") or message.get("document"):
        handle_receipt(token, db, user, chat, message, cfg, owner_ids)
        return True
    if user["state"] == "receipt":
        send(token, chat_id, "📸 Нужен скриншот или фото чека — пришлите его картинкой.",
             reply_markup=markup([button("⬅️ Отмена", "u:sub")]))
        return True
    if text:
        send(token, chat_id, "🤔 Чтобы добавить поиск, пришлите ссылку на выдачу OLX.",
             reply_markup=markup([button("❓ Как добавить поиск", "u:add")], [HOME]))
    return True


# --- Команды владельца ------------------------------------------------------
def handle_admin_command(token: str, connection, message: dict, config: dict | None = None) -> bool:
    text = (message.get("text") or "").strip()
    command = text.split()[0].split("@", 1)[0].lower() if text.startswith("/") else ""
    chat_id = str(message["chat"]["id"])
    if command not in {"/grant", "/users"}:
        return False
    db = FeedDB(connection)
    cfg = settings(config)
    if command == "/users":
        show(token, chat_id, admin_screen(db, cfg))
        return True
    parts = text.split()[1:]
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        send(token, chat_id, "Использование: <code>/grant 123456789 30</code> или "
                             "<code>/grant @username 30</code> (дни; отрицательное число — убавить).\n"
                             "Удобнее — кнопками: /users → Пользователи.")
        return True
    target = db.find_user(parts[0])
    user = db.grant(target["chat_id"], int(parts[1]), by=chat_id) if target else None
    if not user:
        send(token, chat_id, "Такого пользователя нет — он должен хотя бы раз написать боту /start.")
        return True
    send(token, chat_id, f"✅ {html.escape(parts[0])}: доступ до {date_text(user['paid_until'])}",
         reply_markup=markup([button("👤 Карточка", f"a:u:{user['chat_id']}")]))
    if int(parts[1]) > 0:
        notify_granted(db, token, user)
    return True
