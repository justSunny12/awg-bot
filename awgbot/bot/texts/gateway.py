"""Роль gateway (docs/ROADMAP.md, п.7): панель агента, монитор здоровья, настройки, бандл."""

from __future__ import annotations

from awgbot.domain.gwchecks import CHECK_GROUPS_QUIET_UNKNOWN, failure_detail
from awgbot.util import timeutil

from .fmt import _e, human_bytes, updown_brief, plural_ru
from .settings import SVC_CONFIRM_AWG, ssh_owner_refusal, warnings_block, address_list_line, ssh_port_ask


# ─────────────────────────────────────────────────────────────────────────────
# Роль gateway (docs/ROADMAP.md, п.7)
# ─────────────────────────────────────────────────────────────────────────────


def _n(n) -> str:
    """«1 234 567» — разряды пробелом."""
    return f"{int(n or 0):,}".replace(",", " ")


def _gw_link_short(st) -> str:
    """«📡 Линк 🟢 40 с» — линк одной строкой панели."""
    if not st.link_up:
        return "📡 Линк 🔴 интерфейс лежит"
    if st.handshake_age is None:
        return "📡 Линк 🟡 хендшейка не было"
    from awgbot.core import settings as s
    if st.handshake_age <= s.get_int("app.gateway.handshake_max_age", 300):
        return f"📡 Линк 🟢 {timeutil.age_short(st.handshake_age)}"
    return f"📡 Линк 🟡 {timeutil.age_short(st.handshake_age)}"


def _uniq(names) -> list[str]:
    """Имена проверок без повторов: «применение локальной сети» бывает у двух."""
    return list(dict.fromkeys(names))


def _gw_health_summary(checks) -> str:
    broken = [c for c in checks if c.ok is False]
    # «не проверено» у SMB соседних сетей (нет avahi на шлюзе без NAS) и своих
    # списков (ждут синхронизации) — штатно, в сводку панели не идёт
    unknown = [c for c in checks if c.ok is None and getattr(c, "group", "") not in CHECK_GROUPS_QUIET_UNKNOWN]
    if broken:
        return f"🔴 проблем: {len(broken)} — " + ", ".join(c.name for c in broken[:4])
    if unknown:
        return "⚪ не проверено: " + ", ".join(c.name for c in unknown[:4])
    return "✅"


def _channel_view(chan: dict | None) -> dict:
    """Состояние канала: аргументом от обработчика; без него — из клиента
    канала (единственное место, где тексты его спрашивают)."""
    if chan is not None:
        return chan
    from awgbot.runtime import linkclient
    return linkclient.view()


def channel_panel_line(chan: dict | None = None) -> str:
    """Хвост строки линка про упр. канал: «🔗 упр. канал 🟢»; канал не включён
    бандлом — пусто: сказать о нём нечего, а «выключено» читалось бы как поломка."""
    chan = _channel_view(chan)
    if not chan["enabled"]:
        return ""
    return "🔗 упр. канал " + ("🟢" if chan["online"] else "⚪ нет связи")


def _gw_role(st, chan: dict) -> str:
    """Роль по каналу: «несёт трафик» / «в резерве»; канала или связи нет —
    состояние линка."""
    role = chan["role"] if chan["enabled"] and chan["online"] else ""
    if role == "active":
        return "🟢 несёт трафик"
    if role:
        return "🟢 в резерве"
    return "🟢 линк поднят" if st.link_up else "🔴 линк лежит"


