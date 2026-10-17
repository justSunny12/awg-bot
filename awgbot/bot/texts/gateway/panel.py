"""panel.py — часть пакета gateway (см. __init__)."""
from __future__ import annotations

from awgbot.bot import ui
from awgbot.domain import gwchecks
from ..fmt import _e, human_bytes, updown_brief, num
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


def gateway_wait_line(w: dict) -> str:
    """«⬆️ v3.3.2 — сначала обнови бот сервера AWG до v3.3.1» (О6); версия сервера
    неизвестна — «⬆️ v3.3.2 — версию сервера AWG не проверить: он не на связи» (О7)."""
    def v(x):
        return x if str(x).startswith("v") else f"v{x}"
    server = w["peers"][0].version if w.get("peers") else ""
    tail = (f"сначала обнови бот сервера AWG до {_e(v(w['need']))}" if server
            else "версию сервера AWG не проверить: он не на связи")
    return f"<b>⬆️ {_e(v(w['tag']))} — {tail}</b>"


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


def gateway_panel(st, update_tag: str = "", chan: dict | None = None, pending=(),
                  update_wait: dict | None = None, bot_username: str = "") -> str:
    """Панель агента по строкам: имя, роль и аптайм, пустая строка, линк, канал
    конфигурации, предупреждения, SSH, железо, VPN-транзит и списки, SMB,
    здоровье и трафик, пустая строка, свежесть ссылкой.
    chan — состояние канала (linkclient.view()); None — спросить самим.
    update_wait — допустимой цели нет, а новейшая ждёт сервер AWG (update_waiting)."""
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
    mark = getattr(st, "mark_status", "") or ""
    if mark and mark != "confirmed":
        # Статусы производит ровно один источник — routing-gw-setup.sh:
        # unmarked | confirmed | foreign | unconfirmed. Токен пометки при живом
        # канале агент отправляет сам — просить переслать сообщение незачем.
        unmarked = ("⚠️ Шлюз в боте сервера AWG не назначен — запрос ушёл по каналу конфигурации"
                    if chan["online"] else
                    "⚠️ Шлюз в боте сервера AWG не назначен, канала конфигурации нет — назначь это устройство "
                    "в «🛰 Шлюзы» на сервере AWG")
        parts.append({"unmarked": unmarked,
                      "foreign": "⚠️ Шлюз указывает на устройство, отличное от выбранного; линк лежит",
                      "unconfirmed": "⚠️ Аплинк этого устройства не найден — шлюз не подтверждён"}
                     .get(mark, f"⚠️ Пометка: {_e(mark)}"))
    if update_tag:
        tag = update_tag if str(update_tag).startswith("v") else f"v{update_tag}"
        parts.append(f"<b>⬆️ Доступна {_e(tag)}</b>")
    elif update_wait:
        parts.append(pkg.gateway_wait_line(update_wait))
    ssh_line = pkg.gateway_ssh_panel_line(getattr(st, "ssh", None) or {})
    if ssh_line:
        parts.append(ssh_line)
    hw = []
    if st.cpu is not None:
        hw.append(f"CPU {st.cpu:.0f}%" + (f" | {st.temp:.0f} °C" if st.temp is not None else ""))
    elif st.temp is not None:
        hw.append(f"{st.temp:.0f} °C")
    if st.ram is not None:
        hw.append(f"RAM {st.ram:.0f}%")
    if st.disk is not None:
        hw.append(f"диск {st.disk:.0f}%")
    if st.throttled is not None:
        hw.append("питание " + (("⚠️ " + "; ".join(st.throttled["now"])) if st.throttled.get("now") else "ОК"))
    if hw:
        parts.append("📈 " + " · ".join(hw))
    lan = getattr(st, "lan", None) or {}
    if lan:
        bad = [c for c in st.checks if getattr(c, "group", "") == "lan" and c.ok is False]
        state = "🔴 " + ", ".join(pkg._uniq(_lan_fault(c) for c in bad)[:3]) if bad else "🟢"
        parts.append(f"🔀 VPN-транзит {state} · {pkg._packets(lan.get('lan_pkts'))} с роутера")
        own = pkg.own_lists_short(lan)                     # своих нет — хвоста нет
        parts.append(f"📋 Списки: {pkg._lists_counts(lan)}" + (f" · {own}" if own else ""))
        svc = lan.get("svc") or {}
        if svc.get("active"):
            parts.append(pkg.smb_line(svc))
    if pending:
        parts.append(pkg.cfg_pending_line(pending))     # вопросы с сервера AWG ждут человека
    traffic = st.month_rx + st.month_tx
    health = f"🩺 Здоровье {pkg._gw_health_summary(st.checks, st.smart)}"
    if traffic:
        health += f" · 📊 {human_bytes(traffic)} {updown_brief(st.month_rx, st.month_tx)}"
    parts.append(health)
    parts.append("")
    parts.append(pkg.refreshed_line(st, bot_username))
    return "\n".join(parts)


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
        free = f"{st.disk_free_gb:.1f}".replace(".", ",") if st.disk_free_gb < 10 else num(round(st.disk_free_gb))
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
