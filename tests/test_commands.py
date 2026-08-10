"""Команды Telegram и фильтры панели."""
import yaml
from pathlib import Path

import olx_watcher
import pytest

CONFIG = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.example.yaml")
                        .read_text(encoding="utf-8"))


class FakeStorage:
    def __init__(self, settings=None):
        self.settings = settings or {}

    def setting(self, key, default=""):
        return self.settings.get(key, default)


def test_override_price_is_applied():
    """/setprice должен менять именно тот потолок, по которому идёт отбор.

    Сначала переопределение читалось внутри price_limit() через необязательный
    Storage, но ни один вызов его не передавал — команда рапортовала об успехе,
    а фильтр работал по старому значению.
    """
    storage = FakeStorage({"override_max_price": "80000"})
    config = olx_watcher.effective_config(CONFIG, storage)
    assert config["max_price_kzt"] == 80000
    listing = olx_watcher.assess(
        olx_watcher.Listing("x", "u", "Видеокарта GTX 1660 6GB", 70000, ""), config)
    # При потолке из конфига (50 000) это объявление отсеивалось бы.
    assert olx_watcher.evaluate(listing, config) == ""
    assert olx_watcher.evaluate(listing, CONFIG).startswith("дороже потолка")


def test_override_absent_keeps_config_value():
    config = olx_watcher.effective_config(CONFIG, FakeStorage())
    assert config["max_price_kzt"] == CONFIG["max_price_kzt"]


def test_broken_override_falls_back_to_config():
    """Мусор в настройке не должен ронять обход."""
    config = olx_watcher.effective_config(CONFIG, FakeStorage({"override_max_price": "дёшево"}))
    assert config["max_price_kzt"] == CONFIG["max_price_kzt"]


@pytest.mark.parametrize("allowed, sender, permitted", [
    ("123", "123", True),
    ("123", "999", False),
    ("123,456", "456", True),          # несколько чатов через запятую
    ("123, 456", "456", True),         # с пробелами
    ("123,456", "789", False),
])
def test_multiple_chat_ids(tmp_path, monkeypatch, allowed, sender, permitted):
    calls = []
    monkeypatch.setattr(olx_watcher, "telegram_request",
                        lambda *a, **kw: calls.append(kw) or {"result": {"message_id": 1}})
    storage = olx_watcher.Storage(tmp_path / "chats.sqlite3")
    try:
        update = {"update_id": 1, "message": {"chat": {"id": int(sender)}, "text": "/favorites"}}
        olx_watcher.handle_update("token", storage, update, allowed)
        assert bool(calls) is permitted
    finally:
        storage.close()


@pytest.mark.parametrize("query", ["price_min=abc", "price_max=абв", "price_min=&price_max=",
                                   "price_min=10000&price_max=50000"])
def test_dashboard_price_filter_survives_junk(query, tmp_path, monkeypatch):
    """Значения из адресной строки могут быть любыми — страница падать не должна."""
    import dashboard

    storage = olx_watcher.Storage(tmp_path / "dash.sqlite3")
    storage.close()
    monkeypatch.setattr(dashboard, "DB", tmp_path / "dash.sqlite3")
    client = dashboard.app.test_client()
    assert client.get(f"/passed?{query}").status_code == 200


@pytest.mark.parametrize("entry", olx_watcher.BOT_COMMANDS)
def test_bot_commands_match_telegram_rules(entry):
    """Telegram отклоняет весь список целиком, если хоть одна команда не по формату."""
    import re

    assert re.fullmatch(r"[a-z0-9_]{1,32}", entry["command"])
    assert 1 <= len(entry["description"]) <= 256


def test_every_registered_command_is_handled(tmp_path, monkeypatch):
    """Подсказка не должна обещать команду, которой бот не знает."""
    replies = []
    monkeypatch.setattr(olx_watcher, "telegram_request",
                        lambda *a, **kw: replies.append(kw) or {"result": {"message_id": 1}})
    storage = olx_watcher.Storage(tmp_path / "cmd.sqlite3")
    try:
        for index, entry in enumerate(olx_watcher.BOT_COMMANDS):
            replies.clear()
            update = {"update_id": index,
                      "message": {"chat": {"id": 1}, "text": f"/{entry['command']}"}}
            olx_watcher.handle_update("token", storage, update, "1")
            assert replies, f"/{entry['command']} остаётся без ответа"
    finally:
        storage.close()


