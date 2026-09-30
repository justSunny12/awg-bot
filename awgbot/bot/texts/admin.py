"""Экраны администратора: панель, списки, трафик списком, карточки профиля и устройства, создание и продление профилей, привязка устройств."""

from __future__ import annotations

import datetime

from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.core.enums import ActivationStatus, SubStatus

from .fmt import (
    _e, human_bytes, gb, gb_str, _limit_devices_str, device_state, plain_ip,
    rf_line, rf_value, updown_brief, tree, sub_line, profile_link, admin_device_link, access_status_line,
    holder_link, _n_devices, plural_ru, _BYTES_PER_GB)
from .fmt import deep_link as _deep_link
from .routing import routing_status_line, routing_admin_status_line, ROUTING_NAME
from .updates import release_url


RF_PAYLOAD = "traffic_local"     # /start traffic_local[-<id>] — прежние ссылки на экраны РФ
TRAFFIC_PAYLOAD = "traffic"      # /start traffic[-<id>] — экраны трафика
UPD_PAYLOAD = "upd"              # /start upd — раздел обновлений
UNASSIGNED_PAYLOAD = "unassigned"
ONLINE_PAYLOAD = "online"
EXPIRING_PAYLOAD = "expiring"


def month_label() -> str:
    """«09.26» — текущий месяц в заголовках трафика (год — всегда)."""
    return timeutil.now().strftime("%m.%y")


def _total(rx: int, tx: int) -> str:
    """«123.4 ГБ (↑12.1 ↓111.3)»; ноль — без стрелок."""
    total = int(rx) + int(tx)
    return f"{human_bytes(total)} {updown_brief(rx, tx)}" if total else human_bytes(0)


# ─────────────────────────────────────────────────────────────────────────────
# Главная
# ─────────────────────────────────────────────────────────────────────────────

_HOSTNAME: str | None = None


def _hostname() -> str:
    """Имя хоста — один syscall за жизнь процесса, а не на каждый рендер панели."""
    global _HOSTNAME
    if _HOSTNAME is None:
        import socket
        _HOSTNAME = socket.gethostname()
    return _HOSTNAME


def rf_traffic_line(rf: dict, bot_username: str = "") -> str:
    """Вложенная строка РФ-части под трафиком на главной — простым текстом:
    на экран «Трафик» ведёт строка общего трафика над ней; сбой учёта —
    хвостом."""
    rx, tx = int(rf.get("rx") or 0), int(rf.get("tx") or 0)
    line = rf_line(rx, tx, arrows=False)
    if rf.get("error"):
        line += " · ⚠️ учёт трафика РФ-доступа не идёт"
    return line


def admin_panel(st: dict, routing_ok: bool = None, migration=None,
                bot_username: str = "", expiring: int = 0, routing_info: dict = None,
                rf: dict = None, unassigned: int = 0, update_tag: str = "") -> str:
    """Шапка главной: сервер и аптайм одной строкой, метрики, РФ-доступ, счётчики
    ссылками, трафик с РФ-веткой, доступное обновление и переезд — только когда
    есть что сказать."""
    if st.get("ok") is None:
        dot = "…"
    elif st["ok"]:
        dot = "🟢 работает" + (f" {timeutil.brief_units(st['uptime'])}" if st.get("uptime") else "")
    else:
        dot = "🔴 не отвечает"
    host = _e(_hostname() or "AWG")
    lines = [f"🛠 <b>{host}</b> · {dot}"]
    if st.get("cpu") is not None or st.get("ram") is not None or st.get("disk") is not None:
        def _p(v):
            return f"{v:.0f}%" if v is not None else "?"
        metrics = f"📈 CPU {_p(st.get('cpu'))} · RAM {_p(st.get('ram'))} · диск {_p(st.get('disk'))}"
        age = timeutil.age_ago(st.get("age_seconds"))
        if age:
            metrics += f" · {age}"
        lines.append(metrics)
    elif st.get("age_seconds") is None:
        lines.append("📈 Метрики: ещё нет замера")
    if routing_info is not None:
        rt_line = routing_admin_status_line(routing_info, bot_username)
        if rt_line:                                   # «выключен» без шлюзов — строки нет
            lines.append(rt_line)
    elif routing_ok is not None:
        lines.append(routing_status_line(routing_ok))

    def _counter(payload: str, label: str, n: int) -> str:
        # нулевой счётчик — без ссылки: за ней пустой экран
        return _deep_link(bot_username, payload, label) if n else label
    counters = []
    if st.get("online_count") is not None:
        counters.append(_counter(ONLINE_PAYLOAD, f"📶 Онлайн: {st['online_count']}", int(st["online_count"])))
    if expiring:
        counters.append(_counter(EXPIRING_PAYLOAD, f"⏳ Истекают: {expiring}", expiring))
    if unassigned:
        counters.append(_counter(UNASSIGNED_PAYLOAD, f"📦 Без профиля: {unassigned}", unassigned))
    if counters:
        lines.append(" · ".join(counters))
    # строка трафика стоит и при нуле, но ссылкой — только когда экрану есть
    # что показать; РФ-строка при включённой функции тоже: нулём видно, что
    # маркировка не работает
    if st.get("traffic_rx") is not None:
        rx, tx = int(st["traffic_rx"]), int(st["traffic_tx"])
        label = _counter(TRAFFIC_PAYLOAD, f"📊 Трафик за {month_label()}", rx + tx)
        lines.append(f"{label}: {human_bytes(rx + tx)}")
        if rf and rf.get("show"):
            lines.append(rf_traffic_line(rf, bot_username))
    tail = []
    if update_tag:
        tag = update_tag if str(update_tag).startswith("v") else f"v{update_tag}"
        tail.append("<b>" + _deep_link(bot_username, UPD_PAYLOAD, f"⬆️ Доступна {tag}") + "</b> — "
                    + f'<a href="{release_url(tag)}">список изменений</a>')
    mig = migration_line(migration, bot_username)
    if mig:
        tail.append(mig)
    if tail:                                          # временные строки — отдельным блоком
        lines.append("")
        lines.extend(tail)
    return "\n".join(lines)


