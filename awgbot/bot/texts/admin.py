"""Экраны администратора: панель, списки, создание и продление профилей, привязка устройств."""

from __future__ import annotations

from awgbot.util import timeutil

from .fmt import (
    _e, human_bytes, _updown, gb_str, _limit_devices_str, device_label, plain_ip,
    _fmt_age, device_emoji)
from .migration import migration_panel_line
from .routing import routing_status_line, routing_admin_status_line, ROUTING_NAME


# ─────────────────────────────────────────────────────────────────────────────
# Списки и статусы
# ─────────────────────────────────────────────────────────────────────────────


from .fmt import deep_link as _deep_link


def _traffic_triplet(rx: int, tx: int) -> str:
    return f"{human_bytes(rx + tx)} {_updown(rx, tx)}"


def traffic_profiles_text(rows, bot_username: str = "") -> str:
    """Потребление за месяц по профилям; имя профиля — deep-link на разбивку по
    его устройствам."""
    head = "📊 <b>Потребление трафика за текущий месяц:</b>"
    if not rows:
        return head + _LIST_SEP + "Профилей нет."
    return head + _LIST_SEP + _LIST_SEP.join(
        f"👤 {_deep_link(bot_username, f'traffic-{c.id}', c.name)}: {_traffic_triplet(rx, tx)}"
        for c, rx, tx in rows)


# Списки с эмодзи в начале строк: подряд строки визуально налезают друг на
# друга, а межстрочный интервал Telegram не настраивает. Единственный рычаг —
# пустая строка между записями.
_LIST_SEP = "\n\n"


def online_devices_text(rows) -> str:
    # Шлюз считается вместе со всеми: счёт обязан совпадать с длиной списка
    # под ним. Прежде он вычитался, и цифра в заголовке расходилась с тем,
    # что человек видел собственными глазами.
    head = f"📶 <b>Устройства онлайн ({len(rows)}):</b>"
    if not rows:
        return head + _LIST_SEP + "Сейчас никто не подключён."
    return head + _LIST_SEP + _LIST_SEP.join(
        f"{device_emoji(d)} {_e(d.name)} ({_e(client_name)}) — {plain_ip(d.address)}"
        for d, client_name in rows)


def traffic_devices_text(client_name: str, rows) -> str:
    head = f"📊 <b>Потребление профиля {_e(client_name)} за текущий месяц:</b>"
    if not rows:
        return head + _LIST_SEP + "Устройств нет."
    # та же метка, что в списке устройств у админа: онлайн, блок, «не ботом»
    return head + _LIST_SEP + _LIST_SEP.join(
        f"{device_label(d, for_admin=True)}: {_traffic_triplet(rx, tx)}" for d, rx, tx in rows)


def expiring_text(rows, bot_username: str = "") -> str:
    """Истекающие подписки: остаток, период, «Продлить?» — deep-link в
    стандартный маршрут продления с возвратом сюда."""
    from awgbot.util import timeutil
    head = "⏳ <b>Истекающие подписки:</b>"
    if not rows:
        return head + _LIST_SEP + "Истекающих подписок нет."
    items = []
    for c, secs in rows:
        end = timeutil.parse_iso(c.period_end)
        start = timeutil.parse_iso(c.period_start) if c.period_start else None
        period = (f"{timeutil.fmt_dt(start)} → " if start else "… → ") + timeutil.fmt_dt(end)
        items.append(f"👤 {_e(c.name)} — осталось {timeutil.fmt_remaining(end)}\n"
                     f"Период подписки: {period}\n"
                     f"<b>{_deep_link(bot_username, f'extend-{c.id}', 'Продлить?')}</b>")
    return head + _LIST_SEP + _LIST_SEP.join(items)


_HOSTNAME: str | None = None


def _hostname() -> str:
    """Имя хоста — один syscall за жизнь процесса, а не на каждый рендер панели."""
    global _HOSTNAME
    if _HOSTNAME is None:
        import socket
        _HOSTNAME = socket.gethostname()
    return _HOSTNAME


