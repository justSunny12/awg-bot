"""Машина живости линка (util/livestep.py): у объекта `Liveness` свой замок,
и такт (`step`) со словами (`word`, `pong`) исполняются под ним целиком —
такт идёт из потока планировщика, слова канала из потоков обработчиков.

Цена ошибки: слово посреди шага «подозрение → недоступна» затирает `state` на
`ok` после того, как шаг уже вернул «down»: уведомление «недоступен» ушло,
отметка на диске стоит, а «снова на связи» не наступит никогда — `alive`
отдаёт «up» только из `down`; у зон шлюзов тот же объект на каждую зону.
"""
from __future__ import annotations

import threading
import time

import pytest

from awgbot.util import livestep

pytestmark = pytest.mark.unit

T0 = 10_000.0


class _Spy:
    """Замок-соглядатай: считает входы, на деле реентерабельный."""

    def __init__(self):
        self.real = threading.RLock()
        self.entered = 0

    def __enter__(self):
        self.entered += 1
        return self.real.__enter__()

    def __exit__(self, *a):
        return self.real.__exit__(*a)


@pytest.mark.parametrize("call", [
    lambda lv: livestep.step(lv, T0, 1, suspect_s=40, probe_s=15),
    lambda lv: livestep.word(lv, T0),
    lambda lv: livestep.pong(lv, T0, "n"),
    lambda lv: livestep.alive(lv, T0),
    lambda lv: livestep.ping(lv, T0),
], ids=["step", "word", "pong", "alive", "ping"])
def test_every_transition_takes_the_object_lock(call):
    lv = livestep.Liveness()
    lv.lock = _Spy()
    call(lv)
    assert lv.lock.entered >= 1, "переход живости идёт мимо замка объекта"


def test_a_word_waits_for_the_step_that_is_in_flight():
    """Шаг держит замок — слово из другого потока ждёт его конца, а не вклинивается."""
    lv = livestep.Liveness()
    livestep.step(lv, T0, 100, suspect_s=40, probe_s=15)
    order: list[str] = []
    with lv.lock:                                   # «шаг в пути»
        t = threading.Thread(target=lambda: (livestep.word(lv, T0 + 50), order.append("word")))
        t.start()
        time.sleep(0.05)
        assert order == [], "слово прошло, пока шаг держал замок"
        order.append("step")
    t.join(2)
    assert order == ["step", "word"]


def test_the_lock_is_per_object_and_outside_equality():
    a, b = livestep.Liveness(), livestep.Liveness()
    assert a.lock is not b.lock
    assert a == b, "замок попал в сравнение объектов"
