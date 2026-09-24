"""Роль gateway (docs/ROADMAP.md, п.7): панель агента, монитор здоровья, настройки, бандл."""

from __future__ import annotations

from .fmt import _e, human_bytes, _updown, _fmt_age, plural_ru
from .settings import SETTINGS_SVC, SVC_CONFIRM_AWG, ssh_owner_refusal, warnings_block, address_list_line


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
    # «не проверено» у сервисов соседей (нет avahi на шлюзе без NAS) — штатно,
    # в сводку панели не идёт; на экране монитора строка остаётся
    unknown = [c for c in checks if c.ok is None and getattr(c, "group", "") != "svc"]
    if broken:
        return f"🔴 проблем: {len(broken)} — " + ", ".join(c.name for c in broken[:4])
    if unknown:
        return "⚪ не проверено: " + ", ".join(c.name for c in unknown[:4])
    return "✅ проблем не выявлено"


def channel_panel_line() -> str:
    """Строка канала до ВПС в панели агента (концепт «канал линка», §7.2).
    Канал не включён бандлом — строки нет вовсе: сказать о нём нечего, а
    «выключено» читалось бы как поломка."""
    from awgbot.runtime import linkclient
    if not linkclient.enabled():
        return ""
    online = linkclient.online()
    line = "🔗 Канал до сервера AWG: " + ("🟢 на связи" if online else "⚪ нет связи")
    role = linkclient.role() if online else ""
    if role:
        # Роль сообщает сервер: решает автомат переключения там, сам агент её
        # не знает. Канал оборван — роль из прошлой сессии могла смениться без
        # нас, поэтому её не показываем вовсе, а не выдаём старую за текущую.
        line += " · " + ("несёт трафик" if role == "active" else "в резерве")
    return line


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
    parts += head + ["", f"📡 Линк до {_e(st.server_name or 'сервера AWG')}: {_gw_link_line(st)}"]
    chan = channel_panel_line()
    if chan:
        parts.append(chan)
    mark = getattr(st, "mark_status", "") or ""
    if mark and mark != "confirmed":
        # Статусы производит ровно один источник — routing-gw-setup.sh:
        # unmarked | confirmed | foreign | unconfirmed. Токен пометки при живом
        # канале агент отправляет сам — просить переслать сообщение незачем.
        from awgbot.runtime import linkclient
        unmarked = ("🛰 Шлюз в основном боте не назначен — запрос на назначение отправлен "
                    "серверу AWG по каналу" if linkclient.online() else
                    "🛰 Шлюз в основном боте не назначен. Канал конфигурации сервера AWG "
                    "недоступен — перешли ему сообщение из отчёта")
        parts.append({"unmarked": unmarked,
                      "foreign": "⚠️ Шлюз этого слота — другое устройство, линк лежит",
                      "unconfirmed": "⚠️ Аплинк этой машины не найден — шлюз не подтверждён"}
                     .get(mark, f"🛰 Пометка: {_e(mark)}"))
    ssh_line = gateway_ssh_panel_line(getattr(st, "ssh", None) or {})
    if ssh_line:
        parts.append(ssh_line)
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
    parts += hw
    lan = getattr(st, "lan", None) or {}
    if lan:
        # локальная сеть без VPN (концепт «локальная сеть» §3.5): своим блоком
        bad = [c for c in st.checks if getattr(c, "group", "") == "lan" and c.ok is False]
        head_ = "🔴 " + ", ".join(c.name for c in bad[:3]) if bad else "🟢 работает"
        parts += ["", f"🏠 Локальная сеть без VPN: {head_}"]
        where = f"{_e(lan.get('iface', '') or '?')}, <code>{_e(lan.get('addr', '') or '?')}</code>"
        parts.append(f"{pad}локальная сеть: {where}")
        parts.append(f"{pad}трафик с роутера: {_packets(lan.get('lan_pkts'))}")
        parts.append(f"{pad}резолвер: апстрим {_e(lan.get('resolver', ''))}")
        parts.append(f"{pad}списки: {_lists_counts(lan)}; " + _lists_updated(lan.get("updated_at") or ""))
        parts.append(f"{pad}свои списки: {lan.get('own_vpn', 0)} в туннель, {lan.get('own_ru', 0)} напрямую")
        svc = lan.get("svc") or {}
        if svc.get("active"):
            # сервисы соседних сетей: работают сами вместе с доступом между подсетями
            parts.append(f"{pad}сервисы соседей: {_svc_peer_short(svc)}")
            parts.append(f"{pad}свои сервисы для соседей: {_svc_own_short(svc)}")
    parts += ["", f"🌡 Монитор здоровья: {_gw_health_summary(st.checks)}", ""]
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
        # детали — с хоста (вывод скрипта, имена интерфейсов): экранируем
        lines.append(f"{mark} {_e(c.name)}" + (f" — {_e(c.detail)}" if c.detail else ""))
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


