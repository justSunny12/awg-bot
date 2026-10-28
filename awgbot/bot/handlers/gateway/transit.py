"""transit.py — часть пакета gateway (см. __init__)."""
from __future__ import annotations

import re

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message
from awgbot.bot import keyboards as kb
from awgbot.bot import texts, ui
from awgbot.bot.callbacks import GwCB
from awgbot.bot.states import GatewayTransitDomain
from awgbot.bot.handlers.common import call, edit_nav, send_menu, cleanup_content, ask_here, ask_tracked
from .panel import router
from awgbot.bot.handlers import gateway as pkg       # имена пакета — через него: их подменяют тесты


# ── 🔀 VPN-транзит: один экран со своими списками ────────────────────────────

async def _own_info(services) -> dict:
    info, _checks = await call(services.own_status)
    return info


async def _transit_screen(services, chat_id: int):
    """(текст, клавиатура) экрана «🔀 VPN-транзит»: факты, списки, свои
    домены кнопками."""
    from awgbot.bot import paging
    st = await pkg._status(services, fresh=False)
    items = await call(services.lan_own_lists)
    return (texts.gateway_transit_text(st, items, await pkg._own_info(services),
                                       virtual=await call(services.lan_virtual), bot=pkg._bot_username(services)),
            kb.gateway_transit_kb(items, page=paging.page_of(chat_id, "lanlist")))


@router.callback_query(GwCB.filter(F.action.in_({"lan", "lan_list"})))
async def gw_transit(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    # сообщение под кнопкой — экран, а не служебное: приглашение к вводу
    # (core.ask) записало его в служебные, уборка снесла бы живое меню
    await cleanup_content(cb.message.bot, services, cb.message.chat.id, keep=cb.message.message_id)
    await edit_nav(cb, services, *await pkg._transit_screen(services, cb.message.chat.id))
    await cb.answer()


@router.callback_query(GwCB.filter(F.action.in_({"lan_add", "lan_ru"})))
async def gw_transit_ask(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    kind = callback_data.action.split("_", 1)[1]
    await state.set_state(GatewayTransitDomain.value)
    await state.update_data(kind=kind)
    await ask_here(cb, services, state, texts.gateway_transit_ask_domain(kind), "set_lan")
    await cb.answer()


@router.message(GatewayTransitDomain.value)
async def gw_transit_domain_received(message: Message, state: FSMContext, services):
    kind = (await state.get_data()).get("kind") or "add"
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    # как у пользователя: пробел, перенос строки, запятая или точка с запятой
    domains = [t for t in re.split(r"[\s,;]+", message.text or "") if t]
    if not domains:
        await ask_tracked(message, services, "⚠️ Пришли хотя бы один домен")
        return
    await state.clear()
    ok, out = await call(services.lan_domains, kind, domains)
    await cleanup_content(message.bot, services, message.chat.id)
    # итог — первыми строками экрана, с новым доменом уже в кнопках
    from awgbot.bot import screens
    text, markup = await pkg._transit_screen(services, message.chat.id)
    sync = await pkg._own_sync_tail(services, ok, out)
    budget = ui.note_budget(text)
    from awgbot.bot.texts.gateway import transit as tt
    link = None
    if tt.transit_result_cut(ok, out, budget=budget, cmd=kind):
        # итог не влез в экран — все строки снимком, хвост — ссылкой на него
        from awgbot.bot.callbacks import GwCB
        from awgbot.bot.lists.snapshot import snap_param
        param = await snap_param(services, message.chat.id, "admin", tt.transit_result_title(kind),
                                 tt.transit_result_rows(ok, out, kind), GwCB(action="lan").pack())
        if param:
            link = (getattr(services, "bot_username", "") or "", param)
    note = texts.gateway_transit_result(ok, out, sync, budget=budget, domains=domains, cmd=kind, link=link)
    await send_menu(message, services, screens.with_note(text, note), markup)


async def _own_sync_tail(services, ok: bool, out: str) -> str:
    """После правки списка: сверка и отправка серверу сразу; хвост итога — куда
    уйдёт правка, и только когда это правда: другого шлюза нет (сервер сказал)
    — хвоста нет; канала нет, а шлюзы есть — «применятся только здесь».
    Изменилось ли что-то, решает сверка файлов, а не слова вывода скрипта."""
    from awgbot.runtime import linkclient
    if not ok:
        return ""
    others = await call(services.link_standby_known)      # True / False / None — сервер не говорил
    if others is False:
        return ""
    info = await pkg._own_info(services)
    if not info.get("active"):
        return ""
    if not await linkclient.own_changed(services):
        return ""
    return "online" if linkclient.online() else "offline"


@router.callback_query(GwCB.filter(F.action.in_({"lan_rm", "lan_rm!"})))
async def gw_transit_remove(cb: CallbackQuery, callback_data: GwCB, services):
    """«➖ домен» — сразу, без подтверждения: всплывашка с итогом («Убран на
    всех шлюзах» при общих списках); скрипт ответил дольше, чем Telegram
    держит нажатие, — итог сообщением. Номер — по отсортированному списку;
    список успел измениться — переспрос, не чужой домен."""
    items = kb.lan_own_sorted(await call(services.lan_own_lists))
    num, _dot, tag = (callback_data.val or "").partition(".")
    idx = int(num) if num.isdigit() else -1
    if not 0 <= idx < len(items) or tag != kb.lan_own_tag(*items[idx]):   # без метки — не наша кнопка
        await cb.answer(ui.Toast.list_changed, show_alert=True)
        await edit_nav(cb, services, *await pkg._transit_screen(services, cb.message.chat.id))
        return
    _kind, dom = items[idx]
    ok, out = await call(services.lan_domains, "del", [dom])
    tail = await pkg._own_sync_tail(services, ok, out)
    try:
        if ok:
            await cb.answer(texts.gateway_transit_removed_toast(dom, sync=tail))
        else:
            await cb.answer(ui.toast(texts.gateway_transit_result(ok, out, tail, domains=[dom], cmd="del"), html=True),
                            show_alert=True)
    except TelegramBadRequest:
        await ask_tracked(cb.message, services, texts.gateway_transit_result(ok, out, tail, domains=[dom], cmd="del"))
    await edit_nav(cb, services, *await pkg._transit_screen(services, cb.message.chat.id))


@router.callback_query(GwCB.filter(F.action == "lan_router"))
async def gw_transit_router(cb: CallbackQuery, callback_data: GwCB, services):
    """Настройка роутера вкладками — тот же текст, что у основного бота, с
    подсетью и настоящим адресом этого шлюза."""
    import socket
    net, addr = await call(services.lan_router_params)
    tab = callback_data.val if callback_data.val in ("mt", "ow") else "mt"
    await edit_nav(cb, services,
                   texts.gateway_router_text(await call(services.link_slot_name) or socket.gethostname(),
                                             net, addr, tab=tab),
                   kb.gateway_transit_router_kb(tab))
    await cb.answer()