def gateway_panel(st, update_tag: str = "", chan: dict | None = None) -> str:
    """Панель агента по строкам: имя, роль и аптайм, пустая строка, линк и канал, предупреждения,
    SSH, железо, VPN-транзит и списки, SMB, здоровье и трафик, свежесть.
    chan — состояние канала (linkclient.view()); None — спросить самим."""
    from awgbot.util import timeutil
    chan = _channel_view(chan)
    host = _e(st.hostname) if st.hostname else "шлюз"
    up = f" · {timeutil.brief_units(timeutil.fmt_remaining_short(int(st.uptime_seconds)))}" \
        if st.uptime_seconds is not None else ""
    parts = [f"🛰 <b>{host}</b> · {_gw_role(st, chan)}{up}", ""]      # шапка — отдельно от остального
    chan_line = channel_panel_line(chan)
    link = _gw_link_short(st)
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
    ssh_line = gateway_ssh_panel_line(getattr(st, "ssh", None) or {})
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
        state = "🔴 " + ", ".join(_uniq(c.name for c in bad)[:3]) if bad else "🟢"
        parts.append(f"🔀 VPN-транзит {state} · {_packets(lan.get('lan_pkts'))} с роутера")
        own = own_lists_short(lan)                     # своих нет — хвоста нет
        parts.append(f"📋 Списки: {_lists_counts(lan)}" + (f" · {own}" if own else ""))
        svc = lan.get("svc") or {}
        if svc.get("active"):
            parts.append(smb_line(svc))
    traffic = st.month_rx + st.month_tx
    health = f"🩺 Здоровье {_gw_health_summary(st.checks)}"
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
    lines = [f"🩺 <b>Здоровье {host}</b> · " + ("✅ проблем нет" if not bad else f"🔴 проблем: {bad}")]
    for c in st.checks:
        mark = "✅" if c.ok else ("⚪" if c.ok is None else "🔴")
        # детали — с хоста (вывод скрипта, имена интерфейсов): экранируем
        lines.append(f"{mark} {_e(c.name)}" + (f" — {_e(c.detail)}" if c.detail else ""))
    hw = []
    if st.ram_free_mb is not None:
        hw.append(f"RAM свободно {_n(st.ram_free_mb)} МБ")
    if st.disk_free_gb is not None:
        free = f"{st.disk_free_gb:.1f}" if st.disk_free_gb < 10 else _n(round(st.disk_free_gb))
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


def _packets(pk) -> str:
    """«1 234 567 пакетов» — с разделителем разрядов и склонением; нет — «нет»."""
    n = int(pk or 0)
    if not n:
        return "нет пакетов"
    return _n(n) + " " + plural_ru(n, "пакет", "пакета", "пакетов")


def _lists_counts(lan: dict) -> str:
    d, n = int(lan.get("domains", 0) or 0), int(lan.get("nets", 0) or 0)
    return (f"{_n(d)} " + plural_ru(d, "домен", "домена", "доменов") + ", "
            + f"{_n(n)} " + plural_ru(n, "подсеть", "подсети", "подсетей"))


def _lists_updated_short(raw: str) -> str:
    """«обн. <когда>» после списков; ещё не обновлялись — так и пишем."""
    from awgbot.util import timeutil
    if not raw:
        return "ещё не обновлялись"
    try:
        return "обн. " + timeutil.fmt_dt_ui(timeutil.parse_iso(raw))
    except ValueError:
        return "обн. ?"


def smb_line(svc: dict) -> str:
    """«🗂 SMB: свои — 1, извне — 2»; нулевая часть не выводится, обе нулевые
    — «не найдены», сервер ещё ничего не присылал — «обновляю…». Имена и
    avahi — только в здоровье."""
    peers = svc.get("peer") or []
    own = len(svc.get("own") or [])
    if not peers and not own:
        return "🗂 SMB: не найдены" if svc.get("ever") else "🗂 SMB: обновляю…"
    parts = ([f"свои — {own}"] if own else []) + ([f"извне — {len(peers)}"] if peers else [])
    return "🗂 SMB: " + ", ".join(parts)


LAN_ABOUT = ("роутер маршрутизирует весь трафик локальной сети сюда, шлюз маршрутизирует: домены и "
             "подсети из списков — в туннель, остальное — напрямую;\nсвои списки синхронизируются "
             "между шлюзами: добавленное или убранное здесь уходит через сервер AWG на остальные "
             "шлюзы — сразу, если они на связи, иначе при подключении;\nвведённый домен накрывает и "
             "все поддомены; правила «напрямую» приоритетнее правил «в туннель»;\nсначала показаны "
             "домены напрямую (🇷🇺), затем в туннель (🌍)")


