"""
handlers/gateway.py — панель и операции агента шлюза (роль gateway, только админ).

Этап 1 — панель; этап 2 — доктор, рестарт линка и реассерт (через
подтверждение), приём шифрованного бандла файлом. Пробы и операции блокирующие
(subprocess) — через call, как всё синхронное в проекте.
"""
from __future__ import annotations

import base64
import io

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.callbacks import GwCB, UpdateCB
from awgbot.bot.filters import RoleFilter
from awgbot.bot.handlers.common import call, edit_nav, send_menu, cleanup_content, purge_menus
from awgbot.util import bundlecrypt

router = Router(name="gateway")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))

# Бандл ~25 КБ; всё, что заметно крупнее, — не наш файл. Лимит спасает от
# скачивания случайно пересланного видео.
_BUNDLE_MAX_BYTES = 512 * 1024


async def _panel(target, services, cb: CallbackQuery | None = None):
    """Панель — через нав-хелперы: одно живое меню в чате, прошлое гаснет,
    история ведётся для /start."""
    st = await call(services.status)
    if cb is not None:
        await edit_nav(cb, services, texts.gateway_panel(st), kb.gateway_panel_kb())
    else:
        await send_menu(target, services, texts.gateway_panel(st), kb.gateway_panel_kb())


@router.message(CommandStart())
async def gw_start(message: Message, services, state: FSMContext):
    await state.clear()
    await purge_menus(message.bot, services, message.chat.id)
    await _panel(message, services)


