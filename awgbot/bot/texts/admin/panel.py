"""panel.py — часть пакета admin (см. __init__): главная администратора и переезд."""

from __future__ import annotations

import datetime

from awgbot.util import timeutil
from awgbot.bot import ui
from ..fmt import _e, human_bytes, rf_line, profile_link, _n_devices
from ..fmt import deep_link as _deep_link
from ..routing import routing_status_line, routing_admin_status_line
from ..updates import release_url, update_gw_names
from .common import (
    TRAFFIC_PAYLOAD, UPD_PAYLOAD, ONLINE_PAYLOAD, EXPIRING_PAYLOAD, UNASSIGNED_PAYLOAD,
    month_label, _LIST_SEP)
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
                update_wait: dict = None) -> str:
    """Шапка главной: сервер и аптайм одной строкой, пустая строка, метрики,
    РФ-доступ, счётчики и трафик со ссылками на значении, РФ-ветка, доступное
    обновление и переезд — только когда есть что сказать. update_wait —
    допустимой цели нет, а новейшая ждёт шлюзы (update_waiting): строка
    ожидания вместо «⬆️ Доступна»."""
    if st.get("ok") is None:
        dot = "…"
    elif st["ok"]:
        dot = "🟢" + (f" · {timeutil.brief_units(st['uptime'])}" if st.get("uptime") else "")
    else:
        dot = "🔴 не отвечает"
    host = _e(_hostname() or "AWG")
    lines = [ui.head(f"🛠 {host}", dot)]
    if st.get("cpu") is not None or st.get("ram") is not None or st.get("disk") is not None:
        def _p(v):
            return f"{v:.0f}%" if v is not None else "?"
        metrics = f"📈 CPU {_p(st.get('cpu'))} · RAM {_p(st.get('ram'))} · диск {_p(st.get('disk'))}"
        age = timeutil.age_ago(st.get("age_seconds"))
        if age:
            metrics += f" · <i>{age}</i>"
        lines.append(metrics)
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
        # «3 (7 устройств)» — профили онлайн и их устройства; ноль — просто «0»
        value = f"{int(st.get('online_profiles') or 0)} ({_n_devices(n)})" if n else "0"
        counters.append(_counter(ONLINE_PAYLOAD, "📶 Онлайн", value, n))
    if expiring:
        counters.append(_counter(EXPIRING_PAYLOAD, "⏳ Истекают", str(expiring), expiring))
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
    if update_tag:
        tag = update_tag if str(update_tag).startswith("v") else f"v{update_tag}"
        tail.append("<b>" + _deep_link(bot_username, UPD_PAYLOAD, f"⬆️ Доступна {tag}") + "</b> — "
                    + f'<a href="{release_url(tag)}">список изменений</a>')
    elif update_wait:
        tag = _e(update_wait["tag"] if str(update_wait["tag"]).startswith("v") else f"v{update_wait['tag']}")
        tail.append("<b>" + _deep_link(bot_username, UPD_PAYLOAD, f"⬆️ {tag} ждёт шлюзы") + "</b>: "
                    + update_gw_names(update_wait.get("peers") or [], bot_username))
    mig = migration_line(migration, bot_username)
    if mig:
        tail.append(mig)
    if tail:                                          # временные строки — отдельным блоком
        lines.append("")
        lines.extend(tail)
    if len(lines) > 1:                                # шапка — отдельной строкой от остального
        lines.insert(1, "")
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
    head = ui.head("🚚 Переезд", meta=params)
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
    head = ui.head("🚚 Переезд", value=f"{profile_link(client, bot_username)}, {done}/{len(rows)} устройств")
    items = []
    for dev, moved, ts in rows:
        seen = (timeutil.fmt_dt_ui(datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc))
                if ts else "не подключалось")
        items.append(f"{'🟢' if moved else '🔴'} {_e(dev.name)} — последний коннект: {seen}")
    return head + (_LIST_SEP + "\n".join(items) if items else "")
