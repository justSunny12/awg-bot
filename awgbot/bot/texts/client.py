"""Экраны клиента и гостя: главный экран, подписка, пауза, отсрочка, устройства, друзья."""

from __future__ import annotations

import datetime

from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.core.enums import SubStatus, ActivationStatus, FriendStatus

from .fmt import (
    rf_line, deep_link, details, device_state,
    _e, human_bytes, used_of_limit, gb, gb_str, client_total_line, device_label,
    plain_ip, client_link, owner_link, holder_link, owner_name, _n_devices, plural_ru,
    _days_word, _days)
from .routing import ROUTING_NAME

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
    """«🇷🇺 РФ-доступ 🟢» / «… 🔴 не работает» / «… выключен»; имя — ссылка на
    экран РФ-доступа. routing_ok=None — функция профилю не выдана: пусто."""
    if routing_ok is None:
        return ""
    label = _link(bot_username, RF_PAYLOAD_CLIENT, f"🇷🇺 {ROUTING_NAME}")
    if not routing_on:
        return f"{label} выключен"
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
    elif client.status != SubStatus.ACTIVE:
        text = "💳 🔴 истекла"
    elif not client.period_end:
        text = "💳 бессрочная"
    else:
        end = timeutil.parse_iso(client.period_end)
        if client.notified_thresholds:
            text = f"💳 🟡 истекает {timeutil.fmt_dt_ui(end)}"
        else:
            text = f"💳 Подписка до {timeutil.fmt_date_ui(end)}"
    return _link(bot_username, SUB_PAYLOAD, text)


def traffic_short(rx: int, tx: int, limit_bytes: int, bonus_bytes: int = 0) -> str:
    """«📊 12.3 из 100 ГБ», с бонусом «📊 12.3 из 100(+50) ГБ», без лимита —
    «📊 12.3 ГБ»; ноль без лимита — пусто (нули не выводим)."""
    total = int(rx or 0) + int(tx or 0)
    if limit_bytes:
        bonus = f"(+{gb(bonus_bytes)})" if bonus_bytes else ""
        return f"📊 {gb(total)} из {gb(limit_bytes)}{bonus} ГБ"
    return f"📊 {human_bytes(total)}" if total else ""


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
    """Главная клиента — четыре строки: имя; VPN и РФ-доступ; подписка и трафик;
    устройства. routing_ok=None — РФ-доступ профилю не выдан, его нет вовсе."""
    lines = [f"👋 {_e(client.name)}",
             status_line(server_ok, routing_ok, routing_on, bot_username)]
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
        rows.append(f"📊 {device_link(d, bot_username)}: {used_of_limit(used, limit)}")
    return rows


def greeting_guest(name: str, server_ok: bool, donor, held, routing_ok: bool = None,
                   *, routing_on: bool = False, bot_username: str = "") -> str:
    """Главная гостя: имя; VPN и РФ-доступ; подписка владельца (только статус —
    срок его дело); трафик по устройствам. name — имя из Telegram."""
    held = list(held)
    if donor is None or not held:
        return f"👋 {_e(name)} · устройств нет — попроси у друга новый код"
    lines = [f"👋 {_e(name)}",
             status_line(server_ok, routing_ok, routing_on, bot_username),
             f"💳 Подписка профиля {client_link(donor)}: {subscription_status_only(donor)}"]
    lines += _guest_traffic_lines(held, donor, bot_username)
    return "\n".join(lines)


def devices_header(used: int, limit: int, held=(), *, guest: bool = False) -> str:
    """Заголовок «📱 Устройства»: клиент — «· 2 из 3 (+1 от профиля Вася)» и
    строка «Лимит исчерпан…»; гость — «· 2 · от профиля Вася»."""
    if guest:
        n = len(list(held))
        return f"📱 Устройства · {n} · от профиля {owner_link(held[0])}" if n else "📱 Устройства"
    head = f"📱 Устройства · {used}" + (f" из {limit}" if limit else "") + held_devices_tail(held)
    tail = limit_exhausted_line(used, limit)
    return head + (f"\n{tail}" if tail else "")


def pick_device_header(kind: str) -> str:
    """«🔗 Ссылка — для какого устройства?» по виду выдачи."""
    label = {"link": "🔗 Ссылка", "qr": "🔳 QR", "file": "📄 Файл"}[kind]
    return f"{label} — для какого устройства?"


