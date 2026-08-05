"""OLX-помощник: полный обход выдачи, Telegram-кнопки и база избранного."""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
import yaml
from bs4 import BeautifulSoup
from curl_cffi import requests as impersonated

ROOT = Path(__file__).parent
STATE_PATH = ROOT / "data" / "watcher.sqlite3"

# OLX стоит за CloudFront, который режет запросы по TLS-отпечатку клиента,
# а не по заголовкам: с сервера обычный requests получает 403 при любом наборе
# User-Agent и Sec-Fetch-*, тогда как curl проходит. curl_cffi имитирует
# отпечаток Chrome, поэтому выдача открывается и с Linux-машины.
IMPERSONATE = "chrome"
HEADERS = {"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7"}

# curl_cffi не наследуется от requests.RequestException, поэтому сетевые ошибки
# OLX ловим обоими классами: иначе сбой одной страницы уронил бы весь обход.
NETWORK_ERRORS = (requests.RequestException, impersonated.RequestsError)


def olx_session() -> impersonated.Session:
    session = impersonated.Session(impersonate=IMPERSONATE)
    session.headers.update(HEADERS)
    return session


@dataclass(frozen=True)
class Listing:
    ad_id: str
    url: str
    title: str
    price_kzt: int | None
    description: str = ""
    image_url: str | None = None
    search_url: str = ""
    model: str | None = None
    risk_flags: str = ""
    seller_name: str | None = None
    seller_since: str | None = None
    profile_id: str = "gpu"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


# Цена — это либо группы по три цифры («2 250 000 ₸»), либо одно число («45000 ₸»).
# Строгие тройки не дают склеить номер модели с ценой: в «GT730 6 000 тг»
# захватывается «6 000», а не «730 6 000».
# (?<!\d) обязателен: без него поиск стартует с середины числа и в
# «rx 6800 200 000 тг» захватывает «800 200 000» вместо «200 000».
PRICE_PATTERN = re.compile(r"(?<!\d)(\d{1,3}(?:[\s\u00a0]\d{3})+|\d+)\s*(?:₸|тг\.?|тенге)")


def extract_price(value: str) -> int | None:
    if not value or "$" in value or "usd" in value.lower():
        return None
    match = PRICE_PATTERN.search(value.lower())
    digits = re.sub(r"\D", "", match.group(1)) if match else ""
    return int(digits) if digits else None


def ad_id_from_url(url: str) -> str | None:
    match = re.search(r"-(ID[\w-]+)(?:\.html)?/?$", url)
    return match.group(1) if match else None


