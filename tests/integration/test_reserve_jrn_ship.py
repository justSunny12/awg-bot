"""Доставка журнала данных основы резерву (journal/ship, keep, route) на двух
базах: сверка до первой отправки, события пачкой за такт, подтверждение,
повтор без подтверждения, досылка по `jneed`, `jstale`, разрыв, пачки
счётчиков, снимок `pkg3` и снятие учтённого у резерва, смешанная пара на
прежнем пути, разбор по линии, когда каждый сервер — основа своей линии и
резерв чужой.

Мир — tests/integration/_reserve_jrn_duo.py: основа «Alpha» (сервисы теста) и
резерв «Beta» (свои сервисы на своей базе), канал — прямой вызов разбора
у второй стороны с проверкой схемой сообщений.

Цена ошибки: основа, отправившая события до сверки с резервом, после
пропажи питания выдаёт уже занятые номера — резерв принимает новые решения
за повтор и отбрасывает, в аварию пользователи поднимаются без них; событие,
сложенное резервом поверх дыры, — база гостя с решениями без предпосылок;
неподтверждённое, которое не повторяется, — резерв навсегда отстал, и
передача бота кнопкой ждёт вечно; снимок, после которого резерв не снял
учтённое, — хранилище растёт без предела и открытие применяет события
дважды; смешанная пара не на прежнем пути — резерв 4.0.x без данных для
переезда; сообщение чужой линии, принятое как своё, — решения второго
сервера пары в базе основы.
"""
from __future__ import annotations

import os

import pytest

from awgbot.core import config
from awgbot.domain.services.reserve import compat
from awgbot.domain.services.reserve.journal import ship
from awgbot.infra.db import Database
from awgbot.domain.services import Services
from awgbot.util.rsvjrn import event
from awgbot.util.rsvjrn.counts import Batch

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_jrn_duo import Duo, sends

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv


@pytest.fixture()
def duo(mv, tmp_path, monkeypatch):
    d = Duo(mv, tmp_path, monkeypatch)
    yield d
    d.close()


def _kept(duo) -> list[int]:
    """Номера событий основы в хранилище резерва."""
    return [event.head(r).n for r in duo.b_keep.events()]


def _bases() -> list[str]:
    path = os.path.join(str(config.DATA_DIR), "reserve", "out")
    return sorted(n for n in (os.listdir(path) if os.path.isdir(path) else []) if n.startswith("base-"))


# ── сверка до первой отправки ───────────────────────────────────────────────

def test_nothing_is_sent_before_the_first_st_of_the_session(duo):
    """Новая сессия: пока не пришёл `st` резерва, такт ничего не шлёт, даже
    если правки есть."""
    duo.journal_on()
    duo.a.reserve_session_opened({})
    duo.edit("Иван")
    assert duo.a.reserve_jrn_tick() == [], "события ушли до сверки с резервом"
    duo.st_b()
    acts = duo.a.reserve_jrn_tick()
    assert [k for k, _ in sends(acts)] == ["jev"], acts


def test_a_snapshot_taken_before_any_event_is_still_a_snapshot(duo):
    """Первый снимок после включения журнала (событий ещё не было, номер снимка 0)
    у резерва есть — новая сессия не требует его заново и события идут сразу."""
    duo.session()
    duo.a.reserve_jrn_sync()
    duo.snapshot()
    assert duo.b.reserve_keep_line() == duo.line
    gen = duo.a.rsv_get("rsv_pkg_sent")["gen"]
    duo.session()
    duo.edit("Иван")
    acts = duo.a.reserve_jrn_tick()
    assert sends(acts, "jev"), "снимок с номером 0 принят за его отсутствие — события ждут нового снимка"
    assert duo.a.rsv_get("rsv_pkg_sent")["gen"] == gen


def test_without_a_snapshot_at_the_reserve_the_snapshot_goes_before_any_event(duo):
    """У резерва нет снимка этой базы — сначала `pkg3` с отметками журнала, события
    — только после его подтверждения и с номера после снимка."""
    duo.session()
    duo.a.reserve_jrn_sync()
    duo.edit("Иван")
    assert duo.a.reserve_jrn_tick() == [], "события ушли раньше снимка"
    built = duo.snapshot()
    assert built["jrn"]["line"] == duo.line and built["jrn"]["i"] == duo.a.db.jrn_last_i(), built["jrn"]
    assert duo.b.rsv_get("rsv_jrn_in")["snap_i"] == built["jrn"]["i"]
    assert duo.a.db.jrn_meta()["acked"] == built["jrn"]["i"], "снимок не подтвердил учтённое в нём"
    assert duo.a.db.jrn_events_after(0) == [], "учтённые снимком события остались у основы"
    duo.edit("Пётр")
    acts = duo.tick()
    ev = sends(acts, "jev")
    assert ev and ev[0][1]["from"] == built["jrn"]["i"] + 1, f"события не с номера после снимка: {acts}"
    assert _kept(duo) and _kept(duo)[0] == built["jrn"]["i"] + 1


