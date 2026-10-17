"""actions.py — часть пакета settingscore (см. __init__): тумблер, порт SSH, бэкап сейчас, канал бэкапа, парольная фраза."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
from awgbot.core import settings
from awgbot.bot import keyboards as kb
from awgbot.bot import ui
from awgbot.bot import texts
from awgbot.bot.states import BackupPassphrase
from awgbot.bot.handlers.common import call, ask_tracked, cleanup_content, send_menu
from awgbot.domain.services import ServiceError
from .base import Hooks, ask, after_input


# ── тумблер ──────────────────────────────────────────────────────────────────

async def toggle_bool(cb: CallbackQuery, services, hooks: Hooks, key: str, sec: str,
                      after_set=None) -> None:
    """Инвертировать bool в conf и перерисовать раздел. email_fallback без
    ящика не включается — предлагаем настроить, а не молча отказываем.
    after_set(key) — что применить сразу после записи (у основного —
    реконсиляция маршрутизации)."""
    if key == "notifications.email_fallback" and not settings.get_bool(key, False) \
            and not await call(services.email_configured):
        from awgbot.bot.handlers.common import edit
        await edit(cb, texts.email_not_configured(sec), hooks.email_offer_kb(sec))
        await cb.answer()
        return
    # дефолт тумблера — по ключу: у большинства «включено», но у ключей с
    # дефолтом «выключено» первое нажатие иначе записало бы «выкл»
    from awgbot.bot import sections
    d = sections.default(key)
    cur = settings.get_bool(key, True if d is None else bool(d))
    try:
        await call(settings.set_value, key, not cur)
    except settings.SettingsWriteError as e:
        await cb.answer(ui.toast(e), show_alert=True)
        return
    if after_set is not None:
        await after_set(key)
    await hooks.render(cb, services, sec)
    await cb.answer(texts.toggle_toast(key, not cur))      # итог — всплывашкой: «Тихие часы: вкл»


# ── резервные копии ──────────────────────────────────────────────────────────

# ── порт SSH: приём нового значения ──────────────────────────────────────────
@dataclass
class PortDialog:
    """Чем роли различаются в диалоге порта: экран раздела и ключ порта в нём,
    отказ при чужом владельце sshd_config (текст и клавиатура), финишер «тот
    же / занят», раздел для итога и текст «порт изменён»."""
    screen: Callable[[], dict]
    port_key: str
    sec: str
    owner_refusal: Callable[[dict, int | None], tuple]
    finisher_kb: Callable[[], object]
    changed_text: Callable[[int, int], str]


async def port_received(message: Message, state: FSMContext, services, hooks: Hooks,
                        spec: PortDialog) -> None:
    """Ввод порта: число 1–65535; чужой владелец конфига — отказ экраном (гонка:
    владелец появился между показом и вводом); тот же порт и занятый — не
    ошибка и не переспрос: диалог закрыт, финишер с выбором; иначе смена с
    проверками сервиса и итог первой строкой раздела."""
    from awgbot.infra import sshd
    from awgbot.domain.gwssh import SshOwnerRefusal
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit() or not sshd.valid_port(int(raw)):
        await ask_tracked(message, services, "⚠️ Порт — число от 1 до 65535, попробуй ещё раз")
        return
    port = int(raw)
    st = await call(spec.screen)
    if st.get("owner"):
        await state.clear()
        await cleanup_content(message.bot, services, message.chat.id)
        await send_menu(message, services,
                        *spec.owner_refusal(st, None if st.get("sshd_down") else st.get(spec.port_key)))
        return
    if not st.get("sshd_down") and port == int(st.get(spec.port_key) or 0):
        await state.clear()
        await cleanup_content(message.bot, services, message.chat.id)
        await send_menu(message, services, texts.ssh_port_same(port), spec.finisher_kb())
        return
    try:
        busy = await call(services.ssh_port_busy, port)
    except ServiceError as e:
        await ask_tracked(message, services, f"⚠️ {texts._e(str(e))}")
        return
    if busy:
        await state.clear()
        await cleanup_content(message.bot, services, message.chat.id)
        await send_menu(message, services, texts.ssh_port_busy(port, "" if busy == "?" else busy),
                        spec.finisher_kb())
        return
    await state.clear()
    try:
        old = await call(services.ssh_port_change, port)
    except SshOwnerRefusal as e:
        await cleanup_content(message.bot, services, message.chat.id)
        st = await call(spec.screen)
        await send_menu(message, services, *spec.owner_refusal(st, e.listening))
        return
    except ServiceError as e:
        # действие не выполнено, ничего не изменилось — ⚠️ (🔴 — состояние объекта)
        await after_input(message, services, hooks, spec.sec, f"⚠️ Порт не изменён: {texts._e(str(e))}")
    else:
        await after_input(message, services, hooks, spec.sec, spec.changed_text(old, port))


async def backup_now(cb: CallbackQuery, services, hooks: Hooks) -> None:
    """«Бэкап сейчас»: в почту, если канал — почта, иначе файлами в чат."""
    from awgbot.infra import mail
    from awgbot.domain.backup.crypto import BackupKeyMissing
    server = hooks.br.has.backup_gateways
    no_key = texts.GW_BACKUP_NO_KEY
    if server:
        from awgbot.domain.notices import backup as nb
        no_key = nb.no_phrase_line(bool(await call(services.db.gateways)))
    if hooks.br.has.backup_encrypted_only and not await call(services.backup_encryption_enabled):
        # копия только шифрованная, фразы нет — отказ всплывашкой, без «Готовлю…»
        await cb.answer(no_key, show_alert=True)
        return
    if server:
        busy = await call(services.backup_rekey_busy)
        if busy is not None:
            # идёт смена ключей линка: на диске .next/.prev — копия после неё
            await cb.answer(nb.REKEY_WAIT_TOAST.format(name=f"«{services._gw_name(busy)}»"), show_alert=True)
            return
    await cb.answer("Готовлю резервную копию…")
    from awgbot.util import timeutil as _tu
    made_at = _tu.to_iso(_tu.now())
    try:
        paths = await call(services.make_backup)
    except BackupKeyMissing:
        await cb.message.answer(no_key, reply_markup=kb.hide_only())
        return
    except Exception as e:                            # noqa: BLE001
        await cb.message.answer(f"⚠️ {texts._e(str(e))}")
        return
    caption = await call(services.backup_caption) if server else ""
    if await call(services.backup_channel) == "email":
        try:
            if caption:
                await call(services.email_send_backup, paths, caption)
            else:
                await call(services.email_send_backup, paths)
            acc = await call(services.email_account)
            await cb.message.answer(texts.backup_mailed(acc.login if acc else "", len(paths)),
                                    reply_markup=kb.hide_only())
            if server:
                await call(services.backup_event_covered, made_at)   # копия по событию уже не нужна
        except mail.MailError as e:
            await cb.message.answer(f"🔴 {texts._e(str(e))}")
        await hooks.render(cb, services, "backup")
        return
    sent = False
    for p in paths:
        try:
            if caption:
                await cb.message.answer_document(FSInputFile(p), caption=caption)
            else:
                await cb.message.answer_document(FSInputFile(p))
            sent = True
        except Exception:                             # noqa: BLE001
            pass
    if sent and server:
        await call(services.backup_event_covered, made_at)       # копия по событию уже не нужна
    await hooks.render(cb, services, "backup")


async def set_backup_channel(cb: CallbackQuery, services, hooks: Hooks, val: str) -> None:
    """telegram | email; почта — только с настроенным ящиком и шифрованием."""
    if val not in ("telegram", "email"):
        await cb.answer(ui.Toast.stale, show_alert=True)
        return
    if val == "email":
        if not await call(services.email_configured):
            from awgbot.bot.handlers.common import edit
            await edit(cb, texts.email_not_configured("backup"), hooks.email_offer_kb("backup"))
            await cb.answer()
            return
        if not await call(services.backup_encryption_enabled):
            await cb.answer(texts.backup_needs_encryption(hooks.br), show_alert=True)
            return
    try:
        await call(settings.set_value, "app.scheduler.backup_channel", val)
    except settings.SettingsWriteError as e:
        await cb.answer(ui.toast(e), show_alert=True)
        return
    await hooks.render(cb, services, "backup")
    await cb.answer(texts.cycle_toast("app.scheduler.backup_channel", val))


async def passphrase_start(cb: CallbackQuery, services, hooks: Hooks, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(BackupPassphrase.first)
    await ask(cb, services, state, texts.BACKUP_ASK_PASSPHRASE, "backup")
    await cb.answer()


async def _take_secret_message(message: Message) -> str:
    text = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:                                  # noqa: BLE001
        pass
    return text
