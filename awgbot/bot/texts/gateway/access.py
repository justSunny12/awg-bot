"""access.py — часть пакета gateway (см. __init__)."""
from __future__ import annotations

from awgbot.bot import ui
from ..fmt import _e, plural_ru, more
from ..settings import warnings_block, address_list_line
from awgbot.bot.texts import gateway as pkg       # имена пакета — через него: их подменяют тесты


# ── 🛡 Доступ по SSH ─────────────────────

def _owner_name(kind: str) -> str:
    return {"omv": "OMV", "generator": "другой процесс"}.get(kind, "")


# Три состояния фильтра снаружи — одной формой строки: «[кружок] Снаружи
# (проброс порта на роутере): фильтр …»; у «выключен» кружка нет — это выбор,
# а не неисправность.
OUTSIDE = "Снаружи (проброс порта на роутере)"


def _nets_tail(nets: list, bot: str, payload: str, shown: int = 3) -> str:
    """«<code>a</code>, <code>b</code>, <code>c</code> и ещё N» — хвост ссылкой
    на экран со всем списком (без имени бота — текстом)."""
    from awgbot.util.textfmt import more_link
    out = ", ".join(f"<code>{_e(n)}</code>" for n in nets[:shown])
    more_n = len(nets) - shown
    if more_n > 0:
        out += more_link(more_n, bot, payload) if bot else f" и ещё {more_n}"
    return out