def page_url(search_url: str, page_number: int) -> str:
    parts = urlsplit(search_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if page_number == 1:
        query.pop("page", None)
    else:
        query["page"] = str(page_number)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def card_title(card, link) -> str:
    """OLX держит заголовок в отдельном h4; у ссылки-обёртки текста нет."""
    for selector in ("h4", "h6", "h5"):
        heading = text(card.select_one(selector))
        if heading:
            return heading
    return link.get("title") or link.get("aria-label") or text(link)


def extract_listings(search_url: str, page: str, limit: int) -> list[Listing]:
    soup = BeautifulSoup(page, "html.parser")
    found: dict[str, Listing] = {}
    for card in soup.select('[data-cy="l-card"], [data-testid="l-card"], article, li'):
        link = next((a for a in card.select("a[href]") if "/d/obyavlenie/" in a.get("href", "")), None)
        if not link:
            continue
        url = urljoin(search_url, link["href"]).split("?")[0]
        ad_id = ad_id_from_url(url)
        if not ad_id:
            continue
        image = card.select_one("img[src], img[data-src]")
        found.setdefault(ad_id, Listing(
            ad_id, url, card_title(card, link),
            extract_price(text(card)), image_url=(image.get("src") or image.get("data-src")) if image else None,
            search_url=search_url,
        ))
        if len(found) >= limit:
            break
    if not found:  # запасной вариант для изменившейся вёрстки OLX
        for link in soup.select('a[href*="/d/obyavlenie/"]'):
            url = urljoin(search_url, link["href"]).split("?")[0]
            ad_id = ad_id_from_url(url)
            if ad_id:
                found.setdefault(ad_id, Listing(ad_id, url, link.get("title") or text(link), None, search_url=search_url))
            if len(found) >= limit:
                break
    return list(found.values())


def fetch_details(session: requests.Session, listing: Listing) -> Listing:
    response = session.get(listing.url, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    description = ""
    for selector in ('[data-cy="ad_description"]', '[data-testid="ad-description"]', '#textContent'):
        description = text(soup.select_one(selector))
        if description:
            break
    if not description:
        meta = soup.select_one('meta[name="description"]')
        description = meta.get("content", "") if meta else ""
    heading = soup.select_one('h1, [data-cy="ad_title"]')
    og_title = soup.select_one('meta[property="og:title"]')
    title = listing.title or text(heading) or (og_title.get("content", "") if og_title else "")
    image = soup.select_one('meta[property="og:image"]')
    price_node = soup.select_one('[data-testid="ad-price"], [data-cy="ad_price"]')
    seller_node = soup.select_one('[data-cy="seller_name"], [data-testid="seller-name"], a[href*="/user/"]')
    seller_name = text(seller_node) or None
    raw_text = soup.get_text(" ", strip=True)
    since = re.search(r"(?:На OLX с|Зарегистрирован(?:а)?)[\s:]+([^|.]{3,40})", raw_text, re.IGNORECASE)
    return Listing(
        listing.ad_id, listing.url, title,
        extract_price(text(price_node)) or listing.price_kzt or extract_price(raw_text),
        description, image.get("content") if image else listing.image_url, listing.search_url,
        seller_name=seller_name, seller_since=since.group(1).strip() if since else None, profile_id=listing.profile_id,
    )


def assess(listing: Listing, config: dict) -> Listing:
    """Определяет модель, выгодность и риски по настраиваемым правилам."""
    content = f"{listing.title} {listing.description}".casefold()
    matched_model = next((model for model in config.get("models", []) if any(
        alias.casefold() in content for alias in model.get("aliases", [])
    )), None)
    risks = [word for word in config.get("risk_keywords", []) if word.casefold() in content]
    return replace(listing, model=matched_model.get("name") if matched_model else None, risk_flags=", ".join(risks))


def price_label(listing: Listing, config: dict) -> str:
    model = next((item for item in config.get("models", []) if item.get("name") == listing.model), None)
    if not model or listing.price_kzt is None:
        return ""
    excellent, normal = int(model["excellent_max"]), int(model["normal_max"])
    if listing.price_kzt <= excellent:
        discount = round((normal - listing.price_kzt) / normal * 100)
        return f"🔥 Выгодно: примерно на {discount}% ниже вашего ориентира {normal:,} ₸"
    if listing.price_kzt <= normal:
        return f"✅ Нормальная цена для {listing.model} (ориентир до {normal:,} ₸)"
    return f"💸 Выше вашего ориентира {normal:,} ₸"


class Storage:
    """SQLite-хранилище. Каждому потоку — отдельное соединение."""
    def __init__(self, path: Path = STATE_PATH) -> None:
        path.parent.mkdir(exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS ads (
              ad_id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT, description TEXT,
              price_kzt INTEGER, image_url TEXT, search_url TEXT, status TEXT NOT NULL DEFAULT 'new',
              first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL, telegram_message_id INTEGER,
              model TEXT, risk_flags TEXT, seller_name TEXT, seller_since TEXT, deal_status TEXT NOT NULL DEFAULT 'new'
              , profile_id TEXT NOT NULL DEFAULT 'gpu'
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, ad_id TEXT NOT NULL, created_at INTEGER NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS price_history (id INTEGER PRIMARY KEY AUTOINCREMENT, ad_id TEXT NOT NULL, recorded_at INTEGER NOT NULL, price_kzt INTEGER);
            CREATE TABLE IF NOT EXISTS errors (
              id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at INTEGER NOT NULL,
              context TEXT NOT NULL, ad_id TEXT, url TEXT, error TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ads_status_price ON ads(status, price_kzt);
            CREATE INDEX IF NOT EXISTS errors_occurred_at ON errors(occurred_at DESC);
        """)
        existing = {row[1] for row in self.connection.execute("PRAGMA table_info(ads)")}
        for name, definition in {
            "model": "TEXT", "risk_flags": "TEXT", "seller_name": "TEXT", "seller_since": "TEXT",
            "deal_status": "TEXT NOT NULL DEFAULT 'new'",
            "profile_id": "TEXT NOT NULL DEFAULT 'gpu'",
        }.items():
            if name not in existing:
                self.connection.execute(f"ALTER TABLE ads ADD COLUMN {name} {definition}")
        # Миграция от ранней версии: не присылаем ранее увиденные объявления повторно.
        old_seen = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='seen'"
        ).fetchone()
        if old_seen:
            self.connection.execute(
                "INSERT OR IGNORE INTO ads(ad_id,url,status,first_seen,last_seen) "
                "SELECT ad_id, '', 'ignored', first_seen, first_seen FROM seen"
            )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def has(self, ad_id: str) -> bool:
        return self.connection.execute("SELECT 1 FROM ads WHERE ad_id=?", (ad_id,)).fetchone() is not None

    def add_baseline(self, listing: Listing) -> None:
        now = int(time.time())
        self.connection.execute(
            "INSERT OR IGNORE INTO ads(ad_id,url,title,price_kzt,image_url,search_url,status,first_seen,last_seen) "
            "VALUES(?,?,?,?,?,?, 'ignored', ?, ?)",
            (listing.ad_id, listing.url, listing.title, listing.price_kzt, listing.image_url, listing.search_url, now, now),
        )
        self.connection.commit()

    def add_new(self, listing: Listing) -> None:
        now = int(time.time())
        self.connection.execute(
            "INSERT INTO ads(ad_id,url,title,description,price_kzt,image_url,search_url,status,first_seen,last_seen,model,risk_flags,seller_name,seller_since,profile_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (listing.ad_id, listing.url, listing.title, listing.description, listing.price_kzt, listing.image_url,
             listing.search_url, 'new', now, now, listing.model, listing.risk_flags, listing.seller_name, listing.seller_since, listing.profile_id),
        )
        self.connection.execute("INSERT INTO price_history(ad_id,recorded_at,price_kzt) VALUES(?,?,?)", (listing.ad_id, now, listing.price_kzt))
        self.connection.commit()

    def set_message_id(self, ad_id: str, message_id: int) -> None:
        self.connection.execute("UPDATE ads SET telegram_message_id=? WHERE ad_id=?", (message_id, ad_id))
        self.connection.commit()

    def set_status(self, ad_id: str, status: str) -> sqlite3.Row | None:
        row = self.connection.execute("SELECT * FROM ads WHERE ad_id=?", (ad_id,)).fetchone()
        if row:
            self.connection.execute("UPDATE ads SET status=?, last_seen=? WHERE ad_id=?", (status, int(time.time()), ad_id))
            self.connection.commit()
        return row

    def favorites(self, limit: int = 30) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM ads WHERE status='interested' ORDER BY price_kzt IS NULL, price_kzt, first_seen DESC LIMIT ?", (limit,)
        ).fetchall()

    def set_deal_status(self, ad_id: str, status: str) -> None:
        self.connection.execute("UPDATE ads SET deal_status=?, last_seen=? WHERE ad_id=?", (status, int(time.time()), ad_id))
        self.connection.commit()

    def add_note(self, ad_id: str, body: str) -> None:
        self.connection.execute("INSERT INTO notes(ad_id,created_at,body) VALUES(?,?,?)", (ad_id, int(time.time()), body))
        self.connection.commit()

    def note_for(self, ad_id: str) -> str | None:
        row = self.connection.execute("SELECT body FROM notes WHERE ad_id=? ORDER BY id DESC LIMIT 1", (ad_id,)).fetchone()
        return row["body"] if row else None

    def seller_count(self, seller_name: str | None) -> int:
        if not seller_name:
            return 0
        return self.connection.execute("SELECT COUNT(*) FROM ads WHERE seller_name=?", (seller_name,)).fetchone()[0]

    def observe_price(self, listing: Listing) -> tuple[int, int] | None:
        """Возвращает (старая, новая), только если цена стала ниже."""
        row = self.connection.execute("SELECT price_kzt,status FROM ads WHERE ad_id=?", (listing.ad_id,)).fetchone()
        if not row:
            return None
        old, current = row["price_kzt"], listing.price_kzt
        self.connection.execute("UPDATE ads SET last_seen=?, title=COALESCE(?,title), url=COALESCE(NULLIF(?,''),url) WHERE ad_id=?", (int(time.time()), listing.title, listing.url, listing.ad_id))
        if current is not None and current != old:
            self.connection.execute("UPDATE ads SET price_kzt=? WHERE ad_id=?", (current, listing.ad_id))
            self.connection.execute("INSERT INTO price_history(ad_id,recorded_at,price_kzt) VALUES(?,?,?)", (listing.ad_id, int(time.time()), current))
        self.connection.commit()
        return (old, current) if old is not None and current is not None and current < old and row["status"] != "ignored" else None

    def get_offset(self) -> int | None:
        row = self.connection.execute("SELECT value FROM meta WHERE key='telegram_offset'").fetchone()
        return int(row["value"]) if row else None

    def baseline_complete(self) -> bool:
        return self.connection.execute("SELECT 1 FROM meta WHERE key='baseline_completed'").fetchone() is not None

    def set_offset(self, offset: int) -> None:
        self.connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('telegram_offset',?)", (str(offset),))
        self.connection.commit()

    def setting(self, key: str, default: str = "") -> str:
        row = self.connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value)); self.connection.commit()

    def record_error(self, context: str, error: Exception, listing: Listing | None = None) -> None:
        self.connection.execute(
            "INSERT INTO errors(occurred_at,context,ad_id,url,error) VALUES(?,?,?,?,?)",
            (int(time.time()), context, listing.ad_id if listing else None,
             listing.url if listing else None, str(error)),
        )
        self.connection.commit()


def telegram_request(token: str, method: str, **data: object) -> dict:
    response = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=35)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise requests.RequestException(result.get("description", "Ошибка Telegram API"))
    return result


def safe_error(error: Exception) -> str:
    """Не допускает утечку токена бота в консоль или файл логов."""
    return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot<redacted>", str(error))


def favorite_text(storage: Storage) -> str:
    rows = storage.favorites()
    if not rows:
        return "В избранном пока нет объявлений. Нажимайте «Интересует» под подходящими вариантами."
    lines = ["<b>Избранное — от дешёвых к дорогим</b>"]
    for index, row in enumerate(rows, start=1):
        price = f"{row['price_kzt']:,} ₸" if row["price_kzt"] is not None else "цена не распознана"
        note = storage.note_for(row["ad_id"])
        details = f" · {html.escape(row['deal_status'] or 'new')}"
        if note:
            details += f"\n📝 {html.escape(note[:120])}"
        lines.append(f"{index}. <b>{html.escape(row['title'] or 'Без названия')}</b> — {price}{details}\n<a href=\"{html.escape(row['url'], quote=True)}\">Открыть OLX</a>")
    return "\n\n".join(lines)


def daily_summary_text(storage: Storage) -> str:
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    new = storage.connection.execute("SELECT COUNT(*) FROM ads WHERE first_seen>=?", (int(today),)).fetchone()[0]
    passed = storage.connection.execute("SELECT COUNT(*) FROM ads WHERE first_seen>=? AND telegram_message_id IS NOT NULL", (int(today),)).fetchone()[0]
    favorites = storage.connection.execute("SELECT COUNT(*) FROM ads WHERE status='interested'").fetchone()[0]
    cheap = storage.connection.execute("SELECT title,price_kzt,url FROM ads WHERE telegram_message_id IS NOT NULL AND price_kzt IS NOT NULL ORDER BY price_kzt LIMIT 5").fetchall()
    models = storage.connection.execute("SELECT COALESCE(model,'Не определена') m,COUNT(*) c FROM ads WHERE first_seen>=? GROUP BY m ORDER BY c DESC LIMIT 5", (int(today),)).fetchall()
    cheap_text = "\n".join(f"• {html.escape(r['title'] or '—')} — {r['price_kzt']:,} ₸" for r in cheap) or "нет"
    model_text = ", ".join(f"{html.escape(r['m'])}: {r['c']}" for r in models) or "нет"
    return f"<b>📊 Ежедневная сводка</b>\nНовых: <b>{new}</b>\nПрошли фильтры: <b>{passed}</b>\nВ избранном: <b>{favorites}</b>\n\n<b>5 самых дешёвых:</b>\n{cheap_text}\n\n<b>Чаще встречались:</b> {model_text}"


def daily_summary_loop(token: str, chat_id: str, config: dict, stop: threading.Event) -> None:
    storage = Storage()
    try:
        while not stop.wait(20):
            now = datetime.now()
            if now.strftime("%H:%M") != config.get("daily_summary_time", "21:00"):
                continue
            marker = now.strftime("%Y-%m-%d")
            row = storage.connection.execute("SELECT value FROM meta WHERE key='daily_summary_date'").fetchone()
            if row and row["value"] == marker:
                continue
            try:
                telegram_request(token, "sendMessage", chat_id=chat_id, text=daily_summary_text(storage), parse_mode="HTML")
                storage.connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('daily_summary_date',?)", (marker,)); storage.connection.commit()
                logging.info("Ежедневная сводка отправлена")
            except requests.RequestException as error:
                logging.warning("Не удалось отправить сводку: %s", safe_error(error))
    finally:
        storage.close()


def listing_caption(listing: Listing, config: dict, storage: Storage) -> str:
    price = f"{listing.price_kzt:,} ₸" if listing.price_kzt is not None else "не распознана"
    extras = [item for item in [f"Модель: <b>{html.escape(listing.model)}</b>" if listing.model else "", price_label(listing, config)] if item]
    if listing.risk_flags:
        extras.append(f"⚠️ Риски в описании: {html.escape(listing.risk_flags)}")
    if listing.seller_name:
        count = storage.seller_count(listing.seller_name)
        seller = f"Продавец: {html.escape(listing.seller_name)}"
        if listing.seller_since:
            seller += f" · на OLX с {html.escape(listing.seller_since)}"
        if count >= 3:
            seller += f" · ⚠️ {count} объявлений в базе"
        extras.append(seller)
    return (
        f"<b>{html.escape(listing.title or 'Новое объявление')}</b>\n"
        f"Цена: <b>{price}</b>\n" + "\n".join(extras) + f"\n\n{html.escape(listing.description[:450])}\n\n"
        f'<a href="{html.escape(listing.url, quote=True)}">Открыть объявление на OLX</a>'
    )


def notify(token: str, chat_id: str, listing: Listing, storage: Storage, config: dict) -> None:
    keyboard = json.dumps({"inline_keyboard": [[
        {"text": "✅ Интересует", "callback_data": f"i:{listing.ad_id}"},
        {"text": "🚫 Не интересует", "callback_data": f"n:{listing.ad_id}"},
    ]]}, ensure_ascii=False)
    caption = listing_caption(listing, config, storage)
    payload = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML", "reply_markup": keyboard}
    result: dict
    if listing.image_url:
        try:
            result = telegram_request(token, "sendPhoto", **payload, photo=listing.image_url)
        except requests.RequestException as error:
            logging.warning("Фото не отправлено (%s); отправляю карточку без фото", error)
            result = telegram_request(token, "sendMessage", chat_id=chat_id, text=caption, parse_mode="HTML", reply_markup=keyboard)
    else:
        result = telegram_request(token, "sendMessage", chat_id=chat_id, text=caption, parse_mode="HTML", reply_markup=keyboard)
    storage.set_message_id(listing.ad_id, result["result"]["message_id"])


def notify_price_drop(token: str, chat_id: str, listing: Listing, old_price: int, new_price: int) -> None:
    telegram_request(
        token, "sendMessage", chat_id=chat_id, parse_mode="HTML", disable_web_page_preview=True,
        text=(f"📉 <b>Цена снизилась</b>\n{html.escape(listing.title)}\n"
              f"<s>{old_price:,} ₸</s> → <b>{new_price:,} ₸</b>\n"
              f'<a href="{html.escape(listing.url, quote=True)}">Открыть объявление</a>'),
    )


def handle_update(token: str, storage: Storage, update: dict) -> None:
    callback = update.get("callback_query")
    if callback:
        action, _, payload = callback.get("data", "").partition(":")
        if action in {"m", "p"}:
            chat_id = callback["message"]["chat"]["id"]
            if action == "m" and payload == "pause":
                paused = storage.setting("watcher_paused", "0") != "1"
                storage.set_setting("watcher_paused", "1" if paused else "0")
                answer = "Мониторинг на паузе" if paused else "Мониторинг возобновлён"
                keyboard = main_menu(storage)
            elif action == "m" and payload == "profiles":
                keyboard, answer = profiles_menu(storage), "Профили"
            elif action == "m" and payload == "summary":
                telegram_request(token, "sendMessage", chat_id=chat_id, text=daily_summary_text(storage), parse_mode="HTML")
                telegram_request(token, "answerCallbackQuery", callback_query_id=callback["id"], text="Сводка отправлена")
                return
            elif action == "m" and payload == "favorites":
                telegram_request(token, "sendMessage", chat_id=chat_id, text=favorite_text(storage), parse_mode="HTML")
                telegram_request(token, "answerCallbackQuery", callback_query_id=callback["id"], text="Избранное отправлено")
                return
            elif action == "m" and payload == "menu":
                keyboard, answer = main_menu(storage), "Меню"
            elif action == "p":
                key = f"profile_enabled:{payload}"
                enabled = storage.setting(key, "1") != "1"
                storage.set_setting(key, "1" if enabled else "0")
                keyboard, answer = profiles_menu(storage), f"Профиль {'включён' if enabled else 'выключен'}"
            else:
                return
            telegram_request(token, "answerCallbackQuery", callback_query_id=callback["id"], text=answer)
            telegram_request(token, "editMessageReplyMarkup", chat_id=chat_id, message_id=callback["message"]["message_id"], reply_markup=json.dumps(keyboard, ensure_ascii=False))
            return
        if action not in {"i", "n", "d", "note"} or not payload:
            return
        ad_id, _, deal = payload.partition(":")
        if action in {"i", "n"}:
            status = "interested" if action == "i" else "ignored"
            row = storage.set_status(ad_id, status)
            answer = "Добавлено в избранное" if status == "interested" else "Больше не покажу это объявление"
            keyboard = {"inline_keyboard": [[
                {"text": "📞 Позвонить", "callback_data": f"d:{ad_id}:позвонить"},
                {"text": "✉️ Написал", "callback_data": f"d:{ad_id}:написал"},
            ], [
                {"text": "🤝 Договорился", "callback_data": f"d:{ad_id}:договорился"},
                {"text": "✅ Купил", "callback_data": f"d:{ad_id}:купил"},
            ], [
                {"text": "📝 Заметка", "callback_data": f"note:{ad_id}"},
                {"text": "🗄 Архив", "callback_data": f"d:{ad_id}:архив"},
            ]]}
            if action == "n":
                keyboard = {"inline_keyboard": []}
        elif action == "d":
            storage.set_deal_status(ad_id, deal)
            row, answer = True, f"Статус: {deal}"
            keyboard = {"inline_keyboard": [[
                {"text": "📞 Позвонить", "callback_data": f"d:{ad_id}:позвонить"},
                {"text": "✉️ Написал", "callback_data": f"d:{ad_id}:написал"},
            ], [
                {"text": "🤝 Договорился", "callback_data": f"d:{ad_id}:договорился"},
                {"text": "✅ Купил", "callback_data": f"d:{ad_id}:купил"},
            ], [
                {"text": "📝 Заметка", "callback_data": f"note:{ad_id}"},
                {"text": "🗄 Архив", "callback_data": f"d:{ad_id}:архив"},
            ]]}
        else:
            chat_id = callback["message"]["chat"]["id"]
            storage.connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (f"pending_note:{chat_id}", ad_id)); storage.connection.commit()
            telegram_request(token, "answerCallbackQuery", callback_query_id=callback["id"], text="Пришлите следующим сообщением заметку")
            telegram_request(token, "sendMessage", chat_id=chat_id, text="📝 Напишите заметку для этого объявления следующим сообщением.")
            return
        telegram_request(token, "answerCallbackQuery", callback_query_id=callback["id"], text=answer)
        if row:
            message = callback.get("message", {})
            telegram_request(token, "editMessageReplyMarkup", chat_id=message["chat"]["id"], message_id=message["message_id"], reply_markup=json.dumps(keyboard, ensure_ascii=False))
        logging.info("Пользователь обновил %s: %s", ad_id, action)
        return
    message = update.get("message", {})
    chat_id, message_text = message.get("chat", {}).get("id"), message.get("text", "")
    pending = storage.connection.execute("SELECT value FROM meta WHERE key=?", (f"pending_note:{chat_id}",)).fetchone() if chat_id else None
    if pending and message_text and not message_text.startswith("/"):
        storage.add_note(pending["value"], message_text)
        storage.connection.execute("DELETE FROM meta WHERE key=?", (f"pending_note:{chat_id}",)); storage.connection.commit()
        telegram_request(token, "sendMessage", chat_id=chat_id, text="Заметка сохранена.")
        return
    command = message_text.split("@", 1)[0].lower()
    if command in {"/favorites", "/избранное"}:
        telegram_request(token, "sendMessage", chat_id=message["chat"]["id"], text=favorite_text(storage), parse_mode="HTML", disable_web_page_preview=True)
    elif command in {"/start", "/help", "/menu"}:
        telegram_request(token, "sendMessage", chat_id=message["chat"]["id"], text="Панель управления помощником", reply_markup=json.dumps(main_menu(storage), ensure_ascii=False))


def main_menu(storage: Storage) -> dict:
    paused = storage.setting("watcher_paused", "0") == "1"
    return {"inline_keyboard": [[
        {"text": "▶️ Продолжить" if paused else "⏸ Пауза", "callback_data": "m:pause"},
        {"text": "🧩 Профили", "callback_data": "m:profiles"},
    ], [
        {"text": "📊 Сводка сейчас", "callback_data": "m:summary"},
        {"text": "⭐ Избранное", "callback_data": "m:favorites"},
    ]]}


def profiles_menu(storage: Storage) -> dict:
    profiles = load_config().get("profiles", [])
    buttons = []
    for profile in profiles:
        default = "1" if profile.get("enabled", True) else "0"
        enabled = storage.setting(f"profile_enabled:{profile['id']}", default) == "1"
        buttons.append([{"text": f"{'✅' if enabled else '⬜'} {profile.get('name', profile['id'])}", "callback_data": f"p:{profile['id']}"}])
    buttons.append([{"text": "⬅️ Меню", "callback_data": "m:menu"}])
    return {"inline_keyboard": buttons}


def telegram_listener(token: str, stop: threading.Event) -> None:
    storage = Storage()
    try:
        while not stop.is_set():
            try:
                offset = storage.get_offset()
                response = requests.get(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params={"offset": offset, "timeout": 25, "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=35,
                )
                response.raise_for_status()
                for update in response.json().get("result", []):
                    try:
                        handle_update(token, storage, update)
                    except requests.RequestException as error:
                        # У callback_query есть короткий срок жизни. Старое нажатие
                        # уже нельзя подтвердить, но оно не должно зацикливать очередь.
                        storage.record_error("Telegram callback/update", Exception(safe_error(error)))
                        logging.warning("Пропущен устаревший или недоступный Telegram update: %s", safe_error(error))
                    except Exception as error:
                        storage.record_error("Telegram update", error)
                        logging.exception("Ошибка обработки Telegram update")
                    finally:
                        # Важно сохранить offset даже при ошибке одного сообщения.
                        storage.set_offset(update["update_id"] + 1)
            except requests.RequestException as error:
                storage.record_error("Telegram listener", Exception(safe_error(error)))
                logging.warning("Telegram listener: %s", safe_error(error))
                stop.wait(5)
    finally:
        storage.close()


def load_config() -> dict:
    path = ROOT / "config.yaml"
    if not path.exists():
        raise SystemExit("Скопируйте config.example.yaml в config.yaml.")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def debug_once(config: dict, count: int) -> None:
    session = olx_session()
    keywords = tuple(word.casefold() for word in config["required_any_keywords"])
    excluded = tuple(word.casefold() for word in config.get("excluded_any_keywords", []))
    hard_excluded = tuple(word.casefold() for word in config.get("hard_excluded_keywords", []))
    for search_url in config["search_urls"]:
        response = session.get(search_url, timeout=25); response.raise_for_status()
        for number, brief in enumerate(extract_listings(search_url, response.text, count), 1):
            listing = assess(fetch_details(session, brief), config)
            content = f"{listing.title} {listing.description}".casefold()
            matches = [word for word in keywords if word in content]
            rejected = [word for word in excluded if word in content]
            fits = bool(matches) and not rejected and not any(word in content for word in hard_excluded) and listing.price_kzt is not None and listing.price_kzt <= int(config["max_price_kzt"])
            print(f"{number}. {'ПОДХОДИТ' if fits else 'не подходит'} | {listing.price_kzt} ₸ | совпало: {', '.join(matches) or 'нет'} | исключено: {', '.join(rejected) or 'нет'}\n   {listing.url}")


def print_favorites() -> None:
    storage = Storage()
    try:
        print(BeautifulSoup(favorite_text(storage), "html.parser").get_text("\n"))
    finally:
        storage.close()


def main(debug_count: int | None = None) -> None:
    config = load_config()
    if debug_count is not None:
        debug_once(config, debug_count); return
    load_dotenv(ROOT / ".env")
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id or "replace_me" in f"{token}{chat_id}":
        raise SystemExit("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env.")
    storage = Storage()
    first_run = not storage.baseline_complete()
    session = olx_session()
    max_pages, page_delay = int(config.get("max_pages_per_search", 0)), float(config.get("page_delay_seconds", 1))
    stop = threading.Event()
    threading.Thread(target=telegram_listener, args=(token, stop), daemon=True, name="telegram-listener").start()
    threading.Thread(target=daily_summary_loop, args=(token, chat_id, config, stop), daemon=True, name="daily-summary").start()
    logging.info("Помощник запущен: обход раз в %s сек.; команды Telegram: /favorites", config["poll_interval_seconds"])
    try:
        while True:
            started = time.monotonic(); scanned = new = matched = 0
            if storage.setting("watcher_paused", "0") == "1":
                logging.info("Мониторинг на паузе — управление доступно в Telegram")
                stop.wait(20)
                continue
            profiles = config.get("profiles", [{"id": "gpu", "enabled": True}])
            profiles = [p for p in profiles if storage.setting(f"profile_enabled:{p['id']}", "1" if p.get("enabled", True) else "0") == "1"]
            for profile in profiles:
                profile_config = {**config, **profile}
                keywords = tuple(word.casefold() for word in profile_config["required_any_keywords"])
                excluded = tuple(word.casefold() for word in profile_config.get("excluded_any_keywords", []))
                hard_excluded = tuple(word.casefold() for word in profile_config.get("hard_excluded_keywords", []))
                for search_url in profile_config.get("search_urls", []):
                    page_number, cycle_ids = 1, set()
                    while max_pages == 0 or page_number <= max_pages:
                        try:
                            response = session.get(page_url(search_url, page_number), timeout=25)
                            response.raise_for_status()
                            cards = extract_listings(search_url, response.text, int(config["max_candidates_per_search"]))
                        except NETWORK_ERRORS as error:
                            storage.record_error("Страница выдачи OLX", error)
                            logging.warning("Не удалось прочитать страницу %d: %s", page_number, error)
                            break
                        if not cards or all(card.ad_id in cycle_ids for card in cards):
                            break
                        page_new = 0
                        for brief in cards:
                            if brief.ad_id in cycle_ids:
                                continue
                            cycle_ids.add(brief.ad_id); scanned += 1
                            if storage.has(brief.ad_id):
                                decrease = storage.observe_price(brief)
                                if decrease:
                                    notify_price_drop(token, chat_id, brief, *decrease)
                                    logging.info("Снижение цены: %s (%s → %s)", brief.ad_id, *decrease)
                                continue
                            new += 1; page_new += 1
                            if first_run and not config.get("notify_existing_on_first_run", False):
                                storage.add_baseline(replace(brief, profile_id=profile_config["id"])); continue
                            try:
                                listing = assess(fetch_details(session, replace(brief, profile_id=profile_config["id"])), profile_config)
                                storage.add_new(listing)
                                content = f"{listing.title} {listing.description}".casefold()
                                words = [word for word in keywords if word in content]
                                if ((words or listing.model) and not any(word in content for word in excluded)
                                        and not any(word in content for word in hard_excluded)
                                        and listing.price_kzt is not None and listing.price_kzt <= int(profile_config["max_price_kzt"])):
                                    notify(token, chat_id, listing, storage, profile_config); matched += 1
                            except NETWORK_ERRORS as error:
                                storage.record_error("Карточка объявления OLX", error, brief)
                                storage.add_baseline(brief)
                                logging.warning("Пропущено объявление %s: %s", brief.ad_id, error)
                            except Exception as error:
                                storage.record_error("Обработка объявления", error, brief)
                                logging.exception("Ошибка при обработке %s", brief.ad_id)
                        logging.info("%s · страница %d: карточек %d; новых %d", profile_config.get("name", profile_config["id"]), page_number, len(cards), page_new)
                        page_number += 1; stop.wait(page_delay)
            # Закрываем baseline только если обход действительно что-то прочитал.
            # Полностью неудачный первый прогон (например, 403 от OLX) иначе
            # помечал бы базу построенной вхолостую, и следующий цикл разослал
            # бы всю выдачу как новинки.
            if first_run and scanned:
                storage.connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('baseline_completed','1')"); storage.connection.commit(); first_run = False
            elif first_run:
                logging.warning("Первый обход не прочитал ни одного объявления; baseline не закрыт, повторю в следующем цикле")
            logging.info("ИТОГ ПРОГОНА: просмотрено=%d, новых=%d, отправлено=%d, время=%.1f сек.", scanned, new, matched, time.monotonic() - started)
            stop.wait(int(config["poll_interval_seconds"]))
    finally:
        stop.set(); storage.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OLX-помощник с Telegram-избранным")
    parser.add_argument("--debug", type=int, metavar="N", help="один раз проверить N карточек без сохранения")
    parser.add_argument("--favorites", action="store_true", help="вывести избранное в терминал, от дешёвых к дорогим")
    args = parser.parse_args()
    ROOT.joinpath("data").mkdir(exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler(); console.setFormatter(formatter)
    error_file = logging.FileHandler(ROOT / "data" / "errors.log", encoding="utf-8")
    error_file.setLevel(logging.WARNING); error_file.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[console, error_file])
    try:
        print_favorites() if args.favorites else main(args.debug)
    except KeyboardInterrupt:
        sys.exit(0)
