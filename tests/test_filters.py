"""Проверки отбора объявлений.

Каждый случай здесь — реальная ошибка, найденная на боевых данных. Держим их
тестами, потому что ломались они молча: неверная цена или потерянная карта
видны только по базе через сутки, а не по упавшему обходу.
"""
from pathlib import Path

import pytest
import yaml

from olx_watcher import (Listing, assess, clean_seller_name, evaluate, extract_price,
                         is_mining_card, model_number_in, price_label, price_limit)

REAL_CONFIG = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.example.yaml")
                             .read_text(encoding="utf-8"))

# Бюджет — настройка, которая меняется по желанию владельца, и привязывать к
# ней проверки правил нельзя: снижение потолка со 100 000 до 50 000 уронило
# шесть тестов, хотя ни одно правило не менялось. Потолок проверяется отдельно.
CONFIG = {**REAL_CONFIG, "max_price_kzt": 100000}


def ad(title: str, price: int | None = 30000, description: str = "") -> Listing:
    return assess(Listing("ID1", "https://olx.kz/ad", title, price, description), CONFIG)


# --- Цена -------------------------------------------------------------------
# Жадная маска склеивала номер модели с ценой и портила 13% записей в базе.
@pytest.mark.parametrize("text, expected", [
    ("GT730 6 000 тг", 6000),
    ("Видеокарта rx 6800 200 000 тг", 200000),       # 4-значная модель вплотную
    ("PULSE RX 6600 150 000 тг", 150000),
    ("Gigabyte 3080 240 000 тг", 240000),
    ("RTX 4070 370 000 тг", 370000),
    ("RTX 3060 90 000 ₸", 90000),
    ("RTX 3060 12Gb 90 000 ₸", 90000),
    ("Видеокарта 2 250 000 ₸", 2250000),
    ("45000 тг", 45000),
    ("Цена договорная", None),
    ("RTX 3080 $600", None),                          # валюта не тенге
])
def test_extract_price(text, expected):
    assert extract_price(text) == expected


# --- Номер модели -----------------------------------------------------------
@pytest.mark.parametrize("title, number, expected", [
    ("видеокарта gigabyte 1080 8gb", "1080", True),
    ("монитор 1920x1080", "1080", False),             # разрешение, не модель
    ("видеокарта для 1080p игр", "1080", False),      # «тянет 1080p» в описании
    ("gtx1050ti 4gb", "1050", True),                  # суффикс ti не мешает
    ("palit 1660 ti", "1660", True),
])
def test_model_number_in(title, number, expected):
    assert model_number_in(number, title) is expected


# --- Отбор ------------------------------------------------------------------
@pytest.mark.parametrize("title, price, description", [
    ("Видеокарта Palit GeForce GTX 1660 Ti Dual 6GB", 75000, "охлаждение: 2 вентилятора"),
    ("RTX asus rog strix 2080 super", 90000, ""),     # ломала подстрока "rtx a"
    ("Видеокарта Palit 1660 ti", 90000, ""),          # голый номер, без слова nvidia
    ("Видеокарта Gigabyte 1080 8GB", 60000, ""),
    ("Видеокарты GTX 1660 / R9 270X", 20000, ""),     # сборный лот: NVIDIA в заголовке
    ("Видеокарта GTX 980 4GB", 70000, ""),            # 9xx оставлены сознательно
])
def test_passes(title, price, description):
    assert evaluate(ad(title, price, description), CONFIG) == ""


@pytest.mark.parametrize("title, price, description, reason_starts", [
    ("Sapphire Pulse RX 570 8GB", 50000, "аналог geforce", "другой вендор"),
    ("Видеокарта Sapphire NITRO Radeon R9 380", 24000, "быстрее GTX 1050", "другой вендор"),
    ("Видеокарта Intel Arc A770 16Gb", 90000, "", "другой вендор"),
    ("Кулер для процессора Aigo ICE200PRO", 5000, "", "комплектующая"),
    ("Вентилятор для видеокарты RTX 3060", 3000, "", "комплектующая"),
    ("Видеокарта NVIDIA GeForce GT 210 1GB", 3000, "", "устаревшая серия"),
    ("Видеокарта Asus GT1030-2G-BRK", 10000, "", "устаревшая серия"),
    ("GTX 760 видеокарта рабочая", 36000, "", "устаревшая серия"),
    ("Видеокарта NVIDIA RTX A4000", 90000, "", "стоп-слово"),
    ("Видеокарта для ноутбука MSI GTX 1060", 40000, "", "стоп-слово"),
    ("Материнская плата ASUS", 30000, "", "нет ключевого слова"),
    ("Видеокарта GTX 1660", None, "", "цена не распознана"),
    ("Видеокарта RTX 3060 12GB", 130000, "", "дороже потолка"),
])
def test_rejected(title, price, description, reason_starts):
    assert evaluate(ad(title, price, description), CONFIG).startswith(reason_starts)


