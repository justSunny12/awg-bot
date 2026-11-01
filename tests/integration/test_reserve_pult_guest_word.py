"""Пульт у основы и слово бота резерва гостю `tg{go, foreign}` (inbound_tgwait:
reserve_guest_webhook_hold_why — единственное решение «ставить ли гостю вебхук»; pult/peer:
reserve_pult_hold_why, reserve_pult_back_in; guest: reserve_guest_webhook_in).

Пока в `st` основы стоит `tg_pult` (Telegram её бота у неё, её пользователи у меня), бот
резерва гостю `go` не даёт — на любом пути слова: смена на такте, hello гостя после его
рестарта (с отметкой «вебхук не здесь» в хранилище и без), рестарт самого бота резерва.
Слово `pult_back` с той же отметкой снимает запрет и даёт явное `foreign` — гость с отметкой
«вебхук не здесь» без него не поставил бы; «не здесь» гостя в окне между отдачей и его
вебхуком запрет не возвращает. Новый эпизод начинает без отметок пульта прошлого.

Мир — host (test_reserve_hosting) для бота резерва, настоящий unix-сокет для канала.

Цена ошибки: гость после рестарта ставит вебхук поверх Telegram, который основа забрала
пультом, — админ основы как раз решает, вернуть пользователей или отдать Telegram, а бот у
него немеет; отданный пульт, который гость не берёт, — пользователи на резерве без бота,
основа отвечает им «на техработах» бессрочно.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import types

import pytest

from awgbot.domain.services import Services
from awgbot.runtime.pairlink import guestctl

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration.test_reserve_guest_tg_word import _TgGuest, _close, _episode, _no_actions
from tests.integration.test_reserve_guestctl import _ResSvc, _until
from tests.integration.test_reserve_hosting import _taking, host

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["host"]

AT = "2026-11-01T10:00:00+05:00"


def _pult(host, at: str = AT) -> None:
    rm.st_in(host.svc, out="away", tg_pult=at, **{"in": "serving"})


def test_the_pult_at_the_primary_holds_the_word_inside_the_single_decision(host):
    _episode(host)
    assert host.svc.reserve_guest_tg_view()["go"] is True
    _pult(host)
    why = host.svc.reserve_guest_webhook_hold_why()
    assert why == host.svc.reserve_pult_hold_why() and why, "пульт не в единственном решении"
    assert host.svc.reserve_guest_tg_view()["go"] is False


def test_no_go_after_a_restart_of_the_guest_with_or_without_its_foreign_mark(host):
    _episode(host)
    _pult(host)
    again = Services(host.svc.db)                             # рестарт бота резерва: память чистая
    assert again.reserve_guest_tg_view()["go"] is False, "после рестарта бота резерва пульт забыт"
    again.reserve_guest_webhook_in({"foreign": True})         # гость с отметкой с прошлого запуска — в hello
    assert again.reserve_guest_tg_view()["go"] is False


def test_giving_back_lifts_the_hold_and_says_foreign_explicitly(host):
    _episode(host)
    _pult(host)
    host.svc.reserve_guest_webhook_in({"foreign": True})      # надзор гостя видел, что вебхук снят
    host.svc.reserve_pult_back_in(AT)
    assert host.svc.reserve_guest_tg_view() == {"go": True, "foreign": True}, \
        "гость с отметкой «вебхук не здесь» без явного слова вебхук не поставит"
    host.svc.reserve_guest_webhook_in({"foreign": True})      # надзор до его вебхука — то же «не здесь»
    assert host.svc.reserve_guest_tg_view()["go"] is True, "окно до вебхука гостя вернуло запрет"
    _pult(host, "2026-11-01T11:00:00+05:00")                  # пульт снова у основы — новая отметка
    assert host.svc.reserve_guest_tg_view()["go"] is False


def test_a_new_episode_starts_without_the_pult_marks_of_the_last(host):
    svc = host.svc
    svc.rsv_update("rsv_in", pult_back=AT, pult_told=AT)
    _taking(host)
    inn = svc.rsv_get("rsv_in")
    assert not inn.get("pult_back") and not inn.get("pult_told"), inn


# ── канал: рестарт гостя на настоящем сокете ────────────────────────────────

class _Res(_ResSvc):
    """Бот резерва: слово — из настоящих сервисов хоста."""

    def __init__(self, svc):
        super().__init__()
        self.svc = svc

    def reserve_guest_tg_view(self):
        return self.svc.reserve_guest_tg_view()

    def reserve_guest_webhook_in(self, body):
        return self.svc.reserve_guest_webhook_in(body)


async def test_a_restarted_guest_gets_no_go_on_hello_while_the_pult_is_at_the_primary(host, monkeypatch):
    from awgbot.runtime import rsvactions
    monkeypatch.setattr(rsvactions, "run", _no_actions)
    _episode(host)
    _pult(host)
    d = tempfile.mkdtemp(prefix="rp", dir="/tmp")             # путь сокета unix — короткий
    path = os.path.join(d, "g.sock")
    server = guestctl.GuestCtlServer(_Res(host.svc), path)
    await server.ensure()
    first = guestctl.GuestCtlClient(_TgGuest(), path)
    first.start()
    p = types.SimpleNamespace(server=server, client=first, dir=d)
    try:
        assert await _until(lambda: server.connected()), "гость не подключился"
        await asyncio.sleep(0.1)
        assert first.tg_go is False
        await first.stop()                                    # рестарт юнита гостя: с отметкой «не здесь»
        p.client = second = guestctl.GuestCtlClient(_TgGuest(foreign=True), path)
        second.start()
        assert await _until(lambda: server.connected()), "перезапущенный гость не подключился"
        await asyncio.sleep(0.2)
        assert second.tg_go is False, "после рестарта гостя — go при пульте у основы"
        host.svc.reserve_pult_back_in(AT)
        assert await server.push_tg() is True
        assert await _until(lambda: second.tg_go and second.tg_foreign_ack), "отданный Telegram гость не возьмёт"
    finally:
        await _close(p)