def test_register_commands_survives_telegram_failure(monkeypatch):
    """Подсказки — удобство: их сбой не должен мешать обходу."""
    import requests

    def boom(*a, **kw):
        raise requests.RequestException("Telegram недоступен")

    monkeypatch.setattr(olx_watcher, "telegram_request", boom)
    olx_watcher.register_commands("token")  # не должно бросить исключение


def test_deal_keyboard_shows_only_next_step():
    """Кнопок должно быть немного, и они должны вести вперёд по сделке."""
    steps = {status: [b["callback_data"].rsplit(":", 1)[1]
                      for b in olx_watcher.deal_keyboard("ID1", status)["inline_keyboard"][0]]
             for status in olx_watcher.DEAL_FLOW}
    assert steps["new"] == ["написал", "архив"]
    assert steps["написал"] == ["договорился", "архив"]
    assert steps["договорился"] == ["купил", "архив"]
    assert steps["купил"] == ["архив"]
    assert steps["архив"] == ["new"]          # из архива можно вернуть


def test_deal_keyboard_shows_current_status():
    keyboard = olx_watcher.deal_keyboard("ID1", "договорился")
    assert "договорился" in keyboard["inline_keyboard"][1][0]["text"]
    assert keyboard["inline_keyboard"][1][1]["callback_data"] == "note:ID1"


def test_deal_keyboard_survives_unknown_status():
    """Статус мог быть выставлен из панели вручную — падать нельзя."""
    keyboard = olx_watcher.deal_keyboard("ID1", "что-то своё")
    assert keyboard["inline_keyboard"][0]


@pytest.mark.parametrize("status", list(olx_watcher.DEAL_FLOW))
def test_callback_data_fits_telegram_limit(status):
    """Telegram обрезает callback_data длиннее 64 байт — кнопка перестаёт работать."""
    for row in olx_watcher.deal_keyboard("IDqZLbh9", status)["inline_keyboard"]:
        for button in row:
            assert len(button["callback_data"].encode()) <= 64


def search_reply(tmp_path, monkeypatch, ads, query="gtx 1060"):
    """Прогоняет /search по подготовленной базе и возвращает текст ответа."""
    replies = []
    monkeypatch.setattr(olx_watcher, "telegram_request",
                        lambda *a, **kw: replies.append(kw) or {"result": {"message_id": 1}})
    storage = olx_watcher.Storage(tmp_path / "search.sqlite3")
    try:
        now = 1
        for ad_id, title, price, gone, mining in ads:
            storage.connection.execute(
                "INSERT INTO ads(ad_id,url,title,price_kzt,status,first_seen,last_seen,"
                "reject_reason,is_gone,is_mining) VALUES(?,?,?,?,'new',?,?,'',?,?)",
                (ad_id, f"https://olx.kz/{ad_id}", title, price, now, now, gone, mining))
        storage.connection.commit()
        olx_watcher.handle_search(
            "token", storage, {"chat": {"id": 1}, "text": f"/search {query}"})
        return replies[0]["text"]
    finally:
        storage.close()


def test_search_hides_removed_listings(tmp_path, monkeypatch):
    """Снятое объявление купить нельзя — в выдаче ему не место."""
    text = search_reply(tmp_path, monkeypatch, [
        ("ID1", "Palit GTX 1060 6GB", 38000, 1, 0),      # снято
        ("ID2", "Gtx 1060 Asus", 30000, 0, 0),
    ])
    assert "Gtx 1060 Asus" in text
    assert "Palit GTX 1060 6GB" not in text
    assert "найдено 1" in text


def test_search_reports_real_total_not_page_size(tmp_path, monkeypatch):
    """В заголовке стояло len(rows) — то есть всегда «15», сколько бы ни нашлось."""
    ads = [(f"ID{i}", f"GTX 1060 вариант {i}", 10000 + i, 0, 0) for i in range(20)]
    text = search_reply(tmp_path, monkeypatch, ads)
    assert "найдено 20" in text
    assert "показаны 15" in text


def test_search_marks_mining_cards(tmp_path, monkeypatch):
    text = search_reply(tmp_path, monkeypatch, [
        ("ID1", "ZOTAC P106-100 аналог GTX 1060", 15000, 0, 1),
    ])
    assert "⛏" in text


def test_search_mentions_removed_when_nothing_left(tmp_path, monkeypatch):
    """Пустая выдача не должна выглядеть так, будто такого не бывает вовсе."""
    text = search_reply(tmp_path, monkeypatch, [
        ("ID1", "Palit GTX 1060 6GB", 38000, 1, 0),
    ])
    assert "ничего не найдено" in text
    assert "Снятых с публикации по этому запросу: 1" in text
