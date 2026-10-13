"""Общие настройки тестов."""
from collections import defaultdict, deque

import pytest


@pytest.fixture(autouse=True)
def fresh_flood_counter(monkeypatch):
    """Антифлуд считает сообщения в памяти процесса — между тестами сбрасываем."""
    import olx_feed

    monkeypatch.setattr(olx_feed, "_recent", defaultdict(deque))


@pytest.fixture(autouse=True)
def no_olx_network(monkeypatch):
    """Тесты не ходят в OLX: добавление поиска запрашивает страницу ради названия
    раздела, и без этой заглушки каждый такой тест ждал ответа по сети."""
    import olx_feed

    class Offline:
        headers = {}

        def get(self, *args, **kwargs):
            raise olx_feed.w.requests.RequestException("сеть в тестах отключена")

    monkeypatch.setattr(olx_feed, "make_session", lambda proxy="": Offline())
