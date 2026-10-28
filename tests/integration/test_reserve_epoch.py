"""Переходы эпохи линии данных у основы (domain/services/reserve/journal/epoch.py):
право записи решений уходит боту основы на резерве по тем же фактам, что
переносят пользователей и бот, и возвращается в каждой точке возврата.

К резерву: финальная реплика передачи собрана, `taken` от резерва, Telegram
сказал «вебхук стоит» (409 на getUpdates, чужой адрес в getWebhookInfo),
резерв несёт моих пользователей мимо меня (`st` с in = serving). Ко мне:
реплика возврата (до слияния), «⚠️ Да, вернуть» без резерва, срыв переезда,
снятый зависший вебхук, резерв без гостя, конец возврата, снятие пары и
страховка такта «пары нет / пользователи и Telegram здесь».

Мир — пара из tests/integration/_reserve_move.py; Bot API у поллинга —
заглушка с ответом getWebhookInfo.

Цена ошибки: пропущенный факт «к резерву» — основа в отъезде пишет решения
по устаревшей базе (пример с продлением — в test_reserve_follow.py);
пропущенная точка возврата — пользователи снова у основы, она отвечает в
Telegram, а каждое действие с профилями получает отказ «пользователи на
резерве», и перезапуск этого не лечит (роль живёт в базе); переход без
пары — чужой вебхук на моём токене (не переезд) навсегда запрещает мне
решения.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from awgbot.runtime import pollctl
from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv


def _ago(minutes: float) -> str:
    return timeutil.to_iso(timeutil.shift(timeutil.now(), -minutes * 60))


def _following(svc) -> bool:
    return svc.db.jrn_following()


class _Bot:
    """Bot API глазами поллинга: только getWebhookInfo."""

    def __init__(self, url: str = ""):
        self.url = url
        self.session = None

    async def get_webhook_info(self):
        return types.SimpleNamespace(url=self.url)


@pytest.fixture()
def away(mv):
    """Пользователи и бот у резерва (переезд кнопкой), резерв их несёт."""
    rm.go_away(mv.svc, gen=7)
    rm.st_in(mv.svc, **{"in": "serving"})
    assert _following(mv.svc), "go_away не перевёл основу в следование — остальные тесты не о том"
    return mv


# ── к резерву ───────────────────────────────────────────────────────────────

def test_the_final_replica_of_the_handover_passes_the_epoch(mv):
    svc = mv.svc
    rm.to_step(svc, "release", gen=7)
    assert not _following(svc), "основа следует ещё до передачи бота"
    assert svc.reserve_release_build() is not None
    meta = svc.db.jrn_meta()
    assert (meta["holder"], meta["role"], meta["epoch"]) == ("peer", "follower", 1), meta


def test_taken_from_the_reserve_passes_the_epoch(mv):
    svc = mv.svc
    rm.to_step(svc, "released", gen=7)
    svc.reserve_taken_in()
    assert svc.reserve_out() == "away" and _following(svc)


def test_the_reserve_carrying_my_clients_past_me_passes_the_epoch(mv):
    svc = mv.svc
    rm.st_in(svc, **{"in": "serving"})
    svc.reserve_out_tick()
    assert svc.reserve_out() == "away" and _following(svc), "переезд мимо меня — основа пишет решения"


def test_a_reserve_only_taking_does_not_pass_the_epoch(mv):
    rm.st_in(mv.svc, **{"in": "taking"})
    mv.svc.reserve_out_tick()
    assert not _following(mv.svc), "taking — ещё не факт: клон может не встать"


async def test_a_foreign_webhook_seen_by_the_polling_passes_the_epoch(mv):
    rm.st_in(mv.svc, **{"in": "serving"})                 # резерв несёт моих пользователей — вебхук его гостя
    ctl = pollctl.PollControl(None, _Bot("https://backup.example.net:8443/hook"), mv.svc)
    assert await ctl._allowed() is False
    assert mv.svc.reserve_poll() == "away" and _following(mv.svc)
    assert "вебхук" in mv.svc.db.jrn_meta()["why"]


async def test_a_409_on_get_updates_passes_the_epoch(mv):
    rm.st_in(mv.svc, **{"in": "serving"})
    ctl = pollctl.PollControl(None, _Bot(), mv.svc)
    ctl.conflict("Conflict: can't use getUpdates method while webhook is active")
    for _ in range(20):
        await asyncio.sleep(0.01)
        if _following(mv.svc):
            break
    assert mv.svc.reserve_poll() == "away" and _following(mv.svc), "409 «webhook is active» не отдал эпоху"


async def test_a_webhook_left_at_a_reserve_without_my_guest_does_not_pass_the_epoch(mv):
    """Вебхук у резерва, а сам резерв на связи и говорит «у меня никого» (idle):
    остаток после возврата или срыва переезда — не держатель; основа решает сама."""
    ctl = pollctl.PollControl(None, _Bot("https://backup.example.net:8443/hook"), mv.svc)
    assert await ctl._allowed() is False
    assert mv.svc.reserve_poll() == "away" and not _following(mv.svc)


async def test_no_webhook_does_not_pass_the_epoch(mv):
    ctl = pollctl.PollControl(None, _Bot(""), mv.svc)
    assert await ctl._allowed() is True
    assert not _following(mv.svc)


async def test_without_a_pair_a_foreign_webhook_is_not_a_move(services, rsv):
    """Без пары чужой вебхук на моём токене — не переезд: передавать право записи
    некому, основа остаётся держателем."""
    assert not services.reserve_paired()
    services.reserve_set_poll("away", why="webhook_info", url="https://x.example.net/hook")
    assert not _following(services) and services.db.jrn_meta()["epoch"] == 0


def test_passing_twice_is_one_epoch(away):
    svc = away.svc
    svc.reserve_set_poll("away", why="webhook")
    svc.reserve_set_poll("away", why="webhook_info")
    assert svc.db.jrn_meta()["epoch"] == 1, "повтор факта — новая эпоха"


# ── ко мне ──────────────────────────────────────────────────────────────────

def test_the_return_replica_brings_the_epoch_back(away):
    svc = away.svc
    svc.reserve_return_start("button")
    svc.reserve_released_in(b"")
    assert not _following(svc)


def test_the_force_home_lever_brings_the_epoch_back(away):
    svc = away.svc
    svc.db.set_state("last_server_ok", "0")
    svc.rsv_update("rsv_peer_state", session=False, down_since=_ago(11))
    assert svc.reserve_force_check() == ""
    assert ("take_home",) in svc.reserve_force_home()
    assert not _following(svc), "«вернуть без резерва» — решения по-прежнему у отрезанного резерва"


def test_a_failed_move_brings_the_epoch_back(mv):
    svc = mv.svc
    rm.to_step(svc, "release", gen=7)
    svc.reserve_release_build()
    assert _following(svc)
    svc._out_step("released", 300)
    svc.reserve_set_out("leaving")
    svc.reserve_down_in("клон не встал")
    assert svc.reserve_out() == "home" and not _following(svc), "переезд сорвался — решения никому"


def test_removing_a_stuck_webhook_brings_the_epoch_back(away):
    svc = away.svc
    svc.reserve_stuck_home()
    assert svc.reserve_out() == "home" and not _following(svc)


def test_the_end_of_the_return_brings_the_epoch_back(away):
    svc = away.svc
    svc.reserve_set_out("returning", kind="button", gen=7, return_started_at=timeutil.now_iso())
    svc._out_step("relay", 300)
    svc.reserve_wiped_in()
    assert svc.reserve_out() == "home" and not _following(svc)


def test_a_reserve_back_without_my_guest_after_the_lever_brings_the_epoch_back(away):
    svc = away.svc
    svc.reserve_set_out("returning", kind="dead", gen=7, return_why="force", return_started_at=timeutil.now_iso())
    svc._out_step("back_req", 300)
    rm.st_in(svc, **{"in": "idle"})
    svc.reserve_out_tick()
    assert svc.reserve_out() == "home" and not _following(svc)


def test_removing_the_pair_brings_the_epoch_back(away):
    svc = away.svc
    svc.reserve_unpair("peer")
    assert not svc.reserve_paired() and not _following(svc), "пара снята — а решения всё ещё «у резерва»"


def test_a_hold_drops_what_the_follower_kept_for_the_holder(away):
    svc = away.svc
    svc.rsv_update("rsv_follow", blocks=["10.8.1.9"], blocks_at=timeutil.now_iso())
    svc.reserve_stuck_home()
    assert svc.rsv_get("rsv_follow") == {}, "блокировки держателя пережили возврат права записи"


# ── страховка такта ─────────────────────────────────────────────────────────

def test_the_tick_takes_the_epoch_back_without_a_pair(services, rsv):
    services.db.jrn_pass(holder="peer", role="follower", why="тест: следование без пары")
    services.reserve_out_tick()
    assert not _following(services), "пары нет — следовать не за кем, а решения запрещены"


def test_the_tick_takes_the_epoch_back_when_clients_and_telegram_are_here(mv):
    svc = mv.svc
    svc.db.jrn_pass(holder="peer", role="follower", why="тест: застрявшее следование")
    assert svc.reserve_out() == "home" and svc.reserve_poll() == "mine"
    svc.reserve_out_tick()
    assert not _following(svc)


@pytest.mark.parametrize("poll", ["away", "mine"])
def test_the_tick_keeps_following_while_the_clients_are_away(away, poll):
    svc = away.svc
    svc.rsv_update("rsv_poll", state=poll)
    svc.reserve_out_tick()
    assert _following(svc), f"пользователи у резерва (poll={poll}) — а страховка вернула право записи"


def test_the_tick_keeps_following_while_home_but_telegram_is_at_the_reserve(mv):
    svc = mv.svc
    rm.st_in(svc, **{"in": "serving", "at": ""})          # слово резерва есть, но тик ещё не перевёл в away
    svc.reserve_set_poll("away", why="webhook")
    assert svc.reserve_out() == "home" and _following(svc)
    svc.reserve_out_tick()
    assert _following(svc), "Telegram у резерва — страховка вернула право записи по одному «home»"
