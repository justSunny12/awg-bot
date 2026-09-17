"""Роль gateway (docs/ROADMAP.md, п.7): панель агента, монитор здоровья, настройки, бандл."""

from __future__ import annotations

from .fmt import _e, human_bytes, _updown, _fmt_age
from .settings import SETTINGS_SVC, SVC_CONFIRM_AWG


# ─────────────────────────────────────────────────────────────────────────────
# Роль gateway (docs/ROADMAP.md, п.7)
# ─────────────────────────────────────────────────────────────────────────────

def _gw_link_line(st) -> str:
    if not st.link_up:
        return "🔴 интерфейс лежит"
    if st.handshake_age is None:
        return "🟡 поднят, хендшейка не было"
    if st.handshake_age < 180:
        return f"🟢 хендшейк {st.handshake_age:.0f} с назад"
    return f"🟡 хендшейк {st.handshake_age/60:.0f} мин назад"


def _gw_health_summary(checks) -> str:
    broken = [c for c in checks if c.ok is False]
    unknown = [c for c in checks if c.ok is None]
    if broken:
        return f"🔴 проблем: {len(broken)} — " + ", ".join(c.name for c in broken[:4])
    if unknown:
        return "⚪ не проверено: " + ", ".join(c.name for c in unknown[:4])
    return "✅ проблем не выявлено"


def gateway_panel(st) -> str:
    """Панель агента — зеркало панели основного бота: сервер, линк, железо,
    монитор здоровья, потребление. Всё из снимка; свежесть — строкой «Обновлено».
    Без свопа в RAM (MemAvailable), без внешнего IP (ВПС к шлюзу не ходит)."""
    from awgbot.util import timeutil
    host = f" ({_e(st.hostname)})" if st.hostname else ""
    server = "🟢 работает" if st.link_up else "🔴 интерфейс линка лежит"
    parts = [f"🛰 <b>РФ-шлюз{host}</b>", ""]
    head = [f"🖥 Сервер: {server}"]
    if st.uptime_seconds is not None:
        head.append(f"⬆️ Аптайм: {timeutil.fmt_remaining_short(int(st.uptime_seconds))}")
    parts += head + ["", f"📡 Линк до {_e(st.server_name or 'ВПС')}: {_gw_link_line(st)}"]
    mark = getattr(st, "mark_status", "") or ""
    if mark and mark != "confirmed":
        # Статусы производит ровно один источник — routing-gw-setup.sh:
        # unmarked | confirmed | foreign. Прежний «released» остался от снятой
        # схемы release-токенов, и ветка была недостижимой.
        parts.append({"unmarked": "🛰 Шлюз в основном боте не назначен — перешли ему сообщение из отчёта",
                      "foreign": "⚠️ В основном боте назначен другой шлюз — линк лежит"}
                     .get(mark, f"🛰 Пометка: {_e(mark)}"))
    parts.append("")

    pad = " " * 7
    cpu = f"{st.cpu:.0f}%" if st.cpu is not None else "?"
    if st.temp is not None:
        cpu += f", {st.temp:.0f}°C"
    hw = [f"📈 CPU: {cpu}"]
    if st.ram is not None:
        free = f", свободно {st.ram_free_mb} МБ" if st.ram_free_mb is not None else ""
        hw.append(f"{pad}RAM: {st.ram:.0f}%{free}")
    if st.disk is not None:
        free = f", свободно {st.disk_free_gb:.0f} ГБ" if st.disk_free_gb is not None else ""
        smart = f", SMART: {st.smart}" if st.smart else ""
        hw.append(f"{pad}Диск: {st.disk:.0f}%{free}{smart}")
    if st.throttled is not None:
        power = ("⚠️ " + "; ".join(st.throttled["now"])) if st.throttled.get("now") else "ОК"
        hw.append(f"{pad}Питание: {power}")
    age = st.age_seconds()
    fresh = _fmt_age(age) if age is not None and age >= 1 else "только что"
    hw.append(f"{pad}<i>(обновлено {fresh})</i>")
    parts += hw + ["", f"🌡 Монитор здоровья: {_gw_health_summary(st.checks)}", ""]
    parts.append(f"📊 Потребление за месяц: {human_bytes(st.month_rx + st.month_tx)} "
                 f"{_updown(st.month_rx, st.month_tx)}")
    return "\n".join(parts)


def gateway_health(st) -> str:
    """Монитор здоровья — по строкам: чинить будут по ним, а не по вердикту.
    Сюда же переехали модуль awg и ядра: в панели они были шумом, здесь —
    справка рядом с проверкой «ядра»."""
    lines = ["🌡 <b>Монитор здоровья</b>", ""]
    for c in st.checks:
        mark = "✅" if c.ok else ("⚪" if c.ok is None else "🔴")
        lines.append(f"{mark} {c.name}" + (f" — {c.detail}" if c.detail else ""))
    lines.append("")
    ver = st.module_version or "?"
    src = f", srcversion {st.srcversion[:8]}…" if st.srcversion else ""
    lines.append(f"Модуль awg: {_e(ver)}{_e(src)}")
    lines.append(f"Загружаемых ядер: {st.kernels_total}")
    if st.throttled is not None:
        now = st.throttled.get("now") or []
        ever = st.throttled.get("ever") or []
        power = ("⚠️ " + "; ".join(now)) if now else "ОК"
        if not now and ever:
            power += " (с загрузки: " + "; ".join(ever) + ")"
        lines.append(f"Питание: {power}")
    bad = sum(1 for c in st.checks if c.ok is False)
    lines += ["", "Проблем не выявлено." if not bad else
              f"Проблем: {bad}. «Мастер восстановления» переставит правила и переподнимет линк."]
    return "\n".join(lines)


