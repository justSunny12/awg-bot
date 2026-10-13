"""gateway.py — часть пакета notices (см. __init__): отчёт агента шлюза о
применении конфигурации — из статуса скрипта (его же показывает экран итога
применения)."""

from __future__ import annotations

from awgbot.util.textfmt import _e, plural_ru


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
            lines.append(f"VPN-транзит: не применён — {_e(err)}")
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