def test_a_primary_base_older_than_the_reserve_snapshots_with_a_reset_first(duo, tmp_path):
    """База основы потеряла хвост (копия без последних транзакций): у резерва
    события дальше её последнего номера — до любого события снимок со сбросом,
    резерв стирает своё по этой линии и принимает новые события после снимка
    (её номера у резерва больше не значат прежних решений)."""
    duo.journal_on()
    duo.edit("Иван")
    duo.tick()
    old = str(tmp_path / "old.db")
    import sqlite3
    dst = sqlite3.connect(old)
    duo.a.db._connection().backup(dst)                    # «копия без последних транзакций»
    dst.close()
    duo.edit("Пётр")
    duo.edit("Вера")
    duo.tick()
    acked = duo.b.rsv_get("rsv_jrn_in")["last"]
    assert acked == duo.a.db.jrn_last_i()
    db2 = Database(old)                                    # основа поднялась на старой копии базы
    try:
        db2.init_schema()
        a2 = Services(db2)
        duo.a = a2
        duo.mp.setattr(a2, "reserve_keep_store", lambda: duo.a_keep)
        assert a2.db.jrn_last_i() < acked
        duo.session()
        assert a2.reserve_jrn_tick() == [], "основа шлёт события с занятыми номерами до снимка"
        built = duo.snapshot()
        assert built["jrn"].get("reset") is True, f"снимок после потери хвоста — без сброса: {built['jrn']}"
        assert _kept(duo) == [], "резерв держит события прежней истории линии после сброса"
        kin = duo.b.rsv_get("rsv_jrn_in")
        assert kin["snap_i"] == built["jrn"]["i"] and kin["last"] == built["jrn"]["i"], kin
        a2.db.create_client("Новая", 1, "2026-01-01T00:00:00+03:00", "2099-01-01T00:00:00+03:00", "INV-DUMMY-N")
        acts = duo.tick()
        ev = sends(acts, "jev")
        assert ev and ev[0][1]["from"] == built["jrn"]["i"] + 1, ev
        assert _kept(duo) == list(range(built["jrn"]["i"] + 1, a2.db.jrn_last_i() + 1)), \
            "резерв не принял новую историю после сброса"
        assert a2.db.jrn_meta()["acked"] == a2.db.jrn_last_i()
        duo.session()
        assert sends(a2.reserve_jrn_tick(), "jev") == [] and not duo.a._jrn_sess().get("hold"), \
            "после сброса сверка снова требует снимок"
    finally:
        db2.close()


def test_events_the_reserve_lacks_and_the_primary_still_has_are_resent_from_its_last(duo):
    """Резерв знает меньше, чем основа выдала (сессия оборвалась до `jack`), а
    события у основы ещё есть — досылка с его последнего + 1, без снимка."""
    duo.journal_on()
    duo.edit("Иван")
    have = duo.b.rsv_get("rsv_jrn_in")["last"]
    duo.a.reserve_jrn_tick()                               # ушло и пропало
    duo.session()
    built_before = duo.a.rsv_get("rsv_pkg_sent")["gen"]
    acts = duo.tick()
    ev = sends(acts, "jev")
    assert ev and ev[0][1]["from"] == have + 1, acts
    assert duo.a.rsv_get("rsv_pkg_sent")["gen"] == built_before
    assert _kept(duo)[-1] == duo.a.db.jrn_last_i()


def test_events_the_primary_no_longer_has_mean_a_snapshot(duo):
    """Резерв отстал, а нужных событий у основы уже нет (сняты подтверждением
    прошлого снимка, а резерв восстановлен из копии) — снимок, а не дыра."""
    duo.journal_on()
    duo.edit("Иван")
    duo.tick()
    duo.edit("Пётр")
    duo.tick()
    # у резерва — снимок, но журнал отстал на пачку, которую основа уже удалила
    duo.b.rsv_update("rsv_jrn_in", last=duo.b.rsv_get("rsv_jrn_in")["snap_i"])
    duo.session()
    assert duo.a.reserve_jrn_tick() == [], "основа шлёт события через дыру"
    built = duo.snapshot()
    assert built["jrn"]["i"] == duo.a.db.jrn_last_i()


