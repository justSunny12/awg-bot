"""selfhost.py — собственный адрес сервера: куда ходят линки шлюзов и линк пары.

`app.network.self_host` — не переключаемый IP или второе имя этого сервера; с
парой адрес для пользователей (`server_host`) переезжает на резерв по A-записи, а
линки своих шлюзов, постоянные линки чужих шлюзов и линк пары должны смотреть
на сам сервер. Пусто — как раньше: `server_host`. Живое значение из настроек,
как у ссылок пользователей: после «✏️ Домен» документ шлюза не должен нести
прежний адрес до рестарта бота.
"""
from __future__ import annotations

import re

from awgbot.core import config, settings

_HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,253}\Z")


def own_host() -> str:
    """Собственный адрес, если задан и похож на адрес, иначе адрес для пользователей;
    пусто — не задан ни один."""
    own = str(settings.get("app.network.self_host", "") or "").strip()
    if own and _HOST_RE.match(own):
        return own
    host = str(settings.get("app.network.server_host", config.SERVER_HOST) or "").strip()
    return host if _HOST_RE.match(host) else ""