def gateway_transit_text(st, items=None, own: dict | None = None) -> str:
    """Экран «🔀 VPN-транзит»: состояние, факты, списки, свои списки, SMB,
    объяснение под «подробнее», состояние синхронизации открыто. items —
    свои домены (для «пока пусто»), own — состояние синхронизации
    (services.own_status)."""
    from .fmt import details
    lan = getattr(st, "lan", None) or {}
    own = own or lan.get("own") or {}
    bad = [c for c in st.checks if getattr(c, "group", "") == "lan" and c.ok is False]
    state = "🔴 " + ", ".join(_uniq(c.name for c in bad)[:3]) if bad else "🟢 работает"
    lines = [f"🔀 <b>VPN-транзит</b> · {state}",
             f"<code>{_e(lan.get('iface', '') or '?')}</code> · <code>{_e(lan.get('addr', '') or '?')}</code> · "
             f"{_packets(lan.get('lan_pkts'))} с роутера",
             f"DNS — <code>{_e(lan.get('resolver', '') or '?')}</code> через "
             f"<code>{_e(lan.get('uplink', '') or 'аплинк')}</code>",
             f"📋 Списки: {_lists_counts(lan)} ({_lists_updated_short(lan.get('updated_at') or '')})",
             own_lists_short(lan, plain=True)]
    if (lan.get("svc") or {}).get("active"):
        lines.append(smb_line(lan["svc"]))
    shared = bool(own.get("active"))
    about = LAN_ABOUT
    if items is not None and not items:                 # подсказки пустого списка — под «подробнее»
        about = ("добавь домены кнопками «➕ В туннель» и «➕ Напрямую»"
                 + (";\nсписки общие для всех шлюзов — добавленное здесь появится и на остальных"
                    if shared else "") + ";\n" + about)
    if shared:
        line = own_lists_state_line(own)
        if line:
            lines.append(line)
    elif own.get("no_channel"):
        lines.append(SYNC_TAILS["no_channel"].replace("Изменения применятся", "Списки применятся"))
    lines.append(details(about))
    return "\n".join(lines)


def gateway_transit_ask_domain(kind: str) -> str:
    head = {"add": "➕ <b>В туннель</b>", "ru": "➕ <b>Напрямую</b>"}[kind]
    return f"{head}\n\nПришли домены через пробел: <code>example.com</code> — накрывает и поддомены"


def own_lists_short(lan: dict, plain: bool = False) -> str:
    """«свои: 5 в туннель, 1 напрямую» (панель) / «Свои списки: …» (экран);
    хвост — состояние синхронизации, если есть что сказать."""
    own = lan.get("own") or {}
    v, r = int(lan.get("own_vpn", 0) or 0), int(lan.get("own_ru", 0) or 0)
    parts = ([f"{v} в туннель"] if v else []) + ([f"{r} напрямую"] if r else [])
    if not parts and not plain:
        return ""                                      # в панели пустой хвост не рисуется
    head = ("Свои списки: " if plain else "свои: ") + (", ".join(parts) if parts else "пусто")
    if not own.get("active"):
        return head
    state = own.get("state", "")
    if state in ("pending", "no_link", "stale_server"):
        return head + " · ⏳ ждут синхронизации"
    if state in ("failed", "old_script"):
        return head + " · ⚠️ не применились (🩺 Здоровье)"
    return head


def own_lists_state_line(own: dict) -> str:
    """Строка состояния синхронизации — только при расхождении."""
    state = own.get("state", "")
    n = int(own.get("pending") or 0)
    n_word = f"{n} " + plural_ru(n, "правка", "правки", "правок")
    if state == "no_link":
        return f"⏳ Ждут синхронизации: {n_word} — нет связи с сервером AWG"
    if state in ("pending", "stale_server"):
        return f"⏳ Ждут синхронизации: {n_word} — сервер AWG ещё не ответил"
    if state in ("failed", "old_script"):
        detail = failure_detail("Не применились", own.get("err") or "скрипт старого образца — обвязка перевыставляется")
        return "⚠️ " + _e(detail[:1].upper() + detail[1:])
    if state == "rejected" and own.get("rej"):
        d, why = own["rej"][0]
        return f"⚠️ Сервер AWG не принял: <code>{_e(d)}</code> — {_e(why)}"
    return ""


SYNC_TAILS = {
    "online": "Изменения синхронизируются с другими шлюзами",
    "offline": "Изменения будут синхронизированы с другими шлюзами, когда появится связь с сервером AWG",
    "no_channel": ("Изменения применятся только для этого шлюза: для синхронизации нужен упр. канал "
                   "до сервера AWG — перевыпусти конфигурацию шлюза"),
}