def _svc_names(names: list) -> str:
    """«naspi5, backup и ещё 2» — имена с малины соседа, экранированные."""
    # в нижнем регистре — так их отдаёт dnsmasq и показывает Finder
    names = [str(n).lower() for n in names if n]
    shown = ", ".join(_e(n) for n in names[:3])
    more = len(names) - 3
    return shown + (f" и ещё {more}" if more > 0 else "")


def _svc_peer_short(svc: dict) -> str:
    names = svc.get("peer") or []
    if names:
        return f"{len(names)} SMB — {_svc_names(names)}"
    return "нет" if svc.get("ever") else "пока не пришли"


def _svc_own_short(svc: dict) -> str:
    if svc.get("browse") is False:
        return "нет avahi-browse (пакет avahi-utils)"
    if svc.get("avahi") is False:
        return "avahi-daemon не запущен"
    names = svc.get("own") or []
    return f"{len(names)} SMB" if names else "нет"


def gateway_services_paragraph(svc: dict) -> str:
    """Абзац экрана «Локальная сеть без VPN»: что видят соседи и как дойти по имени."""
    hosts = [h for h in (svc.get("peer_hosts") or []) if h]
    example = f"smb://{_e(hosts[0])}.awg.internal" if hosts else "smb://имя.awg.internal"
    return (f"Сервисы соседей: {_svc_peer_short(svc)}. На Mac они видны в Finder → «Сеть» → "
            f"awg.internal; с других устройств — по имени, например <code>{example}</code>. "
            "Свои SMB-серверы этой сети видны соседям так же.")


def _packets(pk) -> str:
    """«1 234 567 пакетов» — с разделителем разрядов и склонением; нет — «нет»."""
    n = int(pk or 0)
    if not n:
        return "нет"
    return f"{n:,}".replace(",", " ") + " " + plural_ru(n, "пакет", "пакета", "пакетов")


def _lists_counts(lan: dict) -> str:
    d, n = int(lan.get("domains", 0) or 0), int(lan.get("nets", 0) or 0)
    return (f"{d} " + plural_ru(d, "домен", "домена", "доменов") + ", "
            + f"{n} " + plural_ru(n, "подсеть", "подсети", "подсетей"))


def _lists_updated(raw: str) -> str:
    """«обновлены <когда>» или «ещё не обновлялись» — целой фразой: иначе
    склеивалось «обновлены ещё не обновлялись»."""
    from awgbot.util import timeutil
    if not raw:
        return "ещё не обновлялись"
    try:
        return "обновлены " + timeutil.fmt_dt(timeutil.parse_iso(raw))
    except ValueError:
        return "обновлены ?"


