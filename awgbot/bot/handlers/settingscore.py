"""
settingscore.py — общая механика экранов настроек для ОБЕИХ ролей.

У основного бота (settings.py) и у агента шлюза (gateway.py) одни и те же
диалоги: приём числового/текстового значения, парольная фраза бэкапов в два
шага, «бэкап сейчас», выбор канала бэкапа, пять email-действий, тумблер с
проверкой «ящик настроен». Они жили двумя копиями и расходились: у агента
раздел после ввода приходил через send_menu, у основного — голым answer, и
правка одной копии не доезжала до другой.

Роль отдаёт сюда только то, чем действительно отличается, — колбэки и
клавиатуры (Hooks); сами шаги диалогов здесь. Регистрируются на роутер роли
обработчики сообщений (состояния одни на обе роли), действия по кнопкам роль
зовёт из своих тонких обёрток: у неё свой класс callback_data.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from awgbot.core import settings
from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.states import BackupPassphrase, EmailSetup, SettingsInput
from awgbot.bot.handlers.common import call, ask_tracked, cleanup_content, send_menu


@dataclass
class Hooks:
    """Чем роли различаются.

    cancel_kb(sec)        — «Отмена» на приглашении к вводу (ведёт в раздел sec);
    email_offer_kb(sec)   — «почта не настроена»: назад в sec / настроить;
    email_forget_kb()     — подтверждение отключения ящика;
    render(cb, services, sec) — перерисовать раздел на месте кнопки;
    screen(services, sec) — (text, markup) раздела, для показа новым сообщением.
    """
    cancel_kb: Callable[[str], object]
    email_offer_kb: Callable[[str], object]
    email_forget_kb: Callable[[], object]
    render: Callable[[CallbackQuery, object, str], Awaitable[None]]
    screen: Callable[[object, str], Awaitable[tuple]]


_TOGGLE_DEFAULTS = {"notifications.email_fallback": False}


async def ask(cb: CallbackQuery, services, prompt: str, markup) -> None:
    """Приглашение к вводу — на месте экрана и в служебные: после ответа оно
    отслужило и убирается вместе с вводом (см. after_input)."""
    from awgbot.bot.handlers.common import edit
    await edit(cb, prompt, markup)
    await call(services.db.add_content_msg_id, cb.message.chat.id, cb.message.message_id)


async def after_input(message: Message, services, hooks: Hooks, sec: str) -> None:
    """Раздел после ТЕКСТОВОГО ввода — новым сообщением через send_menu.

    Голый message.answer оставлял в чате два живых экрана: приглашение «введи
    значение» с кнопкой «Отмена» и новый раздел, а нав-указатель так и стоял на
    приглашении — следующий переход гасил не то. Служебное убираем: само
    приглашение, ввод человека, переспросы (всё это трекается); в чате
    остаются финишер «изменено: было → стало» и раздел."""
    await cleanup_content(message.bot, services, message.chat.id)
    await send_menu(message, services, *await hooks.screen(services, sec))


# ── ввод значения ────────────────────────────────────────────────────────────

async def start_edit(cb: CallbackQuery, services, hooks: Hooks, state: FSMContext,
                     key: str, sec: str) -> bool:
    """Открыть ввод значения key; False — ключ неизвестен (старая клавиатура)."""
    if key not in texts.SETTINGS_BOUNDS and key not in texts.SETTINGS_TEXT:
        await cb.answer("Эта настройка недоступна.", show_alert=True)
        return False
    await state.set_state(SettingsInput.value)
    await state.update_data(key=key, sec=sec)
    if key == "email.resume_address":
        prompt = texts.email_ask_resume_address(await call(services.email_resume_address))
    else:
        prompt = texts.settings_prompt(key)
    await ask(cb, services, prompt, hooks.cancel_kb(sec))
    await cb.answer()
    return True


def _validate_server_value(key: str, raw: str) -> tuple[bool, str]:
    """Проверки для правок раздела «Сервер». Пускать сюда что угодно нельзя:
    значение уезжает в КАЖДУЮ следующую ссылку, а сломанную ссылку человек
    увидит только при импорте — и без единого сообщения об ошибке."""
    import ipaddress
    import re as _re
    if not raw:
        return False, "пусто — значение обязательно"
    if key == "app.network.server_host":
        try:
            ipaddress.ip_address(raw)
            return True, ""
        except ValueError:
            pass
        if _re.fullmatch(r"[0-9.]+", raw):
            # «10.8.1.300» — это опечатка в адресе, а не доменное имя: цифры и
            # точки проходят проверку имени, и ссылка уехала бы в никуда.
            return False, "похоже на IP с опечаткой — проверь октеты"
        if _re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
                         r"(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+", raw):
            return True, ""
        return False, "нужен IP или доменное имя"
    if key == "app.client_config.server_name":
        return (True, "") if len(raw) <= 64 else (False, "длинновато: не больше 64 символов")
    if key == "app.client_config.dns1":
        parts = [p for p in raw.replace(",", " ").split() if p]
        if not 1 <= len(parts) <= 2:
            return False, "один или два адреса"
        for p in parts:
            try:
                ipaddress.ip_address(p)
            except ValueError:
                return False, f"«{p}» не IP-адрес"
        return True, ""
    return True, ""


async def _receive_text(message: Message, state: FSMContext, services, hooks: Hooks,
                        key: str, sec: str) -> None:
    from awgbot.domain.services import ServiceError
    raw = (message.text or "").strip()
    if key == "email.resume_address":
        from awgbot.infra import mail
        if raw == "-":                            # «вернуть сам ящик»
            raw = ""
        if raw and not mail.is_address(raw):
            await ask_tracked(message, services, texts.EMAIL_BAD_ADDRESS)
            return
    elif key == "app.firewall.ssh_allow":
        # Вайтлист не «значение настройки», а список: пишет его сервис —
        # он же проверяет каждый адрес и перевыставляет таблицу.
        before = list(settings.get("app.firewall.ssh_allow", []) or [])
        try:
            after = await call(services.firewall_allow_add, raw)
        except ServiceError as e:
            await ask_tracked(message, services, f"⚠️ {texts._e(str(e))}")
            return
        await state.clear()
        await message.answer(texts.settings_ssh_allow_added(
            [x for x in after if x not in before] or [raw]))
        await after_input(message, services, hooks, sec)
        return
    else:
        ok, err = _validate_server_value(key, raw)
        if not ok:
            await ask_tracked(message, services, f"⚠️ {texts._e(err)}")
            return
    old = str(settings.get(key, "") or "")
    shown_new = raw
    if key == "app.client_config.dns1":
        # В конфиге два поля, в UI одна строка. Второй адрес обязан
        # быть тем же, если назван один: стеки опрашивают список не
        # строго по порядку, и «публичный вторым номером» вернул бы
        # утечку резолва мимо нашего dnsmasq.
        parts = [x for x in raw.replace(",", " ").split() if x]
        old2 = str(settings.get("app.client_config.dns2", "") or "")
        old = f"{old}, {old2}" if old2 and old2 != old else old
        await call(settings.set_value, "app.client_config.dns2",
                   parts[1] if len(parts) > 1 else parts[0])
        raw = parts[0]
        shown_new = ", ".join(parts)
    elif key == "email.resume_address":
        old = old or "сам ящик"
        shown_new = raw or "сам ящик"
    try:
        await call(settings.set_value, key, raw)
    except settings.SettingsWriteError as e:
        await state.clear()
        await message.answer(str(e))
        await after_input(message, services, hooks, sec)
        return
    await state.clear()
    await message.answer(texts.settings_changed(key, old, shown_new))
    await after_input(message, services, hooks, sec)


async def receive_value(message: Message, state: FSMContext, services, hooks: Hooks,
                        default_sec: str) -> None:
    data = await state.get_data()
    key, sec = data.get("key"), data.get("sec", default_sec)
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if key in texts.SETTINGS_TEXT:
        await _receive_text(message, state, services, hooks, key, sec)
        return
    if key not in texts.SETTINGS_BOUNDS:      # рассинхрон state (не должен случаться)
        await state.clear()
        await after_input(message, services, hooks, sec)
        return
    lo, hi, _label, _unit = texts.SETTINGS_BOUNDS[key]
    raw = (message.text or "").strip()
    try:
        val = int(raw)
        if not (lo <= val <= hi):
            raise ValueError
    except ValueError:
        await ask_tracked(message, services, texts.settings_bad_value(key))
        return
    old = settings.get(key, None)
    try:
        await call(settings.set_value, key, val)
    except settings.SettingsWriteError as e:
        await state.clear()
        await message.answer(str(e))
        await after_input(message, services, hooks, sec)
        return
    await state.clear()
    await message.answer(texts.settings_changed(key, old, val))
    await after_input(message, services, hooks, sec)


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
        await edit(cb, texts.EMAIL_NOT_CONFIGURED, hooks.email_offer_kb(sec))
        await cb.answer()
        return
    # дефолт тумблера — по ключу: у большинства «включено», но у ключей с
    # дефолтом «выключено» первое нажатие иначе записало бы «выкл»
    cur = settings.get_bool(key, _TOGGLE_DEFAULTS.get(key, True))
    try:
        await call(settings.set_value, key, not cur)
    except settings.SettingsWriteError as e:
        await cb.answer(str(e), show_alert=True)
        return
    if after_set is not None:
        await after_set(key)
    await hooks.render(cb, services, sec)
    await cb.answer()


# ── резервные копии ──────────────────────────────────────────────────────────

async def backup_now(cb: CallbackQuery, services, hooks: Hooks) -> None:
    """«Бэкап сейчас»: в почту, если канал — почта, иначе файлами в чат."""
    from awgbot.infra import mail
    await cb.answer("Готовлю резервную копию…")
    try:
        paths = await call(services.make_backup)
    except Exception as e:                            # noqa: BLE001
        await cb.message.answer(texts.GW_BACKUP_NO_KEY if "шифрован" in str(e) else f"⚠️ {e}")
        return
    if await call(services.backup_channel) == "email":
        try:
            await call(services.email_send_backup, paths)
            acc = await call(services.email_account)
            await cb.message.answer(texts.backup_mailed(acc.login if acc else "", len(paths)),
                                    reply_markup=kb.hide_only())
        except mail.MailError as e:
            await cb.message.answer(f"🔴 {e}")
        await hooks.render(cb, services, "backup")
        return
    for p in paths:
        try:
            await cb.message.answer_document(FSInputFile(p))
        except Exception:                             # noqa: BLE001
            pass
    await hooks.render(cb, services, "backup")


async def set_backup_channel(cb: CallbackQuery, services, hooks: Hooks, val: str) -> None:
    """telegram | email; почта — только с настроенным ящиком и шифрованием."""
    if val not in ("telegram", "email"):
        await cb.answer("Нет такого варианта.", show_alert=True)
        return
    if val == "email":
        if not await call(services.email_configured):
            from awgbot.bot.handlers.common import edit
            await edit(cb, texts.EMAIL_NOT_CONFIGURED, hooks.email_offer_kb("backup"))
            await cb.answer()
            return
        if not await call(services.backup_encryption_enabled):
            await cb.answer(texts.BACKUP_NEEDS_ENCRYPTION, show_alert=True)
            return
    try:
        await call(settings.set_value, "app.scheduler.backup_channel", val)
    except settings.SettingsWriteError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await hooks.render(cb, services, "backup")
    await cb.answer()


async def passphrase_start(cb: CallbackQuery, services, hooks: Hooks, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(BackupPassphrase.first)
    await ask(cb, services, texts.BACKUP_ASK_PASSPHRASE, hooks.cancel_kb("backup"))
    await cb.answer()


async def _take_secret_message(message: Message) -> str:
    text = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:                                  # noqa: BLE001
        pass
    return text


# ── почта ────────────────────────────────────────────────────────────────────

async def email_action(cb: CallbackQuery, services, hooks: Hooks, state: FSMContext,
                       key: str) -> bool:
    """setup | check | test | forget | forget!. False — ключ не наш."""
    from awgbot.bot.handlers.common import edit
    from awgbot.infra import mail
    if key == "setup":
        await state.clear()
        await state.set_state(EmailSetup.address)
        acc = await call(services.email_account)
        prompt = texts.email_ask_address_change(acc.login) if acc else texts.EMAIL_ASK_ADDRESS
        await ask(cb, services, prompt, hooks.cancel_kb("email"))
        await cb.answer()
        return True
    if key == "check":
        await cb.answer("Проверяю…")
        ok, detail = await call(services.email_check)
        await hooks.render(cb, services, "email")
        if not ok:
            await cb.message.answer(texts.email_check_failed(detail))
        return True
    if key == "test":
        await cb.answer("Отправляю…")
        try:
            await call(services.email_send_test)
        except mail.MailError as e:
            await cb.message.answer(f"🔴 {e}")
            return True
        acc = await call(services.email_account)
        await cb.message.answer(texts.email_test_sent(acc.login if acc else ""),
                                reply_markup=kb.hide_only())
        return True
    if key == "forget":
        await edit(cb, texts.EMAIL_FORGET_CONFIRM, hooks.email_forget_kb())
        await cb.answer()
        return True
    if key == "forget!":
        await call(services.email_forget)
        await cb.message.answer(texts.EMAIL_FORGOTTEN)
        await hooks.render(cb, services, "email")
        await cb.answer()
        return True
    return False


# ── регистрация обработчиков сообщений (состояния общие на обе роли) ────────

def register(router, hooks: Hooks, *, default_sec: str = "root") -> dict:
    """Обработчики ввода: значение настройки, парольная фраза (два шага),
    мастер почты. Возвращает их по именам — для тестов."""
    from awgbot.bot.handlers import mailwizard

    @router.message(SettingsInput.value)
    async def _receive_value(message: Message, state: FSMContext, services):
        await receive_value(message, state, services, hooks, default_sec)

    @router.message(BackupPassphrase.first)
    async def _passphrase_first(message: Message, state: FSMContext, services):
        from awgbot.domain.backupcrypto import MIN_PASSPHRASE_LEN
        phrase = await _take_secret_message(message)
        if len(phrase) < MIN_PASSPHRASE_LEN:
            await ask_tracked(message, services,
                              f"⚠️ Фраза короче {MIN_PASSPHRASE_LEN} символов. Пришли другую.")
            return
        await state.update_data(passphrase=phrase)
        await state.set_state(BackupPassphrase.second)
        await ask_tracked(message, services, texts.BACKUP_ASK_PASSPHRASE_AGAIN,
                          reply_markup=hooks.cancel_kb("backup"))

    @router.message(BackupPassphrase.second)
    async def _passphrase_second(message: Message, state: FSMContext, services):
        phrase = await _take_secret_message(message)
        first = (await state.get_data()).get("passphrase", "")
        if phrase != first:
            await state.set_state(BackupPassphrase.first)
            await state.update_data(passphrase="")
            await ask_tracked(message, services, texts.BACKUP_PASSPHRASE_MISMATCH,
                              reply_markup=hooks.cancel_kb("backup"))
            return
        await state.clear()
        await call(services.backup_set_passphrase, phrase)
        await message.answer(texts.BACKUP_PASSPHRASE_SET)
        await after_input(message, services, hooks, "backup")

    async def _email_done(message: Message, services):
        await after_input(message, services, hooks, "email")

    mw = mailwizard.register(router, cancel_kb=lambda: hooks.cancel_kb("email"),
                             done=_email_done)
    return {"receive_value": _receive_value, "passphrase_first": _passphrase_first,
            "passphrase_second": _passphrase_second, **mw}


__all__ = ["Hooks", "register", "ask", "after_input", "start_edit", "toggle_bool",
           "backup_now", "set_backup_channel", "passphrase_start", "email_action"]
