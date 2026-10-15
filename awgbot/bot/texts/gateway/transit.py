"""transit.py — часть пакета gateway (см. __init__)."""
from __future__ import annotations

from awgbot.domain import gwownlists
from awgbot.domain.gwchecks import WRITE_ERROR, failure_detail
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
    """«обновлены <когда>» после списков; ещё не обновлялись — так и пишем."""
    from awgbot.util import timeutil
    if not raw:
        return "ещё не обновлялись"
    try:
        return "обновлены " + timeutil.fmt_dt_ui(timeutil.parse_iso(raw))
    except ValueError:
        return "обновлены ?"


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
    когда канал конфигурации есть (без него списки живут на этом шлюзе, и экран об
    этом говорит строкой выше)."""
    return ("роутер отправляет трафик локальной сети на шлюз; шлюз пускает домены и подсети из списков "
            "в туннель (через сервер AWG), остальное — напрямую (через провайдера);\n"
            + ("свои списки общие для всех шлюзов: добавленное или убранное здесь сервер AWG передаёт "
               "остальным — сразу, если они на связи, иначе при подключении;\n" if shared else "")
            + "домен действует и на поддомены; если домен есть в обоих списках — побеждает «напрямую»")


def gateway_transit_text(st, items=None, own: dict | None = None, virtual: list | None = None) -> str:
    """Экран «🔀 VPN-транзит»: состояние, факты, списки, свои списки, SMB,
    объяснение под «подробнее», состояние синхронизации открыто. items —
    свои домены (для «пока пусто»), own — состояние синхронизации
    (services.own_status), virtual — адреса в туннеле своей сети из `role`
    (сервер старше 3.3.0 их не шлёт — строки нет)."""
    from ..fmt import details
    lan = getattr(st, "lan", None) or {}
    own = own or lan.get("own") or {}
    bad = [c for c in st.checks if getattr(c, "group", "") == "lan" and c.ok is False]
    state = ui.st("bad", ", ".join(pkg._uniq(c.name for c in bad)[:3])) if bad else ui.st("ok", "работает")
    lines = [ui.head("🔀 VPN-транзит", state),
             f"<code>{_e(lan.get('iface', '') or '?')}</code> · <code>{_e(lan.get('addr', '') or '?')}</code> · "
             f"{pkg._packets(lan.get('lan_pkts'))} с роутера",
             *([lan_virtual_line(virtual)] if virtual else []),
             f"DNS — <code>{_e(lan.get('resolver', '') or '?')}</code> через "
             + (f"<code>{_e(lan['uplink'])}</code>" if lan.get("uplink") else "аплинк"),
             f"📋 Списки: {pkg._lists_counts(lan)} · {pkg._lists_updated_short(lan.get('updated_at') or '')}",
             pkg.own_lists_short(lan, plain=True)]
    if (lan.get("svc") or {}).get("active"):
        lines.append(pkg.smb_line(lan["svc"]))
    shared = bool(own.get("active"))
    about = pkg.lan_about(shared)
    if items is not None and not items:                 # подсказки пустого списка — под «подробнее»
        about = "добавь домены кнопками «➕ 🌍 В туннель» и «➕ 🇷🇺 Напрямую»;\n" + about
    if shared:
        line = pkg.own_lists_state_line(own)
        if line:
            lines.append(line)
    lines.append(details(about))
    return "\n".join(lines)


def lan_virtual_line(virtual: list) -> str:
    """«в туннеле — 10.240.1.0/24»: как сеть видна снаружи шлюза; больше
    шести — «и ещё N»."""
    shown = ", ".join(f"<code>{_e(str(n))}</code>" for n in virtual[:6])
    return f"Адреса в туннеле — {shown}" + (f" и ещё {len(virtual) - 6}" if len(virtual) > 6 else "")


def gateway_transit_ask_domain(kind: str) -> str:
    """Приглашение: куда пойдут домены — первой фразой, иначе «В туннель» и
    «Напрямую» отличаются только заголовком."""
    head, where = {"add": (ui.head("➕ 🌍 В туннель"), "в туннель — через сервер AWG"),
                   "ru": (ui.head("➕ 🇷🇺 Напрямую"), "напрямую — через провайдера, мимо туннеля")}[kind]
    return (f"{head}\n\nЭти домены пойдут {where}. Пришли домены через пробел: "
            "<code>example.com</code> — действует и на поддомены")


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
        err = own.get("err") or "скрипт старого образца — обвязка перевыставляется"
        detail = failure_detail("Не применились", err)
        # поломка записи (нет места) чинится не обвязкой — хвост только отказу скрипта
        if err.startswith(WRITE_ERROR):
            tail = " — освободи место на шлюзе" if "нет места" in err else ""
        else:
            tail = "" if state == "old_script" else OWN_FAIL_TAIL
        return "⚠️ " + _e(detail[:1].upper() + detail[1:]) + tail
    if state == "rejected" and own.get("rej"):
        d, why = own["rej"][0]
        return f"⚠️ Сервер AWG не принял: <code>{_e(d)}</code> — {_e(gwownlists.reject_human(why))}"
    return ""


OWN_FAIL_TAIL = " — «🔧 Восстановить»; не поможет — перевыпусти конфигурацию шлюза с сервера AWG"


SYNC_TAILS = {
    "online": "Изменения синхронизируются с другими шлюзами",
    "offline": "Изменения будут синхронизированы с другими шлюзами, когда появится связь с сервером AWG",
}


def gateway_transit_removed_toast(domain: str, shared: bool = False, sync: str = "") -> str:
    """Всплывашка после «➖»: итог и хвост синхронизации — только когда он
    правдив (другого шлюза нет — хвоста нет; shared оставлен для вызовов)."""
    tail = pkg.SYNC_TAILS.get(sync, "")
    # всплывашка — не длиннее 200 знаков: домен уступает место хвосту
    limit = min(80, 200 - len("✅ : убран") - (len(tail) + 1 if tail else 0))
    return f"✅ {pkg.short_name(domain, limit)}: убран" + (f"\n{tail}" if tail else "")


def short_name(name: str, limit: int = 80) -> str:
    """Имя во всплывашку: длинный домен или DynDNS — с многоточием, хвост
    строки («убран», про синхронизацию) остаётся целым."""
    return name if len(name) <= limit else name[:limit - 1] + "…"


def _result_line(row: str) -> str:
    """Строка итога скрипта — со значком по исходу: добавлен/убран — ✅,
    уже в списке / в списках нет — ℹ️, пропущен / нельзя — ⚠️."""
    if row.endswith((": добавлен", ": убран")):
        return "✅ " + row
    if row.endswith((": уже в списке", ": в списках нет")):
        return "ℹ️ " + row
    if row.endswith(": не похоже на домен, пропущен"):
        return "⚠️ " + row[:-len("не похоже на домен, пропущен")] + "не домен, пропущен"
    if row.endswith(": это адрес самого сервера AWG — в туннель его не направить"):
        return "⚠️ " + row
    return row


BATCH_SUMMARY_FROM = 6        # с такого числа доменов итог — сводкой, перечень только пропущенных


def _fail_reason(out: str, cmd: str) -> str:
    """Причина отказа скрипта для человека: «откатываю» в выводе значит, что
    dnsmasq не перезапустился с новым списком и прежний вернулся."""
    text = out.strip().replace("<code>", "").replace("</code>", "")
    if "откатываю" in text:
        return ("dnsmasq не перезапустился — список остался прежним" if cmd == "del"
                else "dnsmasq не перезапустился с новым списком — вернул прежний")
    # «домен: причина» — причину без домена, он в заголовке
    rows = [r for r in text.splitlines() if r.strip()]
    last = rows[-1] if rows else "не удалось"
    return last.split(": ", 1)[1] if ": " in last and not last.startswith(("dnsmasq", "скрипт")) else last


def gateway_transit_result(ok: bool, out: str, sync: str = "", budget: int = 3300,
                           domains=(), cmd: str = "add") -> str:
    """Итог add/ru/del — строки скрипта «домен: добавлен / убран / уже в
    списке» со значками по исходу; служебные строки про адреса в наборе (с
    отступом) не показываем. Больше BATCH_SUMMARY_FROM доменов — сводка
    «✅ Добавлено: 80 · уже в списке: 0 · пропущено: 0» и перечень только
    пропущенных. Отказ — «⚠️ example.org не добавлен: <причина>» (всплывашка
    у «➖», без разметки). sync — хвост про синхронизацию: "online" |
    "offline" | "". budget — предел в знаках (итог идёт первой строкой
    экрана, см. note_budget)."""
    tail = pkg.SYNC_TAILS.get(sync, "")
    if not ok:
        names = ", ".join(domains) if domains else ""
        what = ("не убран" if cmd == "del" else
                ("не добавлены" if len(domains) > 1 else "не добавлен"))
        head = f"{pkg.short_name(names, 120)} {what}" if names else ("Не убран" if cmd == "del" else "Не добавлен")
        return f"⚠️ {_e(head)}: {_e(_fail_reason(out, cmd))}" + (f"\n{tail}" if tail else "")
    rows = [r for r in out.strip().splitlines() if r and not r.startswith(" ")]
    # Домены в строках итога скрипт сам обернул в <code> (иначе Telegram делает из
    # них ссылки): остальное экранируем, свои теги пропускаем.
    def clean(r):
        return _e(r).replace("&lt;code&gt;", "<code>").replace("&lt;/code&gt;", "</code>")
    if len(rows) >= BATCH_SUMMARY_FROM and cmd != "del":
        added = sum(1 for r in rows if r.endswith(": добавлен"))
        known = sum(1 for r in rows if r.endswith(": уже в списке"))
        skipped = [r for r in rows if not r.endswith((": добавлен", ": уже в списке"))]
        lines = [f"✅ Добавлено: {added} · уже в списке: {known} · пропущено: {len(skipped)}"]
        rows = skipped
    else:
        lines = []
    keep, size = [], sum(len(ln) + 1 for ln in lines)
    for r in rows:
        line = _result_line(r)
        if size + len(line) > budget:
            break
        keep.append(line)
        size += len(line) + 1
    body = "\n".join(lines + [clean(r) for r in keep])
    if len(rows) > len(keep):
        rest = len(rows) - len(keep)
        body += f"\n…и ещё {rest} " + plural_ru(rest, "строка", "строки", "строк")
    if not body:
        body = "✅ готово"
    return body + (f"\n{tail}" if tail else "")
