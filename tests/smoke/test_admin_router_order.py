"""Smoke: порядок регистрации в пакете handlers/admin — специфичное раньше общего.

aiogram проверяет фильтры в порядке регистрации, а для пакета из подроутеров
порядок — это порядок include_router в admin/__init__.py плюс порядок внутри
каждого модуля. Единственные пересекающиеся фильтры у админа — на сообщениях:
`admin_start` (CommandStart, без ограничения по состоянию) должен видеть «/start»
раньше любого FSM-обработчика ввода (иначе «/start» посреди диалога ушёл бы
именем профиля или числом дней) и раньше `gateway_claim_message` (текст без
состояния). Колбэки не пересекаются: у каждого класса CallbackData действия
различны, GEN_ACTIONS не содержит прочих действий DeviceCB/AdminSelfCB.

Перестановка include_router молча сломала бы это — отсюда сторож с явным
перечнем пар.
"""
import pytest

from awgbot.bot.handlers import admin

pytestmark = pytest.mark.smoke

# (специфичный, общий): специфичный обязан быть зарегистрирован раньше
_MESSAGE_PAIRS = [
    ("admin_start", "add_client_name"),
    ("admin_start", "add_client_limit"),
    ("admin_start", "add_client_traffic"),
    ("admin_start", "edit_name_apply"),
    ("admin_start", "edit_limit_apply"),
    ("admin_start", "edit_traffic_apply"),
    ("admin_start", "edit_period_start_apply"),
    ("admin_start", "edit_period_end_apply"),
    ("admin_start", "admin_add_device_name"),
    ("admin_start", "admin_add_device_traffic"),
    ("admin_start", "device_edit_name_apply"),
    ("admin_start", "gateway_claim_message"),
    ("admin_start", "self_add_name"),
    ("admin_start", "self_add_traffic"),
    ("admin_start", "admin_block_pause_days"),
    ("admin_start", "broadcast_days"),
    ("admin_start", "broadcast_receive"),
]


def _flat(observer: str) -> list[str]:
    """Имена хендлеров наблюдателя в сквозном порядке обхода цепочки роутеров."""
    return [h.callback.__name__
            for sub in admin.router.chain_tail
            for h in getattr(sub, observer).handlers]


def test_panel_is_included_first():
    assert [r.name for r in admin.router.sub_routers][0] == "admin.panel"


def test_package_router_holds_no_handlers_itself():
    """Все обработчики — в подроутерах: иначе часть проверялась бы до panel."""
    assert not admin.router.message.handlers
    assert not admin.router.callback_query.handlers


def test_admin_start_is_the_first_message_handler():
    assert _flat("message")[0] == "admin_start"


@pytest.mark.parametrize("specific,general", _MESSAGE_PAIRS, ids=lambda s: s)
def test_specific_message_handler_precedes_general(specific, general):
    order = _flat("message")
    assert specific in order and general in order, (specific, general)
    assert order.index(specific) < order.index(general), \
        f"{specific} должен регистрироваться раньше {general}"


def test_every_fsm_message_handler_is_listed():
    """Новый FSM-обработчик ввода обязан попасть в перечень пар: без строки
    здесь его порядок относительно admin_start никто не сторожит."""
    order = _flat("message")
    unlisted = set(order) - {g for _, g in _MESSAGE_PAIRS} - {"admin_start", "admin_document"}
    assert not unlisted, sorted(unlisted)