def gateway_transit_removed_toast(domain: str, shared: bool = False, sync: str = "") -> str:
    """Всплывашка после «➖»: итог и хвост синхронизации — только когда он
    правдив (другого шлюза нет — хвоста нет; shared оставлен для вызовов)."""
    tail = SYNC_TAILS.get(sync, "")
    # всплывашка — не длиннее 200 знаков: домен уступает место хвосту
    limit = min(80, 200 - len(": убран") - (len(tail) + 1 if tail else 0))
    return f"{short_name(domain, limit)}: убран" + (f"\n{tail}" if tail else "")


def short_name(name: str, limit: int = 80) -> str:
    """Имя во всплывашку: длинный домен или DynDNS — с многоточием, хвост
    строки («убран», про синхронизацию) остаётся целым."""
    return name if len(name) <= limit else name[:limit - 1] + "…"


def note_budget(screen_text: str, reserve: int = 300) -> int:
    """Сколько знаков остаётся под итог первой строкой экрана: 4096 минус
    видимый текст экрана и запас на хвост и «…и ещё N строк»."""
    import html as _html
    import re as _re
    visible = _html.unescape(_re.sub(r"<[^>]+>", "", screen_text))
    return max(400, 4096 - len(visible) - reserve)


def gateway_transit_result(ok: bool, out: str, sync: str = "", budget: int = 3300) -> str:
    """Итог add/ru/del — строки скрипта «домен: добавлен / убран / уже в
    списке»; служебные строки про адреса в наборе (с отступом) не показываем.
    sync — хвост про синхронизацию: "online" | "offline" | "". budget —
    предел в знаках (итог идёт первой строкой экрана, см. note_budget)."""
    rows = [r for r in out.strip().splitlines() if r and not r.startswith(" ")]
    keep, size = [], 0
    for r in rows:
        if size + len(r) > budget:
            break
        keep.append(r)
        size += len(r) + 1
    out = "\n".join(keep)
    if len(rows) > len(keep):
        rest = len(rows) - len(keep)
        out += f"\n…и ещё {rest} " + plural_ru(rest, "строка", "строки", "строк")
    # Домены в строках итога скрипт сам обернул в <code> (иначе Telegram делает из
    # них ссылки): остальное экранируем, свои теги пропускаем. Неудача уходит во
    # всплывашку, где HTML не работает, — там теги снимаем.
    if out:
        body = (_e(out).replace("&lt;code&gt;", "<code>").replace("&lt;/code&gt;", "</code>") if ok
                else _e(out.replace("<code>", "").replace("</code>", "")))
    else:
        body = "готово" if ok else "не удалось"
    tail = SYNC_TAILS.get(sync, "")
    return ("✅ " if ok else "⚠️ ") + body + (f"\n{tail}" if tail else "")


def gw_settings_text() -> str:
    from .settings import settings_root_text
    return settings_root_text()


def gw_settings_notify_text() -> str:
    from awgbot.core import settings as s
    from .fmt import details
    lines = ["🔔 <b>Уведомления</b>"]
    if s.get_bool("quiet_hours.quiet_hours_enabled", True):
        lines.append(f"Тихие часы {s.get_int('quiet_hours.quiet_hours_start', 20):02d}:00–"
                     f"{s.get_int('quiet_hours.quiet_hours_end', 7):02d}:00 МСК — без звука, кроме аварий")
    else:
        lines.append("Тихие часы выключены — уведомления со звуком круглые сутки")
    if s.get_bool("resource_alerts.enabled", True):
        lines.append(f"Алерты хоста: CPU {s.get_int('resource_alerts.thresholds_percent.cpu', 80)}% · "
                     f"RAM {s.get_int('resource_alerts.thresholds_percent.ram', 80)}% · "
                     f"диск {s.get_int('resource_alerts.thresholds_percent.disk', 80)}% · "
                     f"{s.get_int('app.gateway.temp_alert_c', 75)} °C")
    else:
        lines.append("Алерты хоста выключены")
    lines.append(details("Аварии на e-mail — только когда Telegram недоступен: линк, выход наружу, "
                         "обвязка, питание, перегрев, перегруз"))
    return "\n".join(lines)


def _streak(n: int) -> str:
    return f"{n} " + plural_ru(n, "плохого замера", "плохих замеров", "плохих замеров")


