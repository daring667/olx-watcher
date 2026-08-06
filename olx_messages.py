"""Уведомления о сообщениях OLX через Partner API. СЕЙЧАС НЕ РАБОТАЕТ.

Заявка на доступ отклонена 6 августа 2026 с формулировкой «Data Scraping /
Competitor Monitoring», ключей приложения нет. Модуль оставлен на случай
повторной подачи заявки; рабочий путь сейчас — olx_mail.py, через почту.

Работает через официальный Partner API OLX: приложение действует от вашего
имени по OAuth (grant authorization_code), пароль от OLX нигде не участвует.

Порядок первой настройки:
    python olx_messages.py --auth-url     напечатает ссылку, откройте её
    python olx_messages.py --auth-code X  обменяет код на токены
    python olx_messages.py --dump         покажет сырой ответ API
    python olx_messages.py --once         разовая проверка
Постоянная работа — сервисом olx-messages.service.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
import time
from secrets import token_urlsafe
from urllib.parse import urlencode

import requests

from olx_watcher import (NETWORK_ERRORS, ROOT, Storage, load_dotenv, olx_session,
                         safe_error, telegram_request)

AUTHORIZE_URL = "https://www.olx.kz/oauth/authorize"
TOKEN_URL = "https://www.olx.kz/api/open/oauth/token"
API_BASE = "https://www.olx.kz/api/partner"
SCOPE = "v2 read write"
REDIRECT_URI = "https://daring667.github.io/olx-watcher/callback/"


def credentials() -> tuple[str, str]:
    load_dotenv(ROOT / ".env")
    client_id, secret = os.getenv("OLX_CLIENT_ID"), os.getenv("OLX_CLIENT_SECRET")
    if not client_id or not secret or "replace_me" in f"{client_id}{secret}":
        raise SystemExit("Заполните OLX_CLIENT_ID и OLX_CLIENT_SECRET в .env.")
    return client_id, secret


def auth_url() -> str:
    client_id, _ = credentials()
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": client_id, "response_type": "code", "scope": SCOPE,
        "redirect_uri": REDIRECT_URI, "state": token_urlsafe(16),
    })


def request_tokens(payload: dict) -> dict:
    # Тот же curl_cffi, что и для выдачи: весь www.olx.kz стоит за CloudFront,
    # который режет обычный requests по TLS-отпечатку.
    session = olx_session()
    response = session.post(TOKEN_URL, json=payload, timeout=30)
    if response.status_code >= 400:
        raise SystemExit(f"OLX отклонил запрос токена ({response.status_code}): {response.text[:300]}")
    return response.json()


def save_tokens(storage: Storage, tokens: dict) -> None:
    storage.set_setting("olx_access_token", tokens["access_token"])
    # Токены OLX переходят на JWT длиной до 4096 символов; meta.value — TEXT,
    # ограничения по длине нет.
    if tokens.get("refresh_token"):
        storage.set_setting("olx_refresh_token", tokens["refresh_token"])
    storage.set_setting("olx_token_expires", str(int(time.time()) + int(tokens.get("expires_in", 3600)) - 60))


def exchange_code(code: str) -> None:
    client_id, secret = credentials()
    tokens = request_tokens({
        "grant_type": "authorization_code", "client_id": client_id, "client_secret": secret,
        "code": code, "scope": SCOPE, "redirect_uri": REDIRECT_URI,
    })
    storage = Storage()
    try:
        save_tokens(storage, tokens)
        print("Токены сохранены. Обновление дальше происходит само, повторять авторизацию не нужно.")
    finally:
        storage.close()


def access_token(storage: Storage) -> str:
    """Действующий токен: обновляет по refresh, когда срок истёк."""
    expires = int(storage.setting("olx_token_expires", "0") or 0)
    token = storage.setting("olx_access_token")
    if token and time.time() < expires:
        return token
    refresh = storage.setting("olx_refresh_token")
    if not refresh:
        raise SystemExit("Нет refresh-токена. Пройдите авторизацию: --auth-url, затем --auth-code.")
    client_id, secret = credentials()
    tokens = request_tokens({
        "grant_type": "refresh_token", "client_id": client_id,
        "client_secret": secret, "refresh_token": refresh,
    })
    save_tokens(storage, tokens)
    return tokens["access_token"]


def api_get(storage: Storage, path: str, **params: object) -> dict:
    session = olx_session()
    response = session.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {access_token(storage)}", "Version": "2.0"},
        params=params or None, timeout=30)
    response.raise_for_status()
    return response.json()


def field(item: dict, *names: str, default: object = None) -> object:
    """Первое непустое из возможных имён поля.

    Точная схема ответа станет известна на живом аккаунте, а имена полей у OLX
    в разных странах отличаются — поэтому берём несколько вариантов сразу,
    вместо того чтобы падать на неугаданном ключе.
    """
    for name in names:
        value = item.get(name)
        if value not in (None, "", [], {}):
            return value
    return default


def is_incoming(message: dict) -> bool:
    kind = str(field(message, "type", "direction", default="")).lower()
    if kind in {"received", "incoming", "in"}:
        return True
    if kind in {"sent", "outgoing", "out"}:
        return False
    # Если поля направления нет, считаем входящим всё, что не помечено своим.
    return not bool(field(message, "is_own", "own", default=False))


def message_text(message: dict) -> str:
    return str(field(message, "text", "body", "content", default="") or "")[:600]


def notify_message(token: str, chat_id: str, thread: dict, message: dict) -> None:
    advert = field(thread, "advert", "ad", default={}) or {}
    title = str(field(advert, "title", "name", default="объявление"))
    url = str(field(advert, "url", "link", default=""))
    interlocutor = field(thread, "interlocutor", "user", "buyer", default={}) or {}
    who = str(field(interlocutor, "name", "login", default="покупатель"))
    text = (f"💬 <b>Новое сообщение на OLX</b>\n"
            f"От: {html.escape(who)}\n"
            f"Объявление: {html.escape(title)}\n\n"
            f"{html.escape(message_text(message))}")
    if url:
        text += f'\n\n<a href="{html.escape(str(url), quote=True)}">Открыть объявление</a>'
    telegram_request(token, "sendMessage", chat_id=chat_id, text=text,
                     parse_mode="HTML", disable_web_page_preview=True)


def poll_once(storage: Storage, token: str, chat_id: str, dry_run: bool = False) -> int:
    threads = field(api_get(storage, "/threads"), "data", "threads", default=[]) or []
    sent = 0
    for thread in threads:
        thread_id = field(thread, "id", "thread_id")
        if thread_id is None:
            continue
        key = f"olx_last_message:{thread_id}"
        seen = storage.setting(key)
        payload = api_get(storage, f"/threads/{thread_id}/messages")
        messages = field(payload, "data", "messages", default=[]) or []
        # Первый проход по новой переписке только запоминает границу: иначе
        # подключение бота обернулось бы пересылкой всей истории сразу.
        newest = str(field(messages[0], "id", default="")) if messages else ""
        if not seen:
            storage.set_setting(key, newest)
            continue
        fresh = []
        for message in messages:
            if str(field(message, "id", default="")) == seen:
                break
            if is_incoming(message):
                fresh.append(message)
        for message in reversed(fresh):
            if dry_run:
                print(f"  [{thread_id}] {message_text(message)[:70]}")
            else:
                notify_message(token, chat_id, thread, message)
            sent += 1
        if newest:
            storage.set_setting(key, newest)
    return sent


def watch(interval: int) -> None:
    load_dotenv(ROOT / ".env")
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise SystemExit("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env.")
    storage = Storage()
    logging.info("Слежу за сообщениями OLX, проверка раз в %d сек.", interval)
    try:
        while True:
            try:
                sent = poll_once(storage, token, chat_id)
                if sent:
                    logging.info("Переслано новых сообщений: %d", sent)
            except NETWORK_ERRORS as error:
                storage.record_error("Сообщения OLX", Exception(safe_error(error)))
                logging.warning("Не удалось прочитать переписки: %s", safe_error(error))
            except requests.RequestException as error:
                logging.warning("Telegram: %s", safe_error(error))
            time.sleep(interval)
    finally:
        storage.close()


def dump() -> None:
    """Сырой ответ API — чтобы сверить имена полей на живом аккаунте."""
    storage = Storage()
    try:
        threads = api_get(storage, "/threads")
        print(json.dumps(threads, ensure_ascii=False, indent=2)[:3000])
        items = field(threads, "data", "threads", default=[]) or []
        if items:
            thread_id = field(items[0], "id", "thread_id")
            print(f"\n--- сообщения переписки {thread_id} ---")
            print(json.dumps(api_get(storage, f"/threads/{thread_id}/messages"),
                             ensure_ascii=False, indent=2)[:3000])
    finally:
        storage.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Сообщения OLX в Telegram")
    parser.add_argument("--auth-url", action="store_true", help="напечатать ссылку авторизации")
    parser.add_argument("--auth-code", metavar="CODE", help="обменять код авторизации на токены")
    parser.add_argument("--dump", action="store_true", help="сырой ответ API для сверки полей")
    parser.add_argument("--once", action="store_true", help="одна проверка без отправки")
    parser.add_argument("--interval", type=int, default=120, help="секунд между проверками")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.auth_url:
            print("Откройте ссылку, разрешите доступ, затем запустите --auth-code с полученным кодом:\n")
            print(auth_url())
        elif args.auth_code:
            exchange_code(args.auth_code)
        elif args.dump:
            dump()
        elif args.once:
            storage = Storage()
            try:
                print(f"Новых входящих: {poll_once(storage, '', '', dry_run=True)}")
            finally:
                storage.close()
        else:
            watch(args.interval)
    except KeyboardInterrupt:
        sys.exit(0)