MIGRATION_PAYLOAD = "migration"   # /start migration[-<id>] — обзор переезда и профиль в нём


def migration_line(p, bot_username: str = "") -> str:
    """«🚚 Переезд: 11/12 профилей, 18/20 устройств» — пока идёт переезд;
    «🚚 Переезд» — ссылка на обзор."""
    if p is None or getattr(p, "clients_total", 0) == 0:
        return ""
    label = _deep_link(bot_username, MIGRATION_PAYLOAD, "🚚 Переезд")
    return (f"{label}: {p.clients_done}/{p.clients_total} профилей, "
            f"{p.devices_done}/{p.devices_total} устройств")


def _mig_dot(done: int, total: int) -> str:
    return "🟢" if total and done >= total else "🔴"


def migration_overview_text(d: dict, bot_username: str = "") -> str:
    """Обзор переезда: параметры одной строкой (только то, что меняется), затем
    профили — не переехавшие целиком сверху, по убыванию оставшегося; имя не
    переехавшего — ссылка на его экран переезда."""
    params = []
    for key, label in (("port", "порт"), ("subnet", "подсеть"), ("generation", "awg")):
        old, new = d.get(key) or ("", "")
        if old and new and str(old) != str(new):
            fmt_ = ((lambda v: f"gen{v}") if key == "generation"
                    else (lambda v: f"<code>{_e(str(v))}</code>") if key == "subnet" else str)
            params.append(f"{label}: {fmt_(old)} → {fmt_(new)}")
    head = "🚚 <b>Переезд</b>" + (f" ({', '.join(params)})" if params else "")
    rows = sorted(d.get("rows") or [], key=lambda r: -(int(r[2]) - int(r[1])))
    if not rows:
        return head + "\n\nПереезжать некому"
    items = []
    for c, done, total in rows:
        name = (_e(c.name) if done >= total
                else _deep_link(bot_username, f"{MIGRATION_PAYLOAD}-{int(c.id)}", c.name))
        items.append(f"{_mig_dot(done, total)} {name}: {done}/{total} устройств")
    return head + _LIST_SEP + _LIST_SEP.join(items)


def migration_client_text(client, rows, bot_username: str = "") -> str:
    """Переезд одного профиля: «🚚 Переезд: [Ксюша], 1/3 устройств» и устройства
    с датой последнего подключения. rows — [(устройство, переехало, unix-время
    последнего подключения)]."""
    done = sum(1 for _d, moved, _t in rows if moved)
    head = f"🚚 <b>Переезд:</b> {profile_link(client, bot_username)}, {done}/{len(rows)} устройств"
    items = []
    for dev, moved, ts in rows:
        seen = (timeutil.fmt_dt_ui(datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc))
                if ts else "не подключалось")
        items.append(f"{'🟢' if moved else '🔴'} {_e(dev.name)} — последний коннект: {seen}")
    return head + (_LIST_SEP + "\n".join(items) if items else "")


# ─────────────────────────────────────────────────────────────────────────────
# Списки: онлайн, истекающие, без профиля
# ─────────────────────────────────────────────────────────────────────────────

# Списки с эмодзи в начале строк: подряд строки сливаются, а межстрочный
# интервал Telegram не настраивает. Единственный рычаг — пустая строка.
_LIST_SEP = "\n\n"
# Экран — не отчёт: длинный список режется, хвост — счётчиком; иначе текст
# упёрся бы в 4096 знаков Telegram примерно на трёх десятках записей
_LIST_CAP = 25


def _more(n: int) -> str:
    return f"{_LIST_SEP}… и ещё {n - _LIST_CAP}" if n > _LIST_CAP else ""


