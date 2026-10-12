"""База публичного режима: пользователи, подписки, оплаты, поиски, избранное.

Весь SQL публичного режима собран здесь, по образцу BotDatabase из
TikTok-бота: остальной код вызывает методы с понятными именами и не знает
о таблицах. Схема дополняется миграциями колонок, поэтому старая база
открывается без потерь.

Время везде хранится как Unix-время (целые секунды), как и в таблицах режима
видеокарт: так проще сравнивать сроки доступа.

Каждому потоку — свой FeedDB поверх своего соединения: SQLite-соединение
нельзя делить между потоками, а WAL и busy_timeout позволяют писать из
обоих потоков без ошибок «database is locked».
"""
from __future__ import annotations

import hashlib
import sqlite3
import statistics
import time

DAY = 86400

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  chat_id            TEXT PRIMARY KEY,
  username           TEXT,
  first_name         TEXT,
  last_name          TEXT,
  language_code      TEXT,
  joined_at          INTEGER NOT NULL,
  last_seen_at       INTEGER,
  paid_until         INTEGER NOT NULL,
  plan               TEXT NOT NULL DEFAULT 'trial',      -- trial | paid (была оплата)
  min_discount       INTEGER NOT NULL DEFAULT 0,
  blocked            INTEGER NOT NULL DEFAULT 0,         -- заблокировал бота
  broadcast_opt_out  INTEGER NOT NULL DEFAULT 0,
  expiring_notified  INTEGER NOT NULL DEFAULT 0,         -- «закончится завтра»
  expired_notified   INTEGER NOT NULL DEFAULT 0,         -- «закончился»
  state              TEXT NOT NULL DEFAULT '',           -- ожидаемый ввод: receipt, broadcast
  state_data         TEXT NOT NULL DEFAULT '',
  admin_note         TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS payments (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id      TEXT NOT NULL,
  days         INTEGER NOT NULL DEFAULT 0,
  amount_kzt   INTEGER NOT NULL DEFAULT 0,
  method       TEXT NOT NULL,                 -- receipt | manual
  status       TEXT NOT NULL,                 -- pending | approved | rejected
  receipt_file TEXT,
  created_at   INTEGER NOT NULL,
  decided_at   INTEGER,
  decided_by   TEXT
);
CREATE TABLE IF NOT EXISTS watches (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id      TEXT NOT NULL,
  search_url   TEXT NOT NULL,
  title        TEXT NOT NULL,
  created_at   INTEGER NOT NULL,
  paused       INTEGER NOT NULL DEFAULT 0,
  sent_count   INTEGER NOT NULL DEFAULT 0,
  last_sent_at INTEGER,
  exclude_words TEXT NOT NULL DEFAULT '',     -- через запятую, ищутся в заголовке
  UNIQUE(chat_id, search_url)
);
CREATE TABLE IF NOT EXISTS deliveries (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id      TEXT NOT NULL,
  watch_id     INTEGER,
  ad_id        TEXT NOT NULL,
  title        TEXT,
  price_kzt    INTEGER,
  discount_pct INTEGER,
  url          TEXT,
  image_url    TEXT,
  sent_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS favorites (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id    TEXT NOT NULL,
  ad_id      TEXT NOT NULL,
  title      TEXT,
  price_kzt  INTEGER,
  url        TEXT,
  image_url  TEXT,
  saved_at   INTEGER NOT NULL,
  UNIQUE(chat_id, ad_id)
);
CREATE TABLE IF NOT EXISTS events (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT,
  kind    TEXT NOT NULL,
  detail  TEXT NOT NULL DEFAULT '',
  at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS deleted_users (
  chat_hash  TEXT PRIMARY KEY,                -- sha256 от chat_id, самого ID не храним
  deleted_at INTEGER NOT NULL
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
CREATE INDEX IF NOT EXISTS watches_url ON watches(search_url);
CREATE INDEX IF NOT EXISTS deliveries_chat ON deliveries(chat_id, sent_at);
CREATE INDEX IF NOT EXISTS payments_status ON payments(status, created_at);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, at);
"""

# Колонки, появившиеся после первой версии публичного режима. SQLite не умеет
# «ADD COLUMN IF NOT EXISTS», поэтому сверяемся с PRAGMA table_info.
MIGRATIONS = {
    "users": {
        "last_name": "TEXT", "language_code": "TEXT", "last_seen_at": "INTEGER",
        "plan": "TEXT NOT NULL DEFAULT 'trial'",
        "broadcast_opt_out": "INTEGER NOT NULL DEFAULT 0",
        "expiring_notified": "INTEGER NOT NULL DEFAULT 0",
        "state": "TEXT NOT NULL DEFAULT ''", "state_data": "TEXT NOT NULL DEFAULT ''",
        "admin_note": "TEXT NOT NULL DEFAULT ''",
    },
    "watches": {
        "paused": "INTEGER NOT NULL DEFAULT 0", "sent_count": "INTEGER NOT NULL DEFAULT 0",
        "last_sent_at": "INTEGER", "exclude_words": "TEXT NOT NULL DEFAULT ''",
    },
}


def now() -> int:
    return int(time.time())


def normalize_words(words: str) -> str:
    """«Чехол, стекло;дисплей» → «чехол, стекло, дисплей»: без дублей и пустых."""
    seen: list[str] = []
    for word in words.replace(";", ",").replace("\n", ",").split(","):
        word = " ".join(word.split()).casefold()[:40]
        if word and word not in seen:
            seen.append(word)
    return ", ".join(seen[:30])


def is_excluded(title: str, words: str) -> bool:
    """Ищем только в заголовке: в описании «без чехла» или «стекло целое» —
    рассказ о самом товаре, и по описанию отсеивались бы нормальные объявления."""
    low = (title or "").casefold()
    return any(word in low for word in words.split(", ") if word)


def chat_hash(chat_id) -> str:
    return hashlib.sha256(f"olx-radar:{chat_id}".encode()).hexdigest()


class FeedDB:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.c = connection
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA busy_timeout=5000")
        self.c.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {row[1] for row in self.c.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    self.c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        self.c.commit()

    def _one(self, sql: str, params=()) -> sqlite3.Row | None:
        return self.c.execute(sql, params).fetchone()

    def _all(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.c.execute(sql, params).fetchall()

    def _write(self, sql: str, params=()) -> sqlite3.Cursor:
        cursor = self.c.execute(sql, params)
        self.c.commit()
        return cursor

    # --- Пользователи -------------------------------------------------------
    def user(self, chat_id) -> sqlite3.Row | None:
        return self._one("SELECT * FROM users WHERE chat_id=?", (str(chat_id),))

    def touch_user(self, chat: dict, trial_days: int) -> tuple[sqlite3.Row, bool]:
        """Создаёт пользователя с пробным доступом или обновляет профиль.

        Возвращает (пользователь, создан_ли_сейчас). Любое сообщение снимает
        флаг blocked: раз человек пишет боту, значит, он его разблокировал.
        """
        chat_id, t = str(chat.get("id")), now()
        # Удалившему свои данные пробный период второй раз не положен: иначе
        # «удалиться и зайти заново» превращалось бы в бесконечный бесплатный доступ.
        trial = 0 if self._one("SELECT 1 FROM deleted_users WHERE chat_hash=?", (chat_hash(chat_id),)) else trial_days
        created = self._write(
            "INSERT OR IGNORE INTO users(chat_id,username,first_name,last_name,language_code,"
            "joined_at,last_seen_at,paid_until) VALUES(?,?,?,?,?,?,?,?)",
            (chat_id, chat.get("username"), chat.get("first_name"), chat.get("last_name"),
             chat.get("language_code"), t, t, t + trial * DAY)).rowcount == 1
        self._write(
            "UPDATE users SET username=COALESCE(?,username), first_name=COALESCE(?,first_name), "
            "last_name=COALESCE(?,last_name), last_seen_at=?, blocked=0 WHERE chat_id=?",
            (chat.get("username"), chat.get("first_name"), chat.get("last_name"), t, chat_id))
        if created:
            self.log(chat_id, "start")
        return self.user(chat_id), created

    def set_user(self, chat_id, **fields) -> None:
        columns = ", ".join(f"{name}=?" for name in fields)
        self._write(f"UPDATE users SET {columns} WHERE chat_id=?", (*fields.values(), str(chat_id)))

    def delete_user(self, chat_id) -> None:
        """«Удалить мои данные»: всё личное стирается, оплаты обезличиваются.

        Оплаты не удаляются целиком — по ним считается выручка, — но в них не
        остаётся ни ID, ни чека. Остаётся только хеш ID, чтобы не выдавать
        пробный период повторно.
        """
        chat_id = str(chat_id)
        for table in ("watches", "favorites", "deliveries", "events"):
            self.c.execute(f"DELETE FROM {table} WHERE chat_id=?", (chat_id,))
        self.c.execute("UPDATE payments SET chat_id='deleted', receipt_file=NULL, decided_by=NULL "
                       "WHERE chat_id=?", (chat_id,))
        self.c.execute("DELETE FROM users WHERE chat_id=?", (chat_id,))
        self.c.execute("INSERT OR REPLACE INTO deleted_users(chat_hash,deleted_at) VALUES(?,?)",
                       (chat_hash(chat_id), now()))
        self.c.commit()

    def receipts_since(self, chat_id, since: int) -> int:
        return self._one("SELECT COUNT(*) FROM payments WHERE chat_id=? AND method='receipt' AND created_at>=?",
                         (str(chat_id), since))[0]

    def find_user(self, target: str) -> sqlite3.Row | None:
        target = target.strip()
        if target.startswith("@"):
            return self._one("SELECT * FROM users WHERE username=? COLLATE NOCASE", (target[1:],))
        return self.user(target)

    def grant(self, chat_id, days: int, by: str = "", amount_kzt: int = 0,
              method: str = "manual", payment_id: int | None = None) -> sqlite3.Row | None:
        """Продлевает доступ от более поздней из дат: сейчас или текущий конец.

        Каждое продление — строка в payments: по ним считается выручка и
        видно, кто и когда платил.
        """
        user = self.user(chat_id)
        if not user:
            return None
        until = max(now(), user["paid_until"]) + days * DAY
        self.c.execute("UPDATE users SET paid_until=?, plan=CASE WHEN ?>0 THEN 'paid' ELSE plan END, "
                       "expired_notified=0, expiring_notified=0 WHERE chat_id=?",
                       (until, amount_kzt, user["chat_id"]))
        if payment_id:
            self.c.execute("UPDATE payments SET status='approved', days=?, amount_kzt=?, decided_at=?, "
                           "decided_by=? WHERE id=?", (days, amount_kzt, now(), by, payment_id))
        else:
            self.c.execute("INSERT INTO payments(chat_id,days,amount_kzt,method,status,created_at,"
                           "decided_at,decided_by) VALUES(?,?,?,?, 'approved', ?,?,?)",
                           (user["chat_id"], days, amount_kzt, method, now(), now(), by))
        self.c.commit()
        self.log(user["chat_id"], "grant", f"{days} дн.")
        return self.user(user["chat_id"])

    def users_page(self, offset: int, limit: int) -> list[sqlite3.Row]:
        return self._all(
            "SELECT u.*, (SELECT COUNT(*) FROM watches w WHERE w.chat_id=u.chat_id) watches_n "
            "FROM users u ORDER BY COALESCE(last_seen_at, joined_at) DESC LIMIT ? OFFSET ?", (limit, offset))

    def search_users(self, query: str, offset: int, limit: int) -> list[sqlite3.Row]:
        like = f"%{query.strip().lstrip('@')}%"
        return self._all(
            "SELECT u.*, (SELECT COUNT(*) FROM watches w WHERE w.chat_id=u.chat_id) watches_n FROM users u "
            "WHERE chat_id LIKE ? OR username LIKE ? OR first_name LIKE ? OR last_name LIKE ? "
            "ORDER BY COALESCE(last_seen_at, joined_at) DESC LIMIT ? OFFSET ?",
            (like, like, like, like, limit, offset))

    def user_count(self) -> int:
        return self._one("SELECT COUNT(*) FROM users")[0]

    def broadcast_targets(self) -> list[str]:
        return [row[0] for row in self._all(
            "SELECT chat_id FROM users WHERE blocked=0 AND broadcast_opt_out=0")]

    def expiring_users(self, within: int) -> list[sqlite3.Row]:
        """Доступ кончится в ближайшие within секунд, а напоминания ещё не было."""
        t = now()
        return self._all("SELECT * FROM users WHERE paid_until>? AND paid_until<=? AND expiring_notified=0 "
                         "AND blocked=0 AND chat_id IN (SELECT chat_id FROM watches)", (t, t + within))

    def expired_users(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM users WHERE paid_until<=? AND expired_notified=0 AND blocked=0 "
                         "AND chat_id IN (SELECT chat_id FROM watches)", (now(),))

    # --- Журнал действий ----------------------------------------------------
    def log(self, chat_id, kind: str, detail: str = "") -> None:
        self._write("INSERT INTO events(chat_id,kind,detail,at) VALUES(?,?,?,?)",
                    (str(chat_id) if chat_id is not None else None, kind, detail, now()))

    # --- Поиски -------------------------------------------------------------
    def watches(self, chat_id) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM watches WHERE chat_id=? ORDER BY id", (str(chat_id),))

    def watch(self, watch_id: int, chat_id=None) -> sqlite3.Row | None:
        if chat_id is None:
            return self._one("SELECT * FROM watches WHERE id=?", (watch_id,))
        return self._one("SELECT * FROM watches WHERE id=? AND chat_id=?", (watch_id, str(chat_id)))

    def add_watch(self, chat_id, search_url: str, title: str) -> int | None:
        """id нового поиска или None, если такой у пользователя уже есть."""
        cursor = self._write("INSERT OR IGNORE INTO watches(chat_id,search_url,title,created_at) VALUES(?,?,?,?)",
                             (str(chat_id), search_url, title, now()))
        if cursor.rowcount != 1:
            return None
        self.log(chat_id, "watch_add", search_url)
        return cursor.lastrowid

    def watch_by_url(self, chat_id, search_url: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM watches WHERE chat_id=? AND search_url=?", (str(chat_id), search_url))

    def delete_watch(self, watch_id: int, chat_id) -> bool:
        removed = self._write("DELETE FROM watches WHERE id=? AND chat_id=?", (watch_id, str(chat_id))).rowcount
        if removed:
            self.log(chat_id, "watch_delete", str(watch_id))
        return bool(removed)

    def set_watch_paused(self, watch_id: int, chat_id, paused: bool) -> bool:
        return bool(self._write("UPDATE watches SET paused=? WHERE id=? AND chat_id=?",
                                (int(paused), watch_id, str(chat_id))).rowcount)

    def set_watch_exclude(self, watch_id: int, chat_id, words: str) -> bool:
        return bool(self._write("UPDATE watches SET exclude_words=? WHERE id=? AND chat_id=?",
                                (normalize_words(words), watch_id, str(chat_id))).rowcount)

    def subscribers(self, search_url: str) -> list[sqlite3.Row]:
        """Подписчики выдачи: поиск не на паузе, бот не заблокирован."""
        return self._all("SELECT w.id watch_id, w.exclude_words, u.* FROM watches w JOIN users u ON u.chat_id=w.chat_id "
                         "WHERE w.search_url=? AND w.paused=0 AND u.blocked=0", (search_url,))

    def polled_urls(self) -> list[str]:
        return [row[0] for row in self._all("SELECT DISTINCT search_url FROM watches WHERE paused=0")]

    # --- Выдачи OLX и рынок -------------------------------------------------
    def url_baseline_done(self, search_url: str) -> bool:
        row = self._one("SELECT baseline_done FROM feed_urls WHERE search_url=?", (search_url,))
        return bool(row and row[0])

    def mark_polled(self, search_url: str, baseline_done: bool | None = None) -> None:
        self.c.execute("INSERT OR IGNORE INTO feed_urls(search_url) VALUES(?)", (search_url,))
        self.c.execute("UPDATE feed_urls SET last_polled=? WHERE search_url=?", (now(), search_url))
        if baseline_done is not None:
            self.c.execute("UPDATE feed_urls SET baseline_done=? WHERE search_url=?", (int(baseline_done), search_url))
        self.c.commit()

    def store_cards(self, search_url: str, cards) -> None:
        t = now()
        self.c.executemany(
            "INSERT OR IGNORE INTO feed_ads(search_url,ad_id,title,price_kzt,url,first_seen) VALUES(?,?,?,?,?,?)",
            [(search_url, c.ad_id, c.title, c.price_kzt, c.url, t) for c in cards])
        self.c.commit()

    def known_ads(self, search_url: str, ad_ids: list[str]) -> set[str]:
        if not ad_ids:
            return set()
        marks = ",".join("?" * len(ad_ids))
        return {row[0] for row in self._all(
            f"SELECT ad_id FROM feed_ads WHERE search_url=? AND ad_id IN ({marks})", (search_url, *ad_ids))}

    def market(self, search_url: str, window_days: int) -> tuple[int | None, int]:
        """Медиана цен выдачи за окно и число объявлений, по которым она посчитана.

        Медиана, а не среднее: одно «iPhone за 1 ₸» или опечатка с лишним нулём
        сдвинули бы среднее на десятки процентов, медиану — почти нет.
        """
        prices = [row[0] for row in self._all(
            "SELECT price_kzt FROM feed_ads WHERE search_url=? AND first_seen>=? AND price_kzt>0",
            (search_url, now() - window_days * DAY))]
        return (int(statistics.median(prices)) if prices else None), len(prices)

    # --- Доставки и избранное -----------------------------------------------
    def record_delivery(self, chat_id, watch_id: int, card, discount_pct: int | None) -> int:
        cursor = self.c.execute(
            "INSERT INTO deliveries(chat_id,watch_id,ad_id,title,price_kzt,discount_pct,url,image_url,sent_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (str(chat_id), watch_id, card.ad_id, card.title, card.price_kzt, discount_pct, card.url,
             card.image_url, now()))
        self.c.execute("UPDATE watches SET sent_count=sent_count+1, last_sent_at=? WHERE id=?", (now(), watch_id))
        self.c.commit()
        return cursor.lastrowid

    def delivery(self, delivery_id: int, chat_id) -> sqlite3.Row | None:
        return self._one("SELECT * FROM deliveries WHERE id=? AND chat_id=?", (delivery_id, str(chat_id)))

    def recent_deliveries(self, chat_id, limit: int = 50) -> list[sqlite3.Row]:
        """Лента: последние присланные объявления, с отметкой избранного."""
        return self._all(
            "SELECT d.*, EXISTS(SELECT 1 FROM favorites f WHERE f.chat_id=d.chat_id AND f.ad_id=d.ad_id) favorite "
            "FROM deliveries d WHERE d.chat_id=? ORDER BY d.sent_at DESC, d.id DESC LIMIT ?", (str(chat_id), limit))

    def add_favorite(self, delivery: sqlite3.Row) -> bool:
        added = self._write(
            "INSERT OR IGNORE INTO favorites(chat_id,ad_id,title,price_kzt,url,image_url,saved_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (delivery["chat_id"], delivery["ad_id"], delivery["title"], delivery["price_kzt"],
             delivery["url"], delivery["image_url"], now())).rowcount == 1
        if added:
            self.log(delivery["chat_id"], "favorite_add", delivery["ad_id"])
        return added

    def is_favorite(self, chat_id, ad_id: str) -> bool:
        return self._one("SELECT 1 FROM favorites WHERE chat_id=? AND ad_id=?", (str(chat_id), ad_id)) is not None

    def remove_favorite_ad(self, chat_id, ad_id: str) -> bool:
        return bool(self._write("DELETE FROM favorites WHERE chat_id=? AND ad_id=?", (str(chat_id), ad_id)).rowcount)

    def favorites(self, chat_id, limit: int = 20) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM favorites WHERE chat_id=? ORDER BY saved_at DESC LIMIT ?",
                         (str(chat_id), limit))

    def delete_favorite(self, favorite_id: int, chat_id) -> bool:
        return bool(self._write("DELETE FROM favorites WHERE id=? AND chat_id=?",
                                (favorite_id, str(chat_id))).rowcount)

    def count(self, table: str, chat_id) -> int:
        assert table in {"watches", "favorites", "deliveries", "payments"}
        return self._one(f"SELECT COUNT(*) FROM {table} WHERE chat_id=?", (str(chat_id),))[0]

    # --- Оплаты -------------------------------------------------------------
    def create_receipt(self, chat_id, file_id: str | None) -> int:
        cursor = self._write("INSERT INTO payments(chat_id,method,status,receipt_file,created_at) "
                             "VALUES(?, 'receipt', 'pending', ?, ?)", (str(chat_id), file_id, now()))
        self.log(chat_id, "receipt")
        return cursor.lastrowid

    def payment(self, payment_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM payments WHERE id=?", (payment_id,))

    def reject_payment(self, payment_id: int, by: str) -> bool:
        return bool(self._write("UPDATE payments SET status='rejected', decided_at=?, decided_by=? "
                                "WHERE id=? AND status='pending'", (now(), by, payment_id)).rowcount)

    def pending_payments(self) -> list[sqlite3.Row]:
        return self._all("SELECT p.*, u.username, u.first_name FROM payments p LEFT JOIN users u USING(chat_id) "
                         "WHERE p.status='pending' ORDER BY p.created_at")

    def payments_of(self, chat_id, limit: int = 5) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM payments WHERE chat_id=? ORDER BY created_at DESC LIMIT ?",
                         (str(chat_id), limit))

    # --- Статистика ---------------------------------------------------------
    def stats(self) -> dict:
        t = now()
        today = t - t % DAY  # полночь UTC: для обзора точности хватает
        month = t - 30 * DAY
        q = lambda sql, *p: self._one(sql, p)[0] or 0  # noqa: E731
        return {
            "users": q("SELECT COUNT(*) FROM users"),
            "new_today": q("SELECT COUNT(*) FROM users WHERE joined_at>=?", today),
            "new_7d": q("SELECT COUNT(*) FROM users WHERE joined_at>=?", t - 7 * DAY),
            "active_7d": q("SELECT COUNT(*) FROM users WHERE last_seen_at>=?", t - 7 * DAY),
            "with_access": q("SELECT COUNT(*) FROM users WHERE paid_until>?", t),
            "trial": q("SELECT COUNT(*) FROM users WHERE paid_until>? AND plan='trial'", t),
            "paid": q("SELECT COUNT(*) FROM users WHERE paid_until>? AND plan='paid'", t),
            "ever_paid": q("SELECT COUNT(DISTINCT chat_id) FROM payments WHERE status='approved' AND amount_kzt>0"),
            "blocked": q("SELECT COUNT(*) FROM users WHERE blocked=1"),
            "watches": q("SELECT COUNT(*) FROM watches"),
            "urls": q("SELECT COUNT(DISTINCT search_url) FROM watches WHERE paused=0"),
            "sent_today": q("SELECT COUNT(*) FROM deliveries WHERE sent_at>=?", today),
            "sent_7d": q("SELECT COUNT(*) FROM deliveries WHERE sent_at>=?", t - 7 * DAY),
            "favorites": q("SELECT COUNT(*) FROM favorites"),
            "revenue_30d": q("SELECT SUM(amount_kzt) FROM payments WHERE status='approved' AND decided_at>=?", month),
            "revenue_total": q("SELECT SUM(amount_kzt) FROM payments WHERE status='approved'"),
            "pending": q("SELECT COUNT(*) FROM payments WHERE status='pending'"),
        }
