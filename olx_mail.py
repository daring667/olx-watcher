"""Уведомления в Telegram о сообщениях OLX — через почту.

Запасной путь после отказа в доступе к Partner API. OLX присылает письмо на
каждое новое сообщение в переписке; бот читает почтовый ящик и пересылает их
в Telegram.

Ящик открывается ТОЛЬКО НА ЧТЕНИЕ: флаги писем не меняются, ничего не
удаляется и не помечается прочитанным. Позиция запоминается по UID в своей
базе, поэтому состояние вашей почты остаётся ровно таким, как было.

Настройка:
    python olx_mail.py --check    проверить подключение и показать письма
    python olx_mail.py --dump     сырой разбор последнего письма
    python olx_mail.py --once     разовая проверка без отправки
"""
from __future__ import annotations

import argparse
import email
import html
import imaplib
import logging
import os
import re
import sys
import time
from email.header import decode_header, make_header
from email.message import Message

import requests

from olx_watcher import ROOT, Storage, load_dotenv, safe_error, telegram_request

DEFAULT_HOST = "imap.gmail.com"
DEFAULT_FOLDER = "INBOX"
# Отправители писем OLX отличаются по странам и типам уведомлений.
DEFAULT_SENDERS = "olx.kz,olx.pl,olxgroup.com,noreply@olx"


def settings() -> dict[str, str]:
    load_dotenv(ROOT / ".env")
    user = os.getenv("MAIL_IMAP_USER", "")
    password = os.getenv("MAIL_IMAP_PASSWORD", "")
    if not user or not password or "replace_me" in f"{user}{password}":
        raise SystemExit(
            "Заполните MAIL_IMAP_USER и MAIL_IMAP_PASSWORD в .env.\n"
            "Для Gmail нужен пароль приложения (не основной пароль): "
            "https://myaccount.google.com/apppasswords")
    return {
        "host": os.getenv("MAIL_IMAP_HOST", DEFAULT_HOST),
        "user": user, "password": password,
        "folder": os.getenv("MAIL_FOLDER", DEFAULT_FOLDER),
        "senders": os.getenv("MAIL_SENDERS", DEFAULT_SENDERS),
    }


def connect(config: dict[str, str]) -> imaplib.IMAP4_SSL:
    mailbox = imaplib.IMAP4_SSL(config["host"])
    mailbox.login(config["user"], config["password"])
    # readonly=True — ключевое: сервер не выставит \Seen и не тронет флаги.
    status, _ = mailbox.select(config["folder"], readonly=True)
    if status != "OK":
        raise SystemExit(f"Не удалось открыть папку {config['folder']!r}.")
    return mailbox


def decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return value


def plain_text(message: Message) -> str:
    """Текст письма: сначала text/plain, иначе HTML без разметки."""
    plain, rich = "", ""
    for part in message.walk():
        if part.get_content_maintype() != "text":
            continue
        if part.get_filename():
            continue
        try:
            body = part.get_payload(decode=True) or b""
            text = body.decode(part.get_content_charset() or "utf-8", errors="replace")
        except (LookupError, ValueError):
            continue
        if part.get_content_subtype() == "plain" and not plain:
            plain = text
        elif part.get_content_subtype() == "html" and not rich:
            rich = text
    if plain:
        return plain
    rich = re.sub(r"(?is)<(script|style).*?</\1>", " ", rich)
    rich = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", rich)
    return html.unescape(re.sub(r"<[^>]+>", " ", rich))