GW_SETTINGS = ("⚙️ <b>Настройки</b>\n\nУведомления, мониторинг, резервное копирование, "
               "обслуживание и обновления бота.")
GW_SETTINGS_NOTIFY = ("🔔 <b>Уведомления</b>\n\nТихие часы (ночью без звука) и алерты "
                      "о загрузке и температуре шлюза.\n\n"
                      "«E-mail при недоступности Telegram»: если Telegram не отвечает, на ящик "
                      "уходят <b>только критичные</b> алерты — мёртвый линк, нет выхода наружу, "
                      "сломанная обвязка, питание, перегрев, перегруз. Остальное по почте не "
                      "дублируется.")
GW_SETTINGS_MON = ("📊 <b>Мониторинг</b>\n\nЧастота опроса, чувствительность алертов "
                   "и поведение при простое линка.")
GW_BACKUP_NO_KEY = ("💾 Резервная копия шлюза — только шифрованная: внутри приватные ключи "
                    "линка. Задай парольную фразу: 🔐 Шифрование.")


def host_rebooted(hostname: str, who: str) -> str:
    """«Хост перезагружен» — после всех проверок старта; who — «бота»/«агента»."""
    return f"⚠️ Хост {_e(hostname)} был перезагружен.\n✅ Запуск {_e(who)} успешен"
GW_MAINT = SETTINGS_SVC                      # зеркально основному боту
GW_CONFIRM_RESTART = ("🔁 <b>Перезапустить AWG?</b>\n\nИнтерфейс линка опустится и поднимется "
                      "заново. РФ-доступ у всех клиентов оборвётся на несколько секунд; "
                      "обвязка не трогается.")
GW_CONFIRM_REASSERT = ("🔧 <b>Мастер восстановления?</b>\n\nЮнит шлюза переставит правила "
                       "(MASQUERADE, изоляция, маркировка) и переподнимет линк. "
                       "Обрыв РФ-доступа на несколько секунд.")
GW_CONFIRM_BOT_RESTART = ("🔁 <b>Перезапустить бота?</b>\n\nАгент перезапустится и вернётся "
                          "через несколько секунд. Линк и обвязка не трогаются.")
GW_BOT_RESTARTING = "🔄 Бот перезапускается — вернётся через несколько секунд."

GW_BUNDLE_NOT_OURS = ("Это не конфигурация шлюза — файл не принят. Её выпускает основной "
                      "бот: «Условная маршрутизация» → «Конфигурация шлюза».")
GW_BUNDLE_PASSPHRASE_QUESTION = (
    "🔐 <b>В конфигурации — парольная фраза шифрования бэкапов, и она отличается "
    "от заданной на шлюзе.</b>\n\nПерезаписать фразу шлюза фразой с ВПС? Прежние "
    "копии шлюза останутся открываемыми только старой фразой. Если оставить свою — "
    "всё остальное из файла применится как обычно.")


def gateway_claim_forward_text(token: str, status: str) -> str:
    head = ("🛰 <b>Шлюз в основном боте не назначен.</b>" if status == "unmarked" else
            "⚠️ <b>В основном боте назначен другой шлюз.</b> Линк на этой машине лежит: "
            "смени шлюз в настройках основного бота (🔁 Сменить шлюз).")
    return (head + "\n\nЗапасной путь — перешли это сообщение основному боту как есть: "
            "он найдёт это устройство по ключу, назначит его шлюзом и выпустит конфигурацию; "
            f"её примени здесь ещё раз.\n\n<code>{_e(token)}</code>")


def gateway_apply_report(st: dict) -> str:
    """Человеческий отчёт применения конфигурации — из статуса скрипта."""
    lines = []
    up = st.get("UPLINK", "")
    if up == "installed":
        lines.append("аплинк обновлён и поднят")
    elif up == "unchanged":
        lines.append("аплинк без изменений")
    link = st.get("LINK", "")
    if link == "up":
        lines.append("линк поднят")
    elif link == "foreign":
        lines.append("линк лежит: в основном боте назначен другой шлюз")
    gs = st.get("GW_STATUS", "")
    if gs == "confirmed":
        lines.append("шлюз подтверждён")
    elif gs == "unmarked":
        lines.append("шлюз в основном боте не назначен")
    if not lines:
        return ""
    text = ", ".join(lines)
    return text[0].upper() + text[1:] + "."


def gateway_op_result(title: str, ok: bool, detail: str) -> str:
    head = f"{'✅' if ok else '🔴'} <b>{title}: {'готово' if ok else 'не удалось'}</b>"
    return head + (f"\n<code>{_e(detail)}</code>" if detail else "")


def awg_restart_warning_body(gateway: bool) -> str:
    """Слово в слово предупреждение экрана «Перезапустить AWG» — у ролей оно разное."""
    src = GW_CONFIRM_RESTART if gateway else SVC_CONFIRM_AWG
    return src.split("\n\n", 1)[1]


def gateway_bundle_received(link_changed: bool) -> str:
    base = ("📦 <b>Получена конфигурация шлюза.</b>\n\nВнутри — конфиг линка и скрипт "
            "обвязки с ВПС. Применение перепишет конфиг линка и переставит правила."
            if link_changed else
            "📦 <b>Получена конфигурация шлюза.</b>\n\nВнутри — конфиг линка и скрипт "
            "обвязки с ВПС. Конфиг линка не изменился — линк не перезапустится, "
            "правила будут переставлены.")
    return base + (f"\n\n{awg_restart_warning_body(True)}" if link_changed else "")
