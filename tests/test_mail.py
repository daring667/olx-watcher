"""Разбор писем-уведомлений OLX.

Формат писем не документирован, поэтому проверяем на собранных вручную
образцах: обычное уведомление о сообщении, только-HTML письмо и рассылка,
которую пересылать не надо.
"""
from email.message import EmailMessage

import olx_mail
import pytest


def letter(subject: str, plain: str = "", rich: str = "") -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "OLX <noreply@olx.kz>"
    if plain:
        message.set_content(plain)
    if rich:
        if plain:
            message.add_alternative(rich, subtype="html")
        else:
            message.set_content(rich, subtype="html")
    return message


MESSAGE_LETTER = letter(
    "Новое сообщение от Айдос",
    "Здравствуйте!\n\nНовое сообщение от Айдос по объявлению «Видеокарта RTX 3060 12GB»\n\n"
    "Ещё актуально? Готов забрать сегодня.\n\n"
    "Ответить: https://www.olx.kz/myaccount/messages/thread/123\n")


def test_parses_sender_advert_and_link():
    parsed = olx_mail.parse_notification(MESSAGE_LETTER)
    assert parsed["sender"] == "Айдос"
    assert parsed["advert"] == "Видеокарта RTX 3060 12GB"
    assert "Ещё актуально?" in parsed["body"]
    assert parsed["link"] == "https://www.olx.kz/myaccount/messages/thread/123"


def test_recognised_as_message_notification():
    assert olx_mail.looks_like_message_notification(
        olx_mail.parse_notification(MESSAGE_LETTER)) is True


@pytest.mark.parametrize("subject, body", [
    ("Ваше объявление скоро истекает", "Объявление «RTX 3060» истекает через 3 дня"),
    ("Подборка для вас", "Рекомендуем посмотреть новые сообщения в подборке"),
    ("Скидка 50% на продвижение", "Акция для продавцов, поднимите объявление"),
])
def test_newsletters_are_ignored(subject, body):
    parsed = olx_mail.parse_notification(letter(subject, body))
    assert olx_mail.looks_like_message_notification(parsed) is False


def test_html_only_letter_is_stripped_to_text():
    rich = ("<html><head><style>p{color:red}</style></head><body>"
            "<p>Новое сообщение от <b>Марат</b></p>"
            "<div>по объявлению «GTX 1660»</div>"
            "<p>Отдадите за 40000?</p>"
            "<script>alert(1)</script></body></html>")
    parsed = olx_mail.parse_notification(letter("Новое сообщение", rich=rich))
    assert "Отдадите за 40000?" in parsed["body"]
    # Разметка, стили и скрипты в текст не попадают.
    assert "<p>" not in parsed["body"]
    assert "alert" not in parsed["body"]
    assert "color:red" not in parsed["body"]


def test_plain_text_wins_over_html():
    parsed = olx_mail.parse_notification(
        letter("Новое сообщение", plain="Текстовая версия", rich="<p>HTML версия</p>"))
    assert "Текстовая версия" in parsed["body"]
    assert "HTML версия" not in parsed["body"]


def test_missing_fields_do_not_break_parsing():
    parsed = olx_mail.parse_notification(letter("Уведомление", "Просто текст без деталей"))
    assert parsed["sender"] == ""
    assert parsed["advert"] == ""
    assert parsed["link"] == ""
    assert parsed["body"] == "Просто текст без деталей"


def test_encoded_subject_is_decoded():
    message = EmailMessage()
    message["Subject"] = "=?utf-8?B?0J3QvtCy0L7QtSDRgdC+0L7QsdGJ0LXQvdC40LU=?="
    message.set_content("тело")
    assert olx_mail.parse_notification(message)["subject"] == "Новое сообщение"