# ── отправка, подтверждение, повтор ─────────────────────────────────────────

def test_an_event_is_sent_stored_acked_and_then_dropped_at_the_primary(duo):
    duo.journal_on()
    duo.edit("Иван")
    last = duo.a.db.jrn_last_i()
    acts = duo.a.reserve_jrn_tick()
    (kind, body), = sends(acts)
    assert kind == "jev" and body["line"] == duo.line and body["from"] + body["n"] - 1 == last, body
    replies = duo.a_to_b(acts)
    assert sends(replies) == [("jack", {"line": duo.line, "e": body["e"], "i": last})], replies
    assert _kept(duo)[-1] == last
    duo.b_to_a(replies)
    assert duo.a.db.jrn_meta()["acked"] == last
    assert duo.a.db.jrn_events_after(0) == [], "подтверждённые события остались у основы"
    assert duo.a.reserve_jrn_all_acked()


def test_an_idle_tick_sends_nothing(duo):
    """В простое — ни байта: событий нет, таймер счётчиков не подошёл."""
    duo.journal_on()
    assert duo.a.reserve_jrn_tick() == []
    duo.clock.advance(5)
    assert duo.a.reserve_jrn_tick() == []


def test_an_unacked_batch_is_resent_from_the_acked_after_the_wait(duo):
    """`jack` не пришёл — после RESEND_S та же пачка снова с подтверждённого + 1;
    раньше — не повторяется."""
    duo.journal_on()
    acked = duo.a.db.jrn_meta()["acked"]
    duo.edit("Иван")
    first = sends(duo.a.reserve_jrn_tick(), "jev")
    assert first and first[0][1]["from"] == acked + 1
    duo.clock.advance(ship.RESEND_S - 5)
    assert duo.a.reserve_jrn_tick() == [], "повтор раньше срока"
    duo.clock.advance(10)
    again = sends(duo.a.reserve_jrn_tick(), "jev")
    assert again and again[0][1]["from"] == acked + 1 and again[0][1]["n"] == first[0][1]["n"], again
    duo.b_to_a(duo.a_to_b([("send", "jev", again[0][1])]))
    assert duo.a.db.jrn_meta()["acked"] == duo.a.db.jrn_last_i()


def test_a_duplicate_batch_is_not_stored_twice_and_is_acked(duo):
    duo.journal_on()
    duo.edit("Иван")
    acts = duo.a.reserve_jrn_tick()
    duo.a_to_b(acts)
    kept = _kept(duo)
    replies = duo.a_to_b(acts)
    assert _kept(duo) == kept, "повтор пачки записан второй раз"
    assert sends(replies, "jack")[0][1]["i"] == kept[-1]


def test_a_lost_batch_makes_the_reserve_ask_from_its_last_and_the_primary_resends(duo):
    """Первая пачка потерялась, вторая пришла — дыра: резерв ничего не пишет и
    просит `jneed` со своего последнего + 1; основа досылает с него."""
    duo.journal_on()
    last = duo.b.rsv_get("rsv_jrn_in")["last"]
    duo.edit("Иван")
    duo.a.reserve_jrn_tick()                               # потерялась
    duo.edit("Пётр")
    second = duo.a.reserve_jrn_tick()
    replies = duo.a_to_b(second)
    assert sends(replies) == [("jneed", {"line": duo.line, "from": last + 1})], replies
    assert _kept(duo) == [], "резерв сложил события поверх дыры"
    duo.b_to_a(replies)
    resent = sends(duo.a.reserve_jrn_tick(), "jev")
    assert resent and resent[0][1]["from"] == last + 1, resent
    duo.b_to_a(duo.a_to_b([("send", "jev", resent[0][1])]))
    while (more := duo.a.reserve_jrn_tick()):
        duo.b_to_a(duo.a_to_b(more))
    assert _kept(duo) == list(range(last + 1, duo.a.db.jrn_last_i() + 1))


def test_headers_out_of_order_are_refused_whole(duo):
    """Заголовки не подряд или не той эпохи, что в сообщении, — ничего не пишется,
    `jneed` с последнего + 1."""
    duo.journal_on()
    duo.edit("Иван")
    (_k, body), = sends(duo.a.reserve_jrn_tick())
    recs = event.split(bytes.fromhex(body["blob"]))
    bad = dict(body, blob=b"".join(reversed(recs)).hex())
    last = duo.b.rsv_get("rsv_jrn_in")["last"]
    assert sends(duo.b.reserve_jrn_in("jev", bad)) == [("jneed", {"line": duo.line, "from": last + 1})]
    assert _kept(duo) == []
    other_e = dict(body, e=body["e"] + 1)
    assert sends(duo.b.reserve_jrn_in("jev", other_e), "jneed"), "эпоха сообщения не та, что в заголовках, — принято"
    assert _kept(duo) == []