def gw_settings_mon_text() -> str:
    from awgbot.core import settings as s
    loud = s.get_bool("app.gateway.link_alert_loud", True)
    from awgbot.bot.keyboards.gateway import link_minutes
    mins = link_minutes(s.get_int("app.gateway.handshake_max_age", 300))
    return (f"🩺 <b>Мониторинг</b> · опрос раз в {s.get_int('app.gateway.monitor_minutes', 3)} мин · алерт после "
            f"{_streak(s.get_int('app.monitoring.alert_streak', 5))} · линк молчит дольше "
            f"{mins} мин — " + ("со звуком круглые сутки" if loud else "по правилам тихих часов"))


GW_BACKUP_NO_KEY = ("💾 Бэкап шлюза — только шифрованный: внутри приватные ключи линка. "
                    "Задай парольную фразу: 🔐 Шифрование")


def host_rebooted(hostname: str, who: str) -> str:
    """«Хост перезагружен» — после всех проверок старта; who — «бота»/«агента»."""
    return f"⚠️ Хост {_e(hostname)} был перезагружен.\n✅ Запуск {_e(who)} успешен"


def gw_confirm_restart(carries: bool = True) -> str:
    """carries — шлюз сейчас несёт трафик и линк жив: только тогда честно
    предупреждать, что РФ-доступ прервётся."""
    return ("🔁 Перезапустить AWG? Линк опустится и поднимется"
            + (" — РФ-доступ у всех прервётся на секунды" if carries else ""))


def gw_confirm_reassert(carries: bool = True) -> str:
    return ("🔧 Восстановить шлюз?\nЮнит переставит правила (маскарад, изоляция, метка) и "
            "переподнимет линк" + (" — РФ-доступ прервётся на секунды" if carries else ""))


GW_CONFIRM_RESTART = gw_confirm_restart()
GW_CONFIRM_REASSERT = gw_confirm_reassert()
GW_CONFIRM_BOT_RESTART = "🔁 Перезапустить бота? Вернётся через несколько секунд; без влияния на пользователей"
GW_BOT_RESTARTING = "🔁 Бот перезапускается — вернётся через несколько секунд"


GW_FIRST_RUN_FILE = ("Это файл первого применения — его не присылают боту, а запускают на устройстве: "
                     "<code>sudo sh awg-gw-bundle.sh --install</code>. Из чата убрал: внутри ключи")
GW_BUNDLE_NOT_OURS = ("Это не конфигурация шлюза — файл не принят. Файлы конфигурации выпускает "
                      "бот сервера AWG: «🛰 Шлюзы» → карточка шлюза → «📤 Конфигурация»")
GW_BUNDLE_PASSPHRASE_QUESTION = (
    "🔐 <b>В конфигурации — парольная фраза шифрования бэкапов, и она отличается "
    "от заданной на шлюзе.</b>\n\nПерезаписать фразу шлюза фразой с сервера AWG? Прежние "
    "копии шлюза останутся открываемыми только старой фразой. Если оставить свою — "
    "всё остальное из файла применится как обычно.")


def gateway_claim_via_channel_text(status: str) -> str:
    """Токен пометки ушёл серверу по каналу — пересылать ничего не нужно."""
    head = _claim_head(status)
    return (head + "\n\nЗапрос на назначение отправлен серверу AWG по управляющему каналу: "
            "бот сервера AWG найдёт это устройство по ключу и перевыпустит файл конфигурации — "
            "отправь его сюда")


def _claim_head(status: str) -> str:
    return ("🛰 <b>Шлюз в боте сервера AWG не назначен.</b>" if status == "unmarked" else
            "⚠️ <b>Аплинк этого устройства не найден.</b> Подтвердить шлюз нечем: подними аплинк "
            "и примени конфигурацию ещё раз." if status == "unconfirmed" else
            "⚠️ <b>Шлюз этого слота — другое устройство.</b> Линк на этом устройстве лежит: "
            "смени шлюз в боте сервера AWG: «🛰 Шлюзы» → карточка шлюза → «✏️ Изменить» → «🔁 Заменить».")


