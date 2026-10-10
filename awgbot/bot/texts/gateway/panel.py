"""panel.py — часть пакета gateway (см. __init__)."""
from __future__ import annotations

from awgbot.bot import ui
from ..fmt import _e, human_bytes, updown_brief, num
from awgbot.bot.texts import gateway as pkg       # имена пакета — через него: их подменяют тесты


def _channel_view(chan: dict | None) -> dict:
    """Состояние канала: аргументом от обработчика; без него — из клиента
    канала (единственное место, где тексты его спрашивают)."""
    if chan is not None:
        return chan
    from awgbot.runtime import linkclient
    return linkclient.view()


def channel_panel_line(chan: dict | None = None) -> str:
    """Хвост строки линка про упр. канал: «🔗 упр. канал 🟢» / «⚪ нет связи» —
    канал обязателен, строка есть всегда."""
    chan = pkg._channel_view(chan)
    return "🔗 упр. канал " + ("🟢" if chan["online"] else "⚪ нет связи")


def _gw_role(st, chan: dict) -> str:
    """Роль по каналу: «несёт трафик» / «в резерве»; канала или связи нет —
    состояние линка."""
    role = chan["role"] if chan["online"] else ""
    if role == "active":
        return "🟢 несёт трафик"
    if role:
        return "🟢 в резерве"
    return "🟢 линк поднят" if st.link_up else "🔴 линк лежит"


def gateway_panel(st, update_tag: str = "", chan: dict | None = None, pending=()) -> str:
    """Панель агента по строкам: имя, роль и аптайм, пустая строка, линк и канал, предупреждения,
    SSH, железо, VPN-транзит и списки, SMB, здоровье и трафик, свежесть.
    chan — состояние канала (linkclient.view()); None — спросить самим."""
    from awgbot.util import timeutil
    chan = pkg._channel_view(chan)
    host = _e(st.hostname) if st.hostname else "шлюз"
    up = f" · {timeutil.brief_units(timeutil.fmt_remaining_short(int(st.uptime_seconds)))}" \
        if st.uptime_seconds is not None else ""
    parts = [ui.head(f"🛰 {host}", pkg._gw_role(st, chan)) + up, ""]      # шапка — отдельно от остального
    chan_line = pkg.channel_panel_line(chan)
    link = pkg._gw_link_short(st)
    # всегда «Линк до …»: голое «Линк» читается как сетевой интерфейс
    link = link.replace("📡 Линк", f"📡 Линк до {_e(st.server_name or 'сервера AWG')}", 1)
    parts.append(link + (f" · {chan_line}" if chan_line else ""))
    mark = getattr(st, "mark_status", "") or ""
    if mark and mark != "confirmed":
        # Статусы производит ровно один источник — routing-gw-setup.sh:
        # unmarked | confirmed | foreign | unconfirmed. Токен пометки при живом
        # канале агент отправляет сам — просить переслать сообщение незачем.
        unmarked = ("⚠️ Шлюз в боте сервера AWG не назначен — запрос ушёл по упр. каналу"
                    if chan["online"] else
                    "⚠️ Шлюз в боте сервера AWG не назначен, упр. канала нет — перешли ему сообщение из отчёта")
        parts.append({"unmarked": unmarked,
                      "foreign": "⚠️ Шлюз этого слота — другое устройство, линк лежит",
                      "unconfirmed": "⚠️ Аплинк этого устройства не найден — шлюз не подтверждён"}
                     .get(mark, f"⚠️ Пометка: {_e(mark)}"))
    if update_tag:
        tag = update_tag if str(update_tag).startswith("v") else f"v{update_tag}"
        parts.append(f"<b>⬆️ Доступна {_e(tag)}</b>")
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
        state = "🔴 " + ", ".join(pkg._uniq(c.name for c in bad)[:3]) if bad else "🟢"
        parts.append(f"🔀 VPN-транзит {state} · {pkg._packets(lan.get('lan_pkts'))} с роутера")
        own = pkg.own_lists_short(lan)                     # своих нет — хвоста нет
        parts.append(f"📋 Списки: {pkg._lists_counts(lan)}" + (f" · {own}" if own else ""))
        svc = lan.get("svc") or {}
        if svc.get("active"):
            parts.append(pkg.smb_line(svc))
    if pending:
        parts.append(pkg.cfg_pending_line(pending))     # вопросы с сервера AWG ждут человека
    traffic = st.month_rx + st.month_tx
    health = f"🩺 Здоровье {pkg._gw_health_summary(st.checks)}"
    if traffic:
        health += f" · 📊 {human_bytes(traffic)} {updown_brief(st.month_rx, st.month_tx)}"
    parts.append(health)
    age = st.age_seconds()
    fresh = timeutil.age_ago(age) if age is not None else "только что"
    parts.append(f"<i>обновлено {fresh}</i>")
    return "\n".join(parts)


def gateway_health(st) -> str:
    """Здоровье по строкам: чинить будут по ним, а не по вердикту; железо и
    модуль awg — справкой внизу."""
    host = _e(st.hostname) if st.hostname else "шлюза"
    bad = sum(1 for c in st.checks if c.ok is False)
    lines = [ui.head(f"🩺 Здоровье {host}", "✅ проблем нет" if not bad else f"🔴 проблем: {bad}")]
    for c in st.checks:
        mark = "✅" if c.ok else ("⚪" if c.ok is None else "🔴")
        # детали — с хоста (вывод скрипта, имена интерфейсов): экранируем
        lines.append(f"{mark} {_e(c.name)}" + (f" — {_e(c.detail)}" if c.detail else ""))
    hw = []
    if st.ram_free_mb is not None:
        hw.append(f"RAM свободно {num(st.ram_free_mb)} МБ")
    if st.disk_free_gb is not None:
        free = f"{st.disk_free_gb:.1f}" if st.disk_free_gb < 10 else num(round(st.disk_free_gb))
        hw.append(f"диск свободно {free} ГБ" + (f", SMART {st.smart}" if st.smart else ""))
    if st.throttled is not None:
        now = st.throttled.get("now") or []
        ever = st.throttled.get("ever") or []
        power = ("⚠️ " + "; ".join(now)) if now else "ОК"
        if not now and ever:
            power += " (с загрузки: " + "; ".join(ever) + ")"
        hw.append(f"питание {power}")
    if hw:
        lines.append(" · ".join(hw))
    ver = st.module_version or "?"
    src = f" · srcversion {st.srcversion[:8]}…" if st.srcversion else ""
    lines.append(f"Модуль awg {_e(ver)}{_e(src)} · ядер {st.kernels_total}")
    if bad:
        lines.append("«🔧 Восстановить» переставит правила и переподнимет линк")
    return "\n".join(lines)

