"""apply.py — часть пакета gateway (см. __init__)."""
from __future__ import annotations

import base64
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from awgbot.bot import keyboards as kb
from awgbot.bot import texts, ui
from awgbot.bot.callbacks import GwCB
from awgbot.bot.handlers.common import call, edit_nav
from .panel import router
from awgbot.bot.handlers import gateway as pkg       # имена пакета — через него: их подменяют тесты


@router.callback_query(GwCB.filter(F.action.in_({"apply!", "apply_ow!", "apply_keep!"})))
async def gw_bundle_apply(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    """apply! — первый шаг: если в файле фраза бэкапов, отличная от местной,
    сначала вопрос; apply_ow!/apply_keep! — ответ на него. Файл до решения
    остаётся в памяти диалога."""
    raw = (await state.get_data()).get("bundle")
    if not raw:
        await state.clear()
        await cb.answer(ui.Toast.no_file, show_alert=True)
        return
    blob = base64.b64decode(raw)
    if callback_data.action == "apply!":
        info = await call(services.inspect_bundle, blob)
        if info.get("ok") and info.get("passphrase_differs"):
            await edit_nav(cb, services, texts.GW_BUNDLE_PASSPHRASE_QUESTION,
                           kb.gateway_bundle_passphrase_kb())
            await cb.answer()
            return
    await state.clear()
    await cb.answer("Применяю…")
    ok, detail = await call(services.apply_bundle, blob, callback_data.action == "apply_ow!")
    if ok:
        # человеческий итог из статуса скрипта; хвост вывода — только при отказе
        detail = await call(services.gateway_apply_report) or detail
    mail = await call(services.bundle_mail_check)
    await edit_nav(cb, services, texts.gateway_config_result(ok, detail, mail), None)
    # итог — серверу сразу: там у файла ждут ответа; отказ уходит тем же путём
    from awgbot.runtime import linkclient
    from awgbot.util import bundlecrypt
    await linkclient.report_applied(services, ok, "" if ok else detail, bundlecrypt.fingerprint(blob))
    if ok:
        # бандл мог только что включить канал — поднять клиента и сразу
        # отправить снимок: человек смотрит на карточку слота на ВПС сейчас
        await linkclient.poke(services)
        if await call(services.lan_lists_needed):
            # режим без VPN включён этим бандлом, списков ещё нет: не ждать
            # планового обновления. Через минуту, а не сейчас: канал, если он
            # есть, за это время привезёт фиды сам, и своё скачивание не понадобится
            pkg.asyncio.get_running_loop().create_task(pkg._lan_lists_soon(services, cb.bot))
        # он ли помеченный шлюз: не помечен или помечен другой → токен пометки.
        # При живом канале агент отправляет его серверу сам — просить человека
        # пересылать сообщение незачем; сообщение с токеном — только когда
        # канала нет. Секунды ожидания — на подключение только что поднятого
        # клиента канала.
        outcome = await call(services.gateway_mark_outcome)
        if outcome.get("claim"):
            for _ in range(6):
                if linkclient.online():
                    break
                await pkg.asyncio.sleep(0.5)
            if linkclient.online():
                await cb.message.answer(texts.gateway_claim_via_channel_text(outcome["status"], await call(services.server_cfg1)),
                                        reply_markup=kb.hide_only())
            else:
                await cb.message.answer(texts.gateway_claim_forward_text(outcome["claim"], outcome["status"], await call(services.server_cfg1)),
                                        reply_markup=kb.hide_only())
    await pkg._panel(cb.message, services, fresh=True, keep_id=cb.message.message_id)


@router.callback_query(GwCB.filter(F.action == "drop"))
async def gw_bundle_drop(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    await pkg._panel(cb.message, services, cb)
    await cb.answer(ui.Toast.file_dropped)
