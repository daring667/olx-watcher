"""Мониторинг «Радара объявлений»: пишет владельцу, если что-то сломалось.

Запускается таймером olx-monitor.timer раз в 5 минут (по образцу
tiktokbot-monitor у TikTok-бота). Проверяет:
- сервисы olx-watcher и olx-miniapp активны;
- приложение отвечает на /health;
- публичные поиски опрашиваются: если есть хоть один поиск с активным
  доступом, последний опрос OLX был не дольше FEED_STALE_MINUTES назад.

О проблеме пишет не чаще раза в час, а когда всё починилось — один раз
сообщает, что работает снова. Отметки хранятся в таблице meta.

Руками: ./venv/bin/python deploy/healthcheck.py
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from olx_watcher import STATE_PATH, load_config, load_dotenv, safe_error, telegram_request  # noqa: E402

UNITS = ("olx-watcher", "olx-miniapp")
APP_HEALTH = os.environ.get("MINIAPP_HEALTH_URL", "http://127.0.0.1:5051/health")
FEED_STALE_MINUTES = 20
ALERT_EVERY = 3600


def unit_problems() -> list[str]:
    problems = []
    for unit in UNITS:
        state = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True).stdout.strip()
        if state != "active":
            problems.append(f"сервис {unit}: {state or 'не найден'}")
    return problems


def app_problems() -> list[str]:
    try:
        with urllib.request.urlopen(APP_HEALTH, timeout=5) as response:
            return [] if response.status == 200 else [f"приложение ответило {response.status}"]
    except OSError as error:
        return [f"приложение не отвечает: {error}"]


def feed_problems(connection: sqlite3.Connection, owner_ids: set[str]) -> list[str]:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"watches", "users", "feed_urls"} <= tables:
        return []
    now = int(time.time())
    marks = ",".join("?" * len(owner_ids)) or "''"
    waiting = connection.execute(
        f"SELECT COUNT(*) FROM watches w JOIN users u USING(chat_id) WHERE w.paused=0 "
        f"AND (u.paid_until>? OR u.chat_id IN ({marks}))", (now, *owner_ids)).fetchone()[0]
    if not waiting:
        return []
    last = connection.execute("SELECT MAX(last_polled) FROM feed_urls").fetchone()[0]
    if last is None:
        return []  # поиски только что добавлены, первого опроса ещё не было
    minutes = (now - last) // 60
    return [f"OLX не опрашивался {minutes} мин. (поисков ждут: {waiting})"] if minutes > FEED_STALE_MINUTES else []


def meta(connection: sqlite3.Connection, key: str, default: str = "") -> str:
    row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))
    connection.commit()


def main() -> int:
    load_dotenv(ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    owners = {c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()}
    connection = sqlite3.connect(STATE_PATH, timeout=10)
    connection.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    problems = unit_problems() + app_problems()
    if load_config().get("feed", {}).get("enabled"):
        problems += feed_problems(connection, owners)
    failing = meta(connection, "monitor_failing") == "1"
    now = int(time.time())
    text = ""
    if problems and now - int(meta(connection, "monitor_alert_at", "0")) >= ALERT_EVERY:
        text = "🚨 <b>Радар объявлений: проблема</b>\n" + "\n".join(f"• {p}" for p in problems)
        set_meta(connection, "monitor_alert_at", str(now))
    elif not problems and failing:
        text = "✅ <b>Радар объявлений снова работает</b>"
    set_meta(connection, "monitor_failing", "1" if problems else "0")
    for owner in owners:
        if text:
            try:
                telegram_request(token, "sendMessage", chat_id=owner, text=text, parse_mode="HTML")
            except Exception as error:  # мониторинг не должен падать из-за Telegram
                print(f"Не отправлено: {safe_error(error)}")
    print("; ".join(problems) or "OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