def test_a_reserve_without_the_line_asks_for_a_snapshot(duo):
    """Резерв не держит снимка этой линии (снял пару, wipe) — `jneed` с нуля, у основы
    — снимок заново."""
    duo.journal_on()
    duo.b.reserve_keep_drop()
    duo.edit("Иван")
    replies = duo.a_to_b(duo.a.reserve_jrn_tick())
    assert sends(replies) == [("jneed", {"line": duo.line, "from": 0})], replies
    duo.b_to_a(replies)
    assert duo.a.reserve_jrn_tick() == [], "после просьбы снимка основа продолжает слать события"
    built = duo.snapshot()
    assert duo.b.rsv_get("rsv_jrn_in")["snap_i"] == built["jrn"]["i"]


def test_a_jneed_for_events_already_gone_means_a_snapshot(duo):
    duo.journal_on()
    duo.edit("Иван")
    duo.tick()
    duo.edit("Пётр")
    first = duo.a.db.jrn_first_i()
    duo.a.reserve_jrn_in("jneed", {"line": duo.line, "from": first - 1})
    assert duo.a.reserve_jrn_tick() == [], "основа шлёт через дыру вместо снимка"


# ── jstale ──────────────────────────────────────────────────────────────────

def test_an_event_of_an_older_epoch_gets_jstale_and_the_primary_follows_only_if_the_reserve_serves(duo):
    """Резерв знает эпоху линии больше (гость у него взял её) — `jstale` в ответ;
    основа уходит в следование, только если резерв несёт её пользователей."""
    duo.journal_on()
    known = duo.a.db.jrn_meta()["epoch"] + 1
    duo.b.rsv_update("rsv_jrn_in", e=known)
    duo.edit("Иван")
    replies = duo.a_to_b(duo.a.reserve_jrn_tick())
    assert sends(replies) == [("jstale", {"line": duo.line, "e": known})], replies
    assert _kept(duo) == [], "событие старой эпохи сложено резервом"
    duo.b_to_a(replies)
    assert duo.a.db.jrn_meta()["role"] == "holder", "основа ушла в следование, хотя резерв её пользователей не несёт"
    rm.st_in(duo.a, caps=duo.b_caps, **{"in": "serving"})
    duo.b_to_a(replies)
    m = duo.a.db.jrn_meta()
    assert (m["epoch"], m["holder"], m["role"]) == (known, "peer", "follower"), m


# ── счётчики ────────────────────────────────────────────────────────────────

def _dev(duo) -> int:
    return duo.a.db.list_all_devices()[-1].id


def test_counts_of_side_a_go_by_the_timer_and_are_dropped_after_jcack(duo):
    duo.edit("Иван")
    duo.journal_on()
    did = _dev(duo)
    duo.a.reserve_jrn_tick()                               # таймер счётчиков заведён
    duo.a.db.add_traffic_bulk([(did, 100, 200)])
    assert duo.a.reserve_jrn_tick() == [], "пачка счётчиков без таймера и без событий"
    duo.clock.advance(43 * 60)
    acts = duo.a.reserve_jrn_tick()
    (kind, body), = sends(acts)
    assert kind == "jcnt" and body["side"] == "a", acts
    replies = duo.a_to_b(acts)
    assert sends(replies) == [("jcack", {"line": duo.line, "side": "a", "s": body["s"]})]
    assert [event.head(r).n for r in duo.b_keep.counts("a")] == [body["s"]]
    _side, batch = event.open_counts(duo.a.reserve_k(), duo.b_keep.counts("a")[0], duo.line)
    assert sum(v[0] for devs in batch.months.values() for v in devs.values()) == 100
    duo.b_to_a(replies)
    assert duo.a.db._connection().execute("SELECT COUNT(*) FROM jrn_counts").fetchone()[0] == 0, \
        "подтверждённые дельты остались у основы"
    duo.clock.advance(43 * 60)
    assert duo.a.reserve_jrn_tick() == [], "пустая пачка ушла"


def test_counts_go_together_with_an_event_batch(duo):
    duo.edit("Иван")
    duo.journal_on()
    duo.a.db.add_traffic_bulk([(_dev(duo), 5, 5)])
    duo.edit("Пётр")
    kinds = [k for k, _ in sends(duo.a.reserve_jrn_tick())]
    assert kinds == ["jev", "jcnt"], kinds