def rf_traffic_line(rf: dict) -> str:
    """Вторая строка группы потребления: РФ-часть — то, что сервер выпустил
    через шлюзы (концепт «учёт РФ-трафика»). Без ссылки до этапа 2."""
    rx, tx = int(rf.get("rx") or 0), int(rf.get("tx") or 0)
    line = f"└ 🇷🇺 {ROUTING_NAME}: {human_bytes(rx + tx)} {_updown(rx, tx)}"
    if rf.get("error"):
        line += " · ⚠️ учёт не идёт"
    return line


def admin_panel(st: dict, routing_ok: bool = None, migration=None,
                bot_username: str = "", expiring: int = 0, routing_info: dict = None,
                rf: dict = None) -> str:
    """Шапка админ-меню: компактный статус из кэша (ноль docker exec).
    st — из services.server_status_cached(); метрики железа (CPU/RAM/диск хоста)
    бот снимает локально (/proc, statvfs); показываем с возрастом. None-поля — «…»."""
    if st.get("ok") is None:
        dot = "…"
    else:
        dot = "🟢 работает" if st["ok"] else "🔴 не отвечает"
    # Блок «сервер»: статус, аптайм и метрики — каждый своей строкой.
    head = [f"🖥 Сервер: {dot}"]
    if st.get("uptime"):
        head.append(f"⬆️ Аптайм: {st['uptime']}")
    if st.get("cpu") is not None or st.get("ram") is not None or st.get("disk") is not None:
        def _p(v):
            return f"{v:.0f}%" if v is not None else "?"
        metrics = (f"📈 CPU {_p(st.get('cpu'))} · RAM {_p(st.get('ram'))} "
                   f"· Диск {_p(st.get('disk'))}")
        age = _fmt_age(st.get("age_seconds"))
        if age:
            metrics += f" · {age}"
        head.append(metrics)
    elif st.get("age_seconds") is None:
        head.append("📈 Метрики: нет данных (монитор ещё не сделал первый замер)")

    # Группы разделяем пустой строкой: [сервер] / [РФ-шлюз] / [онлайн] / [потребление].
    groups = ["\n".join(head)]
    # Статус РФ-шлюза — отдельной группой сразу после сервера: это второй хост,
    # от которого зависит связь, и узнавать о его состоянии заходом в раздел
    # настроек — на один шаг дольше, чем нужно. None — функция не настроена.
    if routing_info is not None:
        groups.append(routing_admin_status_line(routing_info, bot_username))
    elif routing_ok is not None:
        groups.append(routing_status_line(routing_ok))
    if st.get("online_count") is not None:
        label = _deep_link(bot_username, "online", "📶 Устройств онлайн")
        groups.append(f"{label}: {st['online_count']}")
    if st.get("traffic_rx") is not None:
        rx, tx = int(st["traffic_rx"]), int(st["traffic_tx"])
        # Подпись — deep-link в разбивку по профилям: единственный способ сделать
        # текст кликабельным, кнопка под панелью загромождала бы меню.
        label = _deep_link(bot_username, "traffic", "📊 Потребление за месяц (все)")
        line = f"{label}: {human_bytes(rx + tx)} {_updown(rx, tx)}"
        if rf and rf.get("show"):
            line += "\n" + rf_traffic_line(rf)
        groups.append(line)
    mig = migration_panel_line(migration)
    if mig:
        groups.append(mig)
    if expiring:
        label = _deep_link(bot_username, "expiring", "⏳ Истекающие подписки")
        groups.append(f"<b>{label}: {expiring}</b>")
    host = _hostname()
    title = "🛠 <b>Панель администратора" + (f" ({_e(host)})" if host else "") + "</b>"
    return title + "\n\n" + "\n\n".join(groups)


CLIENT_DELETE_PARTIAL = (
    "⚠️ Профиль «{name}» НЕ удалён.\n\n"
    "Сервер не снял пиры: {devices}.\n\n"
    "Удалить запись, оставив пир живым, нельзя: доступ по нему продолжал бы "
    "работать, а найти его стало бы не по чему. Проверь, отвечает ли awg "
    "(«Статус сервера»), и повтори удаление."
)

def admin_bootstrap_device(address: str) -> str:
    """Сообщение о первом устройстве админа, заведённом ботом самостоятельно."""
    return ("🔑 Завёл тебе первое устройство «Админ» (" + _e(address) + ").\n\n"
            "Пиры, созданные в обход бота, теперь попадают в карантин и поднимают "
            "тревогу — значит взять себе доступ «снаружи» больше нельзя, и первое "
            "устройство бот обязан выдать сам. Ссылка ниже: импортируй её в "
            "AmneziaVPN.\n\n"
            "QR и файл — в меню, карточка устройства.")


