"""Эпоха кворума и журнал данных на двух базах (journal/guestj.py, quorum/fence.py,
infra/db/jrn.py): точка расхождения при принятии эпохи по пробе, огороженный гость.

- основа принимает эпоху гостя по пробе голосующего: точка расхождения —
  подтверждённое резервом (её неподтверждённые решения — в хвост, не затираются),
  события гостя после неё применяются, а не теряются;
- гость после перезапуска стартует огороженным («жду состав», self/follower):
  `takeover` от бота резерва эпоху не поднимает и вебхук не даёт — ограждение
  снимают состав и голоса, иначе эпоха + 1 и вебхук на каждый hello;
- возврат по кнопке при огороженном госте: он отдаёт эпоху основе (holder peer), а не
  остаётся self/follower под прежней e — иначе пробы голосующих сняли бы ограждение,
  и он снова писал бы под той же эпохой, что основа.

Мир — основа «Alpha», резерв «Beta» и гость на трёх базах
(tests/integration/_reserve_jrn_duo.py); процесс открытия — настоящий
(util/rsvjrn/opener), ключи — заглушки.

Цена ошибки: решения, принятые гостем за время аварии, молча исчезают у основы
(базы расходятся); два писателя под одной эпохой; вебхук у гостя без большинства «да».
"""
from __future__ import annotations

import pytest

from awgbot.core import config
from awgbot.domain.services import Services
from awgbot.infra import awg

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_jrn_duo import Duo

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv

_START, _FUTURE = "2026-01-01T00:00:00+03:00", "2099-01-01T00:00:00+03:00"


@pytest.fixture()
def duo(mv, tmp_path, monkeypatch, fake_awg):
    monkeypatch.setattr(awg, "blocked_ips", lambda: set(fake_awg.blocked), raising=False)
    d = Duo(mv, tmp_path, monkeypatch)
    yield d
    d.close()


def _opened(duo) -> None:
    duo.journal_on()
    cid = duo.edit("Иван")
    duo.a.db.update_client_fields(cid, tg_id=7100)
    duo.tick()
    duo.open_guest(button=False)                              # аварийное открытие: гость держатель


def _restart_guest(duo) -> None:
    """Перезапуск процесса гостя: те же база и диск, сервисы с нуля."""
    old = config.GUEST
    config.GUEST = True
    try:
        duo.g = Services(duo.gdb)
    finally:
        config.GUEST = old


# ── точка расхождения при принятии по пробе ─────────────────────────────────

def test_adopting_the_guests_epoch_keeps_my_unconfirmed_decisions_in_the_tail_and_takes_his(duo):
    _opened(duo)
    with duo.as_guest():
        g = duo.gdb.jrn_meta()
    duo.edit("Пётр")                                          # основа отрезана: решение, которого резерв не подтвердил
    a0 = duo.a.db.jrn_meta()
    assert duo.a.db.jrn_last_i() > int(a0["acked"]), "подготовка: нет неподтверждённого решения"
    duo.a.reserve_quorum_adopt(int(g["epoch"]), "пробы a1")   # связь вернулась: проба голосующего с эпохой гостя
    a = duo.a.db.jrn_meta()
    assert (a["holder"], a["role"], a["epoch"]) == ("peer", "follower", int(g["epoch"]))
    assert int(a["applied"]) == int(a0["acked"]), f"применённое не на подтверждённом: {a['applied']} vs acked {a0['acked']}"
    assert [ev.i for ev in duo.a.db.jrn_tail_rows(("hold",))], "неподтверждённые решения основы не отложены в хвост"
    with duo.as_guest():
        cid = duo.gdb.create_client("Гостевой", 3, _START, _FUTURE, "INV-DUMMY-G")
        msgs = duo.g.guest_jrn_tick()
    assert [k for k, _ in msgs if k == "jev"], "подготовка: гость не отдал событие"
    duo.b_to_a(duo.guest_to_b(msgs))
    got = duo.a.db.get_client(cid)
    assert got is not None and got.name == "Гостевой", "решение гостя после принятия эпохи потеряно у основы"


# ── огороженный гость ───────────────────────────────────────────────────────

def test_takeover_does_not_bump_the_epoch_of_a_fenced_guest(duo):
    _opened(duo)
    with duo.as_guest():
        m0 = duo.gdb.jrn_meta()
    _restart_guest(duo)
    with duo.as_guest():
        duo.g.reserve_quorum_guest_start()
        m1 = duo.gdb.jrn_meta()
        assert (m1["holder"], m1["role"]) == ("self", "follower") and not duo.g.guest_can_take_tg()
        for _ in range(3):
            duo.g.guest_jrn_in("takeover", {})                # бот резерва на каждый hello при tg_take_forced
        m2 = duo.gdb.jrn_meta()
        assert (m2["holder"], m2["role"]) == ("self", "follower"), "takeover снял ограждение «жду состав»"
        assert m2["epoch"] == m0["epoch"], "takeover поднял эпоху уже своей линии"
        assert not duo.g.guest_can_take_tg() and not duo.g.guest_jrn_take_tg(), "вебхук гостю до большинства «да»"


def test_a_fenced_guest_hands_the_epoch_over_on_release(duo):
    _opened(duo)
    _restart_guest(duo)
    with duo.as_guest():
        duo.g.reserve_quorum_guest_start()
        e = duo.gdb.jrn_meta()["epoch"]
        body = duo.g.guest_jrn_release()
        m = duo.gdb.jrn_meta()
        assert (m["holder"], m["role"]) == ("peer", "follower"), f"огороженный гость не отдал эпоху: {m['holder']}/{m['role']}"
        assert body["epoch"] == e and m["epoch"] == e + 1, "released несёт не ту эпоху или эпоха основе не + 1"
        assert not duo.g.guest_can_take_tg()
