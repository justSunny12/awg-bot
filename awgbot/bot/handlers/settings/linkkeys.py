"""linkkeys.py — «🔑 Ключи линка» в «⚙️ Настройки → 🔧 Сервис»: выбор шлюза, диалог, запуск смены ключей.

Кнопка редкая и аварийная (утечка файла первого применения, засвеченная
обфускация линка), поэтому живёт в «Сервисе» рядом с перезапусками и
переездом, а не на рабочих экранах шлюза. Карточка слота показывает только
состояние перехода. Регистрируется раньше общего do (actions)."""
from __future__ import annotations

from aiogram import F
from aiogram.types import CallbackQuery

from awgbot.bot import keyboards as kb
from awgbot.bot import sections, texts, ui
from awgbot.bot.callbacks import SetCB
from awgbot.bot.handlers.common import call, edit
from awgbot.bot.roles import MAIN
from awgbot.domain.services.types import ServiceError

from ._router import router
from .slots import _slot_state

_BUSY = ("staged", "asked", "switched")


@router.callback_query(SetCB.filter((F.sec == "lk") & (F.act == "do")))
async def linkkeys_action(cb: CallbackQuery, callback_data: SetCB, services):
    """pick — выбор шлюза (один — сразу диалог); ask — диалог по слоту;
    yes — смена запущена, «🔧 Сервис» с итогом первой строкой."""
    key = callback_data.key
    if key == "pick":
        slots = await call(services.gateway_rekey_slots)
        if not slots:
            await cb.answer(ui.toast("Сменить ключи линка по каналу конфигурации не у кого: нужен агент шлюза v3.3.0"),
                            show_alert=True)
            return
        if len(slots) > 1:
            await edit(cb, texts.GATEWAY_KEYS_PICK, kb.gateway_keys_pick(slots))
            await cb.answer()
            return
        await _ask(cb, services, slots[0][0])
        return
    try:
        slot = int(callback_data.val or 0)
    except ValueError:
        slot = 0
    if key == "ask" and slot:
        await _ask(cb, services, slot)
        return
    if key == "yes" and slot:
        await _yes(cb, services, slot)
        return
    await cb.answer(ui.Toast.stale, show_alert=True)


async def _ask(cb: CallbackQuery, services, slot: int) -> None:
    """Диалог: переход уже идёт (у этого слота или у другого) — всплывашка."""
    st = await _slot_state(cb, services, slot, lazy_ping=False)
    if st is None:
        return
    if (st.get("rekey") or {}).get("state") in _BUSY:
        await cb.answer(ui.toast("Смена ключей уже идёт"), show_alert=True)
        return
    for other in st["states"]:
        if other["gateway"].id != st["gateway"].id and (other.get("rekey") or {}).get("state") in _BUSY:
            await cb.answer(ui.toast(f"Идёт смена ключей у {other['display']} — дождись итога"), show_alert=True)
            return
    live_standby = any(o["gateway"].id != st["gateway"].id and not o.get("active")
                       and o.get("link_ok") and not o.get("unavailable") for o in st["states"])
    await edit(cb, texts.gateway_keys_ask(st, live_standby), kb.gateway_keys_confirm(st["gateway"].id))
    await cb.answer()


async def _yes(cb: CallbackQuery, services, slot: int) -> None:
    """«🔑 Сменить»: новые ключи — в .next обеих сторон, приглашение уходит шлюзу
    по каналу сейчас (на связи) или при подключении; «🔧 Сервис» с итогом."""
    try:
        await call(services.gateway_rekey_stage, slot)
    except ServiceError as e:
        await cb.answer(ui.toast(e), show_alert=True)
        return
    from awgbot.runtime import linkserver
    srv = linkserver.current()
    sent = bool(srv is not None and await srv.deliver_rekey(slot))
    # не на связи — ключи записаны и ждут подключения: «ушли» было бы неправдой
    await sections.render(cb, MAIN, services, "svc",
                          note=texts.GATEWAY_KEYS_SENT if sent else texts.GATEWAY_KEYS_QUEUED)
    await cb.answer()