def test_a_repeated_counts_batch_is_not_stored_twice(duo):
    duo.edit("Иван")
    duo.journal_on()
    duo.a.db.add_traffic_bulk([(_dev(duo), 5, 5)])
    duo.edit("Пётр")
    acts = duo.a.reserve_jrn_tick()
    duo.a_to_b(acts)
    replies = duo.a_to_b(acts)
    assert len(duo.b_keep.counts("a")) == 1, "повтор пачки счётчиков сложен второй раз"
    assert sends(replies, "jcack"), "на повтор нет подтверждения — основа повторяет вечно"


def test_counts_of_side_b_are_not_kept_by_the_reserve(duo):
    """Пачка клона (b) — не резерву на хранение: её складывает основа."""
    duo.journal_on()
    k = duo.a.reserve_k()
    raw = event.seal_counts(k, duo.line, "b", Batch(host=f"{duo.line}:b", bn=1, months={}))
    assert duo.b.reserve_jrn_in("jcnt", {"line": duo.line, "side": "b", "s": 1, "blob": raw.hex()}) == []
    assert duo.b_keep.counts("b") == [] and duo.b_keep.counts("a") == []


# ── снимок pkg3 ─────────────────────────────────────────────────────────────

def test_a_snapshot_removes_what_it_holds_at_the_reserve(duo):
    """Снимок `pkg3` по номер i: у резерва события ≤ i и пачки ≤ cs сняты, у
    основы — подтверждено по i, базы слияния нет."""
    duo.edit("Иван")
    duo.journal_on()
    did = _dev(duo)
    duo.edit("Пётр")
    duo.a.db.add_traffic_bulk([(did, 1, 1)])
    duo.tick()
    assert _kept(duo) and duo.b_keep.counts("a")
    duo.edit("Вера")
    built = duo.snapshot()
    assert built["jrn"]["i"] == duo.a.db.jrn_last_i()
    assert _kept(duo) == [], f"резерв не снял учтённое снимком: {_kept(duo)}"
    assert duo.b_keep.counts("a") == [], "пачки, учтённые снимком, остались у резерва"
    assert duo.a.db.jrn_meta()["acked"] == built["jrn"]["i"] and duo.a.db.jrn_meta()["snap_i"] == built["jrn"]["i"]
    assert _bases() == [], f"база слияния пишется на пути журнала: {_bases()}"
    duo.edit("Ольга")
    duo.tick()
    assert _kept(duo)[0] == built["jrn"]["i"] + 1


def test_a_data_edit_on_the_journal_path_does_not_mark_the_package(duo):
    """Правка данных пользователей — событие, а не новый пакет."""
    duo.journal_on()
    duo.a.reserve_pkg_tick()
    duo.a.__dict__.pop("_rsv_pkg_due_at", None)
    duo.edit("Иван")
    assert duo.a.__dict__.get("_rsv_pkg_due_at") is None, "правка данных пометила пакет"
    assert duo.a.reserve_pkg_build() is None, "снимок без изменений вне базы собрался по таймеру"


def test_many_events_after_the_snapshot_make_a_snapshot_due(duo, monkeypatch):
    duo.journal_on()
    assert duo.a.reserve_jrn_snap_due() is False
    monkeypatch.setattr(ship, "SNAP_EVENTS", 3)
    duo.edit("Иван")
    duo.edit("Пётр")
    assert duo.a.reserve_jrn_snap_due() is True, "порог событий после снимка не сработал"


# ── смешанная пара: прежний путь ────────────────────────────────────────────

def test_a_reserve_without_jrn1_keeps_the_old_path_whole(duo):
    """Резерв 4.0.x: захвата нет, событий нет, пакет `pkg2` без части jrn,
    база слияния пишется, такт журнала молчит, `st` без jrn."""
    duo.b_caps = list(compat.CAPS)
    duo.session()
    duo.a.reserve_jrn_sync()
    assert duo.a.db.jrn_meta()["capture"] == 0
    duo.edit("Иван")
    assert duo.a.db.jrn_events_after(0) == [], "события пишутся без журнала у резерва"
    assert duo.a.reserve_jrn_tick() == []
    built = duo.snapshot()
    assert "jrn" not in built, built.keys()
    assert _bases() == [f"base-{built['gen']}.db"], _bases()
    assert "jrn" not in duo.a.reserve_st_body()


