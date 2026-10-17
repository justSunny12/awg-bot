"""Каталог снимков экранов — склейка модулей ролей.

Snapshot-модули (shots_admin, shots_client, shots_gateway) держат свои SHOTS и
LABEL_EXCEPTIONS; здесь — общий список, константы и Shot из base для тестов.
"""
from __future__ import annotations

from tests.screens import shots_admin, shots_client, shots_gateway
from tests.screens.base import CLIENT_TG, DISPATCHER, HOST, NOW, PEERS, VERSION, Shot, owner

__all__ = ["CLIENT_TG", "DISPATCHER", "HOST", "NOW", "PEERS", "VERSION", "Shot", "owner", "SHOTS", "LABEL_EXCEPTIONS", "TIME_LABEL_EXCEPTIONS",
           "UNREACHABLE"]

SHOTS = [*shots_admin.SHOTS, *shots_client.SHOTS, *shots_gateway.SHOTS]

LABEL_EXCEPTIONS: dict[str, set[str]] = {**shots_admin.LABEL_EXCEPTIONS, **shots_client.LABEL_EXCEPTIONS,
                                         **shots_gateway.LABEL_EXCEPTIONS}

# Строки, где время без метки пояса законно (сторож tests/screens/timelabels),
# — {id снимка: подстроки}: текст объявления, который пишет сам админ.
_BC_TEXT = "В субботу с 02:00 до 03:00"
TIME_LABEL_EXCEPTIONS: dict[str, set[str]] = {
    sid: {_BC_TEXT} for sid in ("adm.bc.preview", "adm.bc.preview.one", "adm.bc.send", "adm.bc.send.holders",
                                "adm.bc.send.one", "adm.bc.send.undelivered")
}

# Построители клавиатур, которых не рисует ни действие человека, ни событие, —
# {«модуль.имя»: причина}. Сторож полноты (test_screens) требует снимок для
# каждого остального; исключение, построитель которого снят, — красное.
UNREACHABLE: dict[str, str] = {}
