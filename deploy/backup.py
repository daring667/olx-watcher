"""Резервная копия базы наблюдателя.

Копирует через штатный механизм SQLite, а не файловым `cp`: бот пишет в базу
почти непрерывно, и простое копирование файла посреди транзакции даёт битый
дамп. Копия сжимается, старые ротируются.

Копия на том же диске не спасёт от поломки самого диска, поэтому архив ещё и
отправляется владельцу в Telegram (BACKUP_TO_TELEGRAM=1 в .env, по умолчанию
включено). Там лежат пользователи и оплаты публичного режима — это главное,
что нельзя потерять.

Запускается systemd-таймером раз в сутки; можно и руками:
    ./venv/bin/python deploy/backup.py
"""
from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data" / "watcher.sqlite3"
# Не внутри data/ и не внутри репозитория: смысл копии в том, чтобы пережить
# порчу рабочего каталога, а не лежать с ним рядом.
DESTINATION = ROOT.parent / "backups"
KEEP = 14


def make_backup() -> Path:
    DESTINATION.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    target = DESTINATION / f"watcher-{stamp}.sqlite3.gz"

    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "snapshot.sqlite3"
        source = sqlite3.connect(f"file:{SOURCE}?mode=ro", uri=True)
        try:
            copy = sqlite3.connect(snapshot)
            try:
                # backup() снимает согласованный слепок, не мешая записи бота.
                source.backup(copy)
            finally:
                copy.close()
        finally:
            source.close()
        with open(snapshot, "rb") as raw, gzip.open(target, "wb", compresslevel=6) as packed:
            shutil.copyfileobj(raw, packed)
    return target


def rotate() -> int:
    backups = sorted(DESTINATION.glob("watcher-*.sqlite3.gz"))
    stale = backups[:-KEEP] if len(backups) > KEEP else []
    for path in stale:
        path.unlink()
    return len(stale)


def send_to_owner(target: Path, caption: str) -> bool:
    """Отправляет архив в личку владельца. Сбой не роняет бэкап: локальная копия уже есть."""
    sys.path.insert(0, str(ROOT))
    from olx_watcher import load_dotenv, safe_error

    load_dotenv(ROOT / ".env")
    if os.environ.get("BACKUP_TO_TELEGRAM", "1") != "1":
        return False
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    owners = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    sent = False
    for owner in owners[:1]:  # хватает одной копии
        try:
            with open(target, "rb") as archive:
                response = requests.post(f"https://api.telegram.org/bot{token}/sendDocument",
                                         data={"chat_id": owner, "caption": caption,
                                               "disable_notification": "true"},
                                         files={"document": (target.name, archive)}, timeout=120)
            sent = bool(response.json().get("ok"))
            if not sent:
                print(f"Telegram не принял архив: {response.json().get('description')}")
        except (requests.RequestException, ValueError) as error:
            print(f"Архив не отправлен в Telegram: {safe_error(error)}")
    return sent


def main() -> None:
    if not SOURCE.exists():
        sys.exit(f"База не найдена: {SOURCE}")
    target = make_backup()
    removed = rotate()
    with sqlite3.connect(f"file:{SOURCE}?mode=ro", uri=True) as con:
        ads = con.execute("SELECT COUNT(*) FROM ads").fetchone()[0]
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0] if "users" in tables else 0
        paid = (con.execute("SELECT COUNT(*) FROM payments WHERE status='approved'").fetchone()[0]
                if "payments" in tables else 0)
    size = target.stat().st_size / 1024
    print(f"Копия: {target} ({size:.0f} КБ, объявлений {ads}); удалено старых: {removed}")
    caption = (f"🗄 Бэкап базы {datetime.now():%d.%m.%Y}: пользователей {users}, оплат {paid}, "
               f"{size:.0f} КБ. Храните этот файл — из него восстанавливается всё.")
    if send_to_owner(target, caption):
        print("Архив отправлен владельцу в Telegram")


if __name__ == "__main__":
    main()