@router.callback_query(GwCB.filter(F.action == "panel"))
async def gw_panel(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    await _panel(cb.message, services, cb)
    await cb.answer()


@router.callback_query(GwCB.filter(F.action == "doctor"))
async def gw_doctor(cb: CallbackQuery, services):
    checks = await call(services.doctor)
    await edit_nav(cb, services, texts.gateway_doctor(checks), kb.gateway_back_kb())
    await cb.answer()


@router.callback_query(GwCB.filter(F.action.in_({"restart", "reassert"})))
async def gw_confirm(cb: CallbackQuery, callback_data: GwCB, services):
    text = (texts.GW_CONFIRM_RESTART if callback_data.action == "restart"
            else texts.GW_CONFIRM_REASSERT)
    await edit_nav(cb, services, text, kb.gateway_confirm_kb(callback_data.action))
    await cb.answer()


@router.callback_query(GwCB.filter(F.action.in_({"restart!", "reassert!"})))
async def gw_execute(cb: CallbackQuery, callback_data: GwCB, services):
    if callback_data.action == "restart!":
        await cb.answer("Перезапускаю линк…")
        ok, detail = await call(services.restart_link)
        title = "Рестарт линка"
    else:
        await cb.answer("Реассерт…")
        ok, detail = await call(services.reassert)
        title = "Реассерт обвязки"
    # Итог остаётся в чате отдельным сообщением: «когда и чем кончилось»
    # спрашивают потом, а панель переписывается следующей навигацией.
    await edit_nav(cb, services, texts.gateway_op_result(title, ok, detail), None)
    await _panel(cb.message, services)


# ── бандл файлом ─────────────────────────────────────────────────────────────

@router.message(F.document)
async def gw_bundle_document(message: Message, services, state: FSMContext):
    doc = message.document
    if doc.file_size and doc.file_size > _BUNDLE_MAX_BYTES:
        await message.answer(texts.GW_BUNDLE_NOT_OURS)
        return
    buf = io.BytesIO()
    await message.bot.download(doc, destination=buf)
    blob = buf.getvalue()
    # Формат проверяем ДО предложения применить, расшифровку — только при
    # применении: чужой файл отбивается сразу, а ключ линка читается один раз.
    if not blob.startswith(bundlecrypt.MAGIC):
        await message.answer(texts.GW_BUNDLE_NOT_OURS)
        return
    await state.update_data(bundle=base64.b64encode(blob).decode())
    await message.answer(texts.GW_BUNDLE_RECEIVED, reply_markup=kb.gateway_bundle_kb())


@router.callback_query(GwCB.filter(F.action == "apply!"))
async def gw_bundle_apply(cb: CallbackQuery, services, state: FSMContext):
    raw = (await state.get_data()).get("bundle")
    await state.clear()
    if not raw:
        await cb.answer("Бандла в памяти нет — пришли файл заново.", show_alert=True)
        return
    await cb.answer("Применяю…")
    ok, detail = await call(services.apply_bundle, base64.b64decode(raw))
    await edit_nav(cb, services, texts.gateway_op_result("Бандл", ok, detail), None)
    await _panel(cb.message, services)


@router.callback_query(GwCB.filter(F.action == "drop"))
async def gw_bundle_drop(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    await _panel(cb.message, services, cb)
    await cb.answer("Бандл отброшен")


# ── самообновление агента (этап 3) — та же механика, что у клиентской роли ────

@router.callback_query(UpdateCB.filter(F.action == "install"))
async def gw_update_install(cb: CallbackQuery, services):
    """Скачать следующую ступень, сверить sha256, запустить апдейтер вне cgroup.
    Итог пришлёт уже новый процесс (report_update_result на старте)."""
    nxt = await call(services.update_next)
    if nxt is None:
        await cb.answer("Обновлять не на что — версия актуальна.", show_alert=True)
        return
    await cb.answer("Запускаю обновление…")
    chat_id = cb.message.chat.id
    await cleanup_content(cb.bot, services, chat_id)
    try:
        await cb.message.delete()
    except Exception:                                 # noqa: BLE001
        pass
    wait = await cb.bot.send_message(chat_id, texts.update_wait(nxt.tag))
    await call(services.set_update_wait, chat_id, wait.message_id)
    try:
        await call(services.apply_update, nxt)
    except Exception as e:                            # noqa: BLE001
        await call(services.pop_update_wait)
        await call(services.db.set_state, "update_pending", "")
        try:
            await wait.delete()
        except Exception:                             # noqa: BLE001
            pass
        await cb.bot.send_message(chat_id, texts.update_failed(str(e)),
                                  reply_markup=kb.update_done_menu())


@router.callback_query(UpdateCB.filter(F.action == "menu"))
async def gw_update_menu(cb: CallbackQuery, services, state: FSMContext):
    """«В меню» на итоге обновления: текст остаётся, кнопка снимается, панель —
    новым сообщением."""
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:                                 # noqa: BLE001
        pass
    await _panel(cb.message, services)
    await cb.answer()


@router.callback_query(UpdateCB.filter(F.action == "mute"))
async def gw_update_mute(cb: CallbackQuery, services):
    await call(services.mute_updates)
    await cb.answer("Уведомления об обновлениях выключены.")
    try:
        await cb.message.delete()
    except Exception:                                 # noqa: BLE001
        pass


# ── раздел обновлений: ручная точка входа (уведомление могло прийти до тебя) ──

_SCHED_RU = {"day": "каждый день", "week": "раз в неделю", "month": "раз в месяц",
             "never": "никогда"}


async def _updates_screen(cb: CallbackQuery, services):
    from awgbot.core import config, settings
    muted = await call(services.updates_muted)
    sched = _SCHED_RU.get(str(settings.get("updates.poll_schedule", "day")).lower(), "?")
    await edit_nav(cb, services,
                   texts.gateway_updates(config.INSTALLED_VERSION, muted, sched),
                   kb.gateway_updates_kb(muted))


@router.callback_query(GwCB.filter(F.action == "updates"))
async def gw_updates_screen(cb: CallbackQuery, services):
    await _updates_screen(cb, services)
    await cb.answer()


@router.callback_query(GwCB.filter(F.action == "upd_toggle"))
async def gw_updates_toggle(cb: CallbackQuery, services):
    if await call(services.updates_muted):
        await call(services.unmute_updates)
    else:
        await call(services.mute_updates)
    await _updates_screen(cb, services)
    await cb.answer()


@router.callback_query(GwCB.filter(F.action == "upd_check"))
async def gw_updates_check(cb: CallbackQuery, services):
    """Проверить сейчас: есть ступень — показать с кнопкой «Обновить» (тот же
    UpdateCB, что и в уведомлении); нет — сказать, что версия актуальна."""
    from awgbot.core import config
    await cb.answer("Проверяю…")
    nxt = await call(services.update_next)
    if nxt is None:
        await edit_nav(cb, services, texts.update_current_ok(config.INSTALLED_VERSION),
                       kb.gateway_updates_kb(await call(services.updates_muted)))
        return
    await edit_nav(cb, services,
                   texts.update_admin_available(config.INSTALLED_VERSION, nxt.tag, nxt.body),
                   kb.gateway_update_available_kb())
