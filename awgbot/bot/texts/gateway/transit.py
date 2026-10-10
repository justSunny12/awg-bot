"""transit.py — часть пакета gateway (см. __init__)."""
from __future__ import annotations

from awgbot.domain.gwchecks import failure_detail
from awgbot.bot import ui
from ..fmt import _e, plural_ru, num
from awgbot.bot.texts import gateway as pkg       # имена пакета — через него: их подменяют тесты


def _packets(pk) -> str:
    """«1 234 567 пакетов» — с разделителем разрядов и склонением; нет — «нет»."""
    n = int(pk or 0)
    if not n:
        return "нет пакетов"
    return num(n) + " " + plural_ru(n, "пакет", "пакета", "пакетов")


def _lists_counts(lan: dict) -> str:
    d, n = int(lan.get("domains", 0) or 0), int(lan.get("nets", 0) or 0)
    return (f"{num(d)} " + plural_ru(d, "домен", "домена", "доменов") + ", "
            + f"{num(n)} " + plural_ru(n, "подсеть", "подсети", "подсетей"))


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


def lan_about(shared: bool) -> str:
    """«Подробнее» экрана VPN-транзита; про синхронизацию своих списков — только
    когда упр. канал есть (без него списки живут на этом шлюзе, и экран об
    этом говорит строкой выше)."""
    return ("роутер маршрутизирует весь трафик локальной сети сюда, шлюз маршрутизирует: домены и "
            "подсети из списков — в туннель, остальное — напрямую;\n"
            + ("свои списки общие для всех шлюзов: добавленное или убранное здесь сервер AWG передаёт "
               "остальным — сразу, если они на связи, иначе при подключении;\n" if shared else "")
            + "введённый домен накрывает и все поддомены; правила «напрямую» приоритетнее правил "
            "«в туннель»;\nсначала показаны домены напрямую (🇷🇺), затем в туннель (🌍)")


def gateway_transit_text(st, items=None, own: dict | None = None) -> str:
    """Экран «🔀 VPN-транзит»: состояние, факты, списки, свои списки, SMB,
    объяснение под «подробнее», состояние синхронизации открыто. items —
    свои домены (для «пока пусто»), own — состояние синхронизации
    (services.own_status)."""
    from ..fmt import details
    lan = getattr(st, "lan", None) or {}
    own = own or lan.get("own") or {}
    bad = [c for c in st.checks if getattr(c, "group", "") == "lan" and c.ok is False]
    state = ui.st("bad", ", ".join(pkg._uniq(c.name for c in bad)[:3])) if bad else ui.st("ok", "работает")
    lines = [ui.head("🔀 VPN-транзит", state),
             f"<code>{_e(lan.get('iface', '') or '?')}</code> · <code>{_e(lan.get('addr', '') or '?')}</code> · "
             f"{pkg._packets(lan.get('lan_pkts'))} с роутера",
             f"DNS — <code>{_e(lan.get('resolver', '') or '?')}</code> через "
             f"<code>{_e(lan.get('uplink', '') or 'аплинк')}</code>",
             f"📋 Списки: {pkg._lists_counts(lan)} ({pkg._lists_updated_short(lan.get('updated_at') or '')})",
             pkg.own_lists_short(lan, plain=True)]
    if (lan.get("svc") or {}).get("active"):
        lines.append(pkg.smb_line(lan["svc"]))
    shared = bool(own.get("active"))
    about = pkg.lan_about(shared)
    if items is not None and not items:                 # подсказки пустого списка — под «подробнее»
        about = "добавь домены кнопками «➕ В туннель» и «➕ Напрямую»;\n" + about
    if shared:
        line = pkg.own_lists_state_line(own)
        if line:
            lines.append(line)
    lines.append(details(about))
    return "\n".join(lines)


def gateway_transit_ask_domain(kind: str) -> str:
    head = {"add": ui.head("➕ В туннель"), "ru": ui.head("➕ Напрямую")}[kind]
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
}


def gateway_transit_removed_toast(domain: str, shared: bool = False, sync: str = "") -> str:
    """Всплывашка после «➖»: итог и хвост синхронизации — только когда он
    правдив (другого шлюза нет — хвоста нет; shared оставлен для вызовов)."""
    tail = pkg.SYNC_TAILS.get(sync, "")
    # всплывашка — не длиннее 200 знаков: домен уступает место хвосту
    limit = min(80, 200 - len(": убран") - (len(tail) + 1 if tail else 0))
    return f"{pkg.short_name(domain, limit)}: убран" + (f"\n{tail}" if tail else "")


def short_name(name: str, limit: int = 80) -> str:
    """Имя во всплывашку: длинный домен или DynDNS — с многоточием, хвост
    строки («убран», про синхронизацию) остаётся целым."""
    return name if len(name) <= limit else name[:limit - 1] + "…"


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
    tail = pkg.SYNC_TAILS.get(sync, "")
    return ("✅ " if ok else "⚠️ ") + body + (f"\n{tail}" if tail else "")