# ─────────────────────────────────────────────────────────────────────────────
# Карточки устройств у клиента и гостя
# ─────────────────────────────────────────────────────────────────────────────

def _usage(dev, profile_limit_bytes: int, whose: str) -> str:
    """«3.2 из 50 ГБ (лимит устройства)» / «3.2 из 100 ГБ (лимит профиля)» /
    «3.2 ГБ»."""
    used = int(dev.traffic_rx_month) + int(dev.traffic_tx_month)
    if dev.traffic_limit:
        return used_of_limit(used, dev.traffic_limit, "лимит устройства")
    if profile_limit_bytes:
        return used_of_limit(used, profile_limit_bytes, whose)
    return human_bytes(used)


def _seen(dev) -> str:
    ago = timeutil.fmt_ago(dev.last_handshake)
    return "Не подключалось" if ago == "никогда" else f"Был в сети {ago}"


def _blocked_line(dev) -> str:
    """«⛔ Заблокировано владельцем», «⛔ Подписка истекла» — причина без
    повтора слова «заблокировано»."""
    from awgbot.core import blocks
    reasons = blocks.device_reasons_ru(int(dev.block_reason), for_admin=False)
    if not reasons:
        return ""
    head = reasons[0][:1].upper() + reasons[0][1:]
    return "⛔ " + ", ".join([head] + reasons[1:])


def device_card_own(dev, profile_limit_bytes: int) -> str:
    """Карточка своего устройства: «🟢 iPhone · 10.8.1.5» / «Был в сети 2 мин
    назад · 3.2 из 50 ГБ (лимит устройства)» + блокировка, приглашение,
    пометка «добавлено не ботом»."""
    parts = [f"{device_state(dev)} {_e(dev.name)} · {plain_ip(dev.address)}",
             f"{_seen(dev)} · {_usage(dev, profile_limit_bytes, 'лимит профиля')}"]
    blocked = _blocked_line(dev)
    if blocked:
        parts.append(blocked)
    if dev.friend_status == FriendStatus.PENDING:
        parts.append("⏳ Приглашение другу ждёт активации")
    if not dev.is_managed:
        parts.append(UNMANAGED_DEVICE_LINE)
    return "\n".join(parts)


def device_card_lent(dev, profile_limit_bytes: int) -> str:
    """Своё переданное — у владельца: кто управляет, чей лимит."""
    parts = [f"{device_state(dev)} {_e(dev.name)} · {plain_ip(dev.address)} · "
             f"управляется профилем {holder_link(dev)}",
             f"{_seen(dev)} · {_usage(dev, profile_limit_bytes, 'лимит твоего профиля')}"]
    blocked = _blocked_line(dev)
    if blocked:
        parts.append(blocked)
    return "\n".join(parts)


def device_card_held(dev, owner_limit_bytes: int) -> str:
    """Удерживаемое (от друга) — у держателя: от кого, чей лимит."""
    parts = [f"{device_state(dev)} {_e(dev.name)} · {plain_ip(dev.address)} · "
             f"от профиля {owner_link(dev)}",
             f"{_seen(dev)} · {_usage(dev, owner_limit_bytes, f'лимит профиля {_e(owner_name(dev))}')}"]
    blocked = _blocked_line(dev)
    if blocked:
        parts.append(blocked)
    return "\n".join(parts)


def held_device_card(dev, owner_limit_bytes: int) -> str:
    return device_card_held(dev, owner_limit_bytes)


def lent_out_marker(dev) -> str:
    """Строка в карточке владельца (админ): кому передано."""
    return f"👤 Передано {holder_link(dev)} и управляется им"


def friend_marker(dev) -> str:
    if dev.is_lent:
        return f"👤 Передано {holder_link(dev)}"
    if dev.friend_status == FriendStatus.PENDING:
        return "⏳ Приглашение другу ждёт активации"
    return ""


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
                "VPN выключится сразу; если Telegram у тебя только через этот VPN, "
                "до бота не достучаться")
    return (f"🗑 Удалить {name}?\n"
            "Ссылка перестанет работать; решишь добавить устройство снова — ссылка изменится")


