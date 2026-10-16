"""lanmap.py — часть пакета notices (см. __init__): адреса в туннеле локальных
сетей шлюзов — строка «выключены» (карточки слотов и замечания при запуске)
и строка отказа таблицы."""

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
    return (f"⚠️ Адреса в туннеле выключены: {_codes(problem)} — поправь <code>{html.escape(key)}</code> "
            "в <code>app.yaml</code>")


def lan_map_failed_line(error: str, *, markup: bool = True) -> str:
    """Таблица адресов в туннеле не поставилась (отказ nft): адреса в туннеле
    не работают, прежние маршруты настоящих подсетей бот не снимал. В карточке
    (markup) сырой ответ nft не показывается — он в журнале, на который
    строка и ведёт; замечание при запуске (без разметки) несёт его целиком."""
    from awgbot.infra.routing.lanmap import LOG_PREFIX
    err = " ".join(str(error).split())[:160]
    cmd = f"journalctl -u awg-bot | grep '{LOG_PREFIX.rstrip(':')}'"
    if not markup:
        return f"адреса в туннеле не работают: таблица awg_lan_map не поставлена ({err}) — журнал: {cmd}"
    return (f"⚠️ Адреса в туннеле не работают: nft не принял таблицу <code>awg_lan_map</code> — "
            f"подробности: <code>{html.escape(cmd, quote=False)}</code>")


__all__ = ["lan_space_off_line", "lan_map_failed_line"]
