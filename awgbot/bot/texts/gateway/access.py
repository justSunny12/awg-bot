"""access.py — часть пакета gateway (см. __init__)."""
from __future__ import annotations

from awgbot.bot import ui
from ..fmt import _e, plural_ru, more
from ..settings import warnings_block, address_list_line
from awgbot.bot.texts import gateway as pkg       # имена пакета — через него: их подменяют тесты


# ── 🛡 Доступ по SSH ─────────────────────

def _owner_name(kind: str) -> str:
    return {"omv": "OMV", "generator": "другой процесс"}.get(kind, "")


def gateway_ssh_text(st: dict) -> str:
    """Раздел: порт (факт) и его владелец, туннель, локальная сеть, снаружи,
    адреса, предупреждения — по месту. Блоки разделены пустой строкой;
    статусные строки — без точки в конце."""
    port = st.get("port")
    lines = [ui.head("🛡 SSH-доступ"), ""]
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
        lines.append("⚠️ Обвязка шлюза старого образца: фильтр снаружи появится после «🔧 Восстановить»"
                     if st.get("server_cfg1") else
                     "⚠️ Обвязка шлюза старого образца: фильтр снаружи появится после "
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
        names = more(unresolved)
        tail = (" — держу прошлый адрес: " + more(held)
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


def gateway_ssh_port_changed(old: int, new: int) -> str:
    return (f"✅ Порт SSH: {old} → {new}. Текущие сеансы не рвутся — проверь вход новым подключением "
            f"на порт {new}; проброс порта на роутере (при наличии) поправь сам: снаружи &lt;любой порт&gt; → шлюз:{new}")


GW_SSH_ALLOW_ASK = (ui.head("➕ Адреса для SSH-доступа") + "\n\nПришли IP, подсеть или доменное имя через пробел. "
                    "Только IPv4: проброса IPv6 через роутер нет; имя буду резолвить сам")


def gateway_ssh_allow_added(entries: list[str], merged: list[str] | None = None) -> str:
    """merged — записи, которые схлопнулись в добавленную подсеть (nft не
    принимает пересечения; человеку — что объединено, а не отказ)."""
    # адреса и подсети — моноширинным: жирный адрес Telegram превращает в ссылку
    text = "✅ Адреса для SSH-доступа: добавлено " + more(entries) \
        if entries else "✅ Адреса для SSH-доступа"
    if merged:
        text += "; объединено с новой подсетью: " + more(merged)
    return text


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
        outside = "открыт"
    return f"🛡 SSH :{port}{who}, {outside}"
