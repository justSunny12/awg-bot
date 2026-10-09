"""Smoke/unit: рендер-функции пакетов texts/ и keyboards/.

Pure-форматтеры проверяем на ожидаемые подстроки там, где ветки нет в эталонах
экранов (tests/screens) и в tests/unit/test_ui_atoms.py; объект-рендеры (карточки,
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


def test_gb_str_and_slots_and_limit_notice():
    assert "ГБ" in texts.gb_str(5 * 1024 ** 3)
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
    owner = texts.reassign_recipient_notice("Pi4", 6, 0, recipient_is_admin=True)
    assert owner.endswith("Теперь у тебя 6 устройств"), owner
    assert "∞" not in owner, "безлимит — без «из ∞»"
    # с лимитом — тот же вид «m из n» (снимок adm.dev.reassign.go)
    # после «из» слово не склоняем по первому числу: «1 из 5 подключённое устройство» — брак
    assert "подключённое" not in texts.reassign_donor_notice("Тел", 1, 5)


# ── клавиатуры без БД ────────────────────────────────────────────────────────
def _is_markup(m):
    return isinstance(m, InlineKeyboardMarkup) and len(m.inline_keyboard) >= 1


def test_static_keyboards_build():
    assert _is_markup(kb.hide_only())
    assert _is_markup(kb.help_menu(is_initial=True))
    assert _is_markup(kb.period_kb("extend", ref=1, min_days=7))
    assert _is_markup(kb.grace_offer(1, 14))
    assert _is_markup(kb.block_pause_kb(1))
    assert _is_markup(kb.block_notify_kb("cli", 1, pause_days=0))
    assert _is_markup(kb.help_menu(guest=True))
    assert _is_markup(kb.guest_main()) and _is_markup(kb.guest_main(routing_visible=True, client_id=1))


def test_block_unblock_reasons_lists_active_bits():
    mask = int(DeviceBlock.ADMIN_SILENT | DeviceBlock.USER)
    assert _is_markup(kb.block_unblock_reasons("dev", 1, mask))


# ── объект-рендеры на живых доменных объектах ────────────────────────────────
def test_object_renders_do_not_crash(services, make_active_client):
    client = make_active_client(name="Смок", tg_id=8500, traffic_limit=100 * 1024 ** 3)
    services.add_device(client.id, "Устройство")
    client = services.db.get_client(client.id)
    assert texts.greeting_client(client, server_ok=True, slots=(1, 3))


def test_friend_panel_and_admin_panel_render(services, make_active_client):
    owner = make_active_client(name="Хозяин", tg_id=8501)
    dc = services.add_device(owner.id, "Ноут")
    services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=98501)
    dev = services.db.get_device(dc.device_id)
    host = services.db.get_client(owner.id)
    assert texts.device_card_held(dev, int(host.traffic_limit))
    assert texts.greeting_guest("Артём", True, host, [dev])
    # статусный блок админ-панели из state (метрик железа нет — рендер обязан пережить)
    st = services.server_status_cached()
    assert texts.admin_panel(st)


def test_object_keyboards_build(services, make_active_client):
    client = make_active_client(tg_id=8502)
    dc = services.add_device(client.id, "d")
    dev = services.db.get_device(dc.device_id)
    assert _is_markup(kb.device_actions(dev, is_admin=True, back_target="cli"))
    assert _is_markup(kb.admin_client_actions(client))
    assert _is_markup(kb.admin_main())


def test_rf_traffic_line_render():
    """Вложенная строка РФ под трафиком на главной: суффикс — только при ошибке
    учёта; без данных о РФ и при show=False строки нет. Обычная строка «└ 🇷🇺
    РФ-доступ: N ГБ» — в эталоне (adm.main)."""
    G = 1024 ** 3
    assert texts.rf_traffic_line({"rx": 0, "tx": 0, "error": "x"}) == \
        "└ 🇷🇺 РФ-доступ: 0 ГБ · ⚠️ учёт трафика РФ-доступа не идёт"
    st = {"ok": True, "traffic_rx": 1, "traffic_tx": 2}
    assert "🇷🇺 РФ-доступ:" not in texts.admin_panel(st), "строка РФ без данных о ней"
    assert "🇷🇺 РФ-доступ:" not in texts.admin_panel(st, rf={"rx": G, "tx": G, "show": False})