def test_the_old_path_hands_over_the_bot_with_a_replica_for_the_merge(duo):
    """Без журнала у резерва передача бота кнопкой везёт финальную реплику (по ней
    при возврате идёт слияние), а не released{epoch, last}."""
    duo.b_caps = list(compat.CAPS)
    duo.session()
    duo.a.reserve_jrn_sync()
    duo.edit("Иван")
    rm.to_step(duo.a, "release")
    built = duo.a.reserve_release_build()
    assert built is not None and built.get("blob"), f"прежний путь без реплики: {built}"
    assert "epoch" not in built and "last" not in built, built


def test_turning_to_the_old_path_drops_the_journal_on_both_sides(duo):
    """Резерв откатился на 4.0.x: захват выключен, накопленное снято, пакет
    заново; резерв на `pkg2` снимает хранилище журнала."""
    duo.journal_on()
    duo.edit("Иван")
    duo.tick()
    duo.edit("Пётр")
    duo.b_caps = list(compat.CAPS)
    duo.st_b()
    duo.a.reserve_jrn_sync()
    assert duo.a.db.jrn_meta()["capture"] == 0
    assert duo.a.db.jrn_events_after(0) == [], "события прежней схемы остались"
    assert duo.a.__dict__.get("_rsv_pkg_due_at") is not None, "смена пути не пометила пакет"
    built = duo.snapshot()
    assert "jrn" not in built
    assert duo.b.reserve_keep_line() == "" and _kept(duo) == [], "резерв на pkg2 держит журнал"


def test_no_blocks_message_on_the_journal_path_and_blocks_on_the_old_one(duo):
    """Блокировки держателя для оставшихся у основы сессий: с журналом они уже в
    базе основы (события), прежнее сообщение `blocks` не в ходу; без журнала —
    по сообщению держателя."""
    duo.journal_on()
    rm.go_away(duo.a)
    duo.a.reserve_epoch_sync()
    assert duo.a.reserve_following()
    duo.a.reserve_holder_blocks_in(["10.8.1.50"])
    assert duo.a.reserve_holder_blocks() is None, "с журналом основа берёт блокировки из сообщения, а не из базы"
    duo.b_caps = list(compat.CAPS)
    duo.st_b()
    assert duo.a.reserve_holder_blocks() == ["10.8.1.50"], "прежний путь потерял blocks держателя"


# ── разбор по линии ─────────────────────────────────────────────────────────

def test_each_server_is_primary_of_its_line_and_reserve_of_the_other(duo):
    """Alpha и Beta шлют друг другу события своих линий одним каналом: у каждого
    свои события подтверждаются, чужие ложатся в его хранилище, не в базу."""
    duo.journal_on()
    b, a = duo.b, duo.a
    bline = b.db.jrn_line()
    # Beta — основа своей линии: снимок у Alpha
    a.reserve_keep_snapshot_in({"line": bline, "e": 0, "i": 0, "cs": 0})
    b.db.jrn_capture_set(True)
    b.db.create_client("Бета-1", 1, "2026-01-01T00:00:00+03:00", "2099-01-01T00:00:00+03:00", "INV-DUMMY-B1")
    bevs = b.db.jrn_events_after(0)
    kb = os.urandom(32)
    jev_b = {"line": bline, "e": 0, "from": bevs[0].i, "n": len(bevs),
             "blob": b"".join(event.seal_event(kb, bline, ev) for ev in bevs).hex()}
    clients_before = len(a.db.list_clients())
    duo.edit("Иван")
    a_acts = a.reserve_jrn_tick()
    # встречные сообщения
    to_b = duo.a_to_b(a_acts)
    to_a = a.reserve_jrn_in("jev", jev_b)
    assert sends(to_a) == [("jack", {"line": bline, "e": 0, "i": bevs[-1].i})], to_a
    assert [event.head(r).n for r in duo.a_keep.events()] == [ev.i for ev in bevs], "события Beta не у Alpha на хранении"
    assert len(a.db.list_clients()) == clients_before + 1, "события второй линии попали в базу основы"
    duo.b_to_a(to_b)
    assert a.db.jrn_meta()["acked"] == a.db.jrn_last_i(), "свой jack не дошёл до своей линии"
    # jack Alpha на линию Beta — ответ основы Beta её боту у меня: только гостю
    assert b.reserve_jrn_in("jack", {"line": bline, "e": 0, "i": 1}) == [], \
        "ответ на свою линию у Beta ушёл не туда"


