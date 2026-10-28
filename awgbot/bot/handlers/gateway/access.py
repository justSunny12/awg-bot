"""access.py — часть пакета gateway (см. __init__)."""
from __future__ import annotations

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from awgbot.bot import keyboards as kb
from awgbot.bot import texts, ui
from awgbot.bot.roles import GATEWAY
from awgbot.bot.callbacks import GwCB
from awgbot.bot.handlers import settingscore as core
from awgbot.bot.handlers.common import call, edit_nav, send_menu, ask_here, ask_tracked
from awgbot.bot.states import SshPort, GwSshAllow
from awgbot.domain.services import ServiceError
from .panel import router
from awgbot.bot.handlers import gateway as pkg       # имена пакета — через него: их подменяют тесты


# ── 🛡 Доступ по SSH ─────────────────────────────────────────────────────────
# Та же механика, что в разделе основного бота (handlers/settings/inputs.py): ввод
# порта — FSM, «тот же» и «занят» — финишер с выбором, результат — со «Скрыть»
# и раздел следом. Сверх того — отказ при чужом владельце sshd_config.

@router.callback_query(GwCB.filter(F.action.in_({"ssh_port", "ssh_port_retry"})))
async def gw_ssh_port_ask(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    st = await call(services.ssh_screen)
    if st.get("owner"):
        # Чужой владелец — отказ сразу по кнопке, без ввода: у целевых машин
        # (OMV) это основной случай, лишний шаг ни к чему.
        await state.clear()
        await edit_nav(cb, services,
                       texts.ssh_owner_refusal(st, None if st.get("sshd_down") else st["port"], GATEWAY, pressed=True),
                       kb.gateway_back_kb("ssh"))
        await cb.answer()
        return
    await state.set_state(SshPort.value)
    if callback_data.action == "ssh_port_retry":
        # с финишера: он остаётся в чате с одной «Скрыть», приглашение — новым
        try:
            await cb.message.edit_reply_markup(reply_markup=kb.hide_only())
        except Exception:                                 # noqa: BLE001
            pass
        await state.update_data(ctx_kind="set_ssh", ctx_ref=0)
        await send_menu(cb.message, services, texts.ssh_port_ask(st.get("port"), GATEWAY), kb.cancel_input("set_ssh"),
                        keep_id=cb.message.message_id)         # финишер остаётся с одной «Скрыть»
    else:
        await ask_here(cb, services, state, texts.ssh_port_ask(st.get("port"), GATEWAY), "set_ssh")
    await cb.answer()


@router.callback_query(GwCB.filter(F.action == "ssh_port_back"))
async def gw_ssh_port_back(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=kb.hide_only())
    except Exception:                                     # noqa: BLE001
        pass
    await send_menu(cb.message, services, *await pkg._section("ssh", services), keep_id=cb.message.message_id)
    await cb.answer()


def _port_dialog(services) -> core.PortDialog:
    return core.PortDialog(
        screen=services.ssh_screen, port_key="port", sec="ssh",
        owner_refusal=lambda st, listening: (texts.ssh_owner_refusal(st, listening, GATEWAY),
                                             kb.gateway_back_kb("ssh")),
        finisher_kb=kb.gateway_ssh_port_finisher_kb,
        # адрес шлюза в локальной сети — в хвост про проброс на роутере
        changed_text=lambda old, new: texts.gateway_ssh_port_changed(old, new, _lan_addr(services)))


def _lan_addr(services) -> str:
    try:
        return services.lan_router_params()[1] or ""
    except Exception:                                     # noqa: BLE001
        return ""


@router.message(SshPort.value)
async def gw_ssh_port_received(message: Message, state: FSMContext, services):
    await core.port_received(message, state, services, pkg._hooks(), pkg._port_dialog(services))


@router.callback_query(GwCB.filter(F.action == "ssh_add"))
async def gw_ssh_allow_ask(cb: CallbackQuery, services, state: FSMContext):
    await state.set_state(GwSshAllow.value)
    await ask_here(cb, services, state, texts.GW_SSH_ALLOW_ASK, "set_ssh")
    await cb.answer()


@router.message(GwSshAllow.value)
async def gw_ssh_allow_received(message: Message, state: FSMContext, services):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    before = await call(services.ssh_allow_current)
    try:
        after = await call(services.ssh_allow_add, raw)
    except ServiceError as e:
        # html — деталь уже размечена и экранирована доменом (адрес моноширинным)
        why = str(e) if getattr(e, "html", False) else texts._e(str(e))
        await ask_tracked(message, services, f"⚠️ {why}. Попробуй ещё раз")
        return
    await state.clear()
    new = [x for x in after if x not in before]
    gone = [x for x in before if x not in after]          # схлопнуто в добавленную подсеть
    from awgbot.bot.texts.gateway import access as ta
    from awgbot.bot.texts.settings.inputs import SSH_ADDED_TITLE
    link = await _snap_codes(message, services, SSH_ADDED_TITLE, new)
    link_m = await _snap_codes(message, services, ta.SSH_MERGED_TITLE, gone)
    await core.after_input(message, services, pkg._hooks(), "ssh",
                           texts.gateway_ssh_allow_added(new, gone, link, link_m) if new or gone
                           else texts.GW_SSH_ALLOW_ALREADY)


@router.callback_query(GwCB.filter(F.action.in_({"ssh_del", "ssh_del!", "ssh_on", "ssh_on!",
                                                  "ssh_off", "ssh_off!"})))
async def gw_ssh_action(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    """Удаление адреса, включение и выключение фильтра — сразу, без
    подтверждений: всё отменяется той же кнопкой. Включение фильтра —
    с предупреждением всплывашкой."""
    await state.clear()
    act = callback_data.action.rstrip("!")
    try:
        if act == "ssh_del":
            st = await call(services.ssh_screen)
            allow = st.get("allow") or []
            num, _dot, tag = (callback_data.val or "").partition(".")
            idx = int(num) if num.isdigit() else -1
            if not 0 <= idx < len(allow) or tag != kb.entry_tag(allow[idx]):
                await cb.answer(ui.Toast.list_changed, show_alert=True)
            else:
                await call(services.ssh_allow_remove, allow[idx])
                await cb.answer(ui.toast(texts.gateway_ssh_removed(
                    allow[idx], bool(st.get("filter")) and len(allow) == 1)))
        elif act == "ssh_on":
            await call(services.ssh_filter_on)
            empty = not await call(services.ssh_allow_current)
            await cb.answer(texts.GW_SSH_FILTER_ON_EMPTY_ALERT if empty else texts.GW_SSH_FILTER_ON_ALERT,
                            show_alert=True)
        else:
            await call(services.ssh_filter_off)
            await cb.answer(texts.GW_SSH_FILTER_OFF, show_alert=True)
    except ServiceError as e:
        await cb.answer(ui.toast(e), show_alert=True)
    await pkg._render(cb, services, "ssh")


# Перезапуски AWG и бота — «🔧 Сервис» общих разделов; здесь — восстановление
# шлюза с панели и экрана здоровья.
@router.callback_query(GwCB.filter(F.action == "reassert"))
async def gw_confirm(cb: CallbackQuery, callback_data: GwCB, services):
    if callback_data.action != "reassert":
        await pkg._shared(cb, callback_data, services)
        return
    back = callback_data.val if callback_data.val in ("panel", "health") else "panel"   # откуда пришли — туда и отмена
    await edit_nav(cb, services, texts.gw_confirm_reassert(await call(services.carries_traffic)),
                   kb.gateway_confirm_kb("reassert", back))
    await cb.answer()


@router.callback_query(GwCB.filter(F.action == "reassert!"))
async def gw_execute(cb: CallbackQuery, callback_data: GwCB, services):
    if callback_data.action != "reassert!":
        await pkg._shared(cb, callback_data, services)
        return
    await cb.answer("Восстанавливаю…")
    ok, detail = await call(services.reassert)
    # снимок с новым состоянием — на ВПС сейчас, а не через тик
    from awgbot.runtime import linkclient
    await linkclient.poke(services)
    # Итог остаётся в чате отдельным сообщением: «когда и чем кончилось»
    # спрашивают потом, а панель переписывается следующей навигацией.
    await edit_nav(cb, services, texts.gateway_op_result("Шлюз восстановлен", "Шлюз не восстановлен", ok, detail,
                                                                 texts.REASSERT_FAIL_HINT), None)
    await pkg._panel(cb.message, services, fresh=True, keep_id=cb.message.message_id)



async def _snap_codes(message, services, title: str, items: list, shown: int = 12):
    """Итог ввода — больше shown адресов: все снимком («⬅️ Назад» — в «🛡 SSH-доступ»)."""
    if len(items) <= shown:
        return None
    from awgbot.util import listlink
    rows = [f"<code>{texts._e(x)}</code>" for x in items]
    key = await call(services.db.list_snap_keep, message.chat.id, "admin", title, rows,
                     GwCB(action="ssh").pack(), False)
    return (getattr(services, "bot_username", "") or "", listlink.build("snap", key=key)) if key else None
