"""lanmap.py — часть пакета notices (см. __init__): адреса в туннеле локальных
сетей шлюзов — строка «выключены» (карточки слотов и замечания при запуске)
и разовое уведомление после обновления."""

from __future__ import annotations

import html
import re

_ADDR = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?")


def lan_space_off_line(problem: str, *, markup: bool = True) -> str:
    """Пространство адресов в туннеле неисправно — адреса выключены целиком.
    problem — причина из util/lanspace.space_problem. markup=False — без
    разметки и значка: замечания при запуске экранируют текст сами."""
    tail = " — поправь routing.lan_space в app.yaml"
    if not markup:
        return f"адреса в туннеле выключены: {problem}{tail}"
    body = _ADDR.sub(lambda m: f"<code>{m.group(0)}</code>", html.escape(problem, quote=False))
    return f"⚠️ Адреса в туннеле выключены: {body} — поправь <code>routing.lan_space</code> в app.yaml"


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " и " + names[-1]


def lan_peers_joined(names: list[str]) -> str:
    """Хвост уведомления после обновления: прежний тумблер связи подсетей был
    выключен, а шлюзов с VPN-транзитом два и больше — их сети теперь связаны.
    names — имена шлюзов готовой разметкой (ссылки на карточки)."""
    return ("Связь подсетей теперь часть VPN-транзита, отдельного выключателя больше нет: "
            f"сети {_and(names)} связаны")


__all__ = ["lan_space_off_line", "lan_peers_joined"]