def test_outdated_wins_over_mixed_lot_rescue():
    """Поблажка для сборных лотов действует только на чужого вендора.

    «GTX 660Ti / R9 270X» остаётся отсеянным: NVIDIA в заголовке спасает от
    правила про Radeon, но не от правила про устаревшую серию.
    """
    assert evaluate(ad("Видеокарты GTX 660Ti/GTX 660/R9 270X", 20000), CONFIG) \
        .startswith("устаревшая серия")


def test_cooler_in_description_does_not_reject():
    """Описание настоящей карты почти всегда упоминает систему охлаждения."""
    listing = ad("Видеокарта Palit GeForce GTX 1660 Ti", 60000,
                 "кулера не шумят, термопрокладки заменены, радиатор чистый")
    assert evaluate(listing, CONFIG) == ""


def test_price_ceiling_is_single():
    """Потолок один для всех: модель на отбор не влияет."""
    limit = int(CONFIG["max_price_kzt"])
    assert price_limit(ad("RTX 4060", 1), CONFIG) == limit
    assert price_limit(ad("Видеокарта без модели", 1), CONFIG) == limit


def test_real_budget_is_applied():
    """Потолок из рабочего конфига действительно отсекает — на реальном значении."""
    budget = int(REAL_CONFIG["max_price_kzt"])
    cheap = assess(Listing("ID1", "u", "Видеокарта GTX 1660 6GB", budget - 1000, ""), REAL_CONFIG)
    pricey = assess(Listing("ID2", "u", "Видеокарта GTX 1660 6GB", budget + 1000, ""), REAL_CONFIG)
    assert evaluate(cheap, REAL_CONFIG) == ""
    assert evaluate(pricey, REAL_CONFIG).startswith("дороже потолка")


# --- Модель и подпись -------------------------------------------------------
def test_model_from_bare_number():
    assert ad("Видеокарта Gigabyte 1080 8GB", 60000).model == "GTX 1080"


def test_resolution_in_description_is_not_a_model():
    assert ad("Видеокарта для игр", 45000, "тянет 1080p на ультра").model is None


@pytest.mark.parametrize("price, marker", [(50000, "🔥"), (68000, "✅"), (95000, "💸")])
def test_price_label(price, marker):
    assert price_label(ad("Видеокарта GTX 1660 6GB", price), CONFIG).startswith(marker)


# --- Майнинговые карты ------------------------------------------------------
@pytest.mark.parametrize("title, expected", [
    ("ZOTAC P106-100. аналог GTX 1060 6GB", True),
    ("Cmp 70hx, майнинг видеокарта", True),
    ("Видеокарта GTX 1060 6gb", False),
])
def test_is_mining_card(title, expected):
    assert is_mining_card(ad(title), CONFIG) is expected


def test_mining_mentioned_in_description_is_not_a_mining_card():
    """«По мощности как p106» — сравнение, а не признак товара."""
    assert is_mining_card(ad("Видеокарта GTX 1060", 50000, "по мощности как p106"), CONFIG) is False