def device_delete_by_holder_ask(name: str) -> str:
    return (f"🗑 Удалить {_e(name)}?\n"
            "Новое устройство можно будет создать только по коду от друга")


def device_delete_by_owner_ask(dev) -> str:
    return device_delete_ask(dev, lent=True)


def device_deleted(name: str, used: int, limit: int) -> str:
    """«🗑 MacBook удалено · можно добавить ещё 2» — остаётся в чате."""
    head = f"🗑 {_e(name)} удалено"
    free = limit - used
    if limit == 0 or free <= 0:
        return head
    return f"{head} · можно добавить ещё {free}"


def block_device_ask(name: str) -> str:
    return (f"🛑 Заблокировать {_e(name)}?\n"
            "Перестанет подключаться, пока не разблокируешь. Если Telegram у тебя "
            "через это устройство и VPN — бот станет недоступен")


def transfer_ask(name: str) -> str:
    n = _e(name)
    return (f"👤 Передать {n} другу?\n"
            "Друг получит это подключение; одно подключение на двух устройствах "
            "работать не будет.\n"
            f"Если устройство {n} твоё — сначала заведи себе новое")


# ── добавление устройства ────────────────────────────────────────────────────

def add_device_prompt(used: int, limit: int, *, for_friend: bool) -> str:
    slots = f" · {used} из {limit}" if limit else ""
    if for_friend:
        return (f"👤 Устройство для друга{slots}{' · займёт твой слот.' if limit else ''}\n"
                "Как назвать? Имя увидит друг")
    return f"➕ Новое устройство{slots}\nКак назвать? Например: «iPhone»"


def device_created(name: str, profile_limit_bytes: int) -> str:
    """«✅ iPhone: создано · трафик в пределах 100 ГБ профиля»."""
    head = f"✅ {_e(name)}: создано"
    if profile_limit_bytes:
        return f"{head} · трафик в пределах {gb_str(profile_limit_bytes)} профиля"
    return head


def device_limit_prompt(name: str, profile_limit_bytes: int) -> str:
    head = f"📊 Лимит трафика устройства {_e(name)}"
    if profile_limit_bytes:
        return f"{head} · не больше {gb_str(profile_limit_bytes)} профиля"
    return head


def device_limit_other_prompt(profile_limit_bytes: int) -> str:
    if profile_limit_bytes:
        return f"✏️ Число ГБ, не больше {gb(profile_limit_bytes)} — лимита профиля; 0 — по лимиту профиля"
    return "✏️ Число ГБ, 0 — без лимита"


def device_limit_over(profile_limit_bytes: int) -> str:
    return f"⚠️ Не больше {gb_str(profile_limit_bytes)} — лимита профиля"


NUMBER_BAD = "⚠️ Нужно целое число ГБ"
NAME_EMPTY = "⚠️ Имя пустое — пришли ещё раз"


def limit_note(old_bytes: int, new_bytes: int, profile_limit_bytes: int = 0) -> str:
    """Итог правки лимита первой строкой карточки: «✅ Лимит: ∞ → 50 ГБ»; без
    своего лимита у профиля с лимитом — «по лимиту профиля»."""
    def _f(b):
        if b:
            return gb_str(b)
        return "по лимиту профиля" if profile_limit_bytes else "∞"
    return f"✅ Лимит: {_f(old_bytes)} → {_f(new_bytes)}"


def name_note(old: str, new: str) -> str:
    return f"✅ Имя устройства: {_e(old)} → {_e(new)}"


def device_name_prompt(name: str) -> str:
    return f"✏️ Новое имя для устройства «{_e(name)}»"


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
    return f"☝️ Отправь приглашение другу — он активирует и получит {_e(device_name)}"


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


