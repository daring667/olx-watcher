"""Общие настройки тестов."""
from collections import defaultdict, deque

import pytest


@pytest.fixture(autouse=True)
def fresh_flood_counter(monkeypatch):
    """Антифлуд считает сообщения в памяти процесса — между тестами сбрасываем."""
    import olx_feed

    monkeypatch.setattr(olx_feed, "_recent", defaultdict(deque))
