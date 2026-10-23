"""panel.py — часть пакета gateway (см. __init__)."""
from __future__ import annotations

from awgbot.bot import ui
from awgbot.domain import gwchecks
from ..fmt import _e, disk_lines, frac2, human_bytes, ram_pair, updown_brief, num
from awgbot.bot.texts import gateway as pkg       # имена пакета — через него: их подменяют тесты


REFRESH_PAYLOAD = "refresh"      # /start refresh — перечитать панель живьём; то же слово, что у главной основного бота


def _channel_view(chan: dict | None) -> dict:
    """Состояние канала: аргументом от обработчика; без него — из клиента
    канала (единственное место, где тексты его спрашивают)."""
    if chan is not None:
        return chan
    from awgbot.runtime import linkclient
    return linkclient.view()


def channel_panel_line(chan: dict | None = None) -> str:
    """Строка канала конфигурации: «🔧 Канал конфигурации 🟢» / «🔴 нет связи» —
    канал обязателен, строка есть всегда, отдельной строкой под линком."""
    chan = pkg._channel_view(chan)
    return "🔧 Канал конфигурации " + ("🟢" if chan["online"] else "🔴 нет связи")


def _gw_role(st, chan: dict) -> str:
    """Роль по каналу: «несёт трафик» / «в резерве»; канала или связи нет —
    состояние линка. Пометка не подтверждена (не назначен, чужое устройство,
    аплинк не найден) — роль из последнего слова сервера не говорим: шлюз,
    которого сервер не назначил, трафик не несёт, а сохранённая роль могла
    остаться от прежней сессии."""
    mark = getattr(st, "mark_status", "") or ""
    role = chan["role"] if chan["online"] and mark in ("", "confirmed") else ""
    if role == "active":
        return "🟢 несёт трафик"
    if role:
        return "🟢 в резерве"
    return "🟢 линк поднят" if st.link_up else "🔴 линк лежит"


def refreshed_line(st, bot_username: str = "") -> str:
    """Последняя строка панели: «<i>обновлено 15.10 08:08:14 (UTC+5) 🔄</i>» —
    ссылкой на /start refresh (перечитать живьём), момент — снимка тика (нет
    снимка — сейчас); общая строка обеих ролей (textfmt.refreshed_link)."""
    from awgbot.util import timeutil
    from ..fmt import refreshed_link
    return refreshed_link(bot_username, REFRESH_PAYLOAD, timeutil.parse_iso(st.ts) if st.ts else None)


