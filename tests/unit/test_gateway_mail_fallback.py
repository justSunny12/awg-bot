"""
Запасной канал алертов агента шлюза: письмо, когда Telegram не отвечает
(`_mail_fallback` в runtime/main.py, роль gateway).

Telegram агента ходит через туннель до сервера AWG. Лёг линк — Telegram нет,
а прямой выход в интернет у малины есть: письмо уйдёт, и человек узнает, что
РФ-доступ лежит. Нет и прямого выхода — письмо не уйдёт тем более; пробовать
SMTP тогда значит засорять журнал отказами и держать поток монитора на
таймаутах. Цена ошибки в обе стороны: молчание, когда письмо могло дойти, или
бессмысленные попытки, когда дойти ему нечем.

Сборка роли настоящая (run_gateway) до места, где запасной канал
регистрируется; дальше сборка останавливается — остальное здесь не нужно.
"""
from __future__ import annotations

import logging

import pytest

from awgbot.bot import notifier
from awgbot.core import config
from awgbot.domain.gateway import GatewayServices
from awgbot.runtime import main


class _Stop(Exception):
    """Сборка дошла до регистрации запасного канала — дальше не идём."""


class _Watcher:
    def __init__(self, *a, **k):
        pass

    def start(self):
        pass


@pytest.fixture()
def fallback(tmp_path, monkeypatch):
    """Функция, которую агент регистрирует как запасной канал, и журнал
    того, что она сделала: письма и пробы выхода наружу."""
    got = {}
    sent: list[str] = []
    probes: list[int] = []
    state = {"enabled": True, "egress": 12.5}

    def capture(fn):
        got["fn"] = fn
        raise _Stop

    # сборка цепляет модульные роутеры к своему диспетчеру; после теста — отцепить
    from awgbot.bot import paging
    from awgbot.bot.handlers import gateway as gateway_handlers
    for r in (paging.router, gateway_handlers.router):
        monkeypatch.setattr(r, "_parent_router", None)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "gw.db"))
    monkeypatch.setattr(config, "BOT_TOKEN", "42:DUMMY")
    monkeypatch.setattr(main, "ConfWatcher", _Watcher)
    monkeypatch.setattr(notifier, "set_email_fallback", capture)
    monkeypatch.setattr(GatewayServices, "email_alert_fallback_enabled", lambda self: state["enabled"])
    monkeypatch.setattr(GatewayServices, "egress_probe",
                        lambda self: probes.append(1) or state["egress"])
    monkeypatch.setattr(GatewayServices, "email_send_alert", lambda self, text: sent.append(text))
    return got, sent, probes, state


async def _registered(got):
    with pytest.raises(_Stop):
        await main.run_gateway()
    assert "fn" in got, "агент не зарегистрировал запасной канал"
    return got["fn"]


async def test_with_a_direct_way_out_the_alert_goes_by_mail(fallback):
    got, sent, probes, _ = fallback
    fn = await _registered(got)
    await fn("🚨 Линк до сервера AWG мёртв")
    assert sent == ["🚨 Линк до сервера AWG мёртв"], "выход наружу есть, а письмо не ушло"
    assert probes == [1]


async def test_without_a_way_out_no_mail_is_tried_and_the_log_says_why(fallback, caplog):
    got, sent, probes, state = fallback
    state["egress"] = None
    fn = await _registered(got)
    with caplog.at_level(logging.WARNING):
        await fn("🚨 Линк до сервера AWG мёртв")
    assert sent == [], "прямого выхода нет, а письмо всё равно пробовали отправить"
    assert probes == [1]
    assert any("письмо не уйдёт" in r.getMessage() for r in caplog.records), (
        "молча проглотили алерт — в журнале не видно, почему письма не было")


async def test_with_mail_fallback_off_nothing_is_probed(fallback):
    """Запасной канал выключен в настройках — ни письма, ни пробы наружу:
    лишний коннект с адреса квартиры ради ненужного ответа."""
    got, sent, probes, state = fallback
    state["enabled"] = False
    fn = await _registered(got)
    await fn("🚨 Линк до сервера AWG мёртв")
    assert sent == [] and probes == []
