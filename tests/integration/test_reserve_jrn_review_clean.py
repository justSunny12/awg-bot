"""Журнал данных резерва после чистки кода по ревью 4.1.0: общие помощники
вместо копий (journal/route, tail, ship, keep, guestj, package) — на трёх
базах мира tests/integration/_reserve_jrn_duo.py и по настоящему локальному
каналу гостя (runtime/pairlink/guestctl.py).

- досылка гостю из хранилища резерва — номера подряд: пропуск в хранилище
  (запасной `.N`-файл рядом с испорченным сегментом) не прикрывается;
- сборка частей хвоста (`jtail`, `jtail_ok`): другая сборка посреди — прежние
  части забыты, повтор полным набором проходит;
- флаг «снимок в пути» снимается на каждом исходе снимка, а не только на
  `pkg_ok`;
- слово гостя о своём журнале (hello по сокету) доходит до сервисов резерва
  числами;
- слишком большая пачка счётчиков у гостя — строка журнала один раз, после
  `jcack` — снова;
- `released` чужой линии — строкой журнала и у резерва, и у гостя.

Ключи — заглушки или случайные.

Цена ошибки: резерв, отдавший гостю события через дыру, — гость применяет
правки основы не по порядку или навсегда застревает в следовании, а люди на
резерве не могут ничего изменить; сборка хвоста, склеившая части двух разных
отправок, — хвост не открывается никогда, и правки, сделанные перед аварией,
пропадают без Т-Ж4; флаг снимка, не снятый после отказа резерва, — пачки
счётчиков не уходят до следующего удачного снимка, лимиты трафика у резерва
считаются без расхода у основы; слово гостя, не дошедшее до сервисов, —
резерв хранит события старой эпохи основы поверх эпохи гостя; строка о
большой пачке на каждый такт — журнал хоста забит, а после подтверждения
молчание — админ не узнаёт, что пачка снова застряла.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import types

import pytest

from awgbot.domain.services.reserve import package
from awgbot.runtime.pairlink import guestctl
from awgbot.util import pairproto
from awgbot.util.rsvjrn import event
from awgbot.util.rsvjrn.segments import Store
from awgbot.util.rsvpkg import bot as pkgbot

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_jrn_duo import Duo, checked, sends, to_guest

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv

_START, _FUTURE = "2026-01-01T00:00:00+03:00", "2099-01-01T00:00:00+03:00"


@pytest.fixture()
def duo(mv, tmp_path, monkeypatch, fake_awg):
    d = Duo(mv, tmp_path, monkeypatch)
    d.fake = fake_awg
    # часы пакета — те же, что у журнала (таймер «пакет в пути» — 120 с)
    monkeypatch.setattr(package, "time", types.SimpleNamespace(monotonic=d.clock.monotonic))
    yield d
    d.close()


def _lines(caplog, part: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if part in r.getMessage()]


def _kept(store: Store) -> list[int]:
    return [event.head(r).n for r in store.events()]


# ── досылка гостю из хранилища: номера подряд ───────────────────────────────

def _store_with_a_hole(duo) -> tuple[int, int]:
    """Хранилище резерва с пропуском: события Ивана и Петра подтверждены, в сегменте
    испорчена запись посреди (мусор — не обрыв), следующие правки основы легли в
    запасной файл рядом. Возвращает (первый пропавший номер, первый после пропуска)."""
    duo.journal_on()
    duo.edit("Иван")
    duo.edit("Пётр")
    duo.tick()
    seg, = [p for p in duo.b_keep.path.iterdir() if p.name.startswith("ev-")]
    recs = Store._read(seg)
    assert len(recs) >= 4, f"условие: в сегменте мало записей для дыры посреди: {len(recs)}"
    mid = len(recs) // 2
    hole = event.head(recs[mid]).n
    off = sum(len(r) for r in recs[:mid])
    data = bytearray(seg.read_bytes())
    data[off:off + 2] = b"\xff\xff"                       # метка записи испорчена — дальше чтение не идёт
    seg.write_bytes(bytes(data))
    duo.edit("Олег")
    duo.tick()
    spare = [p.name for p in duo.b_keep.path.iterdir() if p.name.startswith("ev-") and p != seg]
    assert spare and all(n.count(".") == 2 for n in spare), \
        f"условие: дописка после мусора — не в запасной файл рядом: {sorted(os.listdir(duo.b_keep.path))}"
    kept = _kept(duo.b_keep)
    assert hole not in kept, f"условие: испорченная запись {hole} всё ещё читается: {kept}"
    return hole, next(n for n in kept if n > hole)


def test_the_reserve_gives_the_guest_only_the_run_before_a_hole_in_its_store(duo):
    """`jneed` гостя с номера до дыры: из хранилища — только записи подряд до неё, не
    «через» пропуск (гость применил бы события основы с разрывом)."""
    hole, after = _store_with_a_hole(duo)
    first = _kept(duo.b_keep)[0]
    acts = duo.guest_to_b([("jneed", {"line": duo.line, "from": first})])
    (_k, body), = to_guest(acts, "jev")
    got = [event.head(r).n for r in event.split(bytes.fromhex(body["blob"]))]
    assert got == list(range(first, hole)), f"досылка гостю перескочила пропуск {hole}…{after - 1}: {got}"
    assert (body["from"], body["n"]) == (first, len(got)), body


def test_a_jneed_into_a_hole_gives_the_guest_nothing(duo):
    """`jneed` гостя с первого пропавшего номера: из хранилища гостю — ничего (запись
    после дыры — не та, с которой он просит)."""
    hole, _after = _store_with_a_hole(duo)
    acts = duo.guest_to_b([("jneed", {"line": duo.line, "from": hole})])
    assert not to_guest(acts), f"резерв отдал гостю события не с того номера: {acts}"


def test_a_jneed_into_a_hole_is_a_warning_once_per_number(duo, caplog):
    """Досылать из хранилища нечего, и основе переслать некому — подтверждённые события она
    у себя не держит: гостю ничего, а дыра — строкой журнала уровня warning, один раз на
    номер (гость повторяет `jneed` каждые JRN_NEED_S); о недошедших решениях основа скажет
    после возврата."""
    import logging
    caplog.set_level(logging.WARNING)
    hole, _after = _store_with_a_hole(duo)
    for _ in range(3):
        acts = duo.guest_to_b([("jneed", {"line": duo.line, "from": hole})])
        assert not acts, f"на дыру в хранилище резерв что-то ответил: {acts}"
    told = _lines(caplog, f"нет записи {hole}")
    assert len(told) == 1, told


# ── сборка частей хвоста ────────────────────────────────────────────────────

def _crash_world(duo) -> dict:
    """Авария: правки основы перед ней не дошли (хвост), гость открылся по журналу и
    сделал своё, основа узнала его эпоху и ушла в следование."""
    duo.journal_on()
    ivan = duo.edit("Иван")
    duo.tick()
    duo.a.db.update_client_fields(ivan, name="Иван здесь")
    v = duo.a.db.create_client("Вера", 1, _START, _FUTURE, "INV-DUMMY-V")
    duo.a.db.create_device(v, "Планшет", "DUMMY-pub-vera", "DUMMY-psk", "10.8.1.77", private_key="DUMMY-priv-vera")
    duo.open_guest(button=False)
    with duo.as_guest():
        duo.g.db.update_client_fields(ivan, name="Иван на резерве")
    gm = duo.gdb.jrn_meta()
    keep = duo.b.reserve_keep_view()
    keep.update(guest_e=gm["epoch"], guest_fork=gm["fork_i"])
    duo.b.reserve_guest_jrn_seen({"e": gm["epoch"], "fork": gm["fork_i"]})
    rm.st_in(duo.a, caps=list(duo.b_caps), jrn={"keep": keep}, **{"in": "serving"})
    rm.go_away(duo.a, kind="dead")
    duo.a.reserve_epoch_sync()
    assert duo.a.reserve_following() and duo.a.db.jrn_tail_rows(), "условие: основа следует и держит хвост"
    duo.a.__dict__.pop("_tail_sent_at", None)
    (body,) = [checked(k, b) for k, b in sends(duo.a.reserve_jrn_tick(), "jtail")]
    return body


def _split(body: dict, n: int) -> list[dict]:
    """Тот же хвост n частями (получатель склеивает hex частей по порядку)."""
    blob = body["blob"]
    step = -(-len(blob) // n)
    return [checked("jtail" if "moved" not in body else "jtail_ok",
                    {**body, "part": i, "parts": n, "blob": blob[i * step:(i + 1) * step]}) for i in range(n)]


def _jtail_at_guest(duo, parts: list[dict]) -> list[tuple]:
    out: list[tuple] = []
    for p in parts:
        out += duo.b_to_guest(duo.b.reserve_jrn_in("jtail", p))
    return out


def test_a_tail_assembly_with_another_part_count_forgets_the_earlier_parts(duo):
    """Начало другой сборки (иное число частей) не дошло до конца — её части забыты:
    полный набор новой отправки переносит хвост и даёт `jtail_ok` (иначе сборка ждала бы
    часть, которой никто не пришлёт, и хвост не перенёсся бы никогда)."""
    body = _crash_world(duo)
    two, three = _split(body, 2), _split(body, 3)
    assert _jtail_at_guest(duo, three[:2]) == [], "хвост перенесён из неполной сборки"
    with duo.as_guest():
        assert not [c for c in duo.gdb.list_clients() if c.name == "Вера"], "хвост перенесён из неполной сборки"
    replies = _jtail_at_guest(duo, two)
    assert [k for k, _ in replies] == ["jtail_ok"], f"полный набор после брошенной сборки не перенёс хвост: {replies}"
    with duo.as_guest():
        assert [c for c in duo.gdb.list_clients() if c.name == "Вера"], "хвост не перенесён у гостя"


def test_a_tail_answer_assembly_with_another_part_count_forgets_the_earlier_parts(duo):
    """Ответ гостя частями: начало другой сборки не дошло до конца — хвост у основы цел,
    отчёта нет; полный набор следующей отправки — хвост снят, отчёт для Т-Ж4 есть."""
    body = _crash_world(duo)
    (_k, ok), = _jtail_at_guest(duo, [body])
    ok = checked("jtail_ok", ok)
    two, three = _split(ok, 2), _split(ok, 3)
    for p in three[:2]:
        assert duo.a.reserve_jrn_in("jtail_ok", p) == []
    assert duo.a.db.jrn_tail_rows(), "хвост снят неполной сборкой ответа"
    assert not duo.a.rsv_get("rsv_out").get("tail_report"), "отчёт по неполной сборке"
    for p in two:
        duo.a.reserve_jrn_in("jtail_ok", p)
    assert not duo.a.db.jrn_tail_rows() and duo.a.rsv_get("rsv_out").get("tail_report"), \
        "полный набор ответа после брошенной сборки не принят"


def test_a_tail_assembly_key_is_epoch_fork_and_part_count(services):
    """Ключ сборки — эпоха, точка F и число частей: другая F посреди — сборка заново
    (части прежней не склеиваются с новой); тот же ключ — склейка по порядку частей."""
    asm = services._tail_assemble
    assert asm("_acc", [5, 10, 2], 0, 2, "aa") is None
    assert asm("_acc", [5, 11, 2], 1, 2, "bb") is None, "часть другой F дополнила прежнюю сборку"
    assert asm("_acc", [5, 11, 2], 0, 2, "cc") == "ccbb", "сборка новой F не склеилась по порядку"
    assert asm("_acc", [5, 11, 2], 1, 2, "dd") is None, "после склейки сборка не начата заново"
    assert asm("_acc", [6, 11, 2], 0, 2, "ee") is None
    assert asm("_acc", [6, 11, 2], 1, 2, "ff") == "eeff", "другая эпоха смешалась с остатком прежней"


# ── флаг «снимок в пути» на каждом исходе ───────────────────────────────────

def _dev(duo) -> int:
    return duo.a.db.list_all_devices()[0].id


def _cnt_due(duo) -> list[tuple]:
    duo.a.reserve_jrn_tick()
    duo.clock.advance(43 * 60)
    return sends(duo.a.reserve_jrn_tick(), "jcnt")


def _counts_world(duo) -> int:
    duo.edit("Иван")
    duo.journal_on()
    did = _dev(duo)
    duo.a.db.add_traffic_bulk([(did, 100, 100)])
    return did


def test_counts_fold_again_after_the_reserve_rejects_the_snapshot(duo):
    """Снимок отвергнут резервом (`pkg_bad`) — его строки счётчиков не у резерва:
    пачка снова сворачивается и несёт их."""
    _counts_world(duo)
    built = duo.a.reserve_pkg_build(force=True)
    assert _cnt_due(duo) == [], "условие: пока снимок в пути, пачка не сворачивается"
    duo.a.reserve_pkg_bad(built["gen"], "тест: зона занята")
    (_k, body), = _cnt_due(duo)
    batch = event.open_counts(duo.a.reserve_k(), bytes.fromhex(body["blob"]), duo.line)[1]
    assert sum(v[0] for d in batch.months.values() for v in d.values()) == 100, batch


def test_counts_fold_again_after_the_snapshot_fails_to_build(duo, monkeypatch):
    """Снимок начал собираться (флаг поставлен) и упал на копии базы — такт пакета
    снимает флаг: пачки идут, а не ждут следующего удачного снимка."""
    _counts_world(duo)

    def broken(_path):
        raise OSError("тест: копия базы не снялась")
    monkeypatch.setattr(pkgbot, "replica_marked", broken)
    duo.a.reserve_pkg_mark()
    duo.clock.advance(package.PKG_BATCH_SECONDS + 1)
    assert duo.a.reserve_pkg_tick() is None
    assert duo.a.rsv_get("rsv_view").get("pkg_error"), "условие: сборка снимка не упала"
    assert len(_cnt_due(duo)) == 1, "после упавшей сборки снимка пачка счётчиков не сворачивается"


def test_counts_fold_again_after_the_snapshot_send_goes_stale(duo):
    """Снимок ушёл и ответа нет дольше двух минут — отправка не состоялась: пачки
    снова сворачиваются; до срока — нет."""
    _counts_world(duo)
    duo.a.reserve_pkg_mark()
    duo.clock.advance(package.PKG_BATCH_SECONDS + 1)
    assert duo.a.reserve_pkg_tick() is not None, "условие: такт пакета не собрал снимок"
    duo.clock.advance(60)
    assert duo.a.reserve_pkg_sending_stale() is False
    assert sends(duo.a.reserve_jrn_tick(), "jcnt") == [], "пачка свернута, пока снимок в пути"
    duo.clock.advance(61)
    assert duo.a.reserve_pkg_sending_stale() is True
    assert len(_cnt_due(duo)) == 1, "после просроченной отправки снимка пачка не сворачивается"


# ── слишком большая пачка счётчиков у гостя ─────────────────────────────────

def _away_with_counts(duo) -> int:
    duo.journal_on()
    duo.edit("Иван")
    duo.tick()
    duo.open_guest(button=False)
    rm.go_away(duo.a)
    duo.a.reserve_epoch_sync()
    did = _dev(duo)
    with duo.as_guest():
        duo.g.db.add_traffic_bulk([(did, 300, 400)])
    return did


def _guest_cnt_tick(duo) -> list[tuple]:
    duo.clock.advance(43 * 60)
    with duo.as_guest():
        return [m for m in duo.g.guest_jrn_tick() if m[0] == "jcnt"]


def test_a_too_big_guest_counts_batch_is_logged_once_and_again_after_a_jcack(duo, monkeypatch, caplog):
    """Пачка клона больше части канала не уходит: строка «гость: …» — один раз, а не
    на каждый такт; после подтверждения пачки — снова (новый эпизод)."""
    did = _away_with_counts(duo)
    monkeypatch.setattr(pairproto, "MAX_BLOB", 64)
    caplog.set_level(logging.WARNING)
    for _ in range(3):
        assert _guest_cnt_tick(duo) == [], "пачка больше части канала ушла"
    big = _lines(caplog, "больше части канала")
    assert len(big) == 1 and big[0].startswith("гость:"), big
    with duo.as_guest():
        duo.g.guest_jrn_in("jcack", {"line": duo.line, "side": "b", "s": 1})
        duo.g.db.add_traffic_bulk([(did, 5, 5)])           # следующая пачка — тоже больше части канала
    _guest_cnt_tick(duo)
    _guest_cnt_tick(duo)
    assert len(_lines(caplog, "больше части канала")) == 2, \
        f"после jcack застрявшая пачка снова молчит: {_lines(caplog, 'больше части канала')}"


def test_a_guest_counts_batch_that_fits_is_sent_without_the_line(duo, caplog):
    _away_with_counts(duo)
    caplog.set_level(logging.WARNING)
    assert len(_guest_cnt_tick(duo)) == 1, "пачка клона в пределах части канала не ушла"
    assert _lines(caplog, "больше части канала") == []


# ── released чужой линии ────────────────────────────────────────────────────

def test_a_guest_release_of_a_foreign_line_is_a_log_line_at_the_reserve(duo, caplog):
    duo.journal_on()
    duo.edit("Иван")
    duo.tick()
    duo.open_guest(button=False)
    rm.go_away(duo.a)
    duo.a.reserve_epoch_sync()
    duo.b.rsv_update("rsv_in", state="releasing")
    with duo.as_guest():
        rel = duo.g.guest_jrn_release()
    caplog.set_level(logging.INFO)
    assert duo.b.reserve_guest_released_jrn({**rel, "line": "00" * 8}) == []
    assert len(_lines(caplog, "право записи не той линии")) == 1, caplog.text
    assert duo.b.rsv_get("rsv_in").get("jrn_back") is None


def test_a_hand_over_of_a_foreign_line_is_a_log_line_at_the_guest(duo, caplog):
    duo.journal_on()
    duo.edit("Иван")
    duo.tick()
    duo.open_guest(button=True)
    rm.to_step(duo.a, "release")
    built = duo.a.reserve_release_build()
    (_g, body), = to_guest(duo.b.reserve_released_jrn_store(built["epoch"], built["last"]))
    caplog.set_level(logging.INFO)
    with duo.as_guest():
        assert duo.g.guest_jrn_in("released", {**body, "line": "00" * 8}) == []
    assert len(_lines(caplog, "передача бота не моей линии")) == 1, caplog.text


# ── слово гостя о журнале по локальному каналу ──────────────────────────────

class _GuestSide:
    """Гость на том конце сокета: число устройств и слово о журнале."""

    def __init__(self, jrn: dict):
        self.jrn = jrn

    def guest_state_in(self, body):
        pass

    def guest_devices_online(self):
        return 2

    def guest_jrn_status(self):
        return dict(self.jrn)

    def guest_jrn_in(self, kind, msg):
        return []


async def _until(cond, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


@pytest.fixture()
def sock_dir():
    d = tempfile.mkdtemp(prefix="rj", dir="/tmp")         # путь сокета unix — короче ~100 знаков
    yield os.path.join(d, "g.sock")
    shutil.rmtree(d, ignore_errors=True)


async def _hello_over_the_socket(duo, path: str, jrn: dict) -> None:
    server = guestctl.GuestCtlServer(duo.b, path)
    client = guestctl.GuestCtlClient(_GuestSide(jrn), path)
    await server.ensure()
    client.start()
    try:
        assert await _until(lambda: server.online_count == 2), "приветствие гостя не дошло"
        await asyncio.sleep(0.1)                          # слово о журнале — в том же hello, в потоке
    finally:
        await client.stop()
        await server.stop()


async def test_the_guests_hello_over_the_socket_gives_the_reserve_its_epoch(duo, sock_dir):
    """hello гостя по сокету с `jrn` — сервисы резерва знают его эпоху: события основы
    старше неё резерв не хранит, а отвечает `jstale` с эпохой гостя."""
    duo.journal_on()
    duo.edit("Иван")
    duo.tick()
    duo.b.rsv_update("rsv_in", state="serving")
    e = duo.a.db.jrn_meta()["epoch"]
    await _hello_over_the_socket(duo, sock_dir, {"e": e + 3, "fork": 5, "last": 9})
    assert (duo.b._keep_guest_int("e"), duo.b._keep_guest_int("fork"), duo.b._keep_guest_int("last")) == \
        (e + 3, 5, 9), "слово гостя о журнале не дошло до сервисов резерва"
    kept = _kept(duo.b_keep)
    duo.edit("Пётр")
    replies = duo.a_to_b(duo.a.reserve_jrn_tick())
    assert sends(replies) == [("jstale", {"line": duo.line, "e": e + 3})], replies
    assert _kept(duo.b_keep) == kept, "события старой эпохи основы легли в хранилище поверх эпохи гостя"


async def test_garbage_in_the_guests_word_over_the_socket_is_zeros(duo, sock_dir):
    """Не числа в слове гостя — нули: резерв не падает на сокете и эпоху гостя не
    выдумывает, события основы хранятся как обычно."""
    duo.journal_on()
    duo.edit("Иван")
    duo.tick()
    duo.b.rsv_update("rsv_in", state="serving")
    await _hello_over_the_socket(duo, sock_dir, {"e": "семь", "fork": None, "last": [1]})
    assert (duo.b._keep_guest_int("e"), duo.b._keep_guest_int("fork"), duo.b._keep_guest_int("last")) == (0, 0, 0)
    duo.edit("Пётр")
    replies = duo.a_to_b(duo.a.reserve_jrn_tick())
    assert [k for k, _ in sends(replies)] == ["jack"], replies