def online_devices_text(rows, bot_username: str = "") -> str:
    """«📶 Онлайн: 7»; записи через пустую строку, шлюзы — вверху, имя устройства
    — ссылка на карточку, имя профиля — на карточку профиля. rows — [(устройство,
    профиль или None)]."""
    head = f"📶 <b>Онлайн:</b> {len(rows)}"
    if not rows:
        return head + _LIST_SEP + "Сейчас никто не подключён"
    items = []
    for d, c in rows[:_LIST_CAP]:
        if getattr(d, "is_gateway", 0):
            items.append(f"🛰 {admin_device_link(d, bot_username)} [шлюз] · {plain_ip(d.address)}")
            continue
        who = ("без профиля" if c is None or getattr(c, "is_service", 0)
               else profile_link(c, bot_username))
        items.append(f"{device_state(d, for_admin=True)} {admin_device_link(d, bot_username)} · "
                     f"{who} · {plain_ip(d.address)}")
    return head + _LIST_SEP + _LIST_SEP.join(items) + _more(len(rows))


def expiring_text(rows, bot_username: str = "") -> str:
    """«⏳ Истекают: 2»; «[Ксюша] — 3 дн., до 27.09 18:00»."""
    head = f"⏳ <b>Истекают:</b> {len(rows)}"
    if not rows:
        return head + _LIST_SEP + "Истекающих подписок нет"
    items = []
    for c, _secs in rows[:_LIST_CAP]:
        end = timeutil.parse_iso(c.period_end)
        items.append(f"{profile_link(c, bot_username)} — {timeutil.remaining_brief(end)}, "
                     f"до {timeutil.fmt_dt_ui(end)}")
    return head + _LIST_SEP + _LIST_SEP.join(items) + _more(len(rows))


def unassigned_text(n: int) -> str:
    if not n:
        return "📦 <b>Без профиля:</b> никого"
    return f"📦 <b>Без профиля:</b> {n} — {'пир создан' if n == 1 else 'пиры созданы'} мимо бота"


# ─────────────────────────────────────────────────────────────────────────────
# Трафик списком (общий трафик ведущий, РФ — вложенной строкой под записью)
# ─────────────────────────────────────────────────────────────────────────────

_OUTSIDE = "🧐 Вне профилей: {v} — удалённые устройства и первые минуты новых"


def _rf_sub(rf) -> list[str]:
    """Вложенная строка РФ под записью — только при ненулевой РФ-части."""
    if not rf or int(rf[0]) + int(rf[1]) <= 0:
        return []
    return [f"🇷🇺 {ROUTING_NAME}: {human_bytes(int(rf[0]) + int(rf[1]))}"]


def traffic_profiles_text(rows, bot_username: str = "", total: tuple[int, int] = (0, 0),
                          rf_total: tuple[int, int] | None = None, outside: int = 0) -> str:
    """«📊 Трафик за 09.26: <итог>», под шапкой — РФ-итог и «🧐 Вне профилей»
    (только при ненулевых значениях), затем профили по убыванию общего трафика
    (нулевые не выводятся) со своей РФ-строкой."""
    head = [f"📊 <b>Трафик за {month_label()}:</b> {_total(*total)}"]
    if rf_total and int(rf_total[0]) + int(rf_total[1]) > 0:
        head.append(sub_line(f"🇷🇺 {ROUTING_NAME} (все): {rf_value(*rf_total)}"))
        # «Вне профилей» — часть РФ-итога (удалённые устройства и первые минуты
        # новых), поэтому под шапкой, а не записью среди профилей
        if int(outside or 0) >= _BYTES_PER_GB // 100:
            head.append(sub_line(_OUTSIDE.format(v=human_bytes(outside))))
    branches = []
    live = [(c, rx, tx, rf) for c, rx, tx, rf in rows if int(rx) + int(tx) > 0]
    for c, rx, tx, rf in live[:_LIST_CAP]:
        branches.append((f"👤 {_deep_link(bot_username, f'{TRAFFIC_PAYLOAD}-{c.id}', c.name)}: "
                         f"{_total(rx, tx)}", _rf_sub(rf)))
    if len(live) > _LIST_CAP:
        branches.append((f"… и ещё {len(live) - _LIST_CAP}", []))
    return "\n".join(head) + "\n\n" + (tree(branches) if branches else "Трафика за месяц ещё нет")


def traffic_devices_text(client, rows, total: tuple[int, int] = (0, 0),
                         rf_total: tuple[int, int] | None = None, bot_username: str = "") -> str:
    """«📊 Трафик за 09.26, [Ксюша]: <итог>» — то же по устройствам профиля."""
    head = [f"📊 <b>Трафик за {month_label()}</b>, {profile_link(client, bot_username)}: {_total(*total)}"]
    if rf_total and int(rf_total[0]) + int(rf_total[1]) > 0:
        head.append(sub_line(f"🇷🇺 {ROUTING_NAME} (все): {rf_value(*rf_total)}"))
    branches = []
    live = [r for r in sorted(rows, key=lambda r: -(int(r[1]) + int(r[2]))) if int(r[1]) + int(r[2]) > 0]
    for d, rx, tx, rf in live[:_LIST_CAP]:
        branches.append((f"{device_state(d, for_admin=True)} {_e(d.name)}: {_total(rx, tx)}",
                         _rf_sub(rf)))
    if len(live) > _LIST_CAP:
        branches.append((f"… и ещё {len(live) - _LIST_CAP}", []))
    return "\n".join(head) + "\n\n" + (tree(branches) if branches else "Трафика за месяц ещё нет")


