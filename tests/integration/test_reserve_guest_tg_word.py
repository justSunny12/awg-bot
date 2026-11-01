"""Право гостя на вебхук — словом бота резерва (inbound_tgwait: reserve_guest_tg_view,
reserve_peer_tg_taken_in; guest: guest_tg_may_take, отметка «вебхук не здесь» в
хранилище гостя; guestctl: слово `tg{go, foreign}`, `hook_foreign` в hello).

Гость (бот основы на резерве) ставил вебхук по отметке `tg_take` на диске — она
переживает рестарт юнита, а Telegram к тому времени могла забрать основа («вернуть без
резерва», снятый зависший вебхук, возврат). Теперь главное правило — слово бота резерва:
пользователи ещё у него (taking | serving), бот основы ему передан (отметка `tg_take`,
`released` журнала, принудительный захват) и основа Telegram не забирала (её живой `st`
после начала эпизода не `home`/`returning`, её `taken` не приходил, гость не говорил
«вебхук не здесь»). Подстраховка — отметка «вебхук не здесь» в хранилище гостя: она
переживает рестарт, и при ней обычного слова мало — нужно явное (бот резерва знает об
отметке). Новый эпизод начинает с чистого листа.

Мир — host (test_reserve_hosting) для бота резерва, services для гостя, настоящий
unix-сокет для канала.

Цена ошибки: гость после рестарта ставит вебхук поверх основы, которая уже отвечает
пользователям, — два бота на одном токене, решения пишут обе стороны и расходятся;
слово, которое не даёт вебхук законному держателю, — пользователи на резерве без бота;
отметка прошлого эпизода в новом — бот основы на резерве немой без причины.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import types

import pytest

from awgbot.domain.services import Services
from awgbot.infra import rsvzone
from awgbot.runtime.pairlink import guestctl
from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration.test_reserve_hosting import _serving, _taking, host
from tests.integration.test_reserve_guestctl import _GuestSvc, _ResSvc, _until

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["host"]


def _ago(seconds: int) -> str:
    return timeutil.to_iso(timeutil.shift(timeutil.now(), -seconds))


def _episode(host, **fields) -> None:
    """Бот основы у меня (serving, вебхук был у гостя), эпизод начался час назад; `st`
    основы — живой и после начала эпизода: away."""
    _serving(host, **fields)
    host.svc.rsv_update("rsv_in", open_started=_ago(3600))
    rm.st_in(host.svc, out="away", **{"in": "serving"})
    host.zone.take.parent.mkdir(parents=True, exist_ok=True)
    host.zone.take.write_text("1")


def _go(host) -> bool:
    return host.svc.reserve_guest_tg_view()["go"]


# ── бот резерва: когда гостю ставить вебхук ─────────────────────────────────

def test_users_here_and_the_primary_away_mean_go(host):
    _episode(host)
    assert host.svc.reserve_guest_tg_view() == {"go": True, "foreign": False}


@pytest.mark.parametrize("state", ["idle", "releasing", "relay"])
def test_without_the_users_of_the_primary_here_there_is_no_go(host, state):
    _episode(host)
    host.svc.reserve_set_in(state)
    assert not _go(host), f"гость ставит вебхук в {state}"


def test_taking_waits_for_the_fact_of_the_handover(host):
    svc = host.svc
    _taking(host)
    svc.rsv_update("rsv_in", open_started=_ago(3600))
    rm.st_in(svc, out="leaving")                              # переезд кнопкой: основа отдаёт бота
    assert not _go(host), "вебхук до факта записи и до released основы"
    svc.rsv_update("rsv_in", jrn_released={"epoch": 3, "last": 10})
    assert _go(host), "released журнала — бот передан"
    svc.rsv_update("rsv_in", jrn_released=None)
    host.zone.take.parent.mkdir(parents=True, exist_ok=True)
    host.zone.take.write_text("1")
    assert _go(host), "отметка tg_take — факт записи или released реплики"


def test_the_forced_takeover_is_a_handover_too(host):
    _taking(host)
    host.svc.rsv_update("rsv_in", open_started=_ago(3600), tg_take_forced=True)
    rm.st_in(host.svc, out="leaving")
    assert _go(host)


@pytest.mark.parametrize("out", ["home", "returning"])
def test_a_live_primary_at_home_after_the_episode_began_took_telegram(host, out):
    """«Вернуть без резерва» (линк потом ожил) и возврат: основа отвечает сама."""
    _episode(host)
    rm.st_in(host.svc, out=out)
    assert not _go(host), f"основа на связи и {out}, а гостю — вебхук"


def test_a_stale_home_of_a_dead_primary_does_not_hold_the_guest(host):
    """Аварийный путь: последний `st` мёртвой основы — `home` до аварии."""
    _episode(host)
    rm.st_in(host.svc, out="home")
    host.svc.rsv_update("rsv_peer_state", session=False)
    assert _go(host), "основа не на связи — её прежний home не о Telegram"


def test_a_home_said_before_the_episode_began_is_about_the_past(host):
    _episode(host)
    rm.st_in(host.svc, out="home")
    host.svc.rsv_update("rsv_peer_state", at=_ago(7200))
    assert _go(host)


def test_taken_from_the_primary_while_her_users_are_here_holds_the_guest(host):
    svc = host.svc
    _episode(host)
    assert svc.reserve_taken_in() == [], "taken основы на её возврате — не переход моего направления"
    assert svc.rsv_get("rsv_in").get("peer_tg_taken") and not _go(host)


def test_the_guest_saying_foreign_holds_it_and_ours_releases(host):
    svc = host.svc
    _episode(host)
    svc.reserve_guest_webhook_in({"foreign": True})
    assert svc.reserve_guest_tg_view() == {"go": False, "foreign": True}
    assert "вебхук не здесь" in svc.reserve_guest_webhook_hold_why(), "слово и решение разошлись"
    assert svc.reserve_guest_webhook_in({"ours": True}) == [], "«снова мой» — без уведомления"
    assert svc.reserve_guest_tg_view() == {"go": True, "foreign": False}


def test_a_new_episode_starts_with_a_clean_slate(host):
    svc = host.svc
    svc.rsv_update("rsv_in", webhook_foreign=True, peer_tg_taken=timeutil.now_iso(),
                   jrn_released={"epoch": 2, "last": 5})
    _taking(host)
    inn = svc.rsv_get("rsv_in")
    assert not inn.get("webhook_foreign") and not inn.get("peer_tg_taken") and not inn.get("jrn_released"), inn


# ── гость: решение и отметка в хранилище ────────────────────────────────────

def test_the_guest_takes_only_on_the_word_of_the_reserve(services):
    assert services.guest_tg_may_take(False) is False, "вебхук без слова бота резерва"
    assert services.guest_tg_may_take(True) is True


def test_the_foreign_mark_survives_a_restart_and_needs_an_explicit_word(services):
    services.guest_webhook_foreign()
    assert rsvzone.HOOK_FOREIGN_FILE.exists(), "отметка только в памяти — рестарт её сотрёт"
    again = Services(services.db)                             # рестарт юнита гостя
    assert not again.guest_hook_foreign()
    assert again.guest_hook_foreign_load() is True and again.guest_hook_foreign()
    assert again.guest_tg_may_take(True) is False, "после рестарта вебхук поверх основы по обычному слову"
    assert again.guest_tg_may_take(True, foreign_ack=True) is True, "явное слово бота резерва"
    assert again.guest_webhook_ours() is True and not rsvzone.HOOK_FOREIGN_FILE.exists()
    assert again.guest_webhook_ours() is False, "второе «снова мой» — без слова боту резерва"
    third = Services(services.db)
    assert third.guest_hook_foreign_load() is False and third.guest_tg_may_take(True) is True


def test_the_fence_of_the_quorum_stays_after_a_restart_with_the_mark(services):
    services.guest_webhook_foreign()
    again = Services(services.db)
    again.guest_hook_foreign_load()
    ld = types.SimpleNamespace(yes_count=lambda now: 2, v=2)
    assert again.reserve_quorum_fence_apply("unfence", ld) == []
    assert again.db.jrn_meta()["role"] == "follower", "«да» кворума вернули гостю право записи после рестарта"


# ── канал: слово tg и отметка в hello ───────────────────────────────────────

class _TgRes(_ResSvc):
    def __init__(self):
        super().__init__()
        self.tg = {"go": False, "foreign": False}
        self.webhook_words: list[dict] = []

    def reserve_guest_tg_view(self):
        return dict(self.tg)

    def reserve_guest_webhook_in(self, body):
        self.webhook_words.append(dict(body))
        if body.get("foreign"):
            self.tg = {"go": False, "foreign": True}
        return []


class _TgGuest(_GuestSvc):
    def __init__(self, foreign=False):
        super().__init__()
        self.foreign = foreign

    def guest_hook_foreign(self):
        return self.foreign


async def _pair(foreign=False, go=False):
    d = tempfile.mkdtemp(prefix="rt", dir="/tmp")             # путь сокета unix — короткий
    path = os.path.join(d, "g.sock")
    res, gst = _TgRes(), _TgGuest(foreign)
    res.tg = {"go": go, "foreign": False}
    server, client = guestctl.GuestCtlServer(res, path), guestctl.GuestCtlClient(gst, path)
    await server.ensure()
    client.start()
    return types.SimpleNamespace(server=server, client=client, res=res, dir=d)


async def _close(p) -> None:
    await p.client.stop()
    await p.server.stop()
    shutil.rmtree(p.dir, ignore_errors=True)


async def test_the_word_reaches_the_guest_on_hello_and_on_change_only(monkeypatch):
    from awgbot.runtime import rsvactions
    monkeypatch.setattr(rsvactions, "run", _no_actions)
    p = await _pair()
    try:
        assert await _until(lambda: p.server.connected() and p.res.calls), "hello не дошло"
        await asyncio.sleep(0.1)
        assert p.client.tg_go is False
        p.res.tg = {"go": True, "foreign": False}
        assert await p.server.push_tg() is True
        assert await _until(lambda: p.client.tg_go), "слово «ставь вебхук» не дошло"
        sent = []
        orig = p.server.send

        async def spy(kind, body=None):
            sent.append(kind)
            return await orig(kind, body)
        p.server.send = spy
        assert await p.server.push_tg() is True and sent == [], "то же слово переслано снова"
        p.res.tg = {"go": False, "foreign": False}
        await p.server.push_tg()
        assert await _until(lambda: not p.client.tg_go), "отзыв слова не дошёл"
    finally:
        await _close(p)


async def test_the_word_of_a_closed_session_is_not_kept(monkeypatch):
    from awgbot.runtime import rsvactions
    monkeypatch.setattr(rsvactions, "run", _no_actions)
    p = await _pair(go=True)
    try:
        assert await _until(lambda: p.client.tg_go), "слово на hello не дошло"
        p.res.tg = {"go": False, "foreign": False}
        await p.server.stop()                                 # рестарт бота резерва
        assert await _until(lambda: not p.client.tg_go), "слово прежней сессии осталось в силе"
    finally:
        await _close(p)


async def test_a_guest_with_the_mark_tells_the_reserve_in_hello_before_the_word(monkeypatch):
    from awgbot.runtime import rsvactions
    monkeypatch.setattr(rsvactions, "run", _no_actions)
    p = await _pair(foreign=True, go=True)                    # пока бот резерва не знает об отметке — go
    try:
        assert await _until(lambda: p.res.webhook_words), "отметка с прошлого запуска не сказана боту резерва"
        assert p.res.webhook_words[0] == {"foreign": True}
        await asyncio.sleep(0.1)
        assert p.client.tg_go is False and p.client.tg_foreign_ack is True, \
            "слово на hello собрано без знания об отметке"
    finally:
        await _close(p)


async def _no_actions(acts):
    return None