# --- Доступ к боту ----------------------------------------------------------
def test_foreign_chat_is_ignored(tmp_path, monkeypatch):
    """Бот отвечает только владельцу.

    Без проверки любой, кто узнал username бота, мог отправить /favorites и
    прочитать чужое избранное с заметками или снять мониторинг с паузы.
    """
    import olx_watcher

    calls = []
    monkeypatch.setattr(olx_watcher, "telegram_request",
                        lambda *a, **kw: calls.append(kw) or {"result": {"message_id": 1}})
    storage = olx_watcher.Storage(tmp_path / "test.sqlite3")
    try:
        stranger = {"update_id": 1, "message": {"chat": {"id": 999}, "text": "/favorites"}}
        olx_watcher.handle_update("token", storage, stranger, "12345")
        assert calls == []

        owner = {"update_id": 2, "message": {"chat": {"id": 12345}, "text": "/favorites"}}
        olx_watcher.handle_update("token", storage, owner, "12345")
        assert len(calls) == 1
    finally:
        storage.close()


# --- Снятые с публикации ----------------------------------------------------
def storage_with(tmp_path, ads):
    import olx_watcher
    storage = olx_watcher.Storage(tmp_path / "gone.sqlite3")
    for ad_id, url in ads:
        storage.add_baseline(Listing(ad_id, "https://olx.kz/a", "Карта", 30000, search_url=url))
    return storage


def test_missing_listing_marked_gone_after_threshold(tmp_path):
    """Пропало из выдачи трижды подряд — снято. Раньше — ещё нет."""
    storage = storage_with(tmp_path, [("A", "url1"), ("B", "url1")])
    try:
        for expected_gone in (0, 0, 1):   # третий обход переводит порог
            gone = storage.sweep_missing(["url1"], {"A"}, limit=3)
            assert gone == expected_gone
        row = storage.connection.execute("SELECT is_gone FROM ads WHERE ad_id='B'").fetchone()
        assert row["is_gone"] == 1
        alive = storage.connection.execute("SELECT is_gone FROM ads WHERE ad_id='A'").fetchone()
        assert alive["is_gone"] == 0
    finally:
        storage.close()


def test_reappearing_listing_resets(tmp_path):
    """Вернулось в выдачу — счётчик обнуляется, отметка снимается."""
    storage = storage_with(tmp_path, [("A", "url1"), ("B", "url1")])
    try:
        for _ in range(3):
            storage.sweep_missing(["url1"], {"A"}, limit=3)
        storage.sweep_missing(["url1"], {"A", "B"}, limit=3)
        row = storage.connection.execute(
            "SELECT is_gone, missing_cycles FROM ads WHERE ad_id='B'").fetchone()
        assert (row["is_gone"], row["missing_cycles"]) == (0, 0)
    finally:
        storage.close()


def test_other_searches_are_untouched(tmp_path):
    """Обход одного города не объявляет снятыми объявления другого."""
    storage = storage_with(tmp_path, [("A", "url1"), ("B", "url2")])
    try:
        for _ in range(5):
            storage.sweep_missing(["url1"], {"A"}, limit=3)
        row = storage.connection.execute(
            "SELECT is_gone, missing_cycles FROM ads WHERE ad_id='B'").fetchone()
        assert (row["is_gone"], row["missing_cycles"]) == (0, 0)
    finally:
        storage.close()


def test_empty_cycle_marks_nothing(tmp_path):
    """Оборванный обход ничего не отмечает: иначе снятой станет вся база."""
    storage = storage_with(tmp_path, [("A", "url1"), ("B", "url1")])
    try:
        assert storage.sweep_missing(["url1"], set(), limit=3) == 0
        assert storage.sweep_missing([], {"A"}, limit=3) == 0
        rows = storage.connection.execute("SELECT SUM(missing_cycles) s FROM ads").fetchone()
        assert rows["s"] == 0
    finally:
        storage.close()


def test_url_has_history(tmp_path):
    """Новая ссылка не должна считаться пройденной — иначе город разошлётся весь."""
    storage = storage_with(tmp_path, [("A", "url1")])
    try:
        assert storage.url_has_history("url1") is True
        assert storage.url_has_history("url-нового-города") is False
    finally:
        storage.close()


# --- Продавец ---------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ("svecartem479 на OLX с ноябрь 2023 г. Онлайн 01 августа 2026 г.", "svecartem479"),
    ("Александр на OLX с июнь 2018 г. Онлайн в 05:22", "Александр"),
    ("Дарья", "Дарья"),
    ("Продавец", None),   # заглушка OLX, а не человек с 36 объявлениями
    ("", None),
    (None, None),
])
def test_clean_seller_name(raw, expected):
    assert clean_seller_name(raw) == expected