def gateway_lan_text(st) -> str:
    """🏠 Локальная сеть без VPN (концепт «локальная сеть» §3.5): что настроено, как
    дела со списками, откуда берутся личные."""
    lan = getattr(st, "lan", None) or {}
    return ("🏠 <b>Локальная сеть без VPN</b>\n\n"
            "Роутер маршрутизирует весь трафик локальной сети сюда, далее шлюз принимает роль "
            "маршрутизатора: домены и подсети из списков идут в туннель, остальное — напрямую. "
            "Личные списки: домен накрывает и все поддомены; правила «напрямую» приоритетнее "
            "правил «в туннель».\n\n"
            f"Интерфейс {_e(lan.get('iface', '') or '?')}, адрес <code>{_e(lan.get('addr', '') or '?')}</code>; "
            f"резолвер — апстрим <code>{_e(lan.get('resolver', '') or '?')}</code> через аплинк\n"
            f"Трафик с роутера: {_packets(lan.get('lan_pkts'))}\n"
            f"Списки: {_lists_counts(lan)}; {_lists_updated(lan.get('updated_at') or '')}\n"
            f"Свои списки: {lan.get('own_vpn', 0)} в туннель, {lan.get('own_ru', 0)} напрямую"
            + (("\n\n" + gateway_services_paragraph(lan["svc"]))
               if (lan.get("svc") or {}).get("active") else ""))


def gateway_lan_ask_domain(kind: str) -> str:
    head = {"add": "➕ <b>В туннель</b>", "ru": "➕ <b>Напрямую</b>"}[kind]
    return (f"{head}\n\nПришли домен (можно несколько через пробел). Схема и www. не нужны: "
            "<code>example.com</code>. Домен накрывает и все поддомены.")


def gateway_lan_rm_ask(domain: str, kind: str) -> str:
    where = "напрямую" if kind == "ru" else "в туннель"
    return (f"➖ <b>Убрать <code>{_e(domain)}</code> из своих списков?</b>\n\n"
            f"Сейчас домен идёт {where}; после удаления — как решат общие списки.")


def gateway_lan_own_text(items: list[tuple[str, str]]) -> str:
    """Сами домены — кнопками под инфобоксом («➖ домен (📤|🇷🇺)», с
    листанием); текст только объясняет порядок и значки."""
    if not items:
        return "📋 <b>Свои списки</b>\n\nПока пусто: добавь домены кнопками «➕ В туннель» и «➕ Напрямую»."
    return ("📋 <b>Свои списки</b>\n\n"
            "Элементы списка отсортированы: сначала показываются домены с маршрутом напрямую "
            "(помечены 🇷🇺), затем с маршрутом в туннель (помечены 📤)")


def gateway_lan_result(ok: bool, out: str) -> str:
    """Итог add/ru/del — строки скрипта «домен: добавлен / убран / уже в
    списке»; служебные строки про адреса в наборе (с отступом) не показываем."""
    rows = [r for r in out.strip().splitlines() if r and not r.startswith(" ")]
    # десятки доменов за раз переросли бы лимит сообщения, и Telegram отверг
    # бы ответ целиком
    keep, size = [], 0
    for r in rows:
        if size + len(r) > 3300:
            break
        keep.append(r)
        size += len(r) + 1
    out = "\n".join(keep)
    if len(rows) > len(keep):
        rest = len(rows) - len(keep)
        out += f"\n…и ещё {rest} " + plural_ru(rest, "строка", "строки", "строк")
    body = _e(out) if out else ("готово" if ok else "не удалось")
    return ("✅ " if ok else "⚠️ ") + body


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
    "от заданной на шлюзе.</b>\n\nПерезаписать фразу шлюза фразой с сервера AWG? Прежние "
    "копии шлюза останутся открываемыми только старой фразой. Если оставить свою — "
    "всё остальное из файла применится как обычно.")


def gateway_claim_via_channel_text(status: str) -> str:
    """Токен пометки ушёл серверу по каналу — пересылать ничего не нужно."""
    head = ("🛰 <b>Шлюз в основном боте не назначен.</b>" if status == "unmarked" else
            "⚠️ <b>Аплинк этой машины не найден.</b> Подтвердить шлюз нечем: подними аплинк "
            "и примени конфигурацию ещё раз." if status == "unconfirmed" else
            "⚠️ <b>Шлюз этого слота — другое устройство.</b> Линк на этой машине лежит: "
            "смени шлюз в настройках основного бота (🔁 Заменить устройство).")
    return (head + "\n\nЗапрос на назначение отправлен серверу AWG по каналу конфигурации: "
            "основной бот найдёт это устройство по ключу и выпустит конфигурацию — её примени "
            "здесь ещё раз.")