def gateway_panel(st, upd: dict | None = None, chan: dict | None = None, pending=(),
                  bot_username: str = "") -> str:
    """Панель агента по строкам: имя, роль и аптайм, пустая строка, линк, канал
    конфигурации, предупреждения, SSH, железо, VPN-транзит (состояние; списки и
    SMB — на его экране), здоровье, трафик месяца своей строкой, пустая строка, свежесть ссылкой.
    chan — состояние канала (linkclient.view()); None — спросить самим.
    upd — команда обновления с сервера AWG (upd_panel): идёт — строка хода, не
    удалась — строка неудачи до следующей удачной."""
    from awgbot.util import timeutil
    chan = pkg._channel_view(chan)
    host = _e(st.hostname) if st.hostname else "шлюз"
    up = f" · {timeutil.brief_units(timeutil.fmt_remaining_short(int(st.uptime_seconds)))}" \
        if st.uptime_seconds is not None else ""
    parts = [ui.head(f"🛰 {host}", pkg._gw_role(st, chan)) + up, ""]      # шапка — отдельно от остального
    link = pkg._gw_link_short(st)
    # всегда «Линк до …»: голое «Линк» читается как сетевой интерфейс
    parts.append(link.replace("📡 Линк", f"📡 Линк до {_e(st.server_name or 'сервера AWG')}", 1))
    parts.append(pkg.channel_panel_line(chan))
    rsv_line = reserve_panel_line(getattr(st, "rsv", None) or {})
    if rsv_line:
        parts.append(rsv_line)
    mark = getattr(st, "mark_status", "") or ""
    if mark and mark != "confirmed":
        # Статусы производит ровно один источник — routing-gw-setup.sh:
        # unmarked | confirmed | foreign | unconfirmed. Токен пометки при живом
        # канале агент отправляет сам — просить переслать сообщение незачем.
        unmarked = ("⚠️ Шлюз в боте сервера AWG не подключён — запрос ушёл по каналу конфигурации"
                    if chan["online"] else
                    "⚠️ Шлюз в боте сервера AWG не подключён, канала конфигурации нет — подключи это устройство "
                    "командой из бота сервера AWG: «🛰 Шлюзы» → «🛰 Подключить шлюз»")
        parts.append({"unmarked": unmarked,
                      "foreign": "⚠️ Шлюз указывает на устройство, отличное от выбранного; линк лежит",
                      "unconfirmed": "⚠️ Аплинк этого устройства не найден — шлюз не подтверждён"}
                     .get(mark, f"⚠️ Пометка: {_e(mark)}"))
    if upd and upd.get("state") == "updating":
        from awgbot.domain.notices.fleet import agent_updating
        parts.append(agent_updating(upd.get("target") or ""))
    elif upd and upd.get("state") == "failed":
        from awgbot.domain.notices.fleet import agent_update_failed
        parts.append(agent_update_failed(upd.get("target") or "", upd.get("prev") or "", upd.get("error") or "",
                                         upd.get("server_bot"), state=upd.get("how") or "rolled_back",
                                         why=str(upd.get("why") or ""), tries=int(upd.get("tries") or 0)))
    ssh_line = pkg.gateway_ssh_panel_line(getattr(st, "ssh", None) or {})
    if ssh_line:
        parts.append(ssh_line)
    # дальше — блоками через пустую строку: железо, затем состояние (транзит,
    # здоровье, трафик); пустой блок строки не оставляет
    parts.append("")
    hw = []
    if st.cpu is not None:
        hw.append(f"CPU {st.cpu:.0f}%" + (f" | {st.temp:.0f} °C" if st.temp is not None else ""))
    elif st.temp is not None:
        hw.append(f"{st.temp:.0f} °C")
    used, total = getattr(st, "ram_used_mb", None), getattr(st, "ram_total_mb", None)
    if used is not None and total:
        hw.append(f"RAM {ram_pair(used, total)}")          # занято/всего
    elif st.ram is not None:
        hw.append(f"RAM {st.ram:.0f}%")                   # снимок прежнего выпуска
    if hw:
        parts.append("📈 " + " · ".join(hw))
    # носители — своей строкой (по два в строку), питание — своей: в одной строке
    # с CPU и RAM они не читались
    parts.extend(disk_lines(getattr(st, "disks", None)))   # снимок без списка — строки нет
    if st.throttled is not None:
        parts.append("🔌 питание " + (("⚠️ " + "; ".join(st.throttled["now"])) if st.throttled.get("now") else "ОК"))
    parts.append("")
    lan = getattr(st, "lan", None) or {}
    if lan:
        bad = [c for c in st.checks if getattr(c, "group", "") == "lan" and c.ok is False]
        state = "🔴 " + ", ".join(pkg._uniq(_lan_fault(c) for c in bad)[:3]) if bad else "🟢"
        # счётчик пакетов, списки и SMB — на экране транзита: на главной только состояние
        parts.append(f"🔀 VPN-транзит {state}")
    if pending:
        parts.append(pkg.cfg_pending_line(pending))     # вопросы с сервера AWG ждут человека
    parts.append(f"🩺 Здоровье {pkg._gw_health_summary(st.checks, st.smart)}")
    traffic = st.month_rx + st.month_tx
    if traffic:                                           # трафик месяца — своей строкой
        parts.append(f"📊 {human_bytes(traffic)} {updown_brief(st.month_rx, st.month_tx)}")
    parts.append("")
    parts.append(pkg.refreshed_line(st, bot_username))
    return "\n".join(_squeeze(parts))


def _squeeze(parts: list) -> list:
    """Пустые строки — разделители блоков: две подряд (пустой блок) схлопываются в одну."""
    out: list = []
    for p in parts:
        if p == "" and out and out[-1] == "":
            continue
        out.append(p)
    return out


