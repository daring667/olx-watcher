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


# Настоящее письмо OLX.kz: ни имени собеседника, ни названия объявления,
# ни текста сообщения — только ссылка на переписку.
REAL_LETTER = letter(
    "Пришел новый ответ на объявление",
    "Пришел новый ответ на объявление\n"
    "https://www.olx.kz/myaccount/answer/2e53204f-502c-4086-8130-4e554f113887/"
    "?action=answer&id=2e53204f-502c-4086-8130-4e554f113887&my_chat=1#last\n"
    "Это сообщение было отправлено автоматически. Пожалуйста, не отвечайте на него.\n")


def test_real_olx_letter_is_recognised():
    parsed = olx_mail.parse_notification(REAL_LETTER)
    assert olx_mail.looks_like_message_notification(parsed) is True
    assert parsed["link"].startswith("https://www.olx.kz/myaccount/answer/")


def test_real_olx_letter_has_no_invented_fields():
    """Чего в письме нет, того не должно появиться: ни отправителя, ни объявления.

    Разделитель перед названием ловил перевод строки, и в поле «объявление»
    попадала следующая строка — сначала ссылка, потом повтор темы.
    """
    parsed = olx_mail.parse_notification(REAL_LETTER)
    assert parsed["sender"] == ""
    assert parsed["advert"] == ""


def test_recognised_as_message_notification():
    assert olx_mail.looks_like_message_notification(
        olx_mail.parse_notification(MESSAGE_LETTER)) is True


@pytest.mark.parametrize("subject, body", [
    ("Ваше объявление скоро истекает", "Объявление «RTX 3060» истекает через 3 дня"),
    ("Подборка для вас", "Рекомендуем посмотреть новые сообщения в подборке"),
    ("Скидка 50% на продвижение", "Акция для продавцов, поднимите объявление"),
    # Настоящие письма из ящика: рассылка сохранённого поиска и отказ портала.
    ("Появились новые объявления, соответствующие критериям вашего поиска", ""),
    ("Приложение отклонено", "Ваша заявка отклонена"),
])
def test_newsletters_are_ignored(subject, body):
    parsed = olx_mail.parse_notification(letter(subject, body))
    assert olx_mail.looks_like_message_notification(parsed) is False


def test_saved_search_letter_is_not_a_message():
    """Рассылка про сохранённый поиск сама предлагает «отправить сообщение».

    По телу такое письмо проходило бы за уведомление, поэтому решает тема.
    """
    rich = ("<p>Появились новые объявления, соответствующие Вашим критериям поиска:</p>"
            "<p>Помните, чем оперативнее вы отправите сообщение, тем выше шансы "
            "успешной сделки.</p><p>Ключевые слова : oneplus 13s</p>")
    parsed = olx_mail.parse_notification(
        letter("Появились новые объявления, соответствующие критериям вашего поиска",
               rich=rich))
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
