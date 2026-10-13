"""lanmap.py — часть пакета notices (см. __init__): адреса в туннеле локальных
сетей шлюзов — строка «выключены» (карточки слотов и замечания при запуске)
и разовое уведомление после обновления."""

from __future__ import annotations

import html

from awgbot.util.textfmt import addr_codes

_codes = addr_codes


def lan_space_off_line(problem: str, key: str = "routing.lan_space", *, markup: bool = True) -> str:
    """Адреса в туннеле выключены целиком: неисправно пространство или метка.
    problem — причина (lan_off), key — ключ conf, которым её чинить.
    markup=False — без разметки и значка: замечания при запуске экранируют
    текст сами."""
    if not markup:
        return f"адреса в туннеле выключены: {problem} — поправь {key} в app.yaml"
    return f"⚠️ Адреса в туннеле выключены: {_codes(problem)} — поправь <code>{html.escape(key)}</code> в app.yaml"


def lan_map_failed_line(error: str, *, markup: bool = True) -> str:
    """Таблица адресов в туннеле не поставилась (отказ nft): адреса в туннеле
    не работают, прежние маршруты настоящих подсетей бот не снимал."""
    err = " ".join(str(error).split())[:160]
    cmd = "journalctl -u awg-bot | grep 'адреса в туннеле'"
    if not markup:
        return f"адреса в туннеле не работают: таблица awg_lan_map не поставлена ({err}) — журнал: {cmd}"
    return (f"⚠️ Адреса в туннеле не работают: таблица awg_lan_map не поставлена "
            f"({html.escape(err, quote=False)}) — журнал: <code>{html.escape(cmd, quote=False)}</code>")


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " и " + names[-1]


def lan_peers_joined(names: list[str]) -> str:
    """Хвост уведомления после обновления: прежний тумблер связи подсетей был
    выключен, а шлюзов с VPN-транзитом два и больше — их сети теперь связаны.
    names — имена шлюзов готовой разметкой (ссылки на карточки)."""
    return ("Связь подсетей теперь часть VPN-транзита, отдельного выключателя больше нет: "
            f"сети {_and(names)} связаны")


LAN_PAIRS_SHOWN = 6           # пар «настоящая → в туннеле» на шлюз в сводке — как в карточке
LAN_PAIRS_SHOWN_MANY = 3      # шлюзов больше восьми — по три: 15 слотов с длинными именами влезают в 4096


def lan_space_moved(rows: list[tuple[str, list[tuple[str, str]]]], *, smb: bool, reissue: bool) -> str:
    """Уведомление после обновления: локальные сети шлюзов теперь открываются
    по адресам в туннеле. rows — [(имя шлюза готовой разметкой, [(настоящая,
    в туннеле)])], до шести пар на шлюз (шлюзов больше восьми — до трёх), дальше
    «и ещё N»; smb — записи SMB с
    адресами в туннеле стоят (имена уже ведут на новые); reissue — конфигу
    устройства админа при раздельном туннеле не хватает строки пространства:
    выданные раньше надо импортировать заново."""
    lines = ["🗺 Локальные сети шлюзов теперь открываются по адресам в туннеле:"]
    limit = LAN_PAIRS_SHOWN if len(rows) <= 8 else LAN_PAIRS_SHOWN_MANY
    for name, pairs in rows:
        shown = ", ".join(f"<code>{html.escape(r)}</code> → <code>{html.escape(t)}</code>"
                          for r, t in pairs[:limit])
        more = len(pairs) - limit
        lines.append(f" · {name}: {shown}" + (f" и ещё {more}" if more > 0 else ""))
    tail = "Реальные адреса сетей через туннель больше не доступны, актуальные см. в «🛰 Шлюзы» → карточка шлюза"
    if smb:
        tail += "; имена SMB (<code>awg.internal</code>) уже ведут на новые"
    if reissue:
        tail += (". На твоих устройствах с раздельным туннелем, выданных раньше, импортируй конфиг заново "
                 "(🔗 Ссылка, 🔳 QR или 📄 Файл в карточке устройства), чтобы адреса в туннеле работали и там")
    lines.append(tail)
    return "\n".join(lines)


__all__ = ["lan_space_off_line", "lan_map_failed_line", "lan_peers_joined", "lan_space_moved"]