def gateway_claim_forward_text(token: str, status: str) -> str:
    head = _claim_head(status)
    return (head + "\n\nКанал конфигурации сервера AWG недоступен. Перешли это сообщение "
            "боту сервера AWG как есть: он найдёт это устройство по ключу, назначит его шлюзом "
            f"и перевыпустит файл конфигурации — отправь его сюда\n\n<code>{_e(token)}</code>")


def gateway_apply_report(st: dict) -> str:
    """Человеческий отчёт применения конфигурации — из статуса скрипта: аплинк,
    линк и подтверждение одной строкой через « · », фильтр SSH и VPN-транзит —
    строками ниже; адреса моноширинным. Готовый HTML."""
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
        lines.append("линк лежит: шлюз этого слота — другое устройство")
    elif link == "unconfirmed":
        lines.append("линк не тронут: аплинк этого устройства не найден")
    gs = st.get("GW_STATUS", "")
    if gs == "confirmed":
        lines.append("шлюз подтверждён")
    elif gs == "unmarked":
        lines.append("шлюз в боте сервера AWG не назначен")
    elif gs == "unconfirmed":
        lines.append("шлюз не подтверждён")
    if st.get("SSH_FILTER") == "1":
        n = int(st.get("SSH_ALLOW_COUNT") or 0)
        lines.append("фильтр SSH снаружи: включён, " + (f"{n} " + plural_ru(n, "адрес", "адреса", "адресов")
                                                           if n else "только сервер AWG"))
    elif st.get("SSH_FILTER") == "0":
        lines.append("фильтр SSH снаружи: выключен")
    if st.get("LAN") == "1":
        err = st.get("LAN_ERROR") or ""
        if err:
            lines.append(f"VPN-транзит: не применён — {err}")
        else:
            lines.append("VPN-транзит: применён"
                         + (f" ({_e(st.get('LAN_IF'))}, <code>{_e(st.get('LAN_ADDR'))}</code>)"
                            if st.get("LAN_IF") else ""))
    if not lines:
        return ""
    head = [ln for ln in lines if not ln.startswith(("фильтр SSH", "VPN-транзит"))]
    rest = [ln for ln in lines if ln.startswith(("фильтр SSH", "VPN-транзит"))]
    text = "\n".join(([" · ".join(head)] if head else []) + rest)
    return text[0].upper() + text[1:]


def gateway_config_result(ok: bool, detail: str, mail: tuple = ("", "")) -> str:
    """Итог применения конфигурации: «✅ Конфигурация шлюза применена» и отчёт
    строками; отказ — общая маска «🔴 Конфигурация шлюза не применена:» и
    причина строкой ниже. mail — (ok|fail|"", причина): почта из файла
    проверена сразу, итог — отдельной строкой."""
    if ok:
        # отчёт — готовый HTML из gateway_apply_report (адреса в <code>)
        lines = ["✅ <b>Конфигурация шлюза применена</b>"] + ([detail] if detail else [])
    else:
        lines = ["🔴 <b>Конфигурация шлюза не применена:</b>", _e(detail or "причина не названа")]
    state, why = mail
    if state == "ok":
        lines.append("✉️ Почта из конфигурации принята: IMAP и SMTP отвечают, вход выполнен")
    elif state == "fail":
        lines.append(f"✉️ Почта из конфигурации принята, но проверка не прошла: {_e(why)}")
    return "\n".join(lines)


def gateway_op_result(title: str, ok: bool, detail: str) -> str:
    head = f"{'✅' if ok else '🔴'} <b>{title}: {'готово' if ok else 'не удалось'}</b>"
    return head + (f"\n<code>{_e(detail)}</code>" if detail else "")


def awg_restart_warning_body(gateway: bool, carries: bool = True) -> str:
    """Слово в слово предупреждение экрана «Перезапустить AWG» — у ролей оно разное."""
    # у обеих ролей подтверждение однострочное: «🔁 Перезапустить AWG? <цена>»
    src = gw_confirm_restart(carries) if gateway else SVC_CONFIRM_AWG
    return src.split("? ", 1)[-1]


def gateway_bundle_received(link_changed: bool, carries: bool = True) -> str:
    if link_changed:
        return ("📦 <b>Конфигурация с сервера AWG</b>\n"
                "Линк перезапустится" + (" — РФ-доступ у всех прервётся на секунды" if carries else "")
                + "; правила переставятся")
    return ("📦 <b>Конфигурация с сервера AWG</b>\n"
            "Конфиг линка не изменился — линк не перезапустится; правила переставятся")