def reserve_panel_line(rsv: dict) -> str:
    """Т-РА1: «🛟 Резерв «Beta»: на связи · часть ключа есть» / «клиенты на нём
    с 03:12 · через резервный линк ↑ … · ↓ …» / «⚠️ … нет связи с 13:58 — при
    падении сервера AWG этот шлюз не голосует». Пусто — раздела нет."""
    if not rsv:
        return ""
    from awgbot.util import timeutil
    name = f"«{_e(rsv.get('name') or 'резерв')}»"

    def when(iso: str) -> str:
        try:
            return timeutil.fmt_clock_ui(timeutil.parse_iso(iso))
        except (ValueError, TypeError):
            return ""
    if rsv.get("seen") in ("down", "absent"):
        since = when(str(rsv.get("since") or ""))
        return (f"⚠️ Резерв {name}: нет связи" + (f" с {since}" if since else "")
                + " — при падении сервера AWG этот шлюз не голосует")
    if rsv.get("open"):
        since = when(str(rsv.get("since_open") or ""))
        rx, tx = int(rsv.get("rx") or 0), int(rsv.get("tx") or 0)
        tail = f" · через резервный линк {updown_brief(rx, tx)}" if rx + tx else ""
        return f"🛟 Резерв {name}: клиенты на нём" + (f" с {since}" if since else "") + tail
    return f"🛟 Резерв {name}: на связи" + (" · часть ключа есть" if rsv.get("share") else " · части ключа ещё нет")


def _lan_fault(c) -> str:
    """Проблема VPN-транзита в строке панели: одно имя после 🔴 не говорит,
    что с ним — «резолвер не отвечает», «списки не загружены»."""
    name = c.name
    if name == gwchecks.CHECK_RESOLVER:
        return "резолвер не отвечает"
    if name == gwchecks.CHECK_LISTS:
        return "списки не загружены"
    if name == gwchecks.CHECK_UPSTREAM:
        return "апстрим не отвечает"
    return name


def gateway_health(st) -> str:
    """Здоровье: «🩺 Здоровье pi» ⏎ «✅ Проблем не выявлено» и ниже железо и модуль;
    проблемы — «🔴 Проблемы: N» и построчно только проблемные строки (🔴) и
    ожидания (🟡 — самопочинка, первый замер); ✅-строки и «не проверено»
    выключенных функций на экран не идут. Чинить будут по строкам, а не по вердикту."""
    host = _e(st.hostname) if st.hostname else "шлюза"
    problems = gwchecks.health_problems(st.checks)
    smart_bad = bool(st.smart and st.smart != "OK")
    n_problems = len(problems) + (1 if smart_bad else 0)
    # вердикт своей строкой под заголовком (решение 16.10.2026)
    lines = [ui.head(f"🩺 Здоровье {host}"),
             "✅ Проблем не выявлено" if not n_problems else f"🔴 Проблемы: {n_problems}"]
    for c in st.checks:
        mark = gwchecks.check_mark(c)
        if mark in ("✅", ""):
            continue
        # детали — с хоста (вывод скрипта, имена интерфейсов): экранируем
        lines.append(f"{mark} {_e(c.name)}" + (f" — {_e(c.detail)}" if c.detail else ""))
    if smart_bad:
        lines.append("🔴 диск — SMART сообщает о сбое: замени носитель — настройки шлюза сохраняются в бэкап сервера AWG")
    # «RAM: свободно 2 345 МБ · диск: свободно 87 ГБ, SMART ОК · питание ОК»:
    # «свободно» у каждой величины — общий префикс читался бы и как «свободно: питание»
    hw = []
    if st.ram_free_mb is not None:
        hw.append(f"RAM: свободно {num(st.ram_free_mb)} МБ")
    disk = ""
    if st.disk_free_gb is not None:
        free = frac2(round(st.disk_free_gb, 2)) if st.disk_free_gb < 10 else num(round(st.disk_free_gb))
        disk = f"диск: свободно {free} ГБ"
    if st.smart == "OK":
        disk = f"{disk}, SMART ОК" if disk else "SMART ОК"
    if disk:
        hw.append(disk)
    if st.throttled is not None:
        now = st.throttled.get("now") or []
        ever = st.throttled.get("ever") or []
        power = ("⚠️ " + "; ".join(now) + " — проверь блок питания") if now else "ОК"
        if not now and ever:
            power += " (с загрузки: " + "; ".join(ever) + ")"
        hw.append(f"питание {power}")
    if hw:
        lines.append(" · ".join(hw))
    ver = st.module_version or "?"
    src = f" · srcversion {st.srcversion[:8]}…" if st.srcversion else ""
    # одно ядро — «собран для всех» ничего не сообщает; хвост — только когда ядер несколько
    kernels = (f" · собран для всех ядер ({st.kernels_total})"
               if st.kernels_total and st.kernels_total > 1 and not st.kernels_missing else "")
    lines.append(f"Модуль awg {_e(ver)}{_e(src)}{kernels}")
    if problems:
        lines.append("«🚑 Восстановить» переставит правила и переподнимет линк")
    return "\n".join(lines)