def _slots_phrase(count: int, limit: int) -> str:
    """«m/n подключённых устройств», n=∞ при безлимите — та же дробь, что в
    отчёте админа о созданном устройстве. Слова после дроби не склоняем: она
    читается целиком («6/10 подключённых устройств»), и согласование по
    числителю дало бы «1/10 подключённое устройство»."""
    return (f"Теперь у тебя {count}/{_limit_devices_str(limit)} "
            "подключённых устройств.")


def reassign_donor_notice(name: str, count: int, limit: int) -> str:
    return (f"Устройство «{_e(name)}» удалено из твоего профиля администратором.\n"
            + _slots_phrase(count, limit))


def reassign_recipient_notice(name: str, count: int, limit: int, *,
                              recipient_is_admin: bool = False) -> str:
    """recipient_is_admin=True — получатель сам админ (взял бесхозное устройство
    себе): «добавлено ... администратором» звучало бы странно (сам себе).
    Обычному клиенту — как и раньше, с указанием, что сделал админ."""
    tail = "" if recipient_is_admin else " администратором"
    return (f"Устройство «{_e(name)}» добавлено в твой профиль{tail}.\n"
            + _slots_phrase(count, limit))


def reassign_recipient_notice_with_slot(name: str, count: int, limit: int, *,
                                        recipient_is_admin: bool = False) -> str:
    tail = "" if recipient_is_admin else " администратором"
    return (f"Устройство «{_e(name)}» добавлено в твой профиль{tail}, "
            "тебе также добавлен слот.\n"
            + _slots_phrase(count, limit))


def activated_admin_notice(name: str, who: str) -> str:
    return f"🎉 Профиль «{_e(name)}» активировал доступ ({_e(who)})."

_PERIOD_WORD = {"day": "на день", "week": "на неделю",
                "month": "на месяц", "year": "на год"}


def client_created_report(name: str, *, device_limit: int, traffic_limit_bytes: int,
                          period_kind, period_end) -> str:
    """Констатирующий результат создания профиля: имя, лимиты, срок подписки.
    Остаётся в чате (не транзиентный invite-контент)."""
    dev = f"до {device_limit} устройств" if device_limit else "количество устройств не ограничено"
    if traffic_limit_bytes:
        traf = f"до {gb_str(traffic_limit_bytes)}"
    else:
        traf = "потребление не ограничено"
    if period_end is None:
        sub = "бессрочная подписка"
    else:
        word = _PERIOD_WORD.get(str(period_kind), "")
        sub = f"подписка {word} до {timeutil.fmt_dt(period_end)}".replace("  ", " ").strip()
    return (f"✅ Профиль «{_e(name)}» создан ({dev}, {traf}), {sub}.\n"
            "Повторный выпуск приглашения возможен из меню клиента до его активации.")

LIMIT_REACHED = "Достигнут лимит устройств."
EXTEND_KEEP_QUESTION = "Сохранить неистраченный остаток ({remainder})?"



TRAFFIC_LIMIT_CLIENT_ASK = (
    "Задай лимит потребления профиля на месяц — это общий потолок по всем его "
    "устройствам.\n\nВведи целое число гигабайт (например 100). "
    "0 — без ограничения.")

def traffic_limit_device_ask(profile_limit_bytes: int) -> str:
    """Приглашение задать лимит устройства. Если у профиля есть свой лимит —
    показываем его («в пределах лимита профиля: N ГБ»); если профиль безлимитный
    (0) — фразу в скобках опускаем целиком."""
    base = ("Задай лимит потребления устройства на месяц.\n\nВведи целое число "
            "гигабайт (например 50). 0 — без ограничения")
    if profile_limit_bytes and int(profile_limit_bytes) > 0:
        return f"{base} (в пределах лимита профиля: {gb_str(profile_limit_bytes)})."
    return f"{base}."

TRAFFIC_LIMIT_BAD = "Нужно целое число гигабайт (0 — без ограничения). Попробуй ещё раз:"