# ─────────────────────────────────────────────────────────────────────────────
# Мои устройства и карточка устройства (админ)
# ─────────────────────────────────────────────────────────────────────────────

def my_devices_header(n: int, limit: int) -> str:
    tail = f" из {limit}" if limit else ", без лимита"
    return f"📱 <b>Мои устройства</b> · {n}{tail}"


def _admin_usage(dev, profile_limit_bytes: int) -> str:
    """«3.2 из 50 ГБ (↑0.4 ↓2.8)» / «3.2 из 100 ГБ (↑0.4 ↓2.8), лимит профиля» /
    «3.2 ГБ (↑0.4 ↓2.8)»; ноль — «0 из 50 ГБ» / «0 ГБ»."""
    rx, tx = int(dev.traffic_rx_month), int(dev.traffic_tx_month)
    limit = int(dev.traffic_limit) or int(profile_limit_bytes or 0)
    whose = "" if dev.traffic_limit else (", лимит профиля" if profile_limit_bytes else "")
    arrows = f" {updown_brief(rx, tx)}" if rx + tx else ""
    if limit:
        return f"{gb(rx + tx)} из {gb(limit)} ГБ{arrows}{whose}"
    return f"{human_bytes(rx + tx)}{arrows}"


def client_devices_header(client, used: int, limit: int) -> str:
    """«📱 <b>Устройства профиля Ксюша</b> · 1 из 3» — список устройств
    профиля у админа, когда в карточку они не влезли."""
    return f"📱 <b>Устройства профиля {_e(client.name)}</b> · {used}" + (f" из {limit}" if limit else "")


def admin_device_card(dev, client, *, rf=None, profile_limit_bytes: int = 0,
                      bot_username: str = "") -> str:
    """Карточка устройства у админа: «🟢 iPhone · 10.8.1.5 · [Ксюша]» / «Был в
    сети 2 мин назад · 3.2 ГБ (↑0.4 ↓2.8) из 50» / РФ-ветка / блокировка /
    передача / «добавлено не ботом»."""
    from awgbot.core import blocks
    who = profile_link(client, bot_username) if client is not None and not getattr(client, "is_service", 0) else "без профиля"
    ago = timeutil.fmt_ago(dev.last_handshake)
    seen = "Не подключался" if ago == "никогда" else f"Был в сети {ago}"
    parts = [f"{device_state(dev, for_admin=True)} <b>{_e(dev.name)}</b> · {plain_ip(dev.address)} · {who}",
             f"{seen} · 📊 {_admin_usage(dev, profile_limit_bytes)}"]
    if rf is not None:                            # ноль тоже — см. карточку профиля
        parts.append(rf_line(*rf, arrows=False))
    extra = []                                    # состояния — отдельным блоком
    reasons = blocks.device_reasons_ru(int(dev.block_reason), for_admin=True)
    if reasons:
        extra.append("⛔ Заблокировано: " + ", ".join(reasons))
    if getattr(dev, "is_lent", False):
        extra.append(f"👤 Передано {holder_link(dev)}")
    elif getattr(dev, "friend_status", None) == "pending":
        extra.append("⏳ приглашение другу ждёт активации")
    if not dev.is_managed:
        extra.append("✳️ Добавлено не ботом — ссылки нет")
    if extra:
        parts.append("")
        parts.extend(extra)
    return "\n".join(parts)


def admin_device_delete_ask(dev, client, *, only: bool, bot_username: str = "") -> str:
    name = _e(dev.name)
    owned = client is not None and not getattr(client, "is_service", 0)
    who = f" ({profile_link(client, bot_username)})" if owned else ""
    head = f"🗑 Удалить устройство «{name}»{who}?"
    if only and client is not None:
        return (f"{head}\n⚠️ Это единственное устройство профиля. "
                "VPN выключится сразу; если владелец использует Telegram только через этот "
                "VPN, до бота он не достучится")
    return f"{head}\nСсылка перестанет работать; при повторном добавлении устройства ссылка изменится"


def device_deleted_note(dev, client, bot_username: str = "") -> str:
    who = f" · профиль {profile_link(client, bot_username)}" if client is not None and not getattr(client, "is_service", 0) else ""
    return f"🗑 {_e(dev.name)} удалено{who}"


