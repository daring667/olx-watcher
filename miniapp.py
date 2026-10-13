"""Telegram Mini App: приложение для управления подпиской прямо в Telegram.

Отдаёт одну страницу (webapp/index.html) и JSON API к той же базе, что и бот.
Схема та же, что у Mini App TikTok-бота: процесс слушает только 127.0.0.1,
наружу его выставляет Tailscale Funnel по HTTPS.

Входа по паролю нет. Каждый запрос несёт заголовок X-Telegram-Init-Data —
строку, которую Telegram подписывает токеном бота, когда открывает Mini App.
Подпись проверяется HMAC-ом, и из неё же берётся пользователь. Подделать
чужой ID без токена бота нельзя.

Запуск: python miniapp.py (адрес — MINIAPP_LISTEN, по умолчанию 127.0.0.1:5051).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl

from flask import Flask, abort, g, jsonify, request, send_file

import olx_feed as feed
import olx_watcher as w
from feed_db import FeedDB

ROOT = Path(__file__).parent
PAGE = ROOT / "webapp" / "index.html"
app = Flask(__name__)


class StripPrefix:
    """Tailscale отдаёт приложение по пути /olx/… и может не срезать префикс.

    Страница обращается к API относительными адресами, поэтому приложению
    всё равно, под каким путём его опубликовали.
    """
    def __init__(self, wsgi, prefix: str = "/olx") -> None:
        self.wsgi, self.prefix = wsgi, prefix

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path == self.prefix or path.startswith(self.prefix + "/"):
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
        return self.wsgi(environ, start_response)


app.wsgi_app = StripPrefix(app.wsgi_app)


class AuthError(Exception):
    pass


def validate_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> dict:
    """Проверка подписи Telegram Web App (как api/auth.py в TikTok-боте)."""
    if not init_data or not bot_token:
        raise AuthError("Откройте приложение из Telegram")
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received = fields.pop("hash", "")
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not received or not hmac.compare_digest(received, expected):
        raise AuthError("Подпись Telegram не прошла проверку")
    try:
        auth_date = int(fields["auth_date"])
        user = json.loads(fields["user"])
        int(user["id"])
    except (KeyError, ValueError, TypeError) as error:
        raise AuthError("Неполные данные Telegram") from error
    if max_age >= 0 and time.time() - auth_date > max_age:
        raise AuthError("Сессия устарела — откройте приложение заново")
    return user


def context() -> tuple[dict, str, set[str]]:
    w.load_dotenv(ROOT / ".env")
    config = w.load_config()
    return config, os.environ.get("TELEGRAM_BOT_TOKEN", ""), {
        cid.strip() for cid in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()}


@app.before_request
def authenticate():
    if not request.path.startswith("/api/"):
        return None
    config, token, owner_ids = context()
    g.cfg, g.token, g.owner_ids = feed.settings(config), token, owner_ids
    try:
        tg_user = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""), token)
    except AuthError as error:
        return jsonify(error=str(error)), 401
    # Отдельное соединение на запрос: waitress обслуживает запросы в нескольких потоках.
    g.connection = sqlite3.connect(w.STATE_PATH)
    g.db = FeedDB(g.connection)
    g.user, _ = g.db.touch_user(tg_user, int(g.cfg["trial_days"]))
    g.owner = g.user["chat_id"] in owner_ids
    if not g.cfg["enabled"] and not g.owner:
        return jsonify(error="Сервис пока закрыт"), 403
    # Лимит на пользователя: приложение открыто в интернет, и подписанный
    # initData не мешает долбить API в цикле.
    if not g.owner and feed.flooding("app:" + g.user["chat_id"], limit=90, window=60):
        return jsonify(error="Слишком много запросов — подождите минуту"), 429
    return None


@app.teardown_request
def close_connection(_error):
    connection = g.pop("connection", None)
    if connection is not None:
        connection.close()


def error(message: str, status: int = 400):
    return jsonify(error=message), status


def body() -> dict:
    return request.get_json(silent=True) or {}


def require_owner():
    if not g.owner:
        abort(403)


# --- Сериализация -----------------------------------------------------------
def access(user) -> dict:
    active = feed.is_active(user, g.owner_ids)
    return {"active": active, "owner": user["chat_id"] in g.owner_ids, "plan": user["plan"],
            "until": user["paid_until"], "left": feed.left_text(user["paid_until"]) if active else ""}


def watch_json(row) -> dict:
    median, count = g.db.market(row["search_url"], int(g.cfg["window_days"]))
    enough = median is not None and count >= int(g.cfg["min_samples"])
    return {"id": row["id"], "title": row["title"], "url": row["search_url"], "paused": bool(row["paused"]),
            "sent": row["sent_count"], "last_sent_at": row["last_sent_at"], "created_at": row["created_at"],
            "median": median if enough else None, "samples": count, "exclude": row["exclude_words"]}


def ad_json(row) -> dict:
    keys = row.keys()
    current = row["current_price"] if "current_price" in keys and row["current_price"] is not None else row["price_kzt"]
    return {"id": row["id"], "ad_id": row["ad_id"], "title": row["title"], "price": current,
            "old_price": row["price_kzt"] if current is not None and row["price_kzt"] and current < row["price_kzt"] else None,
            "gone": bool(row["gone"]) if "gone" in keys else False,
            "url": row["url"], "image": row["image_url"],
            "discount": row["discount_pct"] if "discount_pct" in keys else None,
            "at": row["sent_at"] if "sent_at" in keys else row["saved_at"],
            "favorite": bool(row["favorite"]) if "favorite" in keys else True,
            "watch_id": row["watch_id"] if "watch_id" in keys else None,
            "watch_title": row["watch_title"] if "watch_title" in keys else None}


def user_json(user) -> dict:
    return {"chat_id": user["chat_id"], "username": user["username"], "first_name": user["first_name"],
            "last_name": user["last_name"], "joined_at": user["joined_at"], "last_seen_at": user["last_seen_at"],
            "paid_until": user["paid_until"], "plan": user["plan"], "blocked": bool(user["blocked"]),
            "min_discount": user["min_discount"], "access": access(user), "referrer": user["referrer"]}


# --- Страница ---------------------------------------------------------------
@app.get("/")
def page():
    response = send_file(PAGE)
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/health")
def health():
    return {"ok": True}


# --- Пользователь -------------------------------------------------------------
@app.get("/api/me")
def me():
    user, db = g.user, g.db
    pending = any(p["status"] == "pending" for p in db.payments_of(user["chat_id"]))
    return jsonify(
        brand=g.cfg["brand"], user=user_json(user), access=access(user),
        max_watches=None if g.owner else int(g.cfg["max_watches"]),
        accessory_words=feed.ACCESSORY_WORDS, states=list(feed.STATES.items()),
        filter_steps=list(feed.FILTER_STEPS), price=int(g.cfg["price_kzt"]),
        period_days=int(g.cfg["period_days"]), payment_text=g.cfg["payment_text"],
        price_stars=int(g.cfg["price_stars"]),
        # Списками пар, а не словарями: jsonify сортирует ключи, и «Алматы» уезжал в середину.
        pending_payment=pending, cities=list(feed.CITIES.items()),
        watches=[watch_json(row) for row in db.watches(user["chat_id"])],
        favorites_count=db.count("favorites", user["chat_id"]),
        referral=dict(zip(("invited", "paid"), db.referral_stats(user["chat_id"])),
                      link=feed.referral_link(g.token, user["chat_id"]), days=feed.REFERRAL_DAYS),
    )


def search_from_body(data: dict) -> tuple[str | None, str]:
    """Ссылка поиска из запроса приложения: готовая ссылка или конструктор + фильтры."""
    if data.get("url") and "/obyavlenie/" in data["url"]:
        return None, "Это ссылка на одно объявление. Нужна ссылка на список с фильтрами."
    try:
        low = int(data["price_from"]) if data.get("price_from") else None
        high = int(data["price_to"]) if data.get("price_to") else None
    except (TypeError, ValueError):
        return None, "Цена — только цифры"
    if low and high and low > high:
        return None, "Цена «от» больше, чем «до»"
    base = data.get("url") or feed.search_path(data.get("query", ""), data.get("city", "list"))
    url = feed.apply_filters(base, low, high, data.get("state", "")) if base else None
    return url, "" if url else "Не получилось составить поиск — проверьте ссылку или запрос"


@app.post("/api/preview")
def preview_search():
    """Что найдётся по поиску — до сохранения. Ходит в OLX, поэтому свой лимит."""
    url, problem = search_from_body(body())
    if not url:
        return error(problem)
    if not g.owner and feed.flooding("preview:" + g.user["chat_id"], limit=20, window=60):
        return error("Слишком часто — подождите минуту", 429)
    try:
        return jsonify(feed.preview(feed.make_session(g.cfg["proxy"]), url, body().get("exclude", "")))
    except Exception:
        logging.exception("preview %s", url)
        return error("OLX сейчас не ответил — попробуйте ещё раз", 502)


@app.post("/api/watches")
def add_watch():
    data, user = body(), g.user
    if not feed.is_active(user, g.owner_ids):
        return error("Доступ закончился — продлите подписку во вкладке «Профиль»", 402)
    url, problem = search_from_body(data)
    if not url:
        return error(problem)
    limit = int(g.cfg["max_watches"])
    if not g.owner and g.db.count("watches", user["chat_id"]) >= limit:
        return error(f"Можно следить максимум за {limit} поисками — удалите ненужный")
    # Название из приложения человекочитаемое («Компьютеры и комплектующие»),
    # а не из адреса («kompyutery-i-komplektuyuschie»).
    title = " ".join(str(data.get("title") or "").split())[:90] or feed.watch_title(url)
    watch_id = g.db.add_watch(user["chat_id"], url, title)
    if watch_id is None:
        return error("За этим поиском вы уже следите")
    if data.get("exclude"):
        g.db.set_watch_exclude(watch_id, user["chat_id"], str(data["exclude"]))
    return jsonify(watch=watch_json(g.db.watch(watch_id)))


@app.patch("/api/watches/<int:watch_id>")
def update_watch(watch_id: int):
    data, chat_id = body(), g.user["chat_id"]
    if not g.db.watch(watch_id, chat_id):
        return error("Поиск не найден", 404)
    if "paused" in data:
        g.db.set_watch_paused(watch_id, chat_id, bool(data["paused"]))
        g.db.log(chat_id, "watch_pause" if data["paused"] else "watch_resume", str(watch_id))
    if "exclude" in data:
        g.db.set_watch_exclude(watch_id, chat_id, str(data["exclude"] or ""))
    if data.get("query") or data.get("url"):
        # Изменили сам поиск: новая ссылка, название. Новую выдачу опрос сам
        # «запомнит» без рассылки, а уже присланное повторно не придёт.
        url, problem = search_from_body(data)
        if not url:
            return error(problem)
        title = " ".join(str(data.get("title") or "").split())[:90] or feed.watch_title(url)
        if not g.db.update_watch_search(watch_id, chat_id, url, title):
            return error("Такой поиск у вас уже есть")
    return jsonify(watch=watch_json(g.db.watch(watch_id)))


@app.get("/api/watches/<int:watch_id>/form")
def watch_form(watch_id: int):
    """Поля конструктора для «✏️ Настроить»."""
    watch = g.db.watch(watch_id, g.user["chat_id"])
    if not watch:
        return error("Поиск не найден", 404)
    return jsonify(form={**feed.parse_search(watch["search_url"], watch["title"]),
                         "exclude": watch["exclude_words"]})


@app.delete("/api/watches/<int:watch_id>")
def delete_watch(watch_id: int):
    if not g.db.delete_watch(watch_id, g.user["chat_id"]):
        return error("Поиск не найден", 404)
    return jsonify(ok=True)


@app.post("/api/settings")
def update_settings():
    value = body().get("min_discount")
    if value not in feed.FILTER_STEPS:
        return error("Недопустимое значение фильтра")
    g.db.set_user(g.user["chat_id"], min_discount=value)
    g.db.log(g.user["chat_id"], "filter", str(value))
    return jsonify(ok=True)


@app.get("/api/feed")
def deliveries():
    # Фильтры и сортировка ленты — на стороне приложения, поэтому отдаём с запасом.
    limit = min(max(request.args.get("limit", 200, type=int), 1), 300)
    return jsonify(items=[ad_json(row) for row in g.db.recent_deliveries(g.user["chat_id"], limit)])


@app.get("/api/favorites")
def favorites():
    return jsonify(items=[ad_json(row) for row in g.db.favorites(g.user["chat_id"], 100)])


@app.post("/api/feed/<int:delivery_id>/favorite")
def toggle_favorite(delivery_id: int):
    delivery = g.db.delivery(delivery_id, g.user["chat_id"])
    if not delivery:
        return error("Объявление не найдено", 404)
    if g.db.is_favorite(g.user["chat_id"], delivery["ad_id"]):
        g.db.remove_favorite_ad(g.user["chat_id"], delivery["ad_id"])
        return jsonify(favorite=False)
    g.db.add_favorite(delivery)
    return jsonify(favorite=True)


@app.delete("/api/favorites/<int:favorite_id>")
def delete_favorite(favorite_id: int):
    if not g.db.delete_favorite(favorite_id, g.user["chat_id"]):
        return error("Не найдено", 404)
    return jsonify(ok=True)


@app.delete("/api/me")
def delete_me():
    """«Удалить мои данные» из профиля приложения."""
    g.db.delete_user(g.user["chat_id"])
    return jsonify(ok=True)


@app.post("/api/invoice")
def invoice():
    """Ссылка на счёт в Stars — приложение открывает его через tg.openInvoice."""
    if not int(g.cfg["price_stars"]):
        return error("Оплата звёздами выключена")
    try:
        return jsonify(link=feed.invoice_link(g.token, g.user["chat_id"], g.cfg))
    except Exception:
        logging.exception("createInvoiceLink")
        return error("Не удалось создать счёт — попробуйте ещё раз", 502)


@app.post("/api/paid")
def paid():
    """«Я оплатил»: чек удобнее всего прислать фото в чат с ботом."""
    g.db.set_user(g.user["chat_id"], state="receipt")
    feed.safe_send(g.db, g.token, g.user["chat_id"],
                   "📸 <b>Пришлите скриншот или фото чека</b> сюда, в чат.\n"
                   "Я передам его на проверку и напишу, когда доступ будет продлён.")
    return jsonify(ok=True)


# --- Админка ----------------------------------------------------------------
@app.get("/api/admin/stats")
def admin_stats():
    require_owner()
    pending = [{"id": p["id"], "chat_id": p["chat_id"], "username": p["username"], "first_name": p["first_name"],
                "created_at": p["created_at"]} for p in g.db.pending_payments()]
    return jsonify(stats=g.db.stats(), pending=pending, broadcast_targets=len(g.db.broadcast_targets()))


@app.get("/api/admin/users")
def admin_users():
    require_owner()
    page_n = max(0, request.args.get("page", 0, type=int))
    query = request.args.get("q", "").strip()
    rows = g.db.search_users(query, page_n * 20, 20) if query else g.db.users_page(page_n * 20, 20)
    return jsonify(items=[{**user_json(row), "watches": row["watches_n"]} for row in rows],
                   total=g.db.user_count())


@app.get("/api/admin/users/<chat_id>")
def admin_user(chat_id: str):
    require_owner()
    user = g.db.user(chat_id)
    if not user:
        return error("Пользователь не найден", 404)
    payments = [dict(p) for p in g.db.payments_of(chat_id, 20)]
    return jsonify(user=user_json(user), watches=[watch_json(row) for row in g.db.watches(chat_id)],
                   payments=payments, favorites=g.db.count("favorites", chat_id),
                   deliveries=g.db.count("deliveries", chat_id))


@app.post("/api/admin/users/<chat_id>/grant")
def admin_grant(chat_id: str):
    require_owner()
    data = body()
    try:
        days = int(data.get("days"))
    except (TypeError, ValueError):
        return error("Укажите число дней")
    amount = round(int(g.cfg["price_kzt"]) * days / int(g.cfg["period_days"])) if data.get("paid") else 0
    user = g.db.grant(chat_id, days, by=g.user["chat_id"], amount_kzt=amount)
    if not user:
        return error("Пользователь не найден", 404)
    if days > 0:
        feed.notify_granted(g.db, g.token, user)
    return jsonify(user=user_json(user))


@app.post("/api/admin/payments/<int:payment_id>/<decision>")
def admin_payment(payment_id: int, decision: str):
    require_owner()
    payment = g.db.payment(payment_id)
    if not payment or payment["status"] != "pending":
        return error("Этот чек уже обработан")
    if decision == "approve":
        user = g.db.grant(payment["chat_id"], int(g.cfg["period_days"]), by=g.user["chat_id"],
                          amount_kzt=int(g.cfg["price_kzt"]), method="receipt", payment_id=payment_id)
        feed.notify_granted(g.db, g.token, user)
    elif decision == "reject":
        g.db.reject_payment(payment_id, g.user["chat_id"])
        feed.safe_send(g.db, g.token, payment["chat_id"],
                       "❌ <b>Оплата не подтверждена.</b>\nПроверьте сумму и реквизиты и отправьте чек ещё раз.")
    else:
        return error("Неизвестное действие", 404)
    return jsonify(ok=True)


@app.post("/api/admin/broadcast")
def admin_broadcast():
    require_owner()
    text = (body().get("text") or "").strip()
    if not text:
        return error("Пустой текст")
    targets, token, owner = g.db.broadcast_targets(), g.token, g.user["chat_id"]

    def run() -> None:
        connection = sqlite3.connect(w.STATE_PATH)
        db = FeedDB(connection)
        delivered = 0
        for target in targets:
            delivered += feed.safe_send(db, token, target, text)
            time.sleep(0.05)  # Telegram ограничивает массовую отправку ~30 сообщениями в секунду
        db.log(owner, "broadcast", f"{delivered}/{len(targets)}")
        feed.safe_send(db, token, owner, f"📣 Рассылка завершена: доставлено {delivered} из {len(targets)}.")
        connection.close()

    threading.Thread(target=run, daemon=True, name="broadcast").start()
    return jsonify(queued=len(targets))


if __name__ == "__main__":
    from waitress import serve

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    host, _, port = os.environ.get("MINIAPP_LISTEN", "127.0.0.1:5051").rpartition(":")
    serve(app, host=host or "127.0.0.1", port=int(port), threads=8)
