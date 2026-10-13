"""Резервная копия базы наблюдателя.

Копирует через штатный механизм SQLite, а не файловым `cp`: бот пишет в базу
почти непрерывно, и простое копирование файла посреди транзакции даёт битый
дамп. Копия сжимается, старые ротируются.

Копия на том же диске не спасёт от поломки самого диска, поэтому архив ещё и
отправляется владельцу в Telegram (BACKUP_TO_TELEGRAM=1 в .env, по умолчанию
включено). Там лежат пользователи и оплаты публичного режима — это главное,
что нельзя потерять.

Ещё каждая копия уходит на Google Диск через rclone (BACKUP_RCLONE_REMOTE в
.env, например «gdrive:Радар — бэкапы базы»): в hourly/ — последние 48
часов, в daily/ — по одной за сутки на 30 дней. Живую базу на Диск
синхронизировать нельзя: файл, переписываемый ботом, приехал бы битым.

Запускается systemd-таймером раз в час; в Telegram архив уходит раз в сутки.
Можно и руками:
    ./venv/bin/python deploy/backup.py
"""
from __future__ import annotations

import gzip
import os
import shutil
import subprocess
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
KEEP = 48           # локальных копий: при запуске раз в час это двое суток
DRIVE_HOURLY_AGE = "48h"
DRIVE_DAILY_AGE = "30d"


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


def rclone(*args: str) -> bool:
    try:
        result = subprocess.run(["rclone", *args], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"rclone не запустился: {error}")
        return False
    if result.returncode != 0:
        print(f"rclone {args[0]}: {result.stderr.strip()[-300:]}")
    return result.returncode == 0


def upload_to_drive(target: Path, daily: bool) -> bool:
    """Копия на Google Диск и чистка старых. Сбой не роняет бэкап — локальная копия уже есть."""
    remote = os.environ.get("BACKUP_RCLONE_REMOTE", "").rstrip("/")
    if not remote:
        return False
    ok = rclone("copyto", str(target), f"{remote}/hourly/{target.name}")
    rclone("delete", f"{remote}/hourly", "--min-age", DRIVE_HOURLY_AGE)
    if ok and daily:
        ok = rclone("copyto", str(target), f"{remote}/daily/{target.name}")
        rclone("delete", f"{remote}/daily", "--min-age", DRIVE_DAILY_AGE)
    return ok


def first_today() -> bool:
    """Первая копия за сутки — её же шлём в Telegram и кладём в daily/."""
    today = datetime.now().strftime("%Y%m%d")
    return len(list(DESTINATION.glob(f"watcher-{today}-*.sqlite3.gz"))) == 1


def main() -> None:
    sys.path.insert(0, str(ROOT))
    from olx_watcher import load_dotenv
    load_dotenv(ROOT / ".env")
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
    daily = first_today()
    if upload_to_drive(target, daily):
        print("Копия на Google Диске" + (" (и в daily/)" if daily else ""))
    if daily and send_to_owner(target, caption):
        print("Архив отправлен владельцу в Telegram")


if __name__ == "__main__":
    main()
