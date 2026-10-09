"""«🔧 Сервис»: перезапуск AWG и бота с подтверждением и ценой словами роли,
итог первой строкой раздела; переезд профилей — у роли с has.migration
(кнопки ведут в обработчики переезда основного бота)."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from awgbot.bot import texts
from awgbot.bot import ui
from awgbot.bot.handlers.common import call, edit_nav

from ._kb import back_button
from .base import Confirm

ID, LABEL, BACK = "svc", "🔧 Сервис", "root"
KEYS: tuple[str, ...] = ()
BOUNDS: dict = {}
CYCLES: dict = {}

BOT_RESTARTING = "🔁 Бот перезапускается — вернётся через несколько секунд"


def keyboard(br, migration: str = "", available: bool = False, orphans: int = 0) -> InlineKeyboardMarkup:
    """Перезапуски парой; переезд — по состоянию: идёт — кто не переехал,
    завершить и отменить; нет — начать (если настроен) и переехавшие после
    отмены (если есть)."""
    mig: list = []
    if br.has.migration and available:
        if migration:
            mig = [("👥 Кто не переехал", br.cb.pack("mig", "do", "pending")),
                   [("✅ Завершить", br.cb.pack("mig", "do", "finish")), ("↩️ Отменить", br.cb.pack("mig", "do", "cancel"))]]
        else:
            mig = [("🚚 Начать переезд", br.cb.pack("mig", "do", "start")),
                   (f"⚠️ Переехавшие после отмены: {orphans}", br.cb.pack("mig", "do", "orphans")) if orphans else None]
    return ui.rows(
        [("🔁 Перезапуск AWG", br.cb.pack(ID, "do", "awg")), ("🔁 Перезапуск бота", br.cb.pack(ID, "do", "bot"))],
        *mig,
        back_button(br))


async def screen(br, services, key: str = ""):
    if br.has.migration:
        d = await call(services.svc_screen_data)          # один хоп вместо четырёх
    else:
        d = {"state": "", "progress": None, "available": False, "orphans": 0}
    return (texts.settings_svc_text(br, d["state"], d["progress"], d["available"]),
            keyboard(br, d["state"], available=d["available"], orphans=d["orphans"]))


async def _awg_question(ctx) -> str:
    return texts.svc_confirm_awg(ctx.br, await call(ctx.services.carries_traffic))


async def _awg_restart(ctx) -> str:
    """Итог первой строкой раздела у обеих ролей: «✅ AWG перезапущен» /
    «🔴 AWG не перезапущен: причина»."""
    await ctx.cb.answer("Перезапускаю AWG…")           # ответ сразу: рестарт может идти долго
    ok, detail = await call(ctx.services.restart_awg)
    return ui.result(ok, "AWG перезапущен", "AWG не перезапущен", detail)


async def _bot_question(ctx) -> str:
    return texts.svc_confirm_bot(ctx.br)


async def _bot_restart(ctx) -> None:
    """Обещание на месте меню; исполняет его новый процесс, подменяя это же
    сообщение панелью (restore_panel_after_restart). Рестарт — вне cgroup."""
    await ctx.cb.answer("Перезапускаю бота…")
    await edit_nav(ctx.cb, ctx.services, BOT_RESTARTING, None)
    await call(ctx.services.set_restart_wait, ctx.cb.message.chat.id, ctx.cb.message.message_id)
    await call(ctx.services.restart_bot)
    return None


ACTIONS = {"awg": Confirm(question=_awg_question, label="🔁 Перезапустить", run=_awg_restart),
           "bot": Confirm(question=_bot_question, label="🔁 Перезапустить", run=_bot_restart)}
