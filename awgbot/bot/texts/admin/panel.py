"""panel.py — часть пакета admin (см. __init__): главная администратора и переезд."""

from __future__ import annotations

import datetime

from awgbot.util import timeutil
from awgbot.bot import ui
from ..fmt import _e, disk_lines, human_bytes, num, rf_line, profile_link, plural_ru
from ..fmt import deep_link as _deep_link
from ..routing import routing_status_line, routing_admin_status_line
from ..updates import release_url
from .common import (
    TRAFFIC_PAYLOAD, UPD_PAYLOAD, ONLINE_PAYLOAD, EXPIRING_PAYLOAD, UNASSIGNED_PAYLOAD,
    REFRESH_PAYLOAD, month_label, refreshed_line, _LIST_SEP)
from .lists import online_value
from awgbot.bot.texts import admin as pkg       # _HOSTNAME — через пакет: его подменяют тесты


# ─────────────────────────────────────────────────────────────────────────────
# Главная
# ─────────────────────────────────────────────────────────────────────────────

def _hostname() -> str:
    """Имя хоста — один syscall за жизнь процесса, а не на каждый рендер панели."""
    if pkg._HOSTNAME is None:
        import socket
        pkg._HOSTNAME = socket.gethostname()
    return pkg._HOSTNAME


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
                rf: dict = None, unassigned: int = 0, update_tag: str = "",
                update_run: dict = None, rsv_line: str = "", rsv: dict = None) -> str:
    """Шапка главной: сервер и аптайм одной строкой, пустая строка, метрики,
    РФ-доступ, счётчики и трафик со ссылками на значении, РФ-ветка, доступное
    обновление и переезд — только когда есть что сказать. update_run — идёт
    ход набора обновлений (fleet_progress): «🟡 Обновление до vX: 1 из 3»
    ссылкой в раздел вместо «⬆️ Доступна»."""
    if st.get("ok") is None:
        dot = "…"
    elif st["ok"]:
        dot = "🟢 работает" + (f" · {timeutil.brief_units(st['uptime'])}" if st.get("uptime") else "")
    else:
        dot = "🔴 AWG не отвечает"
    host = _e(_hostname() or "AWG")
    lines = [ui.head(f"🛠 {host}", dot)]
    if rsv:
        # Т-Р17: статус резерва в стиле строки сервера — имя, кружок, суть; нажатие — карточка резерва
        line = f"🛟 <b>{_e(rsv.get('name') or 'резерв')}</b> {_e(rsv.get('dot') or '')} {_e(rsv.get('text') or '')}".rstrip()
        lines.append(f'<a href="https://t.me/{bot_username}?start={RSV_PAYLOAD}">{line}</a>' if bot_username else line)
    elif rsv_line:
        safe = _e(rsv_line)
        lines.append(f'<a href="https://t.me/{bot_username}?start={RSV_PAYLOAD}">{safe}</a>' if bot_username else safe)
    if st.get("cpu") is not None or st.get("ram") is not None or st.get("disk") is not None:
        def _p(v):
            return f"{v:.0f}%" if v is not None else "?"
        free = st.get("ram_free_mb")
        lines.append(f"📈 CPU {_p(st.get('cpu'))} · RAM {_p(st.get('ram'))}"
                     + (f" (свободно {num(free)} МБ)" if free is not None else ""))
        # носители — своей строкой (по два в строку); снимок без списка — процент, как прежде
        lines.extend(disk_lines(st.get("disks")) or ([f"диск {_p(st.get('disk'))}"] if st.get("disk") is not None else []))
    elif st.get("age_seconds") is None:
        lines.append("📈 Метрики: ещё нет замера")
    if routing_info is not None:
        rt_line = routing_admin_status_line(routing_info, bot_username)
        if rt_line:                                   # «выключен» без шлюзов — строки нет
            lines.append(rt_line)
    elif routing_ok is not None:
        lines.append(routing_status_line(routing_ok))

    def _counter(payload: str, label: str, value: str, n: int) -> str:
        # ссылка — на значении; нулевой счётчик — без ссылки: за ней пустой экран
        return f"{label}: " + (_deep_link(bot_username, payload, value) if n else value)
    counters = []
    if st.get("online_count") is not None:
        n = int(st["online_count"])
        # «2 профиля, 7 устройств» — профили онлайн и их устройства; ноль — просто «0»
        value = online_value(int(st.get("online_profiles") or 0), n)
        counters.append(_counter(ONLINE_PAYLOAD, "📶 Онлайн", value, n))
    if expiring:
        counters.append(_counter(EXPIRING_PAYLOAD, "🟡 Истекают", str(expiring), expiring))
    if unassigned:
        counters.append(_counter(UNASSIGNED_PAYLOAD, "📦 Без профиля", str(unassigned), unassigned))
    if counters:
        lines.append(" · ".join(counters))
    # строка трафика стоит и при нуле, но ссылкой — только когда экрану есть
    # что показать; РФ-строка при включённой функции тоже: нулём видно, что
    # маркировка не работает
    if st.get("traffic_rx") is not None:
        rx, tx = int(st["traffic_rx"]), int(st["traffic_tx"])
        lines.append(_counter(TRAFFIC_PAYLOAD, f"📊 Трафик за {month_label()}", human_bytes(rx + tx), rx + tx))
        if rf and rf.get("show"):
            lines.append(rf_traffic_line(rf, bot_username))
    tail = []
    if update_run:
        tag = update_run["tag"] if str(update_run["tag"]).startswith("v") else f"v{update_run['tag']}"
        tail.append(_deep_link(bot_username, UPD_PAYLOAD,
                               f"🟡 Обновление до {_e(tag)}: {update_run['n']} из {update_run['total']}"))
    elif update_tag:
        tag = update_tag if str(update_tag).startswith("v") else f"v{update_tag}"
        tail.append("<b>" + _deep_link(bot_username, UPD_PAYLOAD, f"⬆️ Доступна {tag}") + "</b> — "
                    + f'<a href="{release_url(tag)}">список изменений</a>')
    mig = migration_line(migration, bot_username)
    if mig:
        tail.append(mig)
    if tail:                                          # временные строки — отдельным блоком
        lines.append("")
        lines.extend(tail)
    if len(lines) > 1:                                # шапка — отдельной строкой от остального
        lines.insert(1, "")
    # возраст снимка — последней строкой, ссылкой «перечитать» (кнопки «🔄
    # Обновить» на главной больше нет)
    age = st.get("age_seconds")
    at = None if age is None else timeutil.shift(timeutil.now(), -int(age))   # None — живой замер: сейчас
    lines += ["", refreshed_line(bot_username, REFRESH_PAYLOAD, at)]
    return "\n".join(lines)


