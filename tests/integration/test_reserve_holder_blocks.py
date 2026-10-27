"""Блокировки держателя — следующей основе (обратный ход сообщения `blocks`):
пока решения пишет бот основы на резерве, у основы остаются сессии
пользователей, ещё не переподключившихся к резерву; DROP им ставится по
решениям держателя, а не по устаревшей базе основы.

Путь: гость (domain/services/reserve/guest.py: guest_blocks_to_send — при
изменении и раз в 120 с) → локальный канал (runtime/pairlink/guestctl.py,
слово `blocks`) → такт переезда бота резерва (runtime/scheduler/reserve.py:
только пока он несёт пользователей основы) → канал пары → основа
(runtime/pairlink/inbound.py: _in_blocks → reserve_holder_blocks_in →
reconcile_blocks по адресам держателя, лишние DROP снимаются).

Мир — пара из tests/integration/_reserve_move.py; база гостя — реплика базы
основы; DROP — множество адресов фейкового awg.

Цена ошибки: основа в отъезде держит DROP по своей устаревшей базе — человек,
которому на резерве продлили подписку, но чей телефон ещё ходит через
основу, без интернета до переподключения; основа шлёт резерву свои
устаревшие блокировки — бот на резерве режет продлённого и там; блокировки
держателя, не дошедшие при обрыве сессии, не повторяются — новый
заблокированный ходит через основу без ограничений; бот резерва, пересылающий
слово гостя, когда он никого не несёт, — блокировки чужой линии на основе.
"""
from __future__ import annotations

import os
import time
import types

import pytest

from awgbot.core.blocks import DeviceBlock
from awgbot.domain.services import Services
from awgbot.infra import awg
from awgbot.infra.db import Database
from awgbot.runtime import rsvactions
from awgbot.runtime.pairlink import guestctl
from awgbot.runtime.scheduler import reserve as rsvjobs
from awgbot.util import timeutil
from awgbot.util.rsvpkg import bot as pkgbot

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration.test_reserve_actions import _Handler, _sess
from tests.integration.test_reserve_jobs import _Sched

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv


@pytest.fixture()
def world(mv, fake_awg, make_active_client, monkeypatch, tmp_path):
    """Основа до передачи бота: «Иван» (устройство заблокировано по её базе — срок
    вышел) и «Пётр» (не заблокирован); DROP Ивана стоит. Дальше — следование."""
    svc = mv.svc
    ivan = make_active_client(name="Иван", tg_id=7100)
    idev = svc.add_device(ivan.id, "Телефон")
    petr = make_active_client(name="Пётр", tg_id=7200)
    pdev = svc.add_device(petr.id, "Ноутбук")
    svc.db.device_mask_apply(idev.device_id, or_bits=int(DeviceBlock.EXPIRY))
    mv.a_ivan = svc.db.get_device(idev.device_id).address
    mv.a_petr = svc.db.get_device(pdev.device_id).address
    mv.idev, mv.pdev = idev.device_id, pdev.device_id
    fake_awg.blocked = {mv.a_ivan}
    monkeypatch.setattr(awg, "blocked_ips", lambda: set(fake_awg.blocked), raising=False)
    mv.fake = fake_awg
    mv.tmp = tmp_path
    return mv


def _away(w) -> None:
    rm.go_away(w.svc, gen=7)
    assert w.svc.reserve_following()


def _guest(w) -> Services:
    """Бот основы на резерве: база — реплика базы основы."""
    path = os.path.join(str(w.tmp), "guest", "bot.db")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(pkgbot.replica(w.svc.db.path))
    w.gdb = Database(path)
    return Services(w.gdb)


# ── основа: блокировки держателя ────────────────────────────────────────────

async def test_the_holder_blocks_replace_the_stale_ones_of_the_follower(world):
    """На резерве Ивану продлили (разблокирован), Петра заблокировали: основа в
    следовании снимает DROP Ивана и ставит Петру — по слову держателя, хотя её
    база говорит обратное."""
    _away(world)
    await _Handler(world.svc)._in_blocks(_sess(), {"addrs": [world.a_petr]})
    assert world.fake.blocked == {world.a_petr}, \
        f"DROP следующего не по решениям держателя: {world.fake.blocked}"
    assert int(world.svc.db.get_device(world.idev).block_reason), "база следующего переписана — это решение держателя"


async def test_an_empty_holder_list_unblocks_everyone_left_here(world):
    _away(world)
    await _Handler(world.svc)._in_blocks(_sess(), {"addrs": []})
    assert world.fake.blocked == set(), "держатель никого не блокирует — а DROP устаревшей базы остался"


async def test_the_monitor_reconcile_keeps_the_holder_blocks(world):
    """Такт монитора (reconcile_blocks с prune) после прихода блокировок держателя
    не возвращает блокировки устаревшей базы."""
    _away(world)
    await _Handler(world.svc)._in_blocks(_sess(), {"addrs": [world.a_petr]})
    world.svc.reconcile_blocks(True)
    world.svc.reconcile_blocks(True)
    assert world.fake.blocked == {world.a_petr}, world.fake.blocked


def test_before_the_holder_speaks_the_follower_blocks_by_its_own_base(world):
    """Блокировок держателя ещё не было — DROP по своей базе (решения в ней с
    начала следования не менялись), лишний DROP снимается."""
    _away(world)
    world.fake.blocked = {world.a_petr}
    world.svc.reconcile_blocks(True)
    assert world.fake.blocked == {world.a_ivan}, world.fake.blocked


