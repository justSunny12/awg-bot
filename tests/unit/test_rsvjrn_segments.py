"""Записи журнала данных резерва на диске (util/rsvjrn/segments.py): сегменты
событий `ev-<эпоха>-<первый номер>.seg` и файлы пачек счётчиков
`cnt-<сторона>.seg` у резерва и у бота основы на резерве.

Цена ошибки: дописка без нового сегмента на смене эпохи — события двух
эпох в одном файле, открытие не отличит хвост старой эпохи; сегмент без
предела размера — один файл на годы работы, снятие по снимку переписывает его
целиком; оборванная последняя запись (пропало питание), прочитанная как
целая, — процесс открытия падает и переезд в аварию не состоится, а
дописанная поверх обрыва — все следующие записи нечитаемы; снятие по снимку,
задевшее события после него, — резерв теряет подтверждённые правки, база
гостя отстаёт от основы.
"""
from __future__ import annotations

import os
import stat

import pytest

from awgbot.util.rsvjrn import event, segments
from awgbot.util.rsvjrn.apply import Event
from awgbot.util.rsvjrn.counts import Batch
from awgbot.util.rsvjrn.segments import Store

pytestmark = pytest.mark.unit

LINE = "9f3c1a7e0b2d4c55"
K = bytes(range(32))


def rec(i: int, e: int = 7, pad: int = 0) -> bytes:
    return event.seal_event(K, LINE, Event(i=i, e=e, t="devices", op="U", k={"id": i},
                                           c={"name": "x" * pad}, at="2026-10-27T14:03:11"))


def crec(s: int, side: str = "a") -> bytes:
    return event.seal_counts(K, LINE, side, Batch(host=f"{LINE}:{side}", bn=s, s0=s - 1, s1=s, months={"2026-10": {1: [s, s, 0, 0, None]}}))


def nums(recs: list[bytes]) -> list[int]:
    return [event.head(r).n for r in recs]


def segs(path) -> list[str]:
    return sorted(n for n in os.listdir(path) if n.startswith("ev-"))


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path / "jrn")


# ── дописка и чтение ────────────────────────────────────────────────────────

def test_events_appended_are_read_back_in_order(store):
    store.append_events([rec(1), rec(2)])
    store.append_events([rec(3)])
    assert nums(store.events()) == [1, 2, 3]
    assert nums(store.events(after=2)) == [3], "после номера — только дальше него"
    assert store.last() == (7, 3)
    assert segs(store.path) == ["ev-7-1.seg"], "одна эпоха, мало байт — один сегмент"


def test_the_files_are_private(store):
    """Шифротекст под `K` второй стороны — только владельцу каталога."""
    store.append_events([rec(1)])
    store.append_counts("a", [crec(1)])
    for name in os.listdir(store.path):
        mode = stat.S_IMODE((store.path / name).stat().st_mode)
        assert mode == 0o600, f"{name}: права {oct(mode)}"
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o700


def test_an_empty_store_reads_as_nothing(store):
    """Каталога ещё нет (резерв без снимка) — пусто, а не ошибка."""
    assert store.events() == [] and store.last() == (0, 0)
    assert store.counts("a") == [] and store.counts_last("a") == 0
    assert store.events_size() == 0
    store.append_events([])
    assert not store.path.exists(), "пустая дописка не заводит каталог"


def test_a_new_epoch_starts_a_new_segment(store):
    store.append_events([rec(1, e=7), rec(2, e=7)])
    store.append_events([rec(3, e=8)])
    assert segs(store.path) == ["ev-7-1.seg", "ev-8-3.seg"], segs(store.path)
    assert nums(store.events()) == [1, 2, 3]
    assert store.last() == (8, 3)


def test_events_of_two_epochs_in_one_append_are_refused(store):
    with pytest.raises(event.RecordError):
        store.append_events([rec(1, e=7), rec(2, e=8)])
    assert store.events() == [], "отвергнутая дописка ничего не пишет"


def test_a_segment_over_the_limit_is_followed_by_a_new_one(store, monkeypatch):
    """После SEG_MAX байт — новый сегмент той же эпохи с первым номером в имени."""
    monkeypatch.setattr(segments, "SEG_MAX", 2000)
    store.append_events([rec(1, pad=1500), rec(2, pad=1500)])          # > 2000 байт
    store.append_events([rec(3)])
    assert segs(store.path) == ["ev-7-1.seg", "ev-7-3.seg"], segs(store.path)
    assert nums(store.events()) == [1, 2, 3]