def _owner_tail(client, bot_username: str = "") -> str:
    """« ([Коля])» — владелец устройства ссылкой; без профиля — пусто."""
    if client is None or getattr(client, "is_service", 0):
        return ""
    return f" ({profile_link(client, bot_username)})"


def reassign_ask(dev, client=None, bot_username: str = "") -> str:
    return f"🔀 <b>Перенос устройства «{_e(dev.name)}»</b>{_owner_tail(client, bot_username)} — в какой профиль?"


def reassign_slot_ask(client, bot_username: str = "", used: int | None = None) -> str:
    """used — занято сейчас: лимит могли понизить ниже числа устройств."""
    lim = client.device_limit
    return (f"📱 У профиля {profile_link(client, bot_username)} исчерпан лимит устройств "
            f"({lim if used is None else used} из {lim}) — добавить слот?")


def limit_reached_line(used: int, limit: int) -> str:
    """«Достигнут лимит устройств: чтобы добавить новое, удали N» — N считает
    от занятого сверх лимита."""
    n = max(1, int(used) - int(limit) + 1)
    return f"{LIMIT_REACHED}: чтобы добавить новое, удали {n}"


def reassigned_note(name: str, client, bot_username: str = "", donor=None) -> str:
    """«✅ устройство «iPhone» перенесено: [Коля] → [Ксюша]»; прежний владелец
    не передан — «… перенесено в профиль [Ксюша]»."""
    to = profile_link(client, bot_username)
    if donor is None:
        return f"✅ устройство «{_e(name)}» перенесено в профиль {to}"
    src = "без профиля" if getattr(donor, "is_service", 0) else profile_link(donor, bot_username)
    return f"✅ устройство «{_e(name)}» перенесено: {src} → {to}"


def block_device_ask_admin(name: str, client=None, bot_username: str = "") -> str:
    return f"🛑 <b>Блокировка {_e(name)}</b>{_owner_tail(client, bot_username)}. Уведомить владельца?"


# ─────────────────────────────────────────────────────────────────────────────
# Профили: карточка, «✏️ Изменить», продление, лимиты, блокировка, удаление
# ─────────────────────────────────────────────────────────────────────────────

def profiles_header(n: int, online: int) -> str:
    return f"👥 <b>Профили</b> · {n} · онлайн {online}"


def _pause_of(client) -> str:
    """«⏸️ Пауза: 4 из 24 дн.» — счёт паузы против максимума типа; у дня и
    недели дни не копятся — строки нет."""
    kind = str(client.period_kind or "")
    if kind == "year":
        cap = 2 * settings.get_int("pause.pause_max_total_days", 28)
    elif kind == "month":
        cap = 12 * settings.get_int("pause.monthly_pause_days", 2)
    else:
        return ""
    return f"⏸️ Пауза: {int(client.pause_balance_days)} из {cap} дн."


_KIND_SHORT = {"day": "день", "week": "неделя", "month": "месяц", "year": "год"}


def _sub_line(client) -> str:
    """«💳 🟢 до 12.10 18:00 · 18 дн. · месяц, с 12.09» и варианты: бессрочная,
    истекла, истекает, своя пауза, пауза администратора."""
    from awgbot.core import blocks
    mask = int(client.block_reason)
    paused = bool(mask & int(blocks.ClientBlock.PAUSED))
    mode = client.pause_mode or ""
    if paused and mode == "user":
        until = ""
        if client.pause_active_since:
            import datetime
            dt = (timeutil.parse_iso(client.pause_active_since)
                  + datetime.timedelta(days=int(client.pause_reserved_days)))
            until = f" до {timeutil.fmt_date_ui(dt)}"
        return f"💳 ⏸️ на паузе{until}"
    if paused:
        return "💳 ⏸️ приостановлена администратором"
    if not client.period_end:
        return "💳 бессрочная"
    end = timeutil.parse_iso(client.period_end)
    kind = _KIND_SHORT.get(str(client.period_kind or ""), "")
    since = f", с {timeutil.fmt_date_ui(timeutil.parse_iso(client.period_start))}" if client.period_start else ""
    tail = f" · {kind}{since}" if kind else since.lstrip(",").strip() and f" · {since[2:]}"
    if client.status != SubStatus.ACTIVE:
        return f"💳 🔴 истекла {timeutil.fmt_date_ui(end)}{tail}"
    dot = "🟡 истекает" if client.notified_thresholds else "🟢 до"
    return f"💳 {dot} {timeutil.fmt_dt_ui(end)} · {timeutil.remaining_brief(end)}{tail}"


def _rf_short(rt_visible: bool, enabled: int, total: int) -> str:
    if not rt_visible:
        return ""
    if not total or not enabled:
        return " · 🇷🇺 выкл"
    return " · 🇷🇺 вкл на всех" if enabled >= total else f" · 🇷🇺 вкл на {enabled} из {total}"


