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
def test_human_bytes_scales():
    assert "КБ" in texts.human_bytes(2048) or "KB" in texts.human_bytes(2048)
    assert texts.human_bytes(0)


def test_gb_str_and_slots_and_limit_notice():
    assert "ГБ" in texts.gb_str(5 * 1024 ** 3)
    assert texts.device_slots_line(2, 3)
    assert texts.limit_changed_notice(0, 10 * 1024 ** 3)


def test_plural_ru_agrees():
    assert texts.plural_ru(1, "день", "дня", "дней").endswith("день")
    assert texts.plural_ru(3, "день", "дня", "дней").endswith("дня")
    assert texts.plural_ru(5, "день", "дня", "дней").endswith("дней")


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
    assert _is_markup(kb.friend_main(1, multi=True))


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
    assert texts.friend_panel(dev, host)
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
