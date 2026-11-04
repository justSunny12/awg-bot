"""dnsrec.py — часть пакета section (см. __init__): экран «🌐 DNS-запись»
(Т-Р16) и приглашения ввода токена провайдера. Строка записи в разделе без
пары — root.dns_line."""
from __future__ import annotations

from awgbot.util.textfmt.base import details

from ...fmt import _e
from ..common import _when


# ── Т-Р16: запись DNS ───────────────────────────────────────────────────────

DNS_MORE = ("▸ подробнее: Бот меняет только эту A-запись: при переезде кнопкой и при возврате — сам, при падении "
            "сервера — резерв, токеном из зашифрованных данных, которые он открывает только при переезде. После такого "
            "переезда бот попросит сменить токен. Раз в несколько минут бот сверяет запись и сообщит, если она "
            "указывает не сюда. Часть операторов связи держит запись в кэше дольше TTL — их абоненты переходят позже. "
            "Лучше дать токену право только на эту зону")


def dns_text(d: dict) -> str:
    name = d.get("name") or ""
    if not name:
        return ("🌐 <b>DNS-запись</b>\n\n⛔ Не задано доменное имя сервера. Задай домен в "
                "«🖥 Сервер AWG» — без него переезд невозможен")
    lines = [f"🌐 <b>DNS-запись</b> · <code>{_e(name)}</code>", ""]
    cur = d.get("current") or ""
    addrs = [a for a in (d.get("addrs") or []) if a]
    if cur:
        who = "этот сервер" if d.get("points_here") else "не этот сервер"
        shown = ", ".join(f"<code>{_e(a)}</code>" for a in addrs) if len(addrs) > 1 else f"<code>{_e(cur)}</code>"
        lines.append(f"Сейчас: {shown} — {who}" + (f" · проверено в {_e(_when(d.get('checked_at') or ''))}"
                                                  if d.get("checked_at") else ""))
        if len(addrs) > 1:
            lines.append(f"⚠️ У записи {len(addrs)} адреса — при переезде часть пользователей останется на старом; оставь одну A-запись")
        ttl = int(d.get("ttl") or 0)
        if ttl:
            lines.append(f"TTL: {ttl} с" + (f" — ⚠️ пользователи перейдут на новый адрес не раньше чем через {ttl // 60} мин; "
                                           f"поставь ≤ {int(d.get('ttl_warn') or 300)} с" if ttl > int(d.get("ttl_warn") or 300) else ""))
    else:
        lines.append("Сейчас: не проверена — «🧪 Проверить»")
    prov = d.get("provider_label") or "вручную"
    if d.get("provider") == "manual":
        lines.append("Провайдер: вручную — при переезде запись меняет человек, аварийный переезд невозможен")
    else:
        lines.append(f"Провайдер: {_e(prov)} · " + ("токен задан" if d.get("has_token") else "⚠️ токен не задан"))
    if d.get("check_ok") is False and d.get("check_why"):
        lines.append("⚠️ " + _e(d["check_why"]))
    lines += ["", details(DNS_MORE)]
    return "\n".join(lines)


def dns_token_prompt(provider: str, zone_hint: str) -> str:
    if provider == "dyndns2":
        return "✏️ <b>DynDNS2</b>\n\nПришли строку <code>https://логин:пароль@сервер/nic/update</code> — сообщение удалю сразу"
    if provider == "duckdns":
        return "✏️ <b>Токен DuckDNS</b>\n\nПришли токен из личного кабинета DuckDNS — сообщение удалю сразу"
    if provider == "digitalplat":
        return ("✏️ <b>Ключ DigitalPlat</b>\n\nПришли API-ключ <code>dp_live_…</code> из кабинета DigitalPlat One "
                "(«Аккаунт» → «API-ключи») с областями <code>dns.read</code> и <code>dns.write</code> — сообщение удалю сразу")
    return (f"✏️ <b>Токен Cloudflare</b>\n\nПришли API-токен с правом Zone → DNS → Edit на зону "
            f"<code>{_e(zone_hint)}</code> — сообщение удалю сразу")
