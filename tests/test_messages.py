"""Разбор ответов OLX и логика пересылки сообщений.

Точная схема ответа станет известна только на живом аккаунте — до одобрения
заявки проверяем то, что от неё не зависит: выбор поля из нескольких имён,
определение направления сообщения и границу «первый проход ничего не шлёт».
"""
import olx_messages
import pytest


@pytest.mark.parametrize("message, incoming", [
    ({"type": "received"}, True),
    ({"type": "sent"}, False),
    ({"direction": "incoming"}, True),
    ({"direction": "out"}, False),
    ({"is_own": True}, False),
    ({"own": True}, False),
    ({}, True),                       # поля направления нет — считаем входящим
])
def test_is_incoming(message, incoming):
    assert olx_messages.is_incoming(message) is incoming


@pytest.mark.parametrize("message, text", [
    ({"text": "актуально?"}, "актуально?"),
    ({"body": "актуально?"}, "актуально?"),
    ({"content": "актуально?"}, "актуально?"),
    ({}, ""),
])
def test_message_text(message, text):
    assert olx_messages.message_text(message) == text


def test_message_text_is_truncated():
    assert len(olx_messages.message_text({"text": "я" * 5000})) == 600


def test_field_picks_first_non_empty():
    assert olx_messages.field({"a": "", "b": "нужное"}, "a", "b") == "нужное"
    assert olx_messages.field({}, "a", "b", default="запас") == "запас"


class FakeStorage:
    """Подменяет Storage: хранит настройки в словаре."""

    def __init__(self, settings=None):
        self.settings = settings or {}

    def setting(self, key, default=""):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


THREAD = {"id": 7, "advert": {"title": "RTX 3060", "url": "https://olx.kz/ad"},
          "interlocutor": {"name": "Айдос"}}


def fake_api(threads, messages):
    def api_get(storage, path, **params):
        return {"data": threads} if path == "/threads" else {"data": messages}
    return api_get


def test_first_pass_only_remembers_boundary(monkeypatch):
    """Подключение бота не должно пересылать всю историю переписки."""
    messages = [{"id": "3", "type": "received", "text": "третье"},
                {"id": "2", "type": "received", "text": "второе"}]
    monkeypatch.setattr(olx_messages, "api_get", fake_api([THREAD], messages))
    storage = FakeStorage()
    assert olx_messages.poll_once(storage, "", "", dry_run=True) == 0
    assert storage.settings["olx_last_message:7"] == "3"


def test_only_messages_after_boundary_are_sent(monkeypatch):
    messages = [{"id": "5", "type": "received", "text": "новое"},
                {"id": "4", "type": "sent", "text": "мой ответ"},
                {"id": "3", "type": "received", "text": "старое"}]
    monkeypatch.setattr(olx_messages, "api_get", fake_api([THREAD], messages))
    storage = FakeStorage({"olx_last_message:7": "3"})
    # Своё сообщение не пересылается, старое — уже за границей.
    assert olx_messages.poll_once(storage, "", "", dry_run=True) == 1
    assert storage.settings["olx_last_message:7"] == "5"


def test_nothing_new_sends_nothing(monkeypatch):
    messages = [{"id": "5", "type": "received", "text": "новое"}]
    monkeypatch.setattr(olx_messages, "api_get", fake_api([THREAD], messages))
    storage = FakeStorage({"olx_last_message:7": "5"})
    assert olx_messages.poll_once(storage, "", "", dry_run=True) == 0


def test_auth_url_has_required_parameters(monkeypatch):
    monkeypatch.setattr(olx_messages, "credentials", lambda: ("cid", "secret"))
    url = olx_messages.auth_url()
    assert url.startswith("https://www.olx.kz/oauth/authorize?")
    for part in ("client_id=cid", "response_type=code", "scope=v2+read+write",
                 "redirect_uri=https%3A%2F%2Fdaring667.github.io%2Folx-watcher%2Fcallback%2F"):
        assert part in url
