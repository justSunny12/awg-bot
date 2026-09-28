"""Smoke/unit: рендер-функции пакетов texts/ и keyboards/.

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
def test_volumes_are_gigabytes_rounded_to_hundredths():
    """Одна шкала на все экраны: ноль — «0 ГБ», любая ненулевая мелочь —
    «0.01 ГБ», дальше — ГБ с арифметическим округлением до сотых, незначащие
    нули долой — «8.99 из 50 ГБ», а не «8.0 МБ (лимит 50.00 ГБ)»."""
    G, M = 1024 ** 3, 1024 ** 2
    assert texts.gb(0) == "0" and texts.gb(50 * G) == "50" and texts.gb(G // 2) == "0.5"
    assert texts.gb(int(8.99 * G)) == "8.99"
    assert texts.gb(int(8.995 * G) + 1) == "9", "округление арифметическое, не банковское"
    assert texts.gb(int(8.994 * G)) == "8.99"
    assert texts.human_bytes(0) == "0 ГБ"
    assert texts.human_bytes(1) == "0.01 ГБ" and texts.human_bytes(2048) == "0.01 ГБ"
    assert texts.human_bytes(8 * M) == "0.01 ГБ" and texts.human_bytes(16 * M) == "0.02 ГБ"
    assert texts.human_bytes(G - 1) == "1 ГБ" and texts.human_bytes(G) == "1 ГБ"
    assert texts.used_of_limit(int(8.99 * G), 50 * G) == "8.99 из 50 ГБ"
    assert texts.used_of_limit(512 * M, 50 * G) == "0.5 из 50 ГБ"
    assert texts.used_of_limit(int(8.99 * G), 50 * G, "лимит устройства") == "8.99 из 50 ГБ (лимит устройства)"
    assert texts.used_of_limit(int(8.99 * G), 0) == "8.99 ГБ"
    assert texts.consumption_line(int(8.99 * G), 50 * G, blocked=True) == \
        "Трафик за месяц: 8.99 из 50 ГБ (лимит устройства) — исчерпан"
    assert texts.consumption_line(8 * M, 0, blocked=False) == "Трафик за месяц: 0.01 ГБ"
    assert texts.client_total_line(G, 2 * G, 50 * G, 10 * G, for_admin=False) == \
        "Трафик за месяц: 3 из 50 + 10 ГБ до конца месяца"
    from awgbot.core import models
    gw = models.Device(id=1, client_id=1, name="Малина", public_key="P", private_key="k",
                       preshared_key="", address="10.8.1.9", block_reason=0, created_at="2026-01-01",
                       is_gateway=1, traffic=models.DeviceTraffic(rx_month=int(0.83 * G), tx_month=int(10.95 * G)))
    assert "Потребление: 11.78 ГБ (↑ 10.95 ГБ | ↓ 0.83 ГБ)" in texts.gateway_device_card(gw), \
        "у шлюза единица дублировалась: «11.79 ГБ ГБ»"
    assert texts.client_total_line(M, 2 * M, 50 * G, 10 * G, for_admin=True) == \
        "Трафик профиля за месяц: 0.01 из 50 + 10 ГБ до конца месяца (↑ 0.01 ГБ | ↓ 0.01 ГБ)"


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
    «m из n», безлимит — просто число («6 из ∞» читается как опечатка).
    Расхождение форматов между двумя сообщениями об одном и том же событии
    заставляет сверять их глазами."""
    admin = texts.device_created_report("Pi4", client_name="Админ", device_count=6,
                                        max_devices=0)
    owner = texts.reassign_recipient_notice("Pi4", 6, 0, recipient_is_admin=True)
    assert "Количество устройств: 6" in admin
    assert owner.endswith("Теперь у тебя 6 устройств"), owner
    assert "∞" not in owner, "безлимит — без «из ∞»"
    # с лимитом — тот же вид, число вместо ∞
    assert texts.reassign_recipient_notice("Тел", 1, 5).endswith("Теперь у тебя 1 из 5 устройств")
    # после «из» слово не склоняем по первому числу: «1 из 5 подключённое устройство» — брак
    assert "подключённое" not in texts.reassign_donor_notice("Тел", 1, 5)


def test_unlimited_consumption_says_it_in_one_phrase():
    """Ни лимита устройства, ни лимита профиля — «Трафик устройства без лимита».
    Прежняя оговорка про рамки лимита профиля намекала на лимит, которого нет."""
    free = texts.device_created_report("П", client_name="В", device_count=1)
    assert "Трафик устройства без лимита." in free
    assert "лимита профиля" not in free, "оговорка про лимит профиля, которого нет"
    withprofile = texts.device_created_report("П", client_name="В", device_count=1,
                                              profile_limit_bytes=100 * 1024 ** 3)
    assert "Трафик устройства — в пределах лимита профиля." in withprofile