def test_a_message_of_an_unknown_line_is_dropped(duo):
    duo.journal_on()
    acked = duo.a.db.jrn_meta()["acked"]
    assert duo.a.reserve_jrn_in("jack", {"line": "00" * 8, "e": 0, "i": 999}) == []
    assert duo.a.db.jrn_meta()["acked"] == acked, "jack чужой линии сдвинул подтверждённое"


# ── правки по ревью: пачки счётчиков ────────────────────────────────────────

def _cnt_due(duo) -> list[tuple]:
    """Такт с подошедшим таймером счётчиков."""
    duo.a.reserve_jrn_tick()
    duo.clock.advance(43 * 60)
    return sends(duo.a.reserve_jrn_tick(), "jcnt")


def _open_cnt(duo, body) -> Batch:
    return event.open_counts(duo.a.reserve_k(), bytes.fromhex(body["blob"]), duo.line)[1]


def test_an_unacked_counts_batch_is_repeated_as_the_same_and_new_rows_wait(duo):
    """Пачка без jcack повторяется той же (номер, строки, суммы), хотя появились
    новые дельты; после подтверждения новые — следующей пачкой без перекрытия."""
    duo.edit("Иван")
    duo.journal_on()
    did = _dev(duo)
    duo.a.db.add_traffic_bulk([(did, 100, 100)])
    (_k, first), = _cnt_due(duo)                          # потерялась
    duo.a.db.add_traffic_bulk([(did, 7, 7)])
    (_k, again), = _cnt_due(duo)
    b1, b2 = _open_cnt(duo, first), _open_cnt(duo, again)
    assert (b2.bn, b2.s0, b2.s1, b2.months) == (b1.bn, b1.s0, b1.s1, b1.months), \
        "неподтверждённая пачка повторена другой — получатель сложит её дважды или потеряет"
    duo.b_to_a(duo.a_to_b([("send", "jcnt", again)]))
    (_k, nxt), = _cnt_due(duo)
    b3 = _open_cnt(duo, nxt)
    assert b3.bn == b1.bn + 1 and b3.s0 == b1.s1, f"перекрытие или дыра между пачками: {b1} → {b3}"
    assert sum(v[0] for d in b3.months.values() for v in d.values()) == 7


def test_a_jcack_of_another_batch_drops_nothing(duo):
    duo.edit("Иван")
    duo.journal_on()
    duo.a.db.add_traffic_bulk([(_dev(duo), 5, 5)])
    (_k, body), = _cnt_due(duo)
    duo.a.reserve_jrn_in("jcack", {"line": duo.line, "side": "a", "s": body["s"] + 3})
    assert duo.a.db._connection().execute("SELECT COUNT(*) FROM jrn_counts").fetchone()[0] == 1, \
        "подтверждение чужого номера сняло дельты"


def test_counts_are_not_folded_while_a_snapshot_is_in_flight(duo):
    """Снимок собран и не подтверждён — пачка не сворачивается (её строки в снимке);
    после pkg_ok — только дельты после снимка."""
    duo.edit("Иван")
    duo.journal_on()
    did = _dev(duo)
    duo.a.db.add_traffic_bulk([(did, 100, 100)])
    built = duo.a.reserve_pkg_build(force=True)
    assert _cnt_due(duo) == [], "пачка свернута из строк, которые уже в снимке в пути"
    duo.deliver_pkg(built)
    duo.a.db.add_traffic_bulk([(did, 3, 3)])
    (_k, body), = _cnt_due(duo)
    b = _open_cnt(duo, body)
    assert sum(v[0] for d in b.months.values() for v in d.values()) == 3, f"в пачке дельты из снимка: {b}"


def test_a_snapshot_covers_the_unacked_batch_and_the_reserve_drops_it_by_cb(duo):
    """Пачка ушла без jcack, следом снимок: снимок учёл её строки (cb), у резерва она
    снята, повторно не уходит, опоздавшая к резерву — не хранится."""
    duo.edit("Иван")
    duo.journal_on()
    duo.a.db.add_traffic_bulk([(_dev(duo), 9, 9)])
    (_k, late), = _cnt_due(duo)
    built = duo.snapshot()
    assert built["jrn"]["cb"] == late["s"], built["jrn"]
    assert "cs" not in built["jrn"], "номер строки счётчиков открыт резерву"
    assert _cnt_due(duo) == [], "пачка, учтённая снимком, ушла снова"
    replies = duo.a_to_b([("send", "jcnt", late)])
    assert duo.b_keep.counts("a") == [], "пачка, учтённая снимком, сложена в хранилище"
    assert sends(replies, "jcack")