def gateway_claim_forward_text(token: str, status: str) -> str:
    head = ("🛰 <b>Шлюз в основном боте не назначен.</b>" if status == "unmarked" else
            "⚠️ <b>Аплинк этой машины не найден.</b> Подтвердить шлюз нечем: подними аплинк "
            "и примени конфигурацию ещё раз." if status == "unconfirmed" else
            "⚠️ <b>Шлюз этого слота — другое устройство.</b> Линк на этой машине лежит: "
            "смени шлюз в настройках основного бота (🔁 Заменить устройство).")
    return (head + "\n\nКанал конфигурации сервера AWG недоступен. Перешли это сообщение "
            "основному боту как есть: он найдёт это устройство по ключу, назначит его шлюзом "
            f"и выпустит конфигурацию; её примени здесь ещё раз.\n\n<code>{_e(token)}</code>")


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
        lines.append("линк лежит: шлюз этого слота — другое устройство")
    elif link == "unconfirmed":
        lines.append("линк не тронут: аплинк этой машины не найден")
    gs = st.get("GW_STATUS", "")
    if gs == "confirmed":
        lines.append("шлюз подтверждён")
    elif gs == "unmarked":
        lines.append("шлюз в основном боте не назначен")
    elif gs == "unconfirmed":
        lines.append("шлюз не подтверждён")
    if st.get("SSH_FILTER") == "1":
        n = int(st.get("SSH_ALLOW_COUNT") or 0)
        lines.append(f"фильтр SSH снаружи: включён, {n} " + plural_ru(n, "адрес", "адреса", "адресов"))
    elif st.get("SSH_FILTER") == "0":
        lines.append("фильтр SSH снаружи: выключен")
    if st.get("LAN") == "1":
        err = st.get("LAN_ERROR") or ""
        if err:
            lines.append(f"локальная сеть без VPN: не применена — {err}")
        else:
            lines.append("локальная сеть без VPN: применена"
                         + (f" ({st.get('LAN_IF')}, {st.get('LAN_ADDR')})" if st.get("LAN_IF") else ""))
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
            "обвязки с сервера AWG. Применение перепишет конфиг линка и переставит правила."
            if link_changed else
            "📦 <b>Получена конфигурация шлюза.</b>\n\nВнутри — конфиг линка и скрипт "
            "обвязки с сервера AWG. Конфиг линка не изменился — линк не перезапустится, "
            "правила будут переставлены.")
    return base + (f"\n\n{awg_restart_warning_body(True)}" if link_changed else "")


# ── 🛡 Доступ по SSH (концепт «доступ по SSH на шлюзе») ─────────────────────

def _owner_name(kind: str) -> str:
    return {"omv": "OMV", "generator": "другой процесс"}.get(kind, "")


_ALLOW_SHOWN = 12       # кнопки — до 8, текст — до 12: лимит 4096 при длинных именах


def gateway_ssh_text(st: dict) -> str:
    """Раздел: порт (факт) и его владелец, туннель, локальная сеть, снаружи,
    адреса, предупреждения — по месту. Блоки разделены пустой строкой;
    статусные строки — без точки в конце."""
    port = st.get("port")
    lines = ["<b>🛡 Доступ по SSH</b>", ""]
    if st.get("sshd_down"):
        lines.append(f"⚪ sshd не запущен. Порт в таблице: {port}.")
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
        lines.append("При включении функции «Доступ между подсетями» будет открыт доступ "
                     "из локальных подсетей других шлюзов")
    lines.append("")
    allow = st.get("allow") or []
    if not st.get("new_plumbing"):
        lines.append("⚠️ Обвязка шлюза старого образца: фильтр снаружи появится после "
                     "перевыпуска конфигурации шлюза с сервера и применения её здесь")
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
        names = ", ".join(_e(n) for n in unresolved[:_ALLOW_SHOWN])
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


GW_SSH_PORT_ASK = ("🅿️ <b>Порт SSH</b>\n\nПришли номер порта (1–65535). Занятый порт не возьму.\n\n"
                   "Переведу на него sshd и фильтр; текущие SSH-сеансы не рвутся — проверь вход "
                   "новым подключением. Проброс порта на роутере (при наличии) поправь сам.")


