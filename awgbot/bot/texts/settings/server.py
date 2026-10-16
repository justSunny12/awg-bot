"""server.py — часть пакета settings (см. __init__): раздел сервера и свой DNS-резолвер."""

from __future__ import annotations

from awgbot.bot import ui
from ..fmt import _e, details


def _looks_like_domain(host: str) -> bool:
    import ipaddress
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return True


def _private_dns_note(p: dict) -> str:
    """Хвост строки DNS — чей это резолвер."""
    mode = p.get("mode")
    if mode == "private":
        return " — свой резолвер"
    if mode == "public":
        if p.get("decision") == "pending":
            return " — публичный; при переезде станет свой"
        return " — публичный"
    return ""


PRIVATE_DNS_WHAT = (
    "Сервер умеет отвечать сам — dnsmasq на собственном адресе в туннеле — и это даёт три вещи:\n"
    "• РФ-доступ у всех, а не «через раз»: с публичным адресом Chrome и Edge молча уходят на DoH "
    "мимо сервера, список адресов РФ-доступа не наполняется, и у такого клиента российские сайты "
    "не открываются;\n"
    "• DNS-запросы клиентов не уходят третьим лицам открытым текстом — резолвит сервер, наружу "
    "идёт уже от него, с кэшем;\n"
    "• защита от обхода через DoH и от DNS rebinding — сразу, без шлюза.\n"
    "Цена: адрес DNS вшит в каждую выданную ссылку, поэтому переход — это переезд профилей: рядом "
    "поднимется новый интерфейс, каждое устройство получит вторую конфигурацию — пользователи "
    "применят её, когда им будет удобно; прежние конфигурации продолжат работать до завершения переезда")

PRIVATE_DNS_BLOCKED_LINE = "🚚 Переехать сейчас нельзя: {reason}"


def private_dns_offer(target: str, blocked: str = "") -> str:
    """Экран решения (и инфобокс при старте): что даёт и как перейти; blocked —
    почему переезд сейчас невозможен (migration_blocked_reason: идёт другой
    или второй интерфейс уже поднят), кнопки «сейчас» нет — и сказано почему."""
    return (ui.head("🔒 Свой DNS-резолвер", "сейчас публичный") + "\n"
            f"Свой — <code>{_e(target)}</code>: РФ-доступ у всех, запросы не уходят третьим лицам, защита от "
            "обхода через DoH. Цена — переезд профилей"
            + ("\n" + PRIVATE_DNS_BLOCKED_LINE.format(reason=_e(blocked)) if blocked else "")
            + details(PRIVATE_DNS_WHAT))


# всплывашки решений: экран на месте — «🖥 Сервер AWG», где решение видно строкой DNS
PRIVATE_DNS_LATER = "Записал: свой резолвер — со следующим переездом профилей"
PRIVATE_DNS_DISMISSED = "DNS клиентов остаётся публичным — передумать можно кнопкой «🔒 Свой резолвер»"
def private_dns_blocked_toast(reason: str) -> str:
    """Всплывашка «🚚 Переехать сейчас» при закрытом переезде — с настоящей причиной."""
    return ui.toast(f"Переехать сейчас нельзя: {reason}")


def settings_server_text(d: dict) -> str:
    """Раздел «Сервер»: имя сервера и домен (или IP) заголовком, пустая строка,
    ядро, DNS, интерфейс — то, что уезжает в новые ссылки, и «подробнее» про
    переезд."""
    host = d.get("host") or ""
    kernel = _e(d.get("kernel") or "не определено")
    gen = f" · поколение AWG gen{d['generation']}" if d.get("generation") else ""
    dns_note = _private_dns_note(d.get("private_dns") or {})
    lines = [
        ui.head(f"🖥 {_e(d['name'])}")
        + (f" · <code>{_e(host)}</code>" + ("" if _looks_like_domain(host) else " (домена нет — в ссылках IP)")
           if host else " · адрес сервера не задан"),
        "",
        f"Ядро {kernel}{gen}",
        f"DNS {', '.join(f'<code>{_e(x.strip())}</code>' for x in str(d['dns']).split(','))}{dns_note} · MTU {d['mtu']} · keepalive {_e(str(d['keepalive']).replace('-', '–'))}",
        f"{_e(d['iface'])} · порт {d['port']} · <code>{_e(d['subnet'])}</code>",
    ]
    warns = []
    conf_port = d.get("port_conf")
    if conf_port and d.get("port") and int(conf_port) != int(d["port"]):
        warns.append(f"⚠️ В конфиге бота порт {conf_port}, а интерфейс слушает {d['port']}: "
                     "ссылки идут с портом интерфейса, проверки смотрят в конфиг — поправь <code>app.yaml</code>")
    blocked = d.get("migration_blocked") or ""
    if blocked:
        warns.append(f"🚚 Сменить порт или подсеть сейчас нельзя: {_e(blocked)}")
    lines += warns
    lines.append(details("Правки уходят только в новые ссылки: старые несут те параметры, с которыми "
                         "их выдали.\nПорт и подсеть меняются переездом: каждое устройство получит вторую "
                         "конфигурацию, пользователи применят её, когда им будет удобно"))
    return "\n".join(lines)