def admin_client_card(d: dict, bot_username: str = "") -> str:
    """Карточка профиля у админа (данные — services.client_card_data)."""
    from awgbot.core import blocks
    client, devices = d["client"], d["devices"]
    from .fmt import tg_link
    name = tg_link(client.name, client.tg_id, getattr(client, "tg_username", ""))
    lines = [f"👤 {name} · " + ("🟢 онлайн" if d["online"] else "⚪ офлайн")]
    if client.activation_status == ActivationStatus.PENDING:
        lines.append("⏳ ждёт активации")
    # ручная блокировка — «⛔»; истечение, пауза и исчерпанный трафик — «🟡»
    # строкой состояния доступа (та же у клиента)
    manual = int(client.block_reason) & int(blocks.CLIENT_MANUAL)
    reasons = blocks.client_reasons_ru(manual, for_admin=True)
    if reasons:
        lines.append("⛔ Заблокирован: " + ", ".join(reasons))
    access = access_status_line(client)
    if access:
        lines.append(access)
    lines.append(_sub_line(client))
    pause = _pause_of(client)
    if pause and client.period_end:
        lines.append(pause)
    lim = client.device_limit
    devs = f"📱 {len(devices)}" + (f" из {lim}" if lim else "")
    enabled, total = d.get("rt_counts") or (0, 0)
    lines.append(devs + _rf_short(bool(d.get("rt_visible")), enabled, total))
    t = d["traffic"]
    rx, tx = int(t["rx_month"]), int(t["tx_month"])
    arrows = f" {updown_brief(rx, tx)}" if rx + tx else ""
    if client.traffic_limit:
        bonus = f"(+{gb(client.bonus_bytes)})" if client.bonus_bytes else ""
        lines.append(f"📊 {gb(rx + tx)} из {gb(client.traffic_limit)}{bonus} ГБ{arrows}")
    elif rx + tx:
        lines.append(f"📊 {human_bytes(rx + tx)}{arrows}")
    else:
        lines.append("📊 0 ГБ (безлимит)")
    rf = d.get("rf")                              # None — строка не положена; ноль при
    if rf is not None:                            # включённой функции показываем: так
        lines.append(rf_line(*rf, arrows=False))  # видно, что маркировка не работает
    if d.get("progress") is not None:
        done, live, total_d = d["progress"]
        if live:                                  # временное — отдельным блоком
            lines.append("")
            lines.append(f"🚚 Переезд: {done} из {live} живых устройств")
    return "\n".join(lines)


def client_edit_text(client) -> str:
    """«✏️ Ксюша — изменить» и строка текущих значений."""
    if client.period_end:
        start = timeutil.parse_iso(client.period_start) if client.period_start else None
        end = timeutil.parse_iso(client.period_end)
        period = f"Период {timeutil.fmt_period_ui(start, end) if start else 'до ' + timeutil.fmt_date_ui(end)}"
    else:
        period = "Бессрочная"
    devs = _n_devices(client.device_limit) if client.device_limit else "∞ устройств"
    traf = f"{gb_str(client.traffic_limit)} в месяц" if client.traffic_limit else "∞ ГБ в месяц"
    return f"✏️ <b>{_e(client.name)}</b> — изменить\n{period} · {devs} · {traf}"


def client_name_prompt(client) -> str:
    return f"✏️ <b>Новое имя для профиля «{_e(client.name)}»</b>"


def client_name_note(old: str, new: str) -> str:
    return f"✅ Имя профиля: {_e(old)} → {_e(new)}"


def devs_limit_prompt(client, used: int) -> str:
    cur = _limit_devices_str(client.device_limit)
    return f"🔢 <b>Лимит устройств профиля {_e(client.name)}</b> · сейчас {cur}, занято {used}"


def devs_limit_note(old: int, new: int, used: int) -> str:
    note = f"✅ Устройств: {_limit_devices_str(old)} → {_limit_devices_str(new)}"
    if new and used > new:
        note += f" · ⚠️ сейчас {used} из {new} — новые не добавить, пока не станет меньше"
    return note


def traffic_limit_prompt(client) -> str:
    cur = gb_str(client.traffic_limit) if client.traffic_limit else "∞"
    return f"📊 <b>Трафик профиля {_e(client.name)} в месяц</b> · сейчас {cur}"


def traffic_limit_note(old_b: int, new_b: int) -> str:
    def _f(b):
        return gb_str(b) if b else "∞"
    return f"✅ Трафик: {_f(old_b)} → {_f(new_b)}"


OTHER_NUMBER_PROMPT = "✏️ Число, 0 — без лимита"
NUMBER_BAD_LIMIT = "⚠️ Нужно целое число, 0 — без лимита"
NAME_EMPTY = "⚠️ Имя пустое — пришли ещё раз"