# ── 🛡 Доступ по SSH ─────────────────────

def _owner_name(kind: str) -> str:
    return {"omv": "OMV", "generator": "другой процесс"}.get(kind, "")


_ALLOW_SHOWN = 12       # кнопки — до 8, текст — до 12: лимит 4096 при длинных именах


def gateway_ssh_text(st: dict) -> str:
    """Раздел: порт (факт) и его владелец, туннель, локальная сеть, снаружи,
    адреса, предупреждения — по месту. Блоки разделены пустой строкой;
    статусные строки — без точки в конце."""
    port = st.get("port")
    lines = ["<b>🛡 SSH-доступ</b>", ""]
    if st.get("sshd_down"):
        lines.append(f"⚪ sshd не запущен. Порт в таблице: {port}")
    elif st.get("owner") == "omv":
        lines.append(f"Порт SSH: {port} — <b>контролирует OMV</b> <i>(в его UI: Службы → SSH)</i>. "
                     "Бот следит за портом и держит фильтр на нём.")
    elif st.get("owner"):
        lines.append(f"Порт SSH: {port} — <b>контролирует другой процесс</b> (см. ниже). Бот следит "
                     "за портом и держит фильтр на нём.")
    else:
        lines.append(f"Порт SSH: {port}")
    lines.append("")
    admins = len(st.get("admin_ips") or [])
    server = f" (<code>{_e(st['server'])}</code>)" if st.get("server") else ""
    lines.append(f"Из туннеля SSH открыт устройствам админа ({admins}) и с сервера AWG{server}")
    lan = st.get("lan") or []
    lines.append("Из локальной сети: открыт всегда"
                 + (" (" + ", ".join(f"<code>{_e(n)}</code>" for n in lan[:3]) + ")" if lan else ""))
    # подсети других шлюзов открыты сами, как только на сервере включён доступ
    # между подсетями: отдельного действия и списка адресов не нужно
    peers = st.get("peer_nets") or []
    if peers:
        lines.append("Из локальных сетей других шлюзов: открыт для "
                     + ", ".join(f"<code>{_e(n)}</code>" for n in peers[:3]))
    else:
        lines.append("Когда подсети связаны, SSH доступен и из подсетей других шлюзов")
    lines.append("")
    allow = st.get("allow") or []
    if not st.get("new_plumbing"):
        lines.append("⚠️ Обвязка шлюза старого образца: фильтр снаружи появится после "
                     "перевыпуска конфигурации шлюза с сервера AWG")
    elif st.get("filter"):
        lines.append("🟢 Снаружи: фильтр включён — только адреса из списка и сервер")
    else:
        lines.append("Снаружи (проброс порта на роутере): фильтр выключен — открыт всем проброшенным")
    lines.append("")
    lines.append(address_list_line(len(allow),
                                   " — снаружи доступ только с сервера AWG" if st.get("filter") else ""))
    warns: list[str] = []
    unresolved = st.get("unresolved") or []
    if unresolved:
        held = st.get("held") or []
        names = ", ".join(f"<code>{_e(n)}</code>" for n in unresolved[:_ALLOW_SHOWN])
        if len(unresolved) > _ALLOW_SHOWN:
            names += f" и ещё {len(unresolved) - _ALLOW_SHOWN}"
        held_s = ", ".join(f"<code>{_e(h)}</code>" for h in held[:_ALLOW_SHOWN])
        if len(held) > _ALLOW_SHOWN:
            held_s += f" и ещё {len(held) - _ALLOW_SHOWN}"
        tail = (" — держу прошлый адрес: " + held_s
                if held else " — прошлого адреса нет, снаружи по этому имени не зайти")
        warns.append("⚠️ Не резолвится: " + names + tail)
    op = st.get("owner_port")
    if st.get("owner") == "omv" and op and port and op != port and not st.get("sshd_down"):
        warns.append(f"⚠️ В OMV задан порт {op}, sshd слушает {port} — нажми «Применить» в OMV")
    conf = [p for p in (st.get("conf_ports") or []) if p != port]
    if conf and not st.get("sshd_down") and not (st.get("owner") == "omv" and op in conf):
        warns.append(f"⚠️ В конфиге sshd порт {conf[0]}, сервис слушает {port} — перезапусти sshd")
    extra = [p for p in (st.get("ports") or []) if p != port]
    if extra:
        warns.append(f"⚠️ sshd слушает ещё порт ({', '.join(map(str, extra))}) — фильтр держит только {port}")
    if st.get("owner") == "generator" and st.get("owner_detail"):
        warns.append(f"ℹ️ В <code>{_e((st.get('owner_files') or ['sshd_config'])[0])}</code> сказано: "
                     f"«<i>{_e(st['owner_detail'])}</i>»")
    if st.get("omv_rules"):
        warns.append(f"⚠️ В OMV заданы правила файервола ({st['omv_rules']}): они действуют рядом "
                     "с таблицей шлюза; при смене порта поправь и там (Сеть → Файервол)")
    if st.get("ufw"):
        warns.append("⚠️ ufw активен: второй владелец правил, новый порт открывай и в нём или выключи ufw")
    return "\n".join(lines + warnings_block(warns))