MIGRATION_PAYLOAD = "migration"   # /start migration[-<id>] — обзор переезда и профиль в нём
RSV_PAYLOAD = "rsv"               # /start rsv — карточка резерва (строка Т-Р17 на главной)


def migration_line(p, bot_username: str = "") -> str:
    """«🚚 Переезд: 11 из 12 профилей · 18 из 20 устройств» — пока идёт
    переезд; «🚚 Переезд» — ссылка на обзор."""
    if p is None or getattr(p, "clients_total", 0) == 0:
        return ""
    label = _deep_link(bot_username, MIGRATION_PAYLOAD, "🚚 Переезд")
    return f"{label}: {migration_counts(p)}"


def migration_counts(p) -> str:
    """«1 из 2 профилей · 1 из 2 устройств» — ход переезда (главная, «🔧 Сервис»)."""
    return (f"{p.clients_done} из {p.clients_total} {plural_ru(p.clients_total, 'профиля', 'профилей', 'профилей')} · "
            f"{p.devices_done} из {p.devices_total} {plural_ru(p.devices_total, 'устройства', 'устройств', 'устройств')}")


def _mig_dot(done: int, total: int) -> str:
    """🟢 переехал целиком, ⏳ ждёт, пока человек перенастроит устройства —
    прежние конфигурации работают до завершения переезда."""
    return "🟢" if total and done >= total else "⏳"


def migration_overview_text(d: dict, bot_username: str = "") -> str:
    """Обзор переезда: параметры одной строкой (только то, что меняется), затем
    профили — не переехавшие целиком сверху, по убыванию оставшегося; имя не
    переехавшего — ссылка на его экран переезда."""
    params = []
    for key, label in (("port", "порт"), ("subnet", "подсеть"), ("generation", "поколение AWG")):
        old, new = d.get(key) or ("", "")
        if old and new and str(old) != str(new):
            fmt_ = ((lambda v: f"gen{v}") if key == "generation"
                    else (lambda v: f"<code>{_e(str(v))}</code>") if key == "subnet" else str)
            params.append(f"{label} {fmt_(old)} → {fmt_(new)}")
    head = ui.head("🚚 Переезд", meta=params)
    rows = sorted(d.get("rows") or [], key=lambda r: -(int(r[2]) - int(r[1])))
    if not rows:
        return head + "\n\nПереезжать некому"
    items = []
    for c, done, total in rows:
        name = (_e(c.name) if done >= total
                else _deep_link(bot_username, f"{MIGRATION_PAYLOAD}-{int(c.id)}", c.name))
        items.append(f"{_mig_dot(done, total)} {name}: {done} из {total} "
                     f"{plural_ru(total, 'устройства', 'устройств', 'устройств')}")
    return head + _LIST_SEP + "\n".join(items)


def migration_client_text(client, rows, bot_username: str = "") -> str:
    """Переезд одного профиля: «🚚 Переезд: [Ксюша], 1 из 3 устройств» и
    устройства с датой последнего подключения. rows — [(устройство,
    переехало, unix-время последнего подключения)]."""
    done = sum(1 for _d, moved, _t in rows if moved)
    total = len(rows)
    head = ui.head("🚚 Переезд", value=f"{profile_link(client, bot_username)}, {done} из {total} "
                   f"{plural_ru(total, 'устройства', 'устройств', 'устройств')}")
    items = []
    for dev, moved, ts in rows:
        if ts:
            seen = "последнее подключение: " + timeutil.fmt_dt_ui(
                datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc))
        else:
            seen = "ещё не подключалось"
        items.append(f"{'🟢' if moved else '⏳'} {_e(dev.name)} — {seen}")
    return head + (_LIST_SEP + "\n".join(items) if items else "")