def gateway_ssh_text(st: dict, bot: str = "") -> str:
    """Раздел: порт (факт) и его владелец, туннель, локальная сеть, снаружи,
    адреса, предупреждения — по месту. Блоки разделены пустой строкой;
    статусные строки — без точки в конце. bot — имя бота шлюза: хвосты
    «и ещё N» — ссылками на экраны со всем списком."""
    port = st.get("port")
    lines = [ui.head("🛡 SSH-доступ"), ""]
    if st.get("sshd_down"):
        lines.append(f"🔴 sshd не запущен. Порт в таблице: {port}")
    elif st.get("owner") == "omv":
        lines.append(f"Порт SSH: {port} — <b>контролирует OMV</b> <i>(в OMV: Службы → SSH)</i>. "
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
    lines.append("Из локальной сети: открыт всегда" + (f" ({_nets_tail(lan, bot, 'list-sshfrom')})" if lan else ""))
    # подсети других шлюзов открыты сами, как только на сервере включён доступ
    # между подсетями: отдельного действия и списка адресов не нужно
    peers = st.get("peer_nets") or []
    if peers:
        lines.append(f"Из подсетей других шлюзов: открыт ({_nets_tail(peers, bot, 'list-sshfrom-2')})")
    else:
        lines.append("Из подсетей других шлюзов: откроется, когда VPN-транзит свяжет подсети")
    lines.append("")
    allow = st.get("allow") or []
    if not st.get("new_plumbing"):
        lines.append("⚠️ Обвязка шлюза старого образца: фильтр снаружи появится после «🚑 Восстановить»")
    elif st.get("filter") and not allow:
        # список пуст: снаружи не войти никому — не «только адреса из списка»
        lines.append(f"🔴 {OUTSIDE}: фильтр включён, список пуст — SSH открыт только серверу AWG; "
                     "тебе — из туннеля и локальной сети")
    elif st.get("filter"):
        lines.append(f"🟢 {OUTSIDE}: фильтр включён — только адреса из списка и сервер AWG")
    else:
        lines.append(f"{OUTSIDE}: фильтр выключен — открыт всем проброшенным")
    lines.append("")
    lines.append(address_list_line(len(allow), bool(st.get("filter"))))
    warns: list[str] = []
    unresolved = st.get("unresolved") or []
    if unresolved:
        held = st.get("held") or []
        link = (bot, "list-sshunres") if bot else None
        names = more(unresolved, link=link)
        tail = (" — держу прошлый адрес: " + more(held, link=(bot, "list-sshunres-2") if bot else None)
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
        warns.append(f"⚠️ В <code>{_e((st.get('owner_files') or ['sshd_config'])[0])}</code> сказано: "
                     f"«<i>{_e(st['owner_detail'])}</i>»")
    if st.get("omv_rules"):
        warns.append(f"⚠️ В OMV свои правила файервола ({st['omv_rules']}) — они работают вместе "
                     "с фильтром бота; при смене порта поправь и их (OMV: Сеть → Файервол)")
    if st.get("ufw"):
        warns.append("⚠️ ufw активен: второй владелец правил, новый порт открывай и в нём или выключи ufw")
    return "\n".join(lines + warnings_block(warns))


def gateway_ssh_port_changed(old: int, new: int, lan_addr: str = "") -> str:
    """lan_addr — адрес шлюза в локальной сети (статус обвязки): его человек
    вписывает в проброс на роутере; неизвестен — «шлюз:порт»."""
    target = f"<code>{_e(lan_addr)}:{new}</code>" if lan_addr else f"шлюз:{new}"
    return (f"✅ Порт SSH: {old} → {new}. Текущие сеансы не рвутся — проверь вход новым подключением "
            f"на порт {new}; проброс порта на роутере (при наличии) поправь сам: снаружи &lt;любой порт&gt; → {target}")


GW_SSH_ALLOW_ASK = (ui.head("➕ Адреса для SSH-доступа") + "\n\nПришли IP, подсеть или доменное имя через пробел, "
                    "запятую или с новой строки. IPv6 не подойдёт — фильтр на шлюзе работает только по IPv4; "
                    "адрес по имени буду обновлять сам")


SSH_ADDED_TITLE = "✅ Добавлено для SSH-доступа"
SSH_MERGED_TITLE = "Вошли в добавленные подсети"


def gateway_ssh_allow_added(entries: list[str], merged: list[str] | None = None, link: tuple | None = None,
                            link_merged: tuple | None = None) -> str:
    """merged — записи, которые схлопнулись в добавленную подсеть (nft не
    принимает пересечения; человеку — что объединено, а не отказ); link,
    link_merged — (имя бота, параметр /start) снимков всего списка для хвоста."""
    # адреса и подсети — моноширинным: жирный адрес Telegram превращает в ссылку
    text = "✅ Адреса для SSH-доступа: добавлено " + more(entries, link=link) \
        if entries else "✅ Адреса для SSH-доступа"
    if merged:
        text += "; вошли в неё и убраны из списка: " + more(merged, link=link_merged)
    return text


GW_SSH_ALLOW_ALREADY = "ℹ️ Всё из введённого уже в списке — ничего не менял"
GW_SSH_FILTER_ON_ALERT = ("Фильтр включён: снаружи — только адреса из списка и сервер AWG. Проверь вход новым "
                          "подключением; если роутер подменяет адрес при пробросе, фильтр по адресам "
                          "не сработает")


# включён при пустом списке: экран под всплывашкой — «🔴 … список пуст»,
# обещать «адреса из списка» нельзя
GW_SSH_FILTER_ON_EMPTY_ALERT = ("Фильтр включён, но список пуст: снаружи SSH открыт только серверу AWG — "
                                "добавь «➕ Адрес», чтобы входить снаружи со своих адресов")
GW_SSH_FILTER_OFF = "Фильтр выключен: снаружи SSH открыт всем проброшенным"


def gateway_ssh_removed(entry: str, now_empty: bool) -> str:
    """Всплывашка «➖»: последний адрес при включённом фильтре — с ценой,
    экран под ней переходит в «🔴 … список пуст»."""
    return f"✅ {entry}: убран" + (" — список пуст, снаружи SSH открыт только серверу AWG" if now_empty else "")


def gateway_ssh_panel_line(ssh: dict) -> str:
    """Строка панели: порт (владелец) · снаружи."""
    if not ssh:
        return ""
    port = ssh.get("port")
    who = f" ({pkg._owner_name(ssh.get('owner', ''))})" if ssh.get("owner") else ""
    if ssh.get("sshd_down"):
        return "🛡 SSH: sshd не запущен"
    if not ssh.get("new_plumbing"):
        outside = "без фильтра (обвязка старого образца)"
    elif ssh.get("filter"):
        n = int(ssh.get("allow") or 0)
        outside = (f"фильтр: {n} " + plural_ru(n, "адрес", "адреса", "адресов")) if n \
            else "фильтр: только сервер AWG"
    else:
        outside = "без фильтра"
    return f"🛡 SSH :{port}{who}, {outside}"
