"""Показывает ID чата после того, как вы отправили боту любое сообщение."""
from __future__ import annotations

import os
from pathlib import Path

import requests


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


load_dotenv(Path(".env"))
token = os.getenv("TELEGRAM_BOT_TOKEN")
if not token:
    raise SystemExit("Заполните TELEGRAM_BOT_TOKEN в файле .env")

response = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
response.raise_for_status()
updates = response.json()
if not updates.get("ok") or not updates.get("result"):
    raise SystemExit("Нет сообщений. Откройте бота в Telegram, нажмите Start и запустите команду снова.")

for update in updates["result"]:
    message = update.get("message") or update.get("channel_post")
    if message:
        chat = message["chat"]
        print(f"chat_id={chat['id']}  ({chat.get('title') or chat.get('username') or chat.get('first_name', '')})")

