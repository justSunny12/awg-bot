"""Строка пояса в «🔧 Сервис» обеих ролей (bot/texts/zone): под «подробнее» —
имя, смещение, откуда взят, как сменить; нераспознанный ключ — открытой
строкой ⚠️."""
from __future__ import annotations

import pytest

from awgbot.bot import roles
from awgbot.bot.texts import zone as zt
from awgbot.core import config

pytestmark = pytest.mark.unit


def _zone(mp, name, source="host", host="", bad=""):
    from zoneinfo import ZoneInfo
    from awgbot.util import timeutil
    tz = ZoneInfo(name) if name else timeutil.TZ
    mp.setattr(timeutil, "TZ", tz)
    for k, v in (("TZ", tz), ("TZ_NAME", name), ("TZ_SOURCE", source),
                 ("TZ_HOST_NAME", host if host or source != "host" else name), ("TZ_BAD_VALUE", bad)):
        mp.setattr(config, k, v)


def test_host_zone(monkeypatch):
    _zone(monkeypatch, "Asia/Yekaterinburg")
    assert zt.svc_zone_line(roles.MAIN) == (
        "Часовой пояс — Asia/Yekaterinburg (UTC+5), пояс сервера. Сменить: "
        "<code>sudo timedatectl set-timezone &lt;пояс&gt;</code> и «🔁 Перезапуск бота»")
    assert ", пояс шлюза. " in zt.svc_zone_line(roles.GATEWAY)
    assert zt.svc_zone_warning(roles.MAIN) == ""


def test_host_zone_without_a_name(monkeypatch):
    _zone(monkeypatch, "")
    assert zt.svc_zone_line(roles.MAIN).startswith("Часовой пояс — UTC+5, пояс сервера. ")


def test_key_differs_from_the_host(monkeypatch):
    _zone(monkeypatch, "Europe/Moscow", source="conf", host="Asia/Kolkata")
    assert zt.svc_zone_line(roles.GATEWAY) == (
        "Часовой пояс — Europe/Moscow (UTC+3), задан ключом <code>timezone</code> в app.yaml; у шлюза — "
        "Asia/Kolkata (UTC+5:30). Перейти на пояс шлюза: убери ключ и перезапусти бота")


def test_key_same_as_the_host_no_tail(monkeypatch):
    _zone(monkeypatch, "Europe/Moscow", source="conf", host="Europe/Moscow")
    assert "; у сервера" not in zt.svc_zone_line(roles.MAIN)


def test_unknown_key_is_an_open_warning(monkeypatch):
    _zone(monkeypatch, "Etc/UTC", source="conf_bad", bad="Europe/<b>")
    assert zt.svc_zone_line(roles.MAIN) == ""
    assert zt.svc_zone_warning(roles.MAIN) == (
        "⚠️ Часовой пояс <code>Europe/&lt;b&gt;</code> из app.yaml не распознан — взят пояс сервера "
        "Etc/UTC (UTC). Исправь ключ <code>timezone</code> и перезапусти бота")
