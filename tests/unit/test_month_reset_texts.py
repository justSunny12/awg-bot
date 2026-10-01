"""Уведомления о начале месяца: профилю — две строки без перечня устройств,
гостю — его лимитные устройства списком « · имя — объём» с вылетом; имена экранированы."""
from awgbot.domain.services.traffic import _reset_client_text, _reset_friend_text


def test_profile_reset_text_has_two_lines_and_no_device_list():
    assert _reset_client_text(500 * 1024 ** 3, ["Телефон — 50 ГБ"]) == (
        "Начался новый месяц — лимиты трафика обнулены 🙂\n"
        "Доступно на текущий месяц: 500 ГБ")
    assert _reset_client_text(0, ["Телефон — 50 ГБ"]) == "Начался новый месяц — лимиты трафика обнулены 🙂"


def test_guest_reset_text_lists_its_devices_with_a_middle_dot():
    assert _reset_friend_text(["тест3 — 50 ГБ", "ноут — 100 ГБ"]) == (
        "Начался новый месяц — лимиты трафика обнулены 🙂\n"
        "Доступно на текущий месяц:\n"
        " · тест3 — 50 ГБ\n · ноут — 100 ГБ")