def finish_config(kind: str, name: str) -> str:
    return {"link": finish_link, "qr": finish_qr, "file": finish_file}[kind](name)


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
                f"Доступно {_days(pc.after)}.")
    if pc.reason == "grace":
        return ("⏸️ Дни паузы за этот период не начислены: в прошлом периоде использована "
                f"отсрочка. Доступно {_days(pc.after)}.")
    if pc.reason == "cap":
        return ("⏸️ Дни паузы не добавлены: достигнуто максимальное количество для "
                f"{_pause_kind_ru(pc.kind)} подписки ({pc.cap}).")
    full = (settings.get_int("pause.pause_max_total_days", 28) if pc.kind == "year"
            else settings.get_int("pause.monthly_pause_days", 2))
    partial = pc.added < full
    note = ("" if not partial
            else " (максимум)" if pc.kind == "year"
            else " (максимум для ежемесячной подписки)")
    return f"⏸️ Дней паузы добавлено: +{pc.added}, доступно {pc.after}{note}."


def pause_credit_admin(pc) -> str:
    if pc is None or pc.kind not in ("year", "month"):
        return ""
    if pc.reason == "expired":
        return f"Дней паузы: не начислены — после истечения, доступно {pc.after}"
    if pc.reason == "grace":
        return f"Дней паузы: не начислены — отсрочка, доступно {pc.after}"
    if pc.reason == "cap":
        return f"Дней паузы: не добавлены — максимум {_pause_kind_ru(pc.kind)} ({pc.cap})"
    return f"Дней паузы: +{pc.added} → {pc.after}" + (" (максимум)" if pc.after == pc.cap else "")


def pause_rules_details() -> str:
    """Свёрнутое «подробнее» о том, как копятся дни паузы."""
    year_days = settings.get_int("pause.pause_max_total_days", 28)
    month_days = settings.get_int("pause.monthly_pause_days", 2)
    return details(f"+{month_days} дн. паузы за своевременное продление на месяц "
                   f"(до {12 * month_days}), +{year_days} за год (до {2 * year_days})")


def pause_balance_line(client) -> str:
    """«⏸️ Пауза: 14 дн. доступно» — всем, кроме бессрочных."""
    if not client.effective_period_end:
        return ""
    bal = int(client.pause_balance_days)
    return f"⏸️ Пауза: {bal} дн. доступно" if bal else "⏸️ Пауза: дней нет"


def _limits_line(client, routing_visible: bool) -> str:
    parts = [f"{gb_str(client.traffic_limit)} в месяц" if client.traffic_limit else "трафик без лимита",
             _n_devices(client.device_limit) if client.device_limit else "устройства без лимита"]
    if routing_visible:
        parts.append(f"🇷🇺 {ROUTING_NAME}")
    return "Лимиты: " + " · ".join(parts)


def subscription_text(client, *, routing_visible: bool) -> str:
    """Экран «💳 Подписка»: тип и статус, период и остаток, пауза (счёт или
    текущая), лимиты. «🇷🇺 РФ-доступ» в лимитах — только когда выдан."""
    paused, mode, pause_visible = _pause_visibility(client)
    kind = subscription_kind_label(client.period_kind)
    if pause_visible:
        status = "⏸️ на паузе" if mode == "user" else "⏸️ приостановлена администратором"
    else:
        status = subscription_status_only(client, expiring=True)
    lines = [f"💳 Подписка: {kind} · {status}"]
    start = timeutil.parse_iso(client.period_start) if client.period_start else None
    end_iso = client.effective_period_end
    if end_iso:
        end = timeutil.parse_iso(end_iso)
        period = timeutil.fmt_period_ui(start, end) if start else f"до {timeutil.fmt_dt_ui(end)}"
        if not pause_visible:
            period += f" · ост. {timeutil.remaining_brief(end)}"
        lines.append(period)
        if pause_visible and mode == "user":
            since = timeutil.parse_iso(client.pause_active_since) if client.pause_active_since else None
            reserved = int(client.pause_reserved_days)
            used = timeutil.ceil_days((timeutil.now() - since).total_seconds()) if since else 0
            used = max(0, min(used, reserved))
            until = _pause_until(client)
            lines.append(f"⏸️ на паузе с {timeutil.fmt_date_ui(since) if since else '—'}, до {until} · "
                         f"израсходовано {used} из {reserved} дн. — снимешь сейчас, остальные вернутся")
        elif not pause_visible:
            lines.append(pause_balance_line(client))
            if str(client.period_kind or "") in ("year", "month"):
                lines.append(pause_rules_details())
    lines += ["", _limits_line(client, routing_visible)]
    return "\n".join(l for l in lines if l is not None)


