"""handlers/fenced.py — загородка лишённого кворума лидера (кворум резерва,
концепт «кворум» §6.3): админу доступен только раздел «🛟 Резерв» и его
действия (карточка, переезд кнопкой, возврат, С9); всё остальное —
всплывашка Т-К10. Роутер стоит ДО админских, как загородка гостя, и до
кнопок уведомлений (reply_commands); действует только пока сервисы говорят
«лишён кворума» (reserve_quorum_fenced).

Загородка — белый список, а не перечень закрытых кнопок: новая кнопка вне
раздела закрыта сама, без правки этого файла (перечень пропускал удаление
устройства, блокировку, паузу, кнопки уведомлений — их действие в базе под
следованием не записывалось, а на хосте уже совершалось)."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, Message

from awgbot.bot.callbacks import CancelCB, HideCB, ListCB, Menu, PageCB, PeerCB, RsvCB, SetCB
from awgbot.bot.filters import RoleFilter
from awgbot.bot.texts.reserve import quorum as qt

router = Router(name="fenced")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))

# разделы настроек, доступные лишённому кворума: корень и «🛟 Резерв»
FENCED_SECTIONS = frozenset({"root", "rsv"})
MENU_ALLOWED = frozenset({"main", "refresh"})
# кнопки, открытые целиком: раздел «🛟 Резерв» и экран админа второго сервера,
# листание, «весь список», «Скрыть», «✖️ Отмена» (листание отдаёт свой колбэк
# заново через диспетчер — тот снова идёт мимо загородки)
FREE: tuple[type[CallbackData], ...] = (RsvCB, PeerCB, PageCB, ListCB, HideCB, CancelCB)


def _fenced(services) -> bool:
    fn = getattr(services, "reserve_quorum_fenced", None)
    try:
        return bool(fn()) if fn is not None else False
    except Exception:                                         # noqa: BLE001
        return False


def _unpack(cls: type[CallbackData], data: str):
    try:
        return cls.unpack(data)
    except (TypeError, ValueError):
        return None


def allowed(data: str | None) -> bool:
    """Кнопка лишённому кворума доступна: раздел «🛟 Резерв» и служебные."""
    data = data or ""
    for cls in FREE:
        if _unpack(cls, data) is not None:
            return True
    sec = _unpack(SetCB, data)
    if sec is not None:
        return sec.sec in FENCED_SECTIONS
    menu = _unpack(Menu, data)
    return menu is not None and menu.action in MENU_ALLOWED


def _outside_reserve(cb: CallbackQuery, services, **_) -> bool:
    return _fenced(services) and not allowed(cb.data)


@router.callback_query(_outside_reserve)
async def outside_reserve(cb: CallbackQuery, **_):
    await cb.answer(qt.ADMIN_FENCED, show_alert=True)


def _fenced_filter(event, services, **_) -> bool:
    return _fenced(services)


@router.message(_fenced_filter, F.document, StateFilter(None))
async def document(message: Message, **_):
    await message.answer(qt.ADMIN_FENCED)


__all__ = ["router", "FENCED_SECTIONS", "MENU_ALLOWED", "FREE", "allowed"]
