"""values.py — часть пакета settingscore (см. __init__): ввод значения настройки с проверками."""
from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from awgbot.core import settings
from awgbot.bot import ui
from awgbot.bot import texts
from awgbot.bot.callbacks import SetCB
from awgbot.bot.states import SettingsInput
from awgbot.bot.handlers.common import call, ask_tracked
from .base import Hooks, ask, after_input


# ── ввод значения ────────────────────────────────────────────────────────────

async def start_edit(cb: CallbackQuery, services, hooks: Hooks, state: FSMContext,
                     key: str, sec: str) -> bool:
    """Открыть ввод значения key; False — ключ неизвестен (старая клавиатура)."""
    from awgbot.bot import sections
    from awgbot.domain.services.traffic import bonus
    if sections.bounds(key) is None and key not in texts.SETTINGS_TEXT and key not in ("backup_when", bonus.SETTING):
        await cb.answer(ui.Toast.stale, show_alert=True)
        return False
    await state.set_state(SettingsInput.value)
    await state.update_data(key=key, sec=sec)
    if key == "email.resume_address":
        # магического «-» нет: «↩️ Сам ящик» — кнопка под приглашением
        from awgbot.bot.sections import email as email_sec
        from awgbot.bot.handlers.common import edit
        prompt = texts.email_ask_resume_address(await call(services.email_resume_address))
        await state.update_data(ctx_kind=f"set_{email_sec.ID}", ctx_ref=0)
        await edit(cb, prompt, email_sec.resume_prompt_kb(hooks.br))
        await call(services.db.add_content_msg_id, cb.message.chat.id, cb.message.message_id)
        await cb.answer()
        return True
    elif key == bonus.SETTING:
        prompt = texts.bonus_prompt(bonus.screen_text(bonus.current(settings)))
    elif key == "backup_when":
        prompt = texts.backup_when_prompt(settings.get_int("app.scheduler.backup_day", 1),
                                          settings.get_int("app.scheduler.backup_hour", 12))
    elif key == "app.client_config.dns1":
        d1 = str(settings.get(key, "") or "")
        d2 = str(settings.get("app.client_config.dns2", "") or "")
        prompt = texts.settings_prompt(key, f"{d1}, {d2}" if d2 and d2 != d1 else d1)
    else:
        cur = settings.get(key, sections.default(key))
        k = sections.scale(key)
        if k != 1 and cur is not None:
            cur = max(1, -(-int(cur) // k))                  # вверх: 90 с — «2 мин»
        prompt = texts.settings_prompt(key, cur)
    await ask(cb, services, state, prompt, sec)
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
        if not mail.is_address(raw):
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
        added = [x for x in after if x not in before] or [raw]
        await after_input(message, services, hooks, sec,
                          texts.settings_ssh_allow_added(added, await _snap_codes(
                              message, services, texts.settings.inputs.SSH_ADDED_TITLE, added, SetCB(sec="fw").pack())))
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
        mailbox = await call(services.email_mailbox)      # без алиаса код идёт на сам ящик
        old = old or mailbox
        shown_new = raw or mailbox
    try:
        await call(settings.set_value, key, raw)
    except settings.SettingsWriteError as e:
        await state.clear()
        await after_input(message, services, hooks, sec, ui.fail("Не сохранено", str(e)))
        return
    await state.clear()
    await after_input(message, services, hooks, sec, texts.settings_changed(key, old, shown_new))


async def _receive_backup_when(message: Message, state: FSMContext, services, hooks: Hooks,
                               sec: str) -> None:
    """«1 12» — день месяца и час автобэкапа одним приглашением."""
    parts = (message.text or "").split()
    try:
        day, hour = int(parts[0]), int(parts[1])
        if not (1 <= day <= 28 and 0 <= hour <= 23):
            raise ValueError
    except (ValueError, IndexError):
        await ask_tracked(message, services, texts.BACKUP_WHEN_BAD)
        return
    old = (settings.get_int("app.scheduler.backup_day", 1), settings.get_int("app.scheduler.backup_hour", 12))
    try:
        await call(settings.set_value, "app.scheduler.backup_day", day)
        await call(settings.set_value, "app.scheduler.backup_hour", hour)
    except settings.SettingsWriteError as e:
        await state.clear()
        await after_input(message, services, hooks, sec, ui.fail("Не сохранено", str(e)))
        return
    await state.clear()
    await after_input(message, services, hooks, sec,
                      texts.backup_when_changed(old, (day, hour)))


async def _receive_bonus(message: Message, state: FSMContext, services, hooks: Hooks, sec: str) -> None:
    """Бонус при исчерпании: «50», «10%», «10% 50» или «0» (выкл.)."""
    from awgbot.domain.services.traffic import bonus
    try:
        spec = bonus.parse(message.text or "")
    except ValueError:
        await ask_tracked(message, services, texts.BONUS_BAD)
        return
    old = bonus.screen_text(bonus.current(settings))
    try:
        await call(settings.set_value, bonus.SETTING, spec.stored())
    except settings.SettingsWriteError as e:
        await state.clear()
        await after_input(message, services, hooks, sec, ui.fail("Не сохранено", str(e)))
        return
    await state.clear()
    await after_input(message, services, hooks, sec, texts.bonus_changed(old, bonus.screen_text(spec)))


async def receive_value(message: Message, state: FSMContext, services, hooks: Hooks,
                        default_sec: str) -> None:
    data = await state.get_data()
    key, sec = data.get("key"), data.get("sec", default_sec)
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if key in texts.SETTINGS_TEXT:
        await _receive_text(message, state, services, hooks, key, sec)
        return
    if key == "backup_when":
        await _receive_backup_when(message, state, services, hooks, sec)
        return
    from awgbot.domain.services.traffic import bonus
    if key == bonus.SETTING:
        await _receive_bonus(message, state, services, hooks, sec)
        return
    from awgbot.bot import sections
    bounds = sections.bounds(key)
    if bounds is None:                        # рассинхрон state (не должен случаться)
        await state.clear()
        await after_input(message, services, hooks, sec)
        return
    lo, hi, _label, _unit = bounds
    raw = (message.text or "").strip()
    try:
        val = int(raw)
        if not (lo <= val <= hi):
            raise ValueError
    except ValueError:
        await ask_tracked(message, services, texts.settings_bad_value(key))
        return
    old = settings.get(key, sections.default(key))
    k = sections.scale(key)
    if k != 1 and old is not None:
        old = max(1, -(-int(old) // k))
    try:
        await call(settings.set_value, key, val * k)
    except settings.SettingsWriteError as e:
        await state.clear()
        await after_input(message, services, hooks, sec, ui.fail("Не сохранено", str(e)))
        return
    await state.clear()
    await after_input(message, services, hooks, sec, texts.settings_changed(key, old, val))


async def _snap_codes(message, services, title: str, items: list, back: str, shown: int = 12):
    """Итог ввода — больше shown адресов: все снимком; (имя бота, параметр ссылки) для хвоста."""
    if len(items) <= shown:
        return None
    from awgbot.util import listlink
    rows = [f"<code>{texts._e(x)}</code>" for x in items]
    key = await call(services.db.list_snap_keep, message.chat.id, "admin", title, rows, back, False)
    return (getattr(services, "bot_username", "") or "", listlink.build("snap", key=key)) if key else None