# ── клавиатуры без БД ────────────────────────────────────────────────────────
def _is_markup(m):
    return isinstance(m, InlineKeyboardMarkup) and len(m.inline_keyboard) >= 1


def test_static_keyboards_build():
    assert _is_markup(kb.hide_only())
    assert _is_markup(kb.help_menu(is_initial=True))
    assert _is_markup(kb.yes_no("keep", ref=1))
    assert _is_markup(kb.period_choices("extend", ref=1, min_days=7))
    assert _is_markup(kb.grace_offer(1, 14))
    assert _is_markup(kb.block_pause_kb(1))
    assert _is_markup(kb.block_notify_kb("cli", 1, pause_days=0))
    assert _is_markup(kb.help_menu(guest=True))
    assert _is_markup(kb.guest_main()) and _is_markup(kb.guest_main(routing_visible=True, client_id=1))


def test_added_by_admin_offers_all_three_ways_and_hides():
    """Уведомление о выданном устройстве: способы выдачи тройкой в один ряд (как
    в главном меню) и «Скрыть» последней строкой — оно проактивное, человек его
    не заказывал."""
    rows = kb.added_by_admin(7).inline_keyboard
    assert [b.text for b in rows[0]] == ["🔗 Ссылка", "🔳 QR", "📄 Файл"]
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


def test_client_greeting_shows_consumption_and_expiry(services, make_active_client):
    """Главная клиента: подписка и трафик одной строкой против лимита, с
    первого напоминания — «🟡 истекает 12.10 18:00»; без лимита — просто
    объём. Уедет строка — человек не видит, сколько осталось, пока не
    откроет «💳 Подписка»."""
    from awgbot.util import timeutil
    G = 1024 ** 3
    c = make_active_client(name="Тестовый клиент", tg_id=8601, traffic_limit=50 * G)
    traffic = {"rx_month": 20 * G, "tx_month": 4 * G + G // 10}
    end = timeutil.parse_iso(c.period_end)
    out = texts.greeting_client(c, True, (3, 4), None, traffic=traffic)
    assert out.splitlines() == [
        "👋 Тестовый клиент", "🟢 VPN работает",
        f"💳 Подписка до {timeutil.fmt_date_ui(end)} · 📊 24.1 из 50 ГБ",
        "📱 Устройств 3 из 4"], out
    services.db.update_client_fields(c.id, notified_thresholds="10080")
    c = services.db.get_client(c.id)
    assert f"💳 🟡 истекает {timeutil.fmt_dt_ui(end)} · 📊 24.1 из 50 ГБ" in \
        texts.greeting_client(c, True, (3, 4), None, traffic=traffic)
    assert "🟢 активна" in texts.subscription_status_only(c), "срок — только на главной клиента"
    services.db.update_client_fields(c.id, traffic_limit=0)
    assert " · 📊 24.1 из ∞ ГБ\n" in texts.greeting_client(
        services.db.get_client(c.id), True, (3, 4), None, traffic=traffic)


def test_friend_panel_and_admin_panel_render(services, make_active_client):
    owner = make_active_client(name="Хозяин", tg_id=8501)
    dc = services.add_device(owner.id, "Ноут")
    services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=98501)
    dev = services.db.get_device(dc.device_id)
    host = services.db.get_client(owner.id)
    assert texts.held_device_card(dev, int(host.traffic_limit))
    assert texts.greeting_guest("Артём", True, host, [dev])
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
    assert _is_markup(kb.admin_main())


def test_rf_traffic_line_render():
    """Вложенная строка РФ под трафиком на главной: объём без ↑↓ (разбивка —
    на экране «Трафик»); суффикс — только при ошибке учёта."""
    G = 1024 ** 3
    assert texts.rf_traffic_line({"rx": G, "tx": 3 * G}) == "└ 🇷🇺 РФ-доступ: 4 ГБ"
    assert texts.rf_traffic_line({"rx": 0, "tx": 0, "error": "x"}) == \
        "└ 🇷🇺 РФ-доступ: 0 ГБ · ⚠️ учёт трафика РФ-доступа не идёт"
    st = {"ok": True, "traffic_rx": 1, "traffic_tx": 2}
    assert "🇷🇺 РФ-доступ:" not in texts.admin_panel(st), "строка РФ без данных о ней"
    assert "🇷🇺 РФ-доступ:" not in texts.admin_panel(st, rf={"rx": G, "tx": G, "show": False})
    assert "└ 🇷🇺 РФ-доступ: 2 ГБ" in texts.admin_panel(st, rf={"rx": G, "tx": G, "show": True})
