"""Экраны клиента и гостя: главный экран, подписка, пауза, отсрочка, устройства, друзья."""

from __future__ import annotations

import datetime

from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.bot import ui
from awgbot.core.enums import SubStatus, ActivationStatus, FriendStatus

from .fmt import (
    rf_line, deep_link, details, device_state, access_status_line,
    _e, used_of_limit, gb, gb_str, unit_for, amount, volume, client_total_line, device_label,
    client_link, owner_link, holder_link, owner_name, _n_devices, plural_ru,
    _days_word, _days)
from .routing import ROUTING_NAME

# Одна формулировка риска «без VPN не будет Telegram»: блокировка, удаление
# единственного устройства, пауза
TELEGRAM_RISK = "Если Telegram у тебя только через этот VPN — до бота не достучаться"

# payload deep-link'ов клиента и гостя (/start <payload>): экран реестра
SUB_PAYLOAD = "sub"
RF_PAYLOAD_CLIENT = "rf"
DEV_PAYLOAD = "dev"


# ─────────────────────────────────────────────────────────────────────────────
# Срок подписки (блок админской карточки — как есть)
# ─────────────────────────────────────────────────────────────────────────────

def subscription_block(client, *, for_admin: bool = False, show_pause: bool = True) -> str:
    """Блок срока: период + остаток. Учитывает приостановку (самоблок клиента и
    админский блок с паузой). Тихий (silent) админ-блок пользователю не виден —
    для него период/статус как будто ничего не произошло.
    for_admin=True — админ видит всё (включая silent-паузу и temp-бессрочность).
    show_pause=False — скрыть счётчик дней приостановки (друг ей не управляет)."""
    from awgbot.core import blocks
    mask = int(client.block_reason)
    paused = bool(mask & int(blocks.ClientBlock.PAUSED))
    mode = client.pause_mode or ""
    silent_admin = bool(mask & int(blocks.ClientBlock.ADMIN_SILENT)) and \
        not bool(mask & int(blocks.ClientBlock.ADMIN_NOTIFIED))
    pause_visible = paused and (for_admin or mode == "user" or not silent_admin)

    if not client.period_end:
        if paused and mode == "admin_open" and not pause_visible and client.pause_saved_end:
            start = timeutil.parse_iso(client.period_start) if client.period_start else None
            end = timeutil.parse_iso(client.pause_saved_end)
            status = "🟢 активна"
            body = f"Период подписки: {timeutil.fmt_period(start, end)}" if start else \
                   f"Период подписки: до {timeutil.fmt_dt(end)}"
            return f"Статус подписки: {status}\n{body}\nДо истечения: {timeutil.fmt_remaining(end)}"
        status = "🟢 активна" if client.status == SubStatus.ACTIVE else "🔴 истекла"
        if pause_visible and mode == "admin_open":
            status = "⏸️ приостановлено администратором"
            return (f"Статус подписки: {status}\n"
                    "Период подписки: временно бессрочный "
                    "(пересчитается при снятии блокировки)")
        return f"Статус подписки: {status}\nПериод подписки: бессрочно"

    if not client.period_start:
        status = "🟢 активна" if client.status == SubStatus.ACTIVE else "🔴 истекла"
        return f"Статус подписки: {status}\nПериод подписки: дата начала не определена"
    start = timeutil.parse_iso(client.period_start)
    end = timeutil.parse_iso(client.period_end)

    if pause_visible:
        status = ("⏸️ приостановлено пользователем" if mode == "user"
                  else "⏸️ приостановлено администратором")
    else:
        status = "🟢 активна" if client.status == SubStatus.ACTIVE else "🔴 истекла"

    lines = [f"Статус подписки: {status}"]
    period_line = f"Период подписки: {timeutil.fmt_period(start, end)}"
    if pause_visible and mode == "user":
        period_line += f" (+ до {int(client.pause_reserved_days)} дней приостановки)"
    elif pause_visible and mode == "admin_fixed":
        period_line += " (пересчитается при снятии блокировки)"
    lines.append(period_line)
    if not pause_visible:
        lines.append(f"До истечения: {timeutil.fmt_remaining(end)}")
    if show_pause and end:
        bal = int(client.pause_balance_days)
        kind = str(client.period_kind or "")
        if kind == "year":
            of = f"/{2 * settings.get_int('pause.pause_max_total_days', 28)}"
        elif kind == "month":
            of = f"/{12 * settings.get_int('pause.monthly_pause_days', 2)}"
        else:
            of = ""
        word = "дней" if of else _days_word(bal)
        lines.append(f"Приостановка: доступно {bal}{of} {word}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Общие кусочки экранов клиента и гостя
# ─────────────────────────────────────────────────────────────────────────────

def _link(bot_username: str, payload: str, label: str) -> str:
    return deep_link(bot_username, payload, label) if bot_username else label


def vpn_status_line(server_ok: bool) -> str:
    return "🟢 VPN работает" if server_ok else "🔴 VPN не отвечает"


def rf_status_short(routing_ok, routing_on: bool, bot_username: str = "") -> str:
    """«🇷🇺 РФ-доступ 🟢» / «… 🔴 не работает» / «… выкл» — простым текстом.
    routing_ok=None — функция профилю не выдана: пусто."""
    if routing_ok is None:
        return ""
    label = f"🇷🇺 {ROUTING_NAME}"
    if not routing_on:
        return f"{label} выкл"
    return f"{label} 🟢" if routing_ok else f"{label} 🔴 не работает"


def status_line(server_ok: bool, routing_ok, routing_on: bool, bot_username: str = "") -> str:
    """Вторая строка главной: VPN и, если выдан, РФ-доступ."""
    rf = rf_status_short(routing_ok, routing_on, bot_username)
    return vpn_status_line(server_ok) + (f" · {rf}" if rf else "")


def _pause_until(client) -> str:
    """Дата авто-снятия паузы пользователя: с начала паузы + зарезервированные дни."""
    if not client.pause_active_since:
        return ""
    until = (timeutil.parse_iso(client.pause_active_since)
             + datetime.timedelta(days=int(client.pause_reserved_days)))
    return timeutil.fmt_date_ui(until)


def subscription_short(client, bot_username: str = "") -> str:
    """Строка подписки на главной клиента — вся ссылкой на «💳 Подписка»:
    «💳 Подписка до 12.10», «💳 🟡 истекает 12.10 18:00», «💳 ⏸️ на паузе до
    01.10», «💳 ⏸️ приостановлена администратором», «💳 🔴 истекла»,
    «💳 бессрочная». Год — только не текущий."""
    _, mode, pause_visible = _pause_visibility(client)
    if pause_visible:
        if mode == "user":
            until = _pause_until(client)
            text = f"💳 ⏸️ на паузе до {until}" if until else "💳 ⏸️ на паузе"
        else:
            text = "💳 ⏸️ приостановлена администратором"
    elif client.status != SubStatus.ACTIVE or (
            client.period_end and timeutil.remaining_seconds(timeutil.parse_iso(client.period_end)) <= 0):
        end = timeutil.parse_iso(client.period_end) if client.period_end else None
        text = f"💳 🔴 истекла {timeutil.fmt_date_ui(end)}" if end else "💳 🔴 истекла"
    elif not client.period_end:
        text = "💳 бессрочная"
    else:
        end = timeutil.parse_iso(client.period_end)
        if client.notified_thresholds:
            text = f"💳 🟡 истекает {timeutil.fmt_end_ui(end)}"
        else:
            text = f"💳 Подписка до {timeutil.fmt_date_ui(end)}"
    return _link(bot_username, SUB_PAYLOAD, text)


def traffic_short(rx: int, tx: int, limit_bytes: int, bonus_bytes: int = 0) -> str:
    """«📊 12.3 из 100 ГБ», с бонусом «📊 12.3 из 100(+50) ГБ», от терабайта —
    в ТБ; без лимита — «📊 12.3 ГБ (безлимит)», и ноль тоже: «📊 0 ГБ (безлимит)»."""
    total = int(rx or 0) + int(tx or 0)
    if limit_bytes:
        unit, ub = unit_for(total, limit_bytes, bonus_bytes)
        bonus = f"(+{amount(bonus_bytes, ub)})" if bonus_bytes else ""
        return f"📊 {amount(total, ub)} из {amount(limit_bytes, ub)}{bonus} {unit}"
    return f"📊 {volume(total)} (безлимит)"


def held_devices_tail(held) -> str:
    """Хвост «(+1 от профиля Вася)» к счётчику устройств клиента, который
    держит чужие; пусто — не держит."""
    if not held:
        return ""
    return f" (+{len(held)} от профиля {owner_link(held[0])})"


def devices_short(slots, held=()) -> str:
    """Строка устройств на главной: «📱 Устройств 2 из 3 (+1 от профиля
    Вася)»; без лимита — «📱 Устройств: 2»; пока нет — «📱 Можно добавить до 3
    устройств»."""
    used, limit = slots
    tail = held_devices_tail(held)
    if limit == 0:
        if not used and not tail:
            return "📱 Устройств пока нет"
        return f"📱 Устройств: {used}{tail}"
    if used == 0 and not tail:
        return f"📱 Можно добавить до {limit} {plural_ru(limit, 'устройства', 'устройств', 'устройств')}"
    return f"📱 Устройств {used} из {limit}{tail}"


def limit_exhausted_line(used: int, limit: int) -> str:
    """«Лимит исчерпан: чтобы добавить новое, удали N», N = занято − лимит + 1;
    пусто, пока место есть."""
    if not limit or used < limit:
        return ""
    n = used - limit + 1
    return f"Лимит исчерпан: чтобы добавить новое, удали {n}"


# ─────────────────────────────────────────────────────────────────────────────
# Главная клиента и гостя
# ─────────────────────────────────────────────────────────────────────────────

def greeting_client(client, server_ok: bool, slots: tuple[int, int] = None,
                    routing_ok: bool = None, held=(), traffic: dict | None = None,
                    *, routing_on: bool = False, bot_username: str = "") -> str:
    """Главная клиента: имя, пустая строка, затем VPN и РФ-доступ; подписка и
    трафик; устройства. routing_ok=None — РФ-доступ профилю не выдан, его нет вовсе."""
    lines = [ui.head(f"👋 {_e(client.name)}"), "",
             status_line(server_ok, routing_ok, routing_on, bot_username)]
    paused, _, pause_visible = _pause_visibility(client)
    access = access_status_line(client)
    if access and (pause_visible or not paused):   # тихая пауза админа клиенту не видна
        lines.append(access)
    sub = subscription_short(client, bot_username)
    if traffic is not None:
        tr = traffic_short(traffic["rx_month"], traffic["tx_month"],
                           client.traffic_limit, client.bonus_bytes)
        if tr:
            sub += f" · {tr}"
    lines.append(sub)
    if slots is not None:
        lines.append(devices_short(slots, held))
    return "\n".join(lines)


def device_link(dev, bot_username: str = "") -> str:
    """Имя устройства ссылкой на его карточку (/start dev-<id>)."""
    return _link(bot_username, f"{DEV_PAYLOAD}-{dev.id}", dev.name)


def _guest_traffic_lines(held, donor, bot_username: str = "") -> list[str]:
    """«📊 Планшет: 0.8 из 100 ГБ» по каждому удерживаемому — против своего
    лимита, иначе профиля владельца; имя — ссылка на карточку; без лимитов и
    без трафика — строки нет."""
    rows = []
    for d in held:
        used = int(d.traffic_rx_month) + int(d.traffic_tx_month)
        limit = int(d.traffic_limit) or int(donor.traffic_limit)
        if not limit and not used:
            continue
        rows.append(f"📊 {device_link(d, bot_username)}: {_of_limit(used, limit)}")
    return rows


def greeting_guest(name: str, server_ok: bool, donor, held, routing_ok: bool = None,
                   *, routing_on: bool = False, bot_username: str = "") -> str:
    """Главная гостя: имя; VPN и РФ-доступ; подписка владельца (только статус —
    срок его дело); трафик по устройствам. name — имя из Telegram."""
    held = list(held)
    if donor is None or not held:
        return ui.head(f"👋 {_e(name)}", meta=["устройств нет — попроси у друга новый код"])
    lines = [ui.head(f"👋 {_e(name)}"), "",
             status_line(server_ok, routing_ok, routing_on, bot_username),
             f"💳 Подписка профиля {client_link(donor)}: {subscription_status_only(donor)}"]
    lines += _guest_traffic_lines(held, donor, bot_username)
    return "\n".join(lines)


def devices_header(used: int, limit: int, held=(), *, guest: bool = False) -> str:
    """Заголовок «📱 Устройства»: клиент — «· 2 из 3 (+1 от профиля Вася)» и
    строка «Лимит исчерпан…»; гость — «· 2 · от профиля Вася»."""
    if guest:
        n = len(list(held))
        return ui.head("📱 Устройства", meta=[str(n), f"от профиля {owner_link(held[0])}"]) if n else ui.head("📱 Устройства")
    head = ui.head("📱 Устройства", meta=[f"{used}" + (f" из {limit}" if limit else "") + held_devices_tail(held)])
    tail = limit_exhausted_line(used, limit)
    return head + (f"\n{tail}" if tail else "")


def pick_device_header(kind: str) -> str:
    """«🔗 Ссылка — для какого устройства?» по виду выдачи."""
    label = {"link": ui.head("🔗 Ссылка"), "qr": ui.head("🔳 QR"), "file": ui.head("📄 Файл")}[kind]
    return f"{label} — для какого устройства?"


# ─────────────────────────────────────────────────────────────────────────────
# Карточки устройств у клиента и гостя
# ─────────────────────────────────────────────────────────────────────────────

def _of_limit(used: int, limit_bytes: int, note: str = "") -> str:
    """«3.2 из 50 ГБ (лимит устройства)»; без лимита — «3.2 ГБ (безлимит)»."""
    if not limit_bytes:
        return f"{volume(used)} (безлимит)"
    return used_of_limit(used, limit_bytes, note)


def _usage(dev, profile_limit_bytes: int, whose: str) -> str:
    """«📊 3.2 из 50 ГБ (лимит устройства)» / «📊 3.2 из 100 ГБ (лимит
    профиля)» / «📊 3.2 ГБ (безлимит)»."""
    used = int(dev.traffic_rx_month) + int(dev.traffic_tx_month)
    if dev.traffic_limit:
        return "📊 " + used_of_limit(used, dev.traffic_limit, "лимит устройства")
    return "📊 " + _of_limit(used, profile_limit_bytes, whose)


def _seen(dev) -> str:
    ago = timeutil.fmt_ago(dev.last_handshake)
    return "Не подключался" if ago == "никогда" else f"Был в сети {ago}"


def _blocked_line(dev) -> str:
    """«⛔ Заблокировано: владельцем», «⛔ Заблокировано: подписка истекла»."""
    from awgbot.core import blocks
    reasons = blocks.device_reasons_ru(int(dev.block_reason), for_admin=False)
    return "⛔ Заблокировано: " + ", ".join(reasons) if reasons else ""


def device_card_own(dev, profile_limit_bytes: int) -> str:
    """Карточка своего устройства: «🟢 iPhone» / «Был в сети 2 мин назад ·
    📊 3.2 из 50 ГБ (лимит устройства)» + блокировка, приглашение, пометка
    «добавлено не ботом». Адрес устройства — только у админа."""
    parts = [f"{device_state(dev)} <b>{_e(dev.name)}</b>",
             f"{_seen(dev)} · {_usage(dev, profile_limit_bytes, 'лимит профиля')}"]
    blocked = _blocked_line(dev)
    if blocked:
        parts.append(blocked)
    if dev.friend_status == FriendStatus.PENDING:
        parts.append("⏳ приглашение другу ждёт активации")
    if not dev.is_managed:
        parts.append(UNMANAGED_DEVICE_LINE)
    return "\n".join(parts)


def device_card_lent(dev, profile_limit_bytes: int) -> str:
    """Своё переданное — у владельца: кто управляет, чей лимит."""
    parts = [f"{device_state(dev)} <b>{_e(dev.name)}</b> · управляется профилем {holder_link(dev)}",
             f"{_seen(dev)} · {_usage(dev, profile_limit_bytes, 'лимит твоего профиля')}"]
    blocked = _blocked_line(dev)
    if blocked:
        parts.append(blocked)
    return "\n".join(parts)


def device_card_held(dev, owner_limit_bytes: int) -> str:
    """Удерживаемое (от друга) — у держателя: от кого, чей лимит."""
    parts = [f"{device_state(dev)} <b>{_e(dev.name)}</b> · от профиля {owner_link(dev)}",
             f"{_seen(dev)} · {_usage(dev, owner_limit_bytes, f'лимит профиля {_e(owner_name(dev))}')}"]
    blocked = _blocked_line(dev)
    if blocked:
        parts.append(blocked)
    return "\n".join(parts)


UNMANAGED_DEVICE_LINE = "✳️ Добавлено не ботом — ссылки нет: удали и добавь заново"


# ── удаление, блокировка, передача ───────────────────────────────────────────

def device_delete_ask(dev, *, only: bool = False, lent: bool = False, held: bool = False) -> str:
    """Вопрос удаления: своё / единственное / переданное (у владельца) /
    удерживаемое (у держателя)."""
    name = _e(dev.name)
    if held:
        return (f"🗑 Удалить {name}?\n"
                "Новое устройство можно будет создать только по коду от друга")
    if lent:
        return (f"🗑 Удалить {name}?\n"
                f"У профиля {holder_link(dev)} пропадёт доступ; новое устройство он получит "
                "только с новым приглашением от тебя")
    if only:
        return (f"⚠️ Удалить {name} — единственное устройство?\n"
                "VPN выключится сразу. " + TELEGRAM_RISK)
    return (f"🗑 Удалить {name}?\n"
            "Ссылка перестанет работать; решишь добавить устройство снова — ссылка изменится")


def device_removed(name: str) -> str:
    """«🗑 Ноут удалено» — итог держателя первой строкой его экрана."""
    return f"🗑 {_e(name)} удалено"


def device_deleted(name: str, used: int, limit: int) -> str:
    """«🗑 MacBook удалено · можно добавить ещё 2» — первой строкой экрана."""
    head = f"🗑 {_e(name)} удалено"
    free = limit - used
    if limit == 0 or free <= 0:
        return head
    return f"{head} · можно добавить ещё {free}"


def block_device_ask(name: str) -> str:
    return (f"🛑 Заблокировать {_e(name)}?\n"
            "Перестанет подключаться, пока не разблокируешь. " + TELEGRAM_RISK)


def transfer_ask(name: str) -> str:
    n = _e(name)
    return (f"👤 Передать {n} другу?\n"
            "Друг получит это подключение; одно подключение на двух устройствах "
            "работать не будет\n"
            f"Если устройство «{n}» твоё — сначала заведи себе новое")


# ── добавление устройства ────────────────────────────────────────────────────

def add_device_prompt(used: int, limit: int, *, for_friend: bool) -> str:
    slots = f" · {used} из {limit}" if limit else ""
    if for_friend:
        return (f"{ui.head('👤 Устройство для друга')}{slots}{' · займёт твой слот.' if limit else ''}\n"
                "Как назвать? Имя увидит друг")
    return f"{ui.head('➕ Новое устройство')}{slots}\nКак назвать? Например: «iPhone»"


def device_created(name: str, profile_limit_bytes: int) -> str:
    """«✅ iPhone создано · трафик в пределах 100 ГБ профиля»."""
    head = f"✅ {_e(name)} создано"
    if profile_limit_bytes:
        return f"{head} · трафик в пределах {gb_str(profile_limit_bytes)} профиля"
    return head


def device_limit_prompt(name: str, profile_limit_bytes: int) -> str:
    head = ui.head(f"📊 Лимит трафика устройства «{_e(name)}»")
    if profile_limit_bytes:
        return f"{head} · не больше {gb_str(profile_limit_bytes)} профиля"
    return head


def device_limit_other_prompt(profile_limit_bytes: int) -> str:
    if profile_limit_bytes:
        return f"✏️ Число ГБ, не больше {gb(profile_limit_bytes)} (лимит профиля); 0 — без ограничений в рамках лимита"
    return "✏️ Число ГБ, 0 — без лимита"


def device_limit_over(profile_limit_bytes: int) -> str:
    return f"⚠️ Не больше {gb_str(profile_limit_bytes)} — лимита профиля"


NUMBER_BAD = "⚠️ Нужно целое число ГБ"
NAME_EMPTY = "⚠️ Имя пустое — пришли ещё раз"


def limit_note(old_bytes: int, new_bytes: int, profile_limit_bytes: int = 0) -> str:
    """Итог правки лимита первой строкой карточки: «✅ Лимит: ∞ → 50 ГБ»;
    снят у профиля с лимитом — «✅ Лимит: 50 ГБ → ∞ (не более 100 ГБ — лимит
    профиля)»."""
    def _f(b):
        return gb_str(b) if b else "∞"
    tail = (f" (не более {gb_str(profile_limit_bytes)} — лимит профиля)"
            if not new_bytes and profile_limit_bytes else "")
    return f"✅ Лимит: {_f(old_bytes)} → {_f(new_bytes)}{tail}"


def name_note(old: str, new: str) -> str:
    return f"✅ Имя устройства: {_e(old)} → {_e(new)}"


def device_name_prompt(name: str) -> str:
    return ui.head(f"✏️ Новое имя для устройства «{_e(name)}»")


# ── приглашение другу ────────────────────────────────────────────────────────

def _invite_link(code: str, bot_username: str) -> str:
    return f"https://t.me/{bot_username}?start={code}"


def friend_invite_message(device_name: str, code: str, bot_username: str) -> str:
    """Сообщение для пересылки: ссылка и команда, «в боте» — ссылкой на бота."""
    bot = f'<a href="https://t.me/{bot_username}">в боте</a>' if bot_username else "в боте"
    return (f"Твоё приглашение для устройства «{_e(device_name)}» 👇\n"
            f"{_invite_link(code, bot_username)}\n"
            f"или {bot}: <code>/code {code}</code>")


def friend_invite_plain(device_name: str, code: str, bot_username: str) -> str:
    """То же без разметки — для кнопок «📋 Скопировать» и «📤 Отправить».
    Текст копирования — не длиннее 256 знаков (лимит Telegram), поэтому имя
    устройства режется, а не код в конце."""
    name = device_name if len(device_name) <= 40 else device_name[:39] + "…"
    return (f"Твоё приглашение для устройства «{name}»: "
            f"{_invite_link(code, bot_username)} "
            f"или в TG-боте (@{bot_username}): /code {code}")


def finish_friend_invite(device_name: str) -> str:
    return f"☝️ Отправь приглашение другу — он активирует и получит устройство «{_e(device_name)}»"


# ─────────────────────────────────────────────────────────────────────────────
# Уведомления о переданных устройствах (как были)
# ─────────────────────────────────────────────────────────────────────────────

def lent_device_deleted_by_holder_notice(dev, used: int, limit: int) -> str:
    """Владельцу: держатель удалил переданное устройство."""
    now = (f"Теперь у тебя {used} из {limit} устройств" if limit
           else f"Теперь у тебя {_n_devices(used)}")
    return (f"Устройство «{_e(dev.name)}», ранее переданное "
            f"{holder_link(dev)}, удалено по его запросу.\n{now}.")


def lent_device_deleted_by_admin_notice(dev) -> str:
    return (f"Устройство «{_e(dev.name)}», которым ты управлял, удалено администратором — "
            "доступ по нему больше не работает.")


def lent_device_reassigned_notice(name: str) -> str:
    return (f"Устройство «{_e(name)}», которым ты управлял, перенесено в другой профиль "
            "администратором — доступ по нему у тебя больше не работает.")


def lent_device_deleted_by_owner_notice(dev) -> str:
    return (f"Устройство «{_e(dev.name)}», которым ты управлял, удалено владельцем "
            f"({owner_link(dev)}) — доступ по нему больше не работает.")


def _names_list(names: list) -> str:
    q = [f"«{_e(n)}»" for n in names]
    if len(q) <= 1:
        return "".join(q)
    return ", ".join(q[:-1]) + " и " + q[-1]


def friend_device_added(dev, donor, n_held: int, own_slots: tuple | None = None) -> str:
    head = (f"✅ Устройство «{_e(dev.name)}» от {client_link(donor)} "
            "успешно добавлено.")
    if own_slots is not None:
        used, limit = own_slots
        own = f"{used} из {limit} устройств" if limit else _n_devices(used)
        return head + f"\nТеперь у тебя {own} (+ {n_held} от {client_link(donor)})."
    if n_held > 1:
        return head + f"\nТеперь у тебя {_n_devices(n_held)}."
    return head


def friend_other_donor_refusal(held: list, donor) -> str:
    names = [d.name for d in held]
    one = len(names) == 1
    link = client_link(donor)
    return (f"У тебя уже есть {'устройство' if one else 'устройства'} {_names_list(names)}, "
            f"{'переданное' if one else 'переданные'} {link}.\n"
            "Владеть устройствами от разных друзей одновременно не получится 😔\n"
            f"Ты можешь либо удалить {'устройство' if one else 'все устройства'} от {link} и "
            "отправить мне этот код повторно, либо оставить всё как есть — решать тебе 🤷‍♂️")


def guest_upgraded(donor, moved: list, limit: int) -> str:
    lines = [ACTIVATION_OK, "",
             f"Переданные тебе устройства от профиля {client_link(donor)} "
             "перенесены в твой профиль — перенастраивать ничего не нужно, они работают как раньше:"]
    lines += [f"• {_e(d.name)}" for d in moved]
    if limit and len(moved) > limit:
        lines += ["", f"В твою подписку входит {_n_devices(limit)}, а перенесено {len(moved)} — "
                      "все они продолжают работать. Добавить новое получится, когда освободится "
                      "место в рамках лимита."]
    return "\n".join(lines)


def guest_upgraded_donor_notice(moved: list, holder, used: int, limit: int) -> str:
    names = [d.name for d in moved]
    one = len(names) == 1
    now = f"У тебя теперь {used} из {limit} устройств" if limit else f"У тебя теперь {_n_devices(used)}"
    return (f"📤 {'Устройство' if one else 'Устройства'} {_names_list(names)} "
            f"{'перешло' if one else 'перешли'} к {client_link(holder)} — он активировал "
            f"собственную подписку, и {'устройство переехало' if one else 'устройства переехали'} "
            f"в его профиль. {now}.")


def guest_upgraded_admin_tail(donor, moved: list, limit: int) -> str:
    return (f"\nПеренесено переданных устройств: {len(moved)} (от {_e(donor.name)}), "
            f"лимит подписки {limit if limit else 'без ограничения'}.")


GUEST_NO_DEVICES_LEFT = "Устройств нет — попроси у друга новый код"

FRIEND_ALREADY_USER = (
    "Ты администратор — принимать чужие устройства незачем: все устройства "
    "сервера и так под твоим управлением 🙂"
)


def friend_activated(device_name: str) -> str:
    return f"🎉 Тебе передали устройство «{_e(device_name)}»"


def friend_activated_host_notice(device_name: str, who: str) -> str:
    return f"👤 Друг ({_e(who)}) активировал устройство «{_e(device_name)}»."


# ── пояснения к выдаче (в одном сообщении с содержимым) ──────────────────────

def finish_link(name: str) -> str:
    return (f"☝️ Ссылка для {_e(name)} — нажми на неё, чтобы скопировать, "
            "и вставь в AmneziaVPN")


def finish_qr(name: str) -> str:
    return f"🔳 Для {_e(name)} — в AmneziaVPN «＋» → «Создать из QR-кода», наведи камеру"


def finish_file(name: str) -> str:
    return f"📄 Для {_e(name)} — импортируй файл в AmneziaVPN"


FINISH_CLIENT_INVITE = (
    "☝️ Выше — ссылка-приглашение. Перешли её человеку, чтобы он активировал доступ.\n\n"
    "❗️ После возврата в меню это сообщение исчезнет — повторно сгенерировать его "
    "будет можно из профиля клиента, до момента принятия приглашения. Уже "
    "пересланное сообщение останется рабочим."
)


# ─────────────────────────────────────────────────────────────────────────────
# Карточка клиента (админ)
# ─────────────────────────────────────────────────────────────────────────────

def client_card(client, devices, traffic, online: bool, *, for_admin: bool,
                rf: tuple[int, int] | None = None) -> str:
    head = f"👤 {_e(client.name)}"
    if for_admin and client.activation_status == ActivationStatus.PENDING:
        head += "  ⏳ ждёт активации"
    online_line = "Сейчас: " + ("🟢 онлайн" if online else "🔴 оффлайн")
    sub = subscription_block(client, for_admin=for_admin)
    tr = client_total_line(
        traffic["rx_month"], traffic["tx_month"],
        client.traffic_limit, client.bonus_bytes, for_admin=for_admin)
    if for_admin and rf is not None:
        tr += "\n" + rf_line(*rf)
    lim = client.device_limit
    limit_line = (f"Устройств: {len(devices)} (без ограничения)" if lim == 0
                  else f"Устройств: {len(devices)} из {lim}")
    dev_block = "\n".join("  " + device_label(d, for_admin=for_admin) for d in devices)
    limit_and_devs = f"{limit_line}\n{dev_block}" if dev_block else limit_line
    parts = [f"{head}\n{online_line}", sub, tr, limit_and_devs]
    from awgbot.core import blocks
    reasons = blocks.client_reasons_ru(int(client.block_reason), for_admin=for_admin)
    if reasons:
        parts.insert(1, "⛔ Заблокирован: " + ", ".join(reasons))
    return "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Подписка
# ─────────────────────────────────────────────────────────────────────────────

def _pause_visibility(client, *, for_admin: bool = False) -> tuple:
    from awgbot.core import blocks
    mask = int(client.block_reason)
    paused = bool(mask & int(blocks.ClientBlock.PAUSED))
    mode = client.pause_mode or ""
    silent_admin = bool(mask & int(blocks.ClientBlock.ADMIN_SILENT)) and \
        not bool(mask & int(blocks.ClientBlock.ADMIN_NOTIFIED))
    pause_visible = paused and (for_admin or mode == "user" or not silent_admin)
    return paused, mode, pause_visible


_KIND_LABELS = {"day": "на день", "week": "на неделю", "month": "ежемесячная",
                "year": "годовая", "never": "бессрочная"}


def subscription_kind_label(kind) -> str:
    return _KIND_LABELS.get(str(kind or ""), str(kind or "—"))


def _pause_kind_ru(kind: str) -> str:
    return "годовой" if kind == "year" else "ежемесячной"


def pause_credit_line(pc) -> str:
    if pc is None or pc.kind not in ("year", "month"):
        return ""
    if pc.reason == "expired":
        return ("⏸️ Дни паузы за этот период не начислены: подписка продлена после истечения. "
                f"Доступно {_days(pc.after)}")
    if pc.reason == "grace":
        return ("⏸️ Дни паузы за этот период не начислены: в прошлом периоде использована "
                f"отсрочка. Доступно {_days(pc.after)}")
    if pc.reason == "cap":
        return ("⏸️ Дни паузы не добавлены: достигнуто максимальное количество для "
                f"{_pause_kind_ru(pc.kind)} подписки ({pc.cap})")
    full = (settings.get_int("pause.pause_max_total_days", 28) if pc.kind == "year"
            else settings.get_int("pause.monthly_pause_days", 2))
    partial = pc.added < full
    note = ("" if not partial
            else " (максимум)" if pc.kind == "year"
            else " (максимум для ежемесячной подписки)")
    return f"⏸️ Дней паузы +{pc.added} → {pc.after}{note}"


def pause_rules_details() -> str:
    """Свёрнутое «подробнее» о том, как копятся дни паузы."""
    year_days = settings.get_int("pause.pause_max_total_days", 28)
    month_days = settings.get_int("pause.monthly_pause_days", 2)
    return details(f"+{month_days} дн. паузы за своевременное продление на месяц "
                   f"(до {12 * month_days} дн.),\n+{year_days} дн. за продление на год (до {2 * year_days} дн.)")


def pause_balance_line(client) -> str:
    """«⏸️ Пауза: 14 дн. доступно» — всем, кроме бессрочных."""
    if not client.effective_period_end:
        return ""
    bal = int(client.pause_balance_days)
    return f"⏸️ Пауза: {bal} дн. доступно" if bal else "⏸️ Пауза: 0 дн. доступно"


def _limits_line(client, routing_visible: bool) -> str:
    parts = [f"{gb_str(client.traffic_limit) if client.traffic_limit else '∞ ГБ'} в месяц",
             _n_devices(client.device_limit) if client.device_limit else "∞ устройств"]
    if routing_visible:
        parts.append(f"🇷🇺 {ROUTING_NAME}")
    return "Включено в подписку: " + " · ".join(parts)


def subscription_text(client, *, routing_visible: bool) -> str:
    """Экран «💳 Подписка»: тип и статус, период и остаток, после пустой строки —
    пауза (счёт или текущая), лимиты. «🇷🇺 РФ-доступ» в лимитах — только когда выдан."""
    paused, mode, pause_visible = _pause_visibility(client)
    kind = subscription_kind_label(client.period_kind)
    access = access_status_line(client)
    lines = [access] if access and (pause_visible or not paused) else []
    if pause_visible and mode == "user":
        lines.append(f"💳 <b>Подписка:</b> {kind}")          # «на паузе» — строкой ниже, без повтора
    elif pause_visible:
        lines.append(f"💳 <b>Подписка:</b> {kind} · ⏸️ приостановлена администратором")
    else:
        lines.append(f"💳 <b>Подписка:</b> {kind} · {subscription_status_only(client, expiring=True)}")
    start = timeutil.parse_iso(client.period_start) if client.period_start else None
    end_iso = client.effective_period_end
    if end_iso:
        end = timeutil.parse_iso(end_iso)
        period = "📅 " + (timeutil.fmt_period_ui(start, end) if start else f"до {timeutil.fmt_end_ui(end)}")
        if not pause_visible and client.status == SubStatus.ACTIVE:
            period += f" · ост. {timeutil.remaining_brief(end)}"
        lines.append(period)
        if pause_visible and mode == "user":
            since = timeutil.parse_iso(client.pause_active_since) if client.pause_active_since else None
            reserved = int(client.pause_reserved_days)
            used = timeutil.ceil_days((timeutil.now() - since).total_seconds()) if since else 0
            used = max(0, min(used, reserved))
            until = _pause_until(client)
            lines.append("")
            lines.append(f"⏸️ на паузе с {timeutil.fmt_date_ui(since) if since else '—'}, до {until} · "
                         f"израсходовано {used} из {reserved} дн. — неиспользованный остаток вернётся при досрочном возобновлении")
        elif not pause_visible:
            lines.append("")
            lines.append(pause_balance_line(client))
            if str(client.period_kind or "") in ("year", "month"):
                lines.append(pause_rules_details())
    lines += ["", _limits_line(client, routing_visible)]
    return "\n".join(l for l in lines if l is not None)


def subscription_status_only(client, *, expiring: bool = False) -> str:
    """Только статус подписки: «🟢 активна», «🟡 истекает 12.10 18:00»,
    «🔴 истекла», «⏸️ на паузе» / «⏸️ приостановлена администратором»."""
    _, mode, pause_visible = _pause_visibility(client)
    if pause_visible:
        return "⏸️ на паузе" if mode == "user" else "⏸️ приостановлена администратором"
    if client.status != SubStatus.ACTIVE or (
            client.period_end and timeutil.remaining_seconds(timeutil.parse_iso(client.period_end)) <= 0):
        return "🔴 истекла"                      # и по сроку: статус переключает такт, а срок уже вышел
    if expiring and client.period_end and client.notified_thresholds:
        end = timeutil.parse_iso(client.period_end)
        return f"🟡 истекает {timeutil.fmt_end_ui(end)}"
    return "🟢 активна"


# ─────────────────────────────────────────────────────────────────────────────
# Пауза
# ─────────────────────────────────────────────────────────────────────────────

PAUSE_WARNING_LINE = f"⚠️ На паузе VPN выключен. {TELEGRAM_RISK}, снять паузу будет нечем"


def pause_ask(available_days: int, *, email_resume: bool = False) -> str:
    """Экран паузы: сколько доступно, что происходит, предупреждение — только
    когда аварийного выхода по почте нет."""
    lines = [f"⏸️ <b>Пауза</b> — до {available_days} дн.",
             "Действие подписки приостановится; снимешь раньше — неизрасходованные дни вернутся"]
    if not email_resume:
        lines.append(PAUSE_WARNING_LINE)
    return "\n".join(lines)


def pause_other_prompt(available_days: int) -> str:
    return f"✏️ Дней, 1–{available_days}"


def pause_days_bad(available_days: int) -> str:
    return f"⚠️ Нужно целое число от 1 до {available_days}"


def pause_emergency_code(code: str, address: str) -> str:
    return (f"🆘 Если потеряешь доступ к боту — отправь письмо на <code>{_e(address)}</code> "
            f"с темой <code>{_e(code)}</code>: пауза снимется сама. Код одноразовый. "
            "Сохрани адрес и код в заметках")


def pause_entered_summary(until: str) -> str:
    """Итог входа в паузу — остаётся в чате. until — дата авто-возобновления
    (уже в формате экрана)."""
    return f"⏸️ Подписка на паузе до {until} — снять раньше можно в разделе «💳 Подписка»"


def pause_unavailable() -> str:
    md = settings.get_int("pause.monthly_pause_days", 2)
    return ("Пауза сейчас недоступна: на счету нет дней. Годовая подписка даёт "
            f"{settings.get_int('pause.pause_max_total_days', 28)} дней за период, "
            f"ежемесячная — по {md} {_days_word(md)} за каждое своевременное продление")


def pause_limit_exhausted() -> str:
    return "Дни паузы на счету закончились — пополнится при продлении подписки"


def pause_resumed_self(actual_days: int, new_end) -> str:
    return (f"▶️ Пауза снята · {actual_days} дн. израсходовано · "
            f"подписка до {timeutil.fmt_end_ui(new_end)}")


# ─────────────────────────────────────────────────────────────────────────────
# Активация, помощь, коды
# ─────────────────────────────────────────────────────────────────────────────

HELP_INTRO = ui.head("❓ Помощь") + " — какое устройство?"
ACTIVATION_OK = "🎉 Доступ открыт"
ACTIVATION_OK_HELP = "🎉 Доступ открыт. Какое у тебя устройство?"
ACTIVATION_INVALID = "🤔 Такого кода нет — проверь и пришли ещё раз"
ACTIVATION_ALREADY = "У тебя уже есть доступ"

COLD_START_GREETING = ("👋 Не узнаю тебя. Пришёл по приглашению, а код не подхватился? "
                       "Отправь: <code>/code КОД</code>")

CODE_NO_ARG = "Отправь код после команды: <code>/code КОД</code>"

UNMANAGED_DEVICE_EXPLAIN = "\n\n" + UNMANAGED_DEVICE_LINE

UNMANAGED_DEVICE_DIALOG = (
    "✳️ Добавлено не ботом — ссылки нет: удали и добавь заново"
)


def limit_changed_notice(old: int, new: int) -> str:
    def _fmt(v):
        return "∞" if v == 0 else str(v)
    return f"Лимит устройств изменён: {_fmt(old)} → {_fmt(new)}"


# ── отсрочка ─────────────────────────────────────────────────────────────────

def grace_activated_client(days: int, end) -> str:
    """end — дата окончания уже строкой экрана."""
    return f"🙏 Продлено на {days} дн., до {end} — вычтется из следующего продления"


GRACE_STALE = "Это предложение уже неактуально"


def grace_activated_admin(link: str, days: int) -> str:
    """link — имя профиля ссылкой (texts.profile_link)."""
    return f"🙏 {link}: взята отсрочка на {days} дн."


CONNECT_METHOD_ASK = "Как подключить устройство?"

DELETE_ONLY_DEVICE_WARNING = (
    "⚠️ Удалить единственное устройство?\n"
    "VPN у профиля выключится сразу; если владелец заходит в Telegram только "
    "через этот VPN, до бота он не достучится"
)

DELETE_DEVICE_CONFIRM = (
    "🗑 Удалить {name}?\n"
    "Ссылка перестанет работать; добавить снова — ссылка изменится"
)
