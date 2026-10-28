"""Запечатка записей журнала данных резерва (util/rsvjrn/event.py): событие
держателя и пачка счётчиков под `K` с открытым заголовком (метка, вид, линия,
эпоха, номер, длина), шифротекст добит до ступени 256 байт.

Резерв ключа `K` не знает и видит только заголовок: по нему он раскладывает
записи по сегментам и проверяет порядок номеров. Открывающий (процесс
открытия, основа в следовании) сверяет линию, эпоху и номер внутри
шифротекста с заголовком.

Цена ошибки: заголовок, не сверенный с содержимым, — резерв (или порча диска)
переставит или повторит записи, и база гостя получит решения не в том
порядке или дважды; запись чужой линии, открытая молча, — в базу основы
попадут решения второго сервера пары; длина шифротекста не ступенями — по
размеру записи видно, что именно поменяли (продление или новое устройство);
оборванная запись, разобранная как целая, — открытие падает на мусоре.
"""
from __future__ import annotations

import os

import pytest

from awgbot.util.rsvjrn import event
from awgbot.util.rsvjrn.apply import Event
from awgbot.util.rsvjrn.counts import Batch

pytestmark = pytest.mark.unit

LINE = "9f3c1a7e0b2d4c55"
OTHER = "0011223344556677"


@pytest.fixture()
def k() -> bytes:
    return os.urandom(32)


def _ev(i: int = 10423, e: int = 7, **over) -> Event:
    base = dict(i=i, e=e, t="devices", op="U", k={"id": 57}, c={"name": "iPhone", "block_reason": 4},
                o={"name": "iPhone 12", "block_reason": 0}, at="2026-10-27T14:03:11.204")
    base.update(over)
    return Event(**base)


def _batch(bn: int = 381) -> Batch:
    return Batch(host=f"{LINE}:a", bn=bn, s0=1200, s1=1290, months={"2026-10": {57: [123456, 98765, 0, 0, 1_700_000_000]}})


# ── событие ─────────────────────────────────────────────────────────────────

def test_an_event_opens_back_whole_with_the_same_key_and_line(k):
    """Запечатанное событие открывается тем же `K` в то же событие — все поля."""
    ev = _ev()
    got = event.open_event(k, event.seal_event(k, LINE, ev), LINE)
    assert got == ev, f"событие после запечатки другое: {got}"


def test_the_header_is_readable_without_the_key(k):
    """Резерв без `K` видит вид, линию, эпоху, номер и длину записи."""
    raw = event.seal_event(k, LINE, _ev(i=12, e=3))
    h = event.head(raw)
    assert (h.kind, h.line, h.e, h.n, h.size) == ("E", LINE, 3, 12, len(raw)), h


@pytest.mark.parametrize("n_bytes", [0, 1, 200, 1000, 5000])
def test_the_ciphertext_is_padded_to_a_256_byte_step(k, n_bytes):
    """Длина шифротекста кратна 256 при любом содержимом: по размеру записи не
    видно, что поменяли."""
    raw = event.seal_event(k, LINE, _ev(c={"name": "x" * n_bytes}))
    ct = len(raw) - event.HEAD.size
    assert ct % event.STEP == 0, f"шифротекст {ct} байт — не ступенью 256 (содержимое {n_bytes})"


def test_a_short_and_a_slightly_longer_event_have_the_same_size(k):
    """Две правки разной длины в пределах одной ступени — записи одной длины."""
    a = event.seal_event(k, LINE, _ev(c={"name": "a"}))
    b = event.seal_event(k, LINE, _ev(c={"name": "a" * 40}))
    assert len(a) == len(b), "длина записи выдаёт длину правки"


def test_a_foreign_key_does_not_open_the_event(k):
    raw = event.seal_event(k, LINE, _ev())
    with pytest.raises(event.RecordError):
        event.open_event(os.urandom(32), raw, LINE)


def test_an_event_of_another_line_is_refused(k):
    """Запись второй линии (данные второго сервера пары) не открывается как своя."""
    raw = event.seal_event(k, OTHER, _ev())
    with pytest.raises(event.RecordError, match="чужой линии"):
        event.open_event(k, raw, LINE)


def _reheader(raw: bytes, **over) -> bytes:
    """Та же запись с подменённым открытым заголовком (шифротекст прежний)."""
    h = event.head(raw)
    vals = {"kind": h.kind.encode(), "line": bytes.fromhex(h.line), "e": h.e, "n": h.n}
    vals.update(over)
    return event.HEAD.pack(event.MAGIC, vals["kind"], vals["line"], vals["e"], vals["n"],
                           len(raw) - event.HEAD.size) + raw[event.HEAD.size:]