def test_a_cut_last_record_is_not_read_and_is_cut_off_on_the_next_append(store):
    """Питание пропало посреди дописки: обрыв не читается, следующая дописка
    обрезает файл до целой части и пишет дальше — читаются все целые записи."""
    store.append_events([rec(1), rec(2)])
    path = store.path / "ev-7-1.seg"
    whole = path.stat().st_size
    with open(path, "ab") as f:
        f.write(rec(3)[:50])
    assert nums(store.events()) == [1, 2], "оборванная запись прочитана как целая"
    assert store.last() == (7, 2)
    store.append_events([rec(3)])
    assert nums(store.events()) == [1, 2, 3], "после обрыва записи не читаются"
    assert path.stat().st_size == whole + len(rec(3))


def test_a_cut_counts_record_is_dropped_the_same_way(store):
    store.append_counts("a", [crec(1)])
    with open(store.path / "cnt-a.seg", "ab") as f:
        f.write(crec(2)[:30])
    assert nums(store.counts("a")) == [1]
    store.append_counts("a", [crec(2)])
    assert nums(store.counts("a")) == [1, 2]


# ── снятие по снимку ────────────────────────────────────────────────────────

def test_drop_events_upto_removes_only_what_the_snapshot_holds(store):
    store.append_events([rec(1, e=7), rec(2, e=7), rec(3, e=7)])
    store.append_events([rec(4, e=8), rec(5, e=8)])
    assert store.drop_events_upto(4) == 4
    assert nums(store.events()) == [5], "снятие задело событие после снимка"
    assert segs(store.path) == ["ev-8-5.seg"], "остаток сегмента — под именем своего первого номера"
    assert store.last() == (8, 5)


def test_drop_events_upto_twice_changes_nothing_more(store):
    """Повтор того же снимка (pkg_ok потерялся) — ничего нового не снимает."""
    store.append_events([rec(1), rec(2), rec(3)])
    assert store.drop_events_upto(2) == 2
    assert store.drop_events_upto(2) == 0
    assert nums(store.events()) == [3]


def test_drop_events_upto_everything_leaves_no_segments(store):
    store.append_events([rec(1), rec(2)])
    store.drop_events_upto(10)
    assert segs(store.path) == [] and store.last() == (0, 0)
    store.append_events([rec(11)])
    assert nums(store.events()) == [11], "после полного снятия — дописка в новый сегмент"


def test_drop_counts_upto_keeps_the_later_batches_of_that_side_only(store):
    store.append_counts("a", [crec(1), crec(2), crec(3)])
    store.append_counts("b", [crec(1, "b")])
    store.drop_counts_upto("a", 2)
    assert nums(store.counts("a")) == [3]
    assert nums(store.counts("b")) == [1], "снятие пачек одной стороны задело другую"
    assert store.counts_last("a") == 3
    store.drop_counts_upto("a", 3)
    assert not (store.path / "cnt-a.seg").exists()
    assert nums(store.counts("a", after=0)) == []


def test_the_size_of_the_events_is_their_bytes(store):
    store.append_events([rec(1), rec(2)])
    assert store.events_size() == len(rec(1)) + len(rec(2))


def test_wipe_removes_every_record_file(store):
    store.append_events([rec(1)])
    store.append_counts("a", [crec(1)])
    store.append_counts("b", [crec(1, "b")])
    (store.path / "ev-7-9.seg.tmp").write_bytes(b"x")
    (store.path / "keep.txt").write_text("не наше")
    store.wipe()
    assert sorted(os.listdir(store.path)) == ["keep.txt"], "wipe убрал не только записи или оставил записи"
    assert store.events() == [] and store.counts("a") == []
    Store(store.path / "nope").wipe()                  # каталога нет — не ошибка


def test_garbage_in_the_middle_is_not_cut_and_the_next_append_goes_beside(store):
    """Порча посреди файла (не оборванный хвост) — подтверждённые записи до неё не
    обрезаются, дописка уходит в файл рядом и читается вместе с ними."""
    store.append_events([rec(1), rec(2)])
    path = store.path / "ev-7-1.seg"
    with open(path, "ab") as f:
        f.write(b"XX" + b"\x00" * 200)                     # не заголовок записи
    size = path.stat().st_size
    store.append_events([rec(3)])
    assert path.stat().st_size == size, "файл с подтверждёнными записями обрезан по мусору"
    assert nums(store.events()) == [1, 2, 3], nums(store.events())
    assert len(segs(store.path)) == 2, segs(store.path)


def test_a_repeated_number_is_read_once(store):
    """Повтор дописки (пропало питание после записи, до подтверждения) — номер
    читается один раз, первая запись остаётся."""
    first = rec(2)
    store.append_events([rec(1), first])
    store.append_events([rec(2), rec(3)])
    got = store.events()
    assert nums(got) == [1, 2, 3], nums(got)
    assert got[1] == first, "повтор номера подменил первую запись"
    store.append_counts("a", [crec(1), crec(1)])
    assert nums(store.counts("a")) == [1]