def extend_text(client, cut_days: int = 0, bot_username: str = "") -> str:
    """«⏱ Продление: [Ксюша]» и текущий срок; отсрочка — предупреждением."""
    head = f"⏱ <b>Продление:</b> {profile_link(client, bot_username)}"
    if not client.period_end:
        now = "Сейчас: бессрочная"
    else:
        end = timeutil.parse_iso(client.period_end)
        if client.status != SubStatus.ACTIVE:
            now = f"Сейчас: истекла {timeutil.fmt_date_ui(end)}"
        else:
            now = f"Сейчас до {timeutil.fmt_dt_ui(end)} · осталось {timeutil.remaining_brief(end)}"
    lines = [head, now]
    if cut_days > 0:
        lines.append(f"⚠️ Брал отсрочку на {cut_days} дн. — вычтется")
    return "\n".join(lines)


_PERIOD_ACC = {"day": "день", "week": "неделю", "month": "месяц", "year": "год"}


def extended_note(client, kind: str, new_end, pause, bot_username: str = "") -> str:
    """След продления двумя строками: «✅ [Ксюша]: подписка продлена на месяц,»
    ⏎ «→ 12.11 18:00 · дней паузы +2 → 6»."""
    who = profile_link(client, bot_username)
    if new_end is None:
        return f"✅ {who}: подписка теперь бессрочная"
    tail = f"→ {timeutil.fmt_dt_ui(new_end)}"
    if pause is not None and getattr(pause, "kind", None) in ("year", "month"):
        if getattr(pause, "reason", None) in ("expired", "grace", "cap"):
            tail += f" · дни паузы не начислены, доступно {pause.after}"
        else:
            tail += f" · дней паузы +{pause.added} → {pause.after}"
    return f"✅ {who}: подписка продлена на {_PERIOD_ACC.get(kind, kind)},\n{tail}"


def period_start_prompt(client) -> str:
    cur = timeutil.fmt_dt_ui(timeutil.parse_iso(client.period_start), seconds=True) if client.period_start else "—"
    return (f"📅 <b>Начало периода профиля {_e(client.name)}</b> · сейчас {cur}\n"
            "Введи дату в формате <code>ДД.ММ.ГГГГ ЧЧ:ММ</code> (без времени — 00:00), «-» — не менять")


def period_end_prompt(client) -> str:
    cur = (timeutil.fmt_dt_ui(timeutil.parse_iso(client.period_end), seconds=True)
           if client.period_end else "бессрочно")
    return (f"📅 <b>Окончание периода профиля {_e(client.name)}</b> · сейчас {cur}\n"
            "Дата в том же формате, «-» — не менять, «0» — бессрочно")


PERIOD_BAD = "⚠️ Не разобрал дату. Формат: ДД.ММ.ГГГГ ЧЧ:ММ, время можно опустить"
PERIOD_NO_START = "⚠️ У профиля нет даты начала — введи её"


def period_changed_note(start, end) -> str:
    e = timeutil.fmt_dt_ui(end, seconds=True) if end else "бессрочно"
    return f"✅ Период: {timeutil.fmt_dt_ui(start, seconds=True)} → {e}"


def block_client_ask(client, bot_username: str = "") -> str:
    return (f"🛑 <b>Блокировка профиля {_e(client.name)}</b>\n"
            "Поставить подписку на паузу на время блокировки?")


BLOCK_NOTIFY_ASK = "Уведомить владельца профиля?"


def blocked_toast(name: str, *, silent: bool, profile: bool, owner: str = "") -> str:
    """Всплывашка — простой текст: «🛑 Профиль Ксюша заблокирован» /
    «🛑 Устройство «iPhone» (Петя) заблокировано»."""
    if profile:
        head = f"🛑 Профиль {name} заблокирован"
    else:
        head = f"🛑 Устройство «{name}»" + (f" ({owner})" if owner else "") + " заблокировано"
    return head + (" (тихо)" if silent else "")


def client_delete_ask(client, devices, lent: list) -> str:
    """«🗑 Удалить профиль Ксюша?» + последствия: устройства и держатели."""
    lines = [f"🗑 Удалить профиль {_e(client.name)}?"]
    n = len(devices)
    if n:
        word = plural_ru(n, "устройством", "устройствами", "устройствами")
        lines.append(f"Вместе с {n} {word} — {'его ссылка перестанет' if n == 1 else 'их ссылки перестанут'} работать" + (";" if lent else ""))
    for dev in lent:
        lines.append(f"профиль {holder_link(dev)} потеряет доступ к переданному устройству «{_e(dev.name)}»")
    return "\n".join(lines)


def client_deleted_note(name: str, n_devices: int) -> str:
    return f"🗑 Профиль {_e(name)} удалён" + (f" · устройств удалено: {n_devices}" if n_devices else "")


CLIENT_DELETE_PARTIAL = (
    "⚠️ Профиль «{name}» НЕ удалён: сервер не снял пиры {devices}. "
    "Удалить запись, оставив пир живым, нельзя — доступ по нему продолжал бы работать. "
    "Проверь, отвечает ли AWG, и повтори"
)


def resumed_note(client, actual: int, new_end, bot_username: str = "") -> str:
    sub = f"подписка до {timeutil.fmt_dt_ui(new_end)}" if new_end else "подписка бессрочная"
    return f"▶️ {profile_link(client, bot_username)}: пауза снята · {actual} дн. списано · {sub}"


