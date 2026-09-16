"""Smoke/unit: рендер-функции texts.py и keyboards.py.

Pure-форматтеры проверяем на ожидаемые подстроки; объект-рендеры (карточки,
панели) и билдеры клавиатур — что не падают и дают непустой результат на живых
доменных объектах. Ловит регрессии сигнатур/полей при рефакторинге моделей.
"""
import pytest

from aiogram.types import InlineKeyboardMarkup

from awgbot.bot import texts, keyboards as kb
from awgbot.core.blocks import DeviceBlock

pytestmark = pytest.mark.smoke


# ── чистые форматтеры (unit: подстроки) ──────────────────────────────────────
def test_bytes_are_always_gigabytes_rounded_to_hundredths():
    """Одна шкала на все экраны: только ГБ, арифметическое округление до сотых,
    незначащие нули долой — «8.99 из 50 ГБ», а не «8.0 МБ (лимит 50.00 ГБ)»."""
    G = 1024 ** 3
    assert texts.gb(0) == "0" and texts.gb(50 * G) == "50" and texts.gb(G // 2) == "0.5"
    assert texts.gb(int(8.99 * G)) == "8.99"
    assert texts.gb(int(8.995 * G) + 1) == "9", "округление арифметическое, не банковское"
    assert texts.gb(int(8.994 * G)) == "8.99"
    assert texts.human_bytes(2048) == "0 ГБ" and texts.human_bytes(8 * 1024 ** 2) == "0.01 ГБ"
    assert texts.used_of_limit(int(8.99 * G), 50 * G) == "8.99 из 50 ГБ"
    assert texts.used_of_limit(int(8.99 * G), 50 * G, "лимит устройства") == "8.99 из 50 ГБ (лимит устройства)"
    assert texts.used_of_limit(int(8.99 * G), 0) == "8.99 ГБ"
    assert texts.consumption_line(int(8.99 * G), 50 * G, blocked=True, until="01.10.2026") == \
        "Потребление за месяц: 8.99 из 50 ГБ (лимит устройства) — исчерпан, приостановлено до 01.10.2026"
    assert texts.client_total_line(G, 2 * G, 50 * G, 10 * G, for_admin=False) == \
        "Потребление за месяц: 3 из 50 + 10 ГБ до конца месяца"


def test_gb_str_and_slots_and_limit_notice():
    assert "ГБ" in texts.gb_str(5 * 1024 ** 3)
    assert texts.device_slots_line(2, 3)
    assert texts.limit_changed_notice(0, 10 * 1024 ** 3)


def test_plural_ru_agrees():
    assert texts.plural_ru(1, "день", "дня", "дней").endswith("день")
    assert texts.plural_ru(3, "день", "дня", "дней").endswith("дня")
    assert texts.plural_ru(5, "день", "дня", "дней").endswith("дней")


def test_device_count_is_a_fraction_everywhere():
    """Счётчик устройств читается одинаково у админа и у владельца профиля:
    дробь m/n, безлимит — ∞. Расхождение форматов между двумя сообщениями об
    одном и том же событии заставляет сверять их глазами."""
    admin = texts.device_created_report("Pi4", client_name="Админ", device_count=6,
                                        max_devices=0)
    owner = texts.reassign_recipient_notice("Pi4", 6, 0, recipient_is_admin=True)
    assert "Количество устройств: 6" in admin
    assert "Теперь у тебя 6/∞ подключённых устройств." in owner
    # с лимитом — тот же вид, число вместо ∞
    assert "Теперь у тебя 1/5 подключённых устройств." in \
        texts.reassign_recipient_notice("Тел", 1, 5)
    # после дроби слова не склоняем: «1/5 подключённое устройство» — брак
    assert "подключённое" not in texts.reassign_donor_notice("Тел", 1, 5)


def test_unlimited_consumption_says_it_in_one_phrase():
    """Ни лимита устройства, ни лимита профиля — «Потребление не ограничено».
    Прежняя оговорка про рамки лимита профиля намекала на лимит, которого нет."""
    free = texts.device_created_report("П", client_name="В", device_count=1)
    assert "Потребление устройства не ограничено." in free
    withprofile = texts.device_created_report("П", client_name="В", device_count=1,
                                              profile_limit_bytes=100 * 1024 ** 3)
    assert "Потребление устройства не ограничено в рамках лимита профиля." in withprofile


# ── клавиатуры без БД ────────────────────────────────────────────────────────
def _is_markup(m):
    return isinstance(m, InlineKeyboardMarkup) and len(m.inline_keyboard) >= 1


def test_static_keyboards_build():
    assert _is_markup(kb.hide_only())
    assert _is_markup(kb.help_menu(is_initial=True))
    assert _is_markup(kb.yes_no("keep", ref=1))
    assert _is_markup(kb.period_choices("extend", ref=1, min_days=7))
    assert _is_markup(kb.grace_offer(1, 14))
    assert _is_markup(kb.block_pause_choice(1))
    assert _is_markup(kb.block_notify_choice("cli", 1, pause_days=0))
    assert _is_markup(kb.friend_help_menu())
    assert _is_markup(kb.guest_main()) and _is_markup(kb.guest_main(routing_visible=True, client_id=1))


def test_added_by_admin_offers_all_three_ways_and_hides():
    """Уведомление о выданном устройстве: способы выдачи тройкой в один ряд (как
    в главном меню) и «Скрыть» последней строкой — оно проактивное, человек его
    не заказывал."""
    rows = kb.added_by_admin(7).inline_keyboard
    assert [b.text for b in rows[0]] == ["🔗 Ссылка", "🔳 QR-код", "📄 Файл"]
    assert len(rows[-1]) == 1 and rows[-1][0].text == "Скрыть"
    assert any("gen_qr" in b.callback_data for b in rows[0]), "QR не выдавался вовсе"


def test_block_unblock_reasons_lists_active_bits():
    mask = int(DeviceBlock.ADMIN_SILENT | DeviceBlock.USER)
    assert _is_markup(kb.block_unblock_reasons("dev", 1, mask))


# ── объект-рендеры на живых доменных объектах ────────────────────────────────
def test_object_renders_do_not_crash(services, make_active_client):
    client = make_active_client(name="Смок", tg_id=8500, traffic_limit=100 * 1024 ** 3)
    dc = services.add_device(client.id, "Устройство")
    dev = services.db.get_device(dc.device_id)
    client = services.db.get_client(client.id)
    devices = services.db.list_devices(client.id)
    traffic = services.db.get_client_traffic(client.id)

    for for_admin in (True, False):
        assert texts.device_card_text(dev, for_admin=for_admin)
        assert texts.subscription_block(client, for_admin=for_admin)
        assert texts.client_card(client, devices, traffic, online=False, for_admin=for_admin)

    assert texts.greeting_client(client, server_ok=True, slots=(1, 3))


def test_friend_panel_and_admin_panel_render(services, make_active_client):
    owner = make_active_client(name="Хозяин", tg_id=8501)
    dc = services.add_device(owner.id, "Ноут")
    services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=98501)
    dev = services.db.get_device(dc.device_id)
    host = services.db.get_client(owner.id)
    assert texts.held_device_card(dev, int(host.traffic_limit))
    assert texts.greeting_guest("Артём", True, host, 1)
    # статусный блок админ-панели из state (метрик железа нет — рендер обязан пережить)
    st = services.server_status_cached()
    assert texts.admin_panel(st)


def test_object_keyboards_build(services, make_active_client):
    client = make_active_client(tg_id=8502)
    dc = services.add_device(client.id, "d")
    dev = services.db.get_device(dc.device_id)
    assert _is_markup(kb.device_actions(dev, is_admin=True, back_target="cli",
                                        reassign_label="Передать"))
    assert _is_markup(kb.admin_client_actions(client))
    assert _is_markup(kb.admin_main(0))


def test_admin_client_keyboard_has_no_dangerous_buttons():
    """Карточка собственного профиля админа: без удаления/лимита/продления/
    блокировки — этого над собой не делают."""
    from awgbot.core import config

    class _C:
        id = 1; activation_status = "active"; block_reason = 0
        tg_id = config.ADMIN_ID
    m = kb.admin_client_actions(_C(), has_devices=True, is_admin_owner=True)
    labels = " ".join(b.text for row in m.inline_keyboard for b in row)
    for forbidden in ("Удалить", "Лимит", "Продлить", "лок"):   # блок/Блок/…
        assert forbidden not in labels, f"кнопка '{forbidden}' не должна быть у админ-клиента"
    assert "Имя" in labels and "Устройства" in labels