def gw_ssh_port_ask(current: int | None = None) -> str:
    return ssh_port_ask(current, gateway=True)


def gateway_ssh_owner_refusal(st: dict, listening: int | None) -> str:
    """Отказ смены порта на шлюзе — тот же текст, что у основного бота."""
    return ssh_owner_refusal(st, listening, place="шлюзе")


def gateway_ssh_port_changed(old: int, new: int) -> str:
    return (f"✅ Порт SSH: {old} → {new}. Текущие сеансы не рвутся — проверь вход новым подключением "
            f"на порт {new}; проброс порта на роутере (при наличии) поправь сам: снаружи &lt;любой порт&gt; → шлюз:{new}")


GW_SSH_ALLOW_ASK = ("➕ <b>Адреса для SSH-доступа</b>\n\nПришли IP, подсеть или доменное имя через пробел. "
                    "Только IPv4: проброса IPv6 через роутер нет; имя буду резолвить сам")


def gateway_ssh_allow_added(entries: list[str], merged: list[str] | None = None) -> str:
    """merged — записи, которые схлопнулись в добавленную подсеть (nft не
    принимает пересечения; человеку — что объединено, а не отказ)."""
    # адреса и подсети — моноширинным: жирный адрес Telegram превращает в ссылку
    text = "✅ Адреса для SSH-доступа: добавлено " + _code_list(entries) \
        if entries else "✅ Адреса для SSH-доступа"
    if merged:
        text += "; объединено с новой подсетью: " + _code_list(merged)
    return text


def _code_list(names: list[str]) -> str:
    """Список адресов моноширинным, не длиннее _ALLOW_SHOWN — «и ещё K»."""
    s = ", ".join(f"<code>{_e(n)}</code>" for n in names[:_ALLOW_SHOWN])
    if len(names) > _ALLOW_SHOWN:
        s += f" и ещё {len(names) - _ALLOW_SHOWN}"
    return s


GW_SSH_ALLOW_ALREADY = "ℹ️ Всё из введённого уже в списке — ничего не менял"
GW_SSH_FILTER_ON_ALERT = ("Фильтр включён: снаружи — только список и сервер. Проверь вход новым "
                          "подключением; если роутер подменяет адрес при пробросе, фильтр по адресам "
                          "не сработает")


GW_SSH_FILTER_OFF = "Фильтр снят: снаружи SSH открыт всем"


def gateway_ssh_panel_line(ssh: dict) -> str:
    """Строка панели: порт (владелец) · снаружи."""
    if not ssh:
        return ""
    port = ssh.get("port")
    who = f" ({_owner_name(ssh.get('owner', ''))})" if ssh.get("owner") else ""
    if ssh.get("sshd_down"):
        return "🛡 SSH: sshd не запущен"
    if not ssh.get("new_plumbing"):
        outside = "без фильтра (обвязка старого образца)"
    elif ssh.get("filter"):
        n = int(ssh.get("allow") or 0)
        outside = (f"фильтр: {n} " + plural_ru(n, "адрес", "адреса", "адресов")) if n \
            else "фильтр: только сервер AWG"
    else:
        outside = "открыт"
    return f"🛡 SSH :{port}{who}, {outside}"