def gateway_ssh_owner_refusal(st: dict, listening: int | None) -> str:
    """Отказ смены порта на шлюзе — тот же текст, что у основного бота."""
    return ssh_owner_refusal(st, listening, place="шлюзе")


def gateway_ssh_port_changed(old: int, new: int) -> str:
    return (f"✅ Порт SSH изменён: {old} → <b>{new}</b>. Текущие SSH-сеансы не рвутся — проверь вход "
            f"новым подключением на порт {new}. Проброс порта на роутере (при наличии) поправь сам: снаружи "
            f"&lt;любой порт&gt; → шлюз:{new} (какой порт открыт снаружи, бот не знает).")


GW_SSH_ALLOW_ASK = ("➕ <b>Адреса для входа снаружи</b>\n\nПришли IP, подсеть или имя DynDNS "
                    "(можно несколько через пробел). Только IPv4: за роутером квартиры v6-проброса "
                    "нет. Имя буду резолвить сам и следить за сменой адреса.")


def gateway_ssh_allow_added(entries: list[str], merged: list[str] | None = None) -> str:
    """merged — записи, которые схлопнулись в добавленную подсеть (nft не
    принимает пересечения; человеку — что объединено, а не отказ)."""
    # адреса и подсети — моноширинным: жирный адрес Telegram превращает в ссылку
    text = "✅ Адреса для входа снаружи: добавлено " + ", ".join(f"<code>{_e(e)}</code>" for e in entries) \
        if entries else "✅ Адреса для входа снаружи"
    if merged:
        text += "; объединено с новой подсетью: " + ", ".join(f"<code>{_e(m)}</code>" for m in merged)
    return text


def gateway_ssh_del_ask(entry: str) -> str:
    return (f"➖ <b>Убрать <code>{_e(entry)}</code> из адресов для входа снаружи?</b>\n\n"
            "При включённом фильтре с этого адреса снаружи будет не зайти.")


GW_SSH_FILTER_OFF_ASK = ("🔴 <b>Выключить фильтр снаружи?</b>\n\nСнаружи SSH откроется всем, до кого "
                         "доходит проброс порта на роутере. Из туннеля и из локальной сети — без изменений.")


GW_SSH_ALLOW_ALREADY = "ℹ️ Всё из введённого уже в списке — ничего не менял."


def gateway_ssh_filter_on_ask(port: int) -> str:
    return (f"🟢 <b>Включить фильтр снаружи?</b>\n\nНа порт SSH шлюза ({port}) снаружи — то есть "
            "через проброс порта на роутере — будут пускаться только адреса из списка и сервер. "
            "Какой порт открыт снаружи, бот не знает и не проверяет. Из туннеля и из локальной сети "
            "доступ остаётся.\n\n"
            "Проверь после включения новым подключением снаружи. Если роутер подменяет адрес "
            "отправителя при пробросе, снаружи все выглядят роутером — тогда фильтр по адресам не "
            "работает: настрой проброс без подмены адреса.\n\nСписок пуст — снаружи останется только сервер.")


GW_SSH_FILTER_OFF = "Фильтр снят: снаружи SSH открыт всем"


def gateway_ssh_panel_line(ssh: dict) -> str:
    """Строка панели: порт (владелец) · снаружи."""
    if not ssh:
        return ""
    port = ssh.get("port")
    who = f" ({_owner_name(ssh.get('owner', ''))})" if ssh.get("owner") else ""
    if ssh.get("sshd_down"):
        head = "sshd не запущен"
    else:
        head = f"порт {port}{who}"
    if not ssh.get("new_plumbing"):
        outside = "снаружи: без фильтра (обвязка старого образца)"
    elif ssh.get("filter"):
        n = int(ssh.get("allow") or 0)
        outside = f"снаружи: фильтр, {n} " + plural_ru(n, "адрес", "адреса", "адресов")
    else:
        outside = "снаружи: открыт"
    return f"🛡 SSH: {head} · {outside}"