def tidy(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def parse_notification(message: Message) -> dict[str, str]:
    """Вытаскивает из письма отправителя, объявление, текст и ссылку.

    Формат писем OLX не документирован и меняется, поэтому берём несколько
    вариантов подряд, а --dump показывает исходник, если что-то не совпало.
    """
    subject = decode(message.get("Subject"))
    body = tidy(plain_text(message))

    sender = ""
    for pattern in (r"(?:новое сообщение|сообщение)\s+от\s+([^\n,.:]{2,40})",
                    r"(?:new message)\s+from\s+([^\n,.:]{2,40})",
                    r"^([^\n]{2,40})\s+(?:написал|отправил)"):
        found = re.search(pattern, f"{subject}\n{body}", re.IGNORECASE | re.MULTILINE)
        if found:
            sender = found.group(1).strip()
            break

    advert = ""
    for pattern in (r"(?:по объявлению|объявление|к объявлению)[:\s»\"]*([^\n]{3,80})",
                    r"(?:regarding|about)[:\s]+([^\n]{3,80})"):
        found = re.search(pattern, body, re.IGNORECASE)
        if found:
            advert = found.group(1).strip(" «»\"")
            break

    link = ""
    found = re.search(r"https?://[^\s\"'<>]*olx\.[a-z]{2,3}/[^\s\"'<>]*", body, re.IGNORECASE)
    if found:
        link = found.group(0)

    return {"subject": subject, "sender": sender, "advert": advert,
            "body": body, "link": link}


def looks_like_message_notification(parsed: dict[str, str]) -> bool:
    """Отсекает рассылки OLX: акции, советы продавцу, «ваше объявление скоро истечёт»."""
    haystack = f"{parsed['subject']} {parsed['body'][:400]}".lower()
    if any(word in haystack for word in ("сообщени", "message", "написал", "ответил")):
        return not any(word in haystack for word in
                       ("рассылк", "подборк", "рекоменду", "истека", "истёк", "продлит",
                        "скидк", "акци", "newsletter"))
    return False


def notify(token: str, chat_id: str, parsed: dict[str, str]) -> None:
    lines = ["💬 <b>Новое сообщение на OLX</b>"]
    if parsed["sender"]:
        lines.append(f"От: {html.escape(parsed['sender'])}")
    if parsed["advert"]:
        lines.append(f"Объявление: {html.escape(parsed['advert'])}")
    excerpt = parsed["body"][:700]
    lines.append(f"\n{html.escape(excerpt)}")
    if parsed["link"]:
        lines.append(f'\n<a href="{html.escape(parsed["link"], quote=True)}">Открыть в OLX</a>')
    telegram_request(token, "sendMessage", chat_id=chat_id, text="\n".join(lines),
                     parse_mode="HTML", disable_web_page_preview=True)


def fetch_new(mailbox: imaplib.IMAP4_SSL, config: dict[str, str],
              storage: Storage) -> list[tuple[int, Message]]:
    """Письма от OLX новее запомненного UID."""
    validity = mailbox.response("UIDVALIDITY")[1][0].decode()
    # UID уникальны только в пределах одного UIDVALIDITY: если сервер его
    # сменил, прежняя позиция бессмысленна и отсчёт начинается заново.
    if storage.setting("mail_uidvalidity") != validity:
        storage.set_setting("mail_uidvalidity", validity)
        storage.set_setting("mail_last_uid", "0")
    last_uid = int(storage.setting("mail_last_uid", "0") or 0)

    uids: set[int] = set()
    for sender in config["senders"].split(","):
        sender = sender.strip()
        if not sender:
            continue
        status, data = mailbox.uid("SEARCH", None, "FROM", f'"{sender}"')
        if status == "OK" and data and data[0]:
            uids.update(int(uid) for uid in data[0].split())

    fresh = sorted(uid for uid in uids if uid > last_uid)
    out = []
    for uid in fresh:
        status, data = mailbox.uid("FETCH", str(uid), "(BODY.PEEK[])")
        # BODY.PEEK вместо BODY: чтение не выставляет флаг \Seen.
        if status == "OK" and data and isinstance(data[0], tuple):
            out.append((uid, email.message_from_bytes(data[0][1])))
    return out


def poll_once(storage: Storage, token: str, chat_id: str, dry_run: bool = False) -> int:
    config = settings()
    mailbox = connect(config)
    try:
        letters = fetch_new(mailbox, config, storage)
        # Первый запуск только запоминает позицию: иначе в чат уехала бы вся
        # переписка за всё время, что письма лежат в ящике.
        first_run = storage.setting("mail_last_uid", "0") in ("", "0")
        sent = 0
        for uid, message in letters:
            parsed = parse_notification(message)
            if not first_run and looks_like_message_notification(parsed):
                if dry_run:
                    print(f"  UID {uid} | {parsed['sender'] or '—'} | {parsed['subject'][:60]}")
                else:
                    notify(token, chat_id, parsed)
                sent += 1
            storage.set_setting("mail_last_uid", str(uid))
        if first_run and letters:
            logging.info("Первый запуск: запомнил позицию на UID %d, писем не пересылал",
                         letters[-1][0])
        return sent
    finally:
        try:
            mailbox.logout()
        except OSError:
            pass


def watch(interval: int) -> None:
    load_dotenv(ROOT / ".env")
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise SystemExit("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env.")
    storage = Storage()
    logging.info("Слежу за письмами OLX, проверка раз в %d сек.", interval)
    try:
        while True:
            try:
                sent = poll_once(storage, token, chat_id)
                if sent:
                    logging.info("Переслано писем: %d", sent)
            except (imaplib.IMAP4.error, OSError) as error:
                storage.record_error("Почта OLX", Exception(safe_error(error)))
                logging.warning("Почта недоступна: %s", safe_error(error))
            except requests.RequestException as error:
                logging.warning("Telegram: %s", safe_error(error))
            time.sleep(interval)
    finally:
        storage.close()


def dump() -> None:
    """Разбор последних писем — чтобы сверить формат на живом ящике."""
    config = settings()
    mailbox = connect(config)
    try:
        uids: set[int] = set()
        for sender in config["senders"].split(","):
            status, data = mailbox.uid("SEARCH", None, "FROM", f'"{sender.strip()}"')
            if status == "OK" and data and data[0]:
                uids.update(int(uid) for uid in data[0].split())
        if not uids:
            print("Писем от OLX в папке не найдено. Проверьте MAIL_FOLDER и MAIL_SENDERS.")
            return
        for uid in sorted(uids)[-3:]:
            status, data = mailbox.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if status != "OK" or not isinstance(data[0], tuple):
                continue
            message = email.message_from_bytes(data[0][1])
            parsed = parse_notification(message)
            print(f"\n{'=' * 70}\nUID {uid} | от: {decode(message.get('From'))}")
            print(f"тема:       {parsed['subject']}")
            print(f"отправитель:{parsed['sender'] or '— не распознан'}")
            print(f"объявление: {parsed['advert'] or '— не распознано'}")
            print(f"ссылка:     {parsed['link'] or '—'}")
            print(f"это сообщение? {looks_like_message_notification(parsed)}")
            print(f"--- текст (600) ---\n{parsed['body'][:600]}")
    finally:
        mailbox.logout()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Письма OLX в Telegram")
    parser.add_argument("--check", action="store_true", help="проверить подключение к ящику")
    parser.add_argument("--dump", action="store_true", help="разбор последних писем от OLX")
    parser.add_argument("--once", action="store_true", help="одна проверка без отправки")
    parser.add_argument("--interval", type=int, default=180, help="секунд между проверками")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.check:
            config = settings()
            box = connect(config)
            print(f"Подключение к {config['host']} как {config['user']}: успешно.")
            print(f"Папка {config['folder']!r} открыта только на чтение.")
            box.logout()
        elif args.dump:
            dump()
        elif args.once:
            storage = Storage()
            try:
                print(f"Новых писем-сообщений: {poll_once(storage, '', '', dry_run=True)}")
            finally:
                storage.close()
        else:
            watch(args.interval)
    except KeyboardInterrupt:
        sys.exit(0)