# ─────────────────────────────────────────────────────────────────────────────
# Новый профиль, приглашение, устройство профилю
# ─────────────────────────────────────────────────────────────────────────────

NEW_PROFILE_NAME = "➕ <b>Новый профиль</b> — как назвать?"


def new_profile_devs(name: str) -> str:
    return f"➕ <b>{_e(name)}</b> — сколько устройств?"


def new_profile_traffic(name: str, devs: int) -> str:
    return f"➕ <b>{_e(name)}</b> · {_n_devices(devs) if devs else '∞ устройств'} — трафик в месяц? Общий на все устройства"


def new_profile_period(name: str, devs: int, gb_limit: int) -> str:
    d = _n_devices(devs) if devs else "∞ устройств"
    t = f"{gb_limit} ГБ" if gb_limit else "∞ ГБ"
    return f"➕ <b>{_e(name)}</b> · {d} · {t} — срок подписки?"


def profile_created_note(client, device_limit: int, traffic_gb: int, period_end,
                         bot_username: str = "") -> str:
    d = _n_devices(device_limit) if device_limit else "∞ устройств"
    t = f"{traffic_gb} ГБ в месяц" if traffic_gb else "∞ ГБ в месяц"
    sub = f"подписка до {timeutil.fmt_dt_ui(period_end)}" if period_end else "бессрочная подписка"
    return f"✅ {profile_link(client, bot_username)}: {d}, {t}, {sub}"


INVITE_FORWARD_TEMPLATE = (
    "Привет! Тебе открыт доступ в свободный интернет 🎉\n"
    "Жми ссылку и «Старт» — дальше подскажу\n"
    "{link}"
)


def invite_plain(link: str) -> str:
    return INVITE_FORWARD_TEMPLATE.format(link=link)


def invite_finisher(client, bot_username: str = "") -> str:
    return f"☝️ Приглашение для профиля {profile_link(client, bot_username)} — работает до активации"


def add_device_prompt_admin(client, used: int, limit: int) -> str:
    slots = f" · {used} из {limit}" if limit else ""
    return f"➕ <b>Устройство профилю {_e(client.name)}</b>{slots}\nКак назвать?"


def device_created_admin(name: str, client, bot_username: str = "") -> str:
    return f"✅ {_e(name)} создано для профиля {profile_link(client, bot_username)}"


def admin_bootstrap_device(address: str) -> str:
    """Сообщение о первом устройстве админа, заведённом ботом самостоятельно."""
    return ("🔑 Завёл тебе первое устройство «Админ» (<code>" + _e(address) + "</code>).\n\n"
            "Пиры, созданные в обход бота, теперь попадают в карантин и поднимают "
            "тревогу — значит взять себе доступ «снаружи» больше нельзя, и первое "
            "устройство бот обязан выдать сам. Ссылка ниже: импортируй её в "
            "AmneziaVPN.\n\n"
            "QR и файл — в меню, карточка устройства.")


# ─────────────────────────────────────────────────────────────────────────────
# Уведомления (тексты) — имена профилей ссылками
# ─────────────────────────────────────────────────────────────────────────────

def _slots_phrase(count: int, limit: int) -> str:
    return f"Теперь у тебя {count} из {limit} устройств" if limit else f"Теперь у тебя {_n_devices(count)}"


def reassign_donor_notice(name: str, count: int, limit: int) -> str:
    return f"Устройство «{_e(name)}» удалено из твоего профиля администратором.\n" + _slots_phrase(count, limit)


def reassign_recipient_notice(name: str, count: int, limit: int, *,
                              recipient_is_admin: bool = False) -> str:
    tail = "" if recipient_is_admin else " администратором"
    return f"Устройство «{_e(name)}» добавлено в твой профиль{tail}.\n" + _slots_phrase(count, limit)


def reassign_recipient_notice_with_slot(name: str, count: int, limit: int, *,
                                        recipient_is_admin: bool = False) -> str:
    tail = "" if recipient_is_admin else " администратором"
    return (f"Устройство «{_e(name)}» добавлено в твой профиль{tail}, максимальное количество устройств увеличено.\n"
            + _slots_phrase(count, limit))


def activated_admin_notice(client_or_name, who: str, bot_username: str = "") -> str:
    """«🎉 [Ксюша]: доступ активирован (@handle)»; имя строкой — без ссылки."""
    name = (profile_link(client_or_name, bot_username) if hasattr(client_or_name, "id")
            else _e(str(client_or_name)))
    return f"🎉 {name}: доступ активирован ({_e(who)})"


LIMIT_REACHED = "Достигнут лимит устройств"
TRAFFIC_LIMIT_CLIENT_ASK = OTHER_NUMBER_PROMPT


TRAFFIC_LIMIT_BAD = NUMBER_BAD_LIMIT