def subscription_manage_text(client, *, routing_visible: bool) -> str:
    return subscription_text(client, routing_visible=routing_visible)


def subscription_status_only(client, *, expiring: bool = False) -> str:
    """Только статус подписки: «🟢 активна», «🟡 истекает 12.10 18:00»,
    «🔴 истекла», «⏸️ на паузе» / «⏸️ приостановлена администратором»."""
    _, mode, pause_visible = _pause_visibility(client)
    if pause_visible:
        return "⏸️ на паузе" if mode == "user" else "⏸️ приостановлена администратором"
    if client.status != SubStatus.ACTIVE:
        return "🔴 истекла"
    if expiring and client.period_end and client.notified_thresholds:
        end = timeutil.parse_iso(client.period_end)
        return f"🟡 истекает {timeutil.fmt_dt_ui(end)}"
    return "🟢 активна"


def server_status_client(ok: bool) -> str:
    return vpn_status_line(ok)


# ─────────────────────────────────────────────────────────────────────────────
# Пауза
# ─────────────────────────────────────────────────────────────────────────────

PAUSE_WARNING_LINE = ("⚠️ На паузе VPN выключен. Снять её можно только здесь — если Telegram "
                      "у тебя только через этот VPN, снять будет нечем")


def pause_ask(available_days: int, *, email_resume: bool = False) -> str:
    """Экран паузы: сколько доступно, что происходит, предупреждение — только
    когда аварийного выхода по почте нет."""
    lines = [f"⏸️ Пауза — до {available_days} дн.",
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
    return f"⏸️ Подписка на паузе до {until} — снять раньше можно в «💳 Подписка»"


def pause_unavailable() -> str:
    md = settings.get_int("pause.monthly_pause_days", 2)
    return ("Пауза сейчас недоступна: на счету нет дней. Годовая подписка даёт "
            f"{settings.get_int('pause.pause_max_total_days', 28)} дней за период, "
            f"ежемесячная — по {md} {_days_word(md)} за каждое своевременное продление")


def pause_limit_exhausted() -> str:
    return "Дни паузы на счету закончились — пополнится при продлении подписки"


def pause_resumed_self(actual_days: int, new_end) -> str:
    return (f"▶️ Пауза снята · {actual_days} дн. израсходовано · "
            f"подписка до {timeutil.fmt_dt_ui(new_end)}")


# ─────────────────────────────────────────────────────────────────────────────
# Активация, помощь, коды
# ─────────────────────────────────────────────────────────────────────────────

HELP_INTRO = "❓ Помощь — какое устройство?"
ACTIVATION_OK = "🎉 Доступ открыт"
ACTIVATION_OK_HELP = "🎉 Доступ открыт. Какое у тебя устройство?"
ACTIVATION_INVALID = "🤔 Такого кода нет — проверь и пришли ещё раз"
ACTIVATION_ALREADY = "У тебя уже есть доступ"

COLD_START_GREETING = ("👋 Не узнаю тебя. Пришёл по приглашению, а код не подхватился? "
                       "Отправь: <code>/code КОД</code>")

CODE_NO_ARG = "Отправь код после команды: <code>/code КОД</code>"

INVITE_FORWARD_TEMPLATE = (
    "Привет! Тебе открыт доступ 😊\n"
    "Жми ссылку и «Старт» — дальше подскажу\n"
    "{link}"
)

UNMANAGED_DEVICE_EXPLAIN = "\n\n" + UNMANAGED_DEVICE_LINE

UNMANAGED_DEVICE_DIALOG = (
    "Это устройство добавлял не бот — ссылки для него нет и взять её неоткуда.\n"
    "Удали его и добавь новое через бота"
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


def grace_activated_admin(name: str, days: int) -> str:
    return f"🙏 Профиль «{_e(name)}» активировал отсрочку на {days} дн."


# ── совместимость: строки, которыми ещё пользуются экраны админа ─────────────
# (карточка устройства и «Мои устройства» админа переделываются следующим
# этапом; до него они зовут эти имена)

def device_slots_line(used: int, limit: int) -> str:
    if limit == 0:
        return f"Устройств: {used}"
    line = f"Устройств {used} из {limit}"
    tail = limit_exhausted_line(used, limit)
    return f"{line}\n{tail}" if tail else line


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