@pytest.mark.parametrize("field,value", [("n", 10424), ("e", 8)])
def test_a_header_rewritten_by_the_reserve_does_not_open(k, field, value):
    """Резерв переставил номер или эпоху в заголовке — содержимое говорит иначе:
    запись отвергается, а не ложится не на своё место."""
    raw = _reheader(event.seal_event(k, LINE, _ev()), **{field: value})
    with pytest.raises(event.RecordError, match="не те, что в заголовке"):
        event.open_event(k, raw, LINE)


def test_a_line_rewritten_in_the_header_does_not_open(k):
    """Заголовок перенесён в другую линию — линия внутри не та."""
    raw = _reheader(event.seal_event(k, LINE, _ev()), line=bytes.fromhex(OTHER))
    with pytest.raises(event.RecordError, match="линия внутри"):
        event.open_event(k, raw)


def test_a_flipped_byte_of_the_ciphertext_is_refused(k):
    raw = bytearray(event.seal_event(k, LINE, _ev()))
    raw[-5] ^= 0x01
    with pytest.raises(event.RecordError):
        event.open_event(k, bytes(raw), LINE)


def test_a_length_not_matching_the_header_is_refused(k):
    raw = event.seal_event(k, LINE, _ev())
    with pytest.raises(event.RecordError):
        event.open_event(k, raw + b"\x00", LINE)
    with pytest.raises(event.RecordError):
        event.open_event(k, raw[:-1], LINE)


def test_a_bad_line_is_not_sealed(k):
    """Линия — 8 байт hex; иное не запечатывается (иначе заголовок съедет)."""
    for line in ("", "zz" * 8, LINE[:-2]):
        with pytest.raises(event.RecordError):
            event.seal_event(k, line, _ev())


def test_garbage_is_not_a_record():
    with pytest.raises(event.RecordError):
        event.head(b"XX" + b"\x00" * 40)
    with pytest.raises(event.RecordError, match="оборвана"):
        event.head(b"AJ")


# ── пачка счётчиков ─────────────────────────────────────────────────────────

def test_a_counts_batch_opens_back_with_its_side(k):
    raw = event.seal_counts(k, LINE, "a", _batch())
    side, got = event.open_counts(k, raw, LINE)
    assert side == "a" and got == _batch(), (side, got)
    h = event.head(raw)
    assert (h.kind, h.e, h.n) == ("a", 0, 381), "у пачки в заголовке — сторона и номер пачки (не номер строки)"


def test_a_counts_batch_with_a_rewritten_side_is_refused(k):
    """Пачка основы, выданная резервом за пачку клона, — отказ: стороны меряют
    разные интерфейсы, и счётчики сложились бы не туда."""
    raw = _reheader(event.seal_counts(k, LINE, "a", _batch()), kind=b"b")
    with pytest.raises(event.RecordError, match="сторона или номер"):
        event.open_counts(k, raw, LINE)


def test_a_counts_batch_with_a_rewritten_number_is_refused(k):
    raw = _reheader(event.seal_counts(k, LINE, "b", _batch()), n=999)
    with pytest.raises(event.RecordError):
        event.open_counts(k, raw, LINE)


def test_an_event_is_not_opened_as_counts_and_back(k):
    with pytest.raises(event.RecordError):
        event.open_counts(k, event.seal_event(k, LINE, _ev()), LINE)
    with pytest.raises(event.RecordError):
        event.open_event(k, event.seal_counts(k, LINE, "a", _batch()), LINE)


def test_the_side_is_only_a_or_b(k):
    with pytest.raises(event.RecordError):
        event.seal_counts(k, LINE, "c", _batch())


# ── split ───────────────────────────────────────────────────────────────────

def test_split_gives_back_the_records_one_by_one(k):
    recs = [event.seal_event(k, LINE, _ev(i=i)) for i in (5, 6, 7)] + [event.seal_counts(k, LINE, "b", _batch())]
    assert event.split(b"".join(recs)) == recs
    assert event.split(b"") == [], "пустое тело — ни одной записи, а не ошибка"


def test_split_refuses_a_cut_tail(k):
    """Тело сообщения, оборванное посреди записи, — отказ целиком (получатель
    просит досылку), а не первые записи молча."""
    blob = b"".join(event.seal_event(k, LINE, _ev(i=i)) for i in (5, 6))
    with pytest.raises(event.RecordError, match="оборвана"):
        event.split(blob[:-10])
    with pytest.raises(event.RecordError):
        event.split(blob + b"AJ")
