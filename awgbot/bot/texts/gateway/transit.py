"""transit.py — часть пакета gateway (см. __init__)."""
from __future__ import annotations

from awgbot.util.textfmt import more_count

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
    """«обн. <когда>» после списков (строка одна со своими списками — коротко);
    ещё не обновлялись — так и пишем."""
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
    когда канал конфигурации есть (без него списки живут на этом шлюзе, и экран об
    этом говорит строкой выше)."""
    return ("роутер отправляет трафик локальной сети на шлюз; шлюз пускает домены и подсети из списков "
            "в туннель (через сервер AWG), остальное — напрямую (через провайдера);\n"
            + ("свои списки общие для всех шлюзов: добавленное или убранное здесь сервер AWG передаёт "
               "остальным — сразу, если они на связи, иначе при подключении;\n" if shared else "")
            + "домен действует и на поддомены; если домен есть в обоих списках — побеждает «напрямую»")


def gateway_transit_text(st, items=None, own: dict | None = None, virtual: list | None = None, bot: str = "") -> str:
    """Экран «🔀 VPN-транзит»: состояние, факты, списки, свои списки, SMB,
    объяснение под «подробнее», состояние синхронизации открыто. items —
    свои домены (для «пока пусто»), own — состояние синхронизации
    (services.own_status), virtual — адреса в туннеле своей сети из `role`
    (сервер старше 3.3.0 их не шлёт — строки нет)."""
    from ..fmt import details
    lan = getattr(st, "lan", None) or {}
    own = own or lan.get("own") or {}
    bad = [c for c in st.checks if getattr(c, "group", "") == "lan" and c.ok is False]
    names = pkg._uniq(c.name for c in bad)
    rest = more_count(len(names) - 3)           # все — на «🩺 Здоровье»
    state = ui.st("bad", ", ".join(names[:3]) + rest) if bad else ui.st("ok", "работает")
    lines = [ui.head("🔀 VPN-транзит", state),
             f"<code>{_e(lan.get('iface', '') or '?')}</code> · <code>{_e(lan.get('addr', '') or '?')}</code> · "
             f"{pkg._packets(lan.get('lan_pkts'))} с роутера",
             *([lan_virtual_line(virtual, bot)] if virtual else []),
             f"DNS — <code>{_e(lan.get('resolver', '') or '?')}</code> через "
             + (f"<code>{_e(lan['uplink'])}</code>" if lan.get("uplink") else "аплинк"),
             # одной строкой с общими: «📋 Списки: … · обн. 13.10 01:27 · свои: 1 в туннель»;
             # своих нет — хвоста нет (решение 16.10.2026)
             f"📋 Списки: {pkg._lists_counts(lan)} · {pkg._lists_updated_short(lan.get('updated_at') or '')}"
             + (f" · {own_tail}" if (own_tail := pkg.own_lists_short(lan)) else "")]
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


def lan_virtual_line(virtual: list, bot: str = "") -> str:
    """«в туннеле — 10.240.1.0/24»: как сеть видна снаружи шлюза; больше
    шести — «и ещё N» ссылкой на экран «🔀 Адреса в туннеле» (без имени бота
    — текстом)."""
    from awgbot.util import listlink
    from awgbot.util.textfmt import more_link
    shown = ", ".join(f"<code>{_e(str(n))}</code>" for n in virtual[:6])
    return f"Адреса в туннеле — {shown}" + more_link(len(virtual) - 6, bot, listlink.build("tunaddr"))


def gateway_transit_ask_domain(kind: str) -> str:
    """Приглашение: куда пойдут домены — первой фразой, иначе «В туннель» и
    «Напрямую» отличаются только заголовком."""
    head, where = {"add": (ui.head("➕ 🌍 В туннель"), "в туннель, через сервер AWG"),
                   "ru": (ui.head("➕ 🇷🇺 Напрямую"), "напрямую, через провайдера, мимо туннеля")}[kind]
    return (f"{head}\n\nПришли домены через пробел, запятую или с новой строки — они пойдут {where}. "
            "<code>example.com</code> действует и на поддомены")


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
        return head + " · 🟡 ждут синхронизации"
    if state in ("failed", "old_script"):
        return head + " · ⚠️ не применились (🩺 Здоровье)"
    return head


def own_lists_state_line(own: dict) -> str:
    """Строка состояния синхронизации — только при расхождении."""
    state = own.get("state", "")
    n = int(own.get("pending") or 0)
    n_word = f"{n} " + plural_ru(n, "правка", "правки", "правок")
    if state == "no_link":
        return f"🟡 Ждут синхронизации: {n_word} — нет связи с сервером AWG"
    if state in ("pending", "stale_server"):
        return f"🟡 Ждут синхронизации: {n_word} — сервер AWG ещё не ответил"
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


OWN_FAIL_TAIL = " — «🚑 Восстановить»; детали ошибки смотри в «🩺 Здоровье»"


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


_NOT_DOMAIN = ": не похоже на домен, пропущен"


def _clean(r: str) -> str:
    """Домены в строках итога скрипт сам обернул в <code> (иначе Telegram делает
    из них ссылки): остальное экранируем, его теги возвращаем."""
    return _e(r).replace("&lt;code&gt;", "<code>").replace("&lt;/code&gt;", "</code>")


def _result_line(row: str) -> str:
    """Строка итога скрипта — готовой разметкой, со значком по исходу: добавлен/убран
    — ✅, уже в списке / в списках нет — ℹ️, пропущен / нельзя — ⚠️. Строку «не
    похоже на домен» скрипт печатает с вводом человека как есть: экранируем его
    целиком и оборачиваем в <code> сами — <code> из ввода в разметку не вернётся."""
    if row.endswith(_NOT_DOMAIN):
        return "⚠️ <code>" + _e(row[:-len(_NOT_DOMAIN)]) + "</code>: не домен, пропущен"
    row = _clean(row)
    if row.endswith((": добавлен", ": убран")):
        return "✅ " + row
    if row.endswith((": уже в списке", ": в списках нет")):
        return "ℹ️ " + row
    if row.endswith(": это адрес самого сервера AWG — в туннель его не направить"):
        return "⚠️ " + row
    return row


BATCH_SUMMARY_FROM = 6        # с такого числа доменов итог — сводкой, перечень только пропущенных


def _result_parts(out: str, cmd: str) -> tuple[list[str], list[str]]:
    """(сводка, перечень) из вывода скрипта — один разбор для итога на экране и
    снимка: строки «домен: исход» без служебных (с отступом); от
    BATCH_SUMMARY_FROM доменов (кроме del) — сводка «✅ Добавлено: 80 · уже в
    списке: 3 · пропущено: 2» (нули не выводим) и перечень только пропущенных.
    Строки перечня — готовые, со значками и экранированием."""
    rows = [r for r in out.strip().splitlines() if r and not r.startswith(" ")]
    if len(rows) >= BATCH_SUMMARY_FROM and cmd != "del":
        added = sum(1 for r in rows if r.endswith(": добавлен"))
        known = sum(1 for r in rows if r.endswith(": уже в списке"))
        skipped = [r for r in rows if not r.endswith((": добавлен", ": уже в списке"))]
        parts = [f"✅ Добавлено: {added}"]
        if known:
            parts.append(f"уже в списке: {known}")
        if skipped:
            parts.append(f"пропущено: {len(skipped)}")
        return [" · ".join(parts)], [_result_line(r) for r in skipped]
    return [], [_result_line(r) for r in rows]


def _fit(summary: list[str], rows: list[str], budget: int) -> list[str]:
    """Сколько строк перечня влезает в budget знаков после сводки (итог идёт
    первой строкой экрана, см. note_budget)."""
    keep, size = [], sum(len(ln) + 1 for ln in summary)
    for line in rows:
        if size + len(line) > budget:
            break
        keep.append(line)
        size += len(line) + 1
    return keep


def transit_result_cut(ok: bool, out: str, budget: int = 3300, cmd: str = "add") -> int:
    """Сколько строк перечня в итог не влезает: больше нуля — обработчик пишет
    снимок всех строк (transit_result_rows) и даёт хвосту ссылку на него."""
    if not ok:
        return 0
    summary, rows = _result_parts(out, cmd)
    return len(rows) - len(_fit(summary, rows, budget))


def _fail_reason(out: str, cmd: str) -> str:
    """Причина отказа скрипта для человека: «откатываю» в выводе значит, что
    dnsmasq не перезапустился с новым списком и прежний вернулся."""
    text = out.strip().replace("<code>", "").replace("</code>", "")
    if "откатываю" in text:
        return "dnsmasq не перезапустился с новым списком — список остался прежним"
    # «домен: причина» — причину без домена, он в заголовке
    rows = [r for r in text.splitlines() if r.strip()]
    last = rows[-1] if rows else "не удалось"
    return last.split(": ", 1)[1] if ": " in last and not last.startswith(("dnsmasq", "скрипт")) else last


def transit_result_title(cmd: str, out: str = "") -> str:
    """Заголовок снимка строк итога (экран «весь список»); при сводке (от
    BATCH_SUMMARY_FROM доменов) в снимок идут только пропущенные — «…, не добавленные»."""
    head = "🔀 Итог: напрямую" if cmd == "ru" else "🔀 Итог: в туннель"
    rows = [r for r in out.strip().splitlines() if r and not r.startswith(" ")]
    return head + (", не добавленные" if out and len(rows) >= BATCH_SUMMARY_FROM and cmd != "del" else "")


def transit_result_rows(ok: bool, out: str, cmd: str = "add") -> list[str]:
    """Все строки перечня итога, как на экране, — для снимка, когда итог не влез."""
    return _result_parts(out, cmd)[1] if ok else []


def gateway_transit_result(ok: bool, out: str, sync: str = "", budget: int = 3300,
                           domains=(), cmd: str = "add", link: tuple | None = None) -> str:
    """Итог add/ru/del — строки скрипта «домен: добавлен / убран / уже в
    списке» со значками по исходу; служебные строки про адреса в наборе (с
    отступом) не показываем. Больше BATCH_SUMMARY_FROM доменов — сводка
    «✅ Добавлено: 80 · пропущено: 2» (нулевые части опущены) и перечень только
    пропущенных (_result_parts). Отказ — «⚠️ example.org не добавлен: <причина>»
    (всплывашка у «➖», без разметки). sync — хвост про синхронизацию: "online" |
    "offline" | "". budget — предел в знаках (итог идёт первой строкой
    экрана, см. note_budget). link — (имя бота, параметр /start) снимка всех строк:
    «…и ещё N строк» — ссылкой на него (без имени бота — текстом)."""
    tail = pkg.SYNC_TAILS.get(sync, "")
    if not ok:
        names = ", ".join(domains) if domains else ""
        what = ("не убран" if cmd == "del" else
                ("не добавлены" if len(domains) > 1 else "не добавлен"))
        head = f"{pkg.short_name(names, 120)} {what}" if names else ("Не убран" if cmd == "del" else "Не добавлен")
        return f"⚠️ {_e(head)}: {_e(_fail_reason(out, cmd))}" + (f"\n\n{tail}" if tail else "")
    summary, rows = _result_parts(out, cmd)
    keep = _fit(summary, rows, budget)
    body = "\n".join(summary + keep)
    rest = len(rows) - len(keep)
    if rest > 0:
        from awgbot.util.textfmt import deep_link
        label = f"и ещё {rest} " + plural_ru(rest, "строка", "строки", "строк")
        body += "\n…" + (deep_link(link[0], link[1], label) if link is not None else label)
    if not body:
        body = "✅ готово"
    return body + (f"\n\n{tail}" if tail else "")   # хвост синхронизации — абзацем
