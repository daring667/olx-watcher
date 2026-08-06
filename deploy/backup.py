"""Резервная копия базы наблюдателя.

Копирует через штатный механизм SQLite, а не файловым `cp`: бот пишет в базу
почти непрерывно, и простое копирование файла посреди транзакции даёт битый
дамп. Копия сжимается, старые ротируются.

Запускается systemd-таймером раз в сутки; можно и руками:
    ./venv/bin/python deploy/backup.py
"""
from __future__ import annotations

import gzip
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

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


def main() -> None:
    if not SOURCE.exists():
        sys.exit(f"База не найдена: {SOURCE}")
    target = make_backup()
    removed = rotate()
    with sqlite3.connect(f"file:{SOURCE}?mode=ro", uri=True) as con:
        ads = con.execute("SELECT COUNT(*) FROM ads").fetchone()[0]
    size = target.stat().st_size / 1024
    print(f"Копия: {target} ({size:.0f} КБ, объявлений {ads}); удалено старых: {removed}")


if __name__ == "__main__":
    main()