async def test_holder_blocks_outside_following_are_dropped(world):
    """Пользователи у основы (держатель здесь) — чужие блокировки не принимаются."""
    await _Handler(world.svc)._in_blocks(_sess(), {"addrs": [world.a_petr]})
    assert world.fake.blocked == {world.a_ivan}, "блокировки чужой линии легли на держателя"
    assert world.svc.rsv_get("rsv_follow") == {}


async def test_the_holder_list_is_capped(world):
    _away(world)
    await _Handler(world.svc)._in_blocks(_sess(), {"addrs": [f"10.8.{i // 250}.{i % 250 + 2}" for i in range(5000)]})
    assert len(world.svc.rsv_get("rsv_follow")["blocks"]) == 4096


def test_the_follower_does_not_send_its_stale_blocks_to_the_reserve(world):
    """В следовании основа свои блокировки резерву не шлёт; у основы прежней
    линии (в отъезде, но держатель — пара без журнала) — шлёт, как раньше."""
    _away(world)
    assert world.svc.reserve_blocks_to_send() is None, "следующий шлёт резерву блокировки устаревшей базы"
    world.svc.db.jrn_pass(holder="self", role="holder", why="тест: контроль")
    assert world.svc.reserve_blocks_to_send() == [world.a_ivan]


# ── гость: что и когда слать ────────────────────────────────────────────────

def test_the_guest_sends_its_blocks_on_change_and_every_two_minutes(world, monkeypatch):
    gsvc = _guest(world)
    clock = types.SimpleNamespace(t=5000.0)
    monkeypatch.setattr(time, "monotonic", lambda: clock.t)
    first = gsvc.guest_blocks_to_send()
    assert first == [world.a_ivan], first
    gsvc.guest_blocks_sent(first)
    clock.t += 60
    assert gsvc.guest_blocks_to_send() is None, "без изменений и до 120 с — повтор"
    clock.t += 61
    assert gsvc.guest_blocks_to_send() == [world.a_ivan], "раз в 120 с не повторено (сессия пары могла потерять)"
    gsvc.guest_blocks_sent([world.a_ivan])
    gsvc.db.device_mask_apply(world.pdev, or_bits=int(DeviceBlock.EXPIRY))
    assert gsvc.guest_blocks_to_send() == sorted([world.a_ivan, world.a_petr]), "изменение не отправлено сразу"


def test_a_send_that_did_not_go_is_retried_at_the_next_tick(world, monkeypatch):
    gsvc = _guest(world)
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    assert gsvc.guest_blocks_to_send() is not None
    # отправка не прошла — guest_blocks_sent не звали: следующий такт шлёт снова
    assert gsvc.guest_blocks_to_send() == [world.a_ivan]


# ── резерв: слово гостя — основе, только пока несу её пользователей ─────────

async def test_the_guest_word_reaches_the_reserve_bot(tmp_path):
    srv = guestctl.GuestCtlServer(types.SimpleNamespace(), str(tmp_path / "g.sock"))
    await srv._handle({"kind": "blocks", "addrs": ["10.8.1.5"] * 5000})
    words = srv.take_words()
    assert len(words) == 1 and words[0][0] == "blocks" and len(words[0][1]) == 4096, words[:1]


@pytest.mark.parametrize("inn, relayed", [("serving", True), ("idle", False)])
async def test_the_reserve_relays_the_guest_blocks_only_while_hosting(mv, monkeypatch, inn, relayed):
    svc = mv.svc
    if inn != "idle":
        svc.reserve_set_in(inn)
    ran: list = []

    async def run(actions):
        ran.append(list(actions))
    monkeypatch.setattr(rsvactions, "run", run)
    monkeypatch.setattr(svc, "reserve_out_tick", lambda: [])
    monkeypatch.setattr(svc, "reserve_in_tick", lambda: [])
    monkeypatch.setattr(svc, "reserve_blocks_to_send", lambda: None)
    srv = types.SimpleNamespace(take_words=lambda: [("blocks", ["10.8.1.5"])], online_count=0, connected=lambda: False)
    monkeypatch.setattr(guestctl, "server", lambda: srv)
    sched = _Sched()
    rsvjobs.setup_reserve_jobs(sched, svc, None)
    await sched.jobs["rsv_move_tick"][0]()
    got = [a for acts in ran for a in acts if a and a[0] == "send" and a[1] == "blocks"]
    if relayed:
        assert got == [("send", "blocks", {"addrs": ["10.8.1.5"]})], ran
    else:
        assert got == [], f"резерв без гостя переслал основе чужие блокировки: {ran}"


async def test_the_hosting_reserve_hands_the_primary_blocks_to_its_guest(mv, monkeypatch):
    """Обратное направление прежнее: основа прежней линии шлёт свои блокировки
    резерву, который несёт её пользователей, — они уходят гостю, а не в
    следование резерва."""
    svc = mv.svc
    svc.reserve_set_in("serving", taken_at=timeutil.now_iso())
    got = []

    class Srv:
        async def send(self, kind, body=None):
            got.append((kind, body))
            return True
    monkeypatch.setattr(guestctl, "server", lambda: Srv())
    await _Handler(svc)._in_blocks(_sess(), {"addrs": ["10.8.1.9"]})
    assert got == [("blocks", {"addrs": ["10.8.1.9"]})], got
    assert svc.rsv_get("rsv_follow") == {}