def test_the_batch_numbers_never_go_below_what_the_reserve_holds(duo):
    """Состояние счётчиков основы потеряно (восстановление): резерв называет номер
    пачки больше — новые пачки с номера после него, иначе резерв их отбросит."""
    duo.edit("Иван")
    duo.journal_on()
    duo.a.db.add_traffic_bulk([(_dev(duo), 4, 4)])
    duo.b_keep.append_counts("a", [event.seal_counts(duo.a.reserve_k(), duo.line, "a",
                                                     Batch(host=f"{duo.line}:a", bn=50, months={}))])
    duo.session()
    (_k, body), = _cnt_due(duo)
    assert body["s"] > 50, body["s"]
    duo.a_to_b([("send", "jcnt", body)])
    assert [event.head(r).n for r in duo.b_keep.counts("a")][-1] == body["s"]


# ── правки по ревью: эпоха, тишина, пределы ─────────────────────────────────

def test_the_holder_sends_no_events_while_the_reserve_serves(duo):
    """Резерв несёт моих пользователей (serving) — держатель там: свои события не шлю."""
    duo.journal_on()
    duo.edit("Иван")
    duo.st_b(**{"in": "serving"})
    assert sends(duo.a.reserve_jrn_tick(), "jev") == []


def test_the_journal_is_quiet_during_a_return(duo):
    duo.journal_on()
    duo.edit("Иван")
    duo.a.reserve_set_out("returning", kind="button", gen=7)
    assert duo.a.reserve_jrn_tick() == []


def test_a_jack_beyond_what_was_sent_is_ignored(duo):
    duo.journal_on()
    duo.edit("Иван")
    acked = duo.a.db.jrn_meta()["acked"]
    duo.a.reserve_jrn_in("jack", {"line": duo.line, "e": 0, "i": duo.a.db.jrn_last_i()})
    assert duo.a.db.jrn_meta()["acked"] == acked, "подтверждено то, что не отправлялось"
    assert duo.a.db.jrn_events_after(acked), "неотправленные события удалены по чужому jack"


def test_a_jneed_beyond_the_last_is_ignored(duo):
    duo.journal_on()
    duo.edit("Иван")
    duo.a.reserve_jrn_in("jneed", {"line": duo.line, "from": duo.a.db.jrn_last_i() + 50})
    assert not duo.a._jrn_sess().get("hold"), "просьба дальше последнего привела к снимку"


def test_the_reserve_answers_jstale_with_the_epoch_of_its_guest(duo):
    """Гость у резерва взял эпоху (авария) — событие основы прежней эпохи:
    jstale с эпохой гостя, в хранилище не ложится."""
    duo.journal_on()
    e = duo.a.db.jrn_meta()["epoch"]
    duo.b.rsv_update("rsv_in", state="serving")
    duo.b.__dict__["_rsv_guest_jrn"] = {"e": e + 1, "fork": duo.a.db.jrn_last_i()}
    duo.edit("Иван")
    replies = duo.a_to_b(duo.a.reserve_jrn_tick())
    assert sends(replies) == [("jstale", {"line": duo.line, "e": e + 1})], replies
    assert _kept(duo) == []


def test_the_reserve_learns_the_epoch_of_the_primary_from_its_st(duo):
    """Смена роли основы без событий: эпоха из её st — известная резерву и в отметке
    для процесса открытия; неправдоподобная — пропуск."""
    import json
    duo.journal_on()
    e = duo.b.rsv_get("rsv_jrn_in")["e"]
    duo.b.reserve_keep_mine_in({"line": duo.line, "e": e + 2})
    assert duo.b.rsv_get("rsv_jrn_in")["e"] == e + 2
    meta = json.loads((duo.b_keep.path / "meta.json").read_text(encoding="utf-8"))
    assert meta == {"line": duo.line, "e": e + 2}, meta
    duo.b.reserve_keep_mine_in({"line": duo.line, "e": e + 10**7})
    assert duo.b.rsv_get("rsv_jrn_in")["e"] == e + 2, "эпоха вне разумного принята"
    duo.b.reserve_keep_mine_in({"line": "00" * 8, "e": e + 5})
    assert duo.b.rsv_get("rsv_jrn_in")["e"] == e + 2, "эпоха чужой линии принята"


def test_a_new_k_means_a_snapshot_with_a_reset(duo):
    """Новый K (после возврата): записи у резерва под прежним ключом — снимок со
    сбросом раньше событий."""
    duo.journal_on()
    duo.a.reserve_new_k()
    duo.edit("Иван")
    assert duo.a.reserve_jrn_tick() == []
    built = duo.snapshot()
    assert built["jrn"]["reset"] is True and _kept(duo) == []
