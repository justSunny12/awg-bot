"""Переход в следование (journal/epoch.py `_epoch_sync_locked`, под замком резерва)
и ответ резерва `jack` на мои события (journal/ship.py `_jrn_ack_in`, поток слов
канала пары): точка расхождения берётся из прочитанного `acked`, если точка гостя
ещё не известна. `jack`, легший в окно между чтением `acked` и `jrn_pass` (или
сразу после перехода — для базы это одно и то же), не должен ни класть в хвост
старой эпохи подтверждённые резервом события (хвост строится по `acked` из базы
одной транзакцией), ни мешать следованию: точка гостя из `st` резерва поднимает
`applied`, события держателя ложатся.

Мир — tests/integration/_reserve_jrn_duo.py (основа, резерв и гость на трёх базах).

Цена ошибки: подтверждённые события в хвосте — при встрече они легли бы держателю
повторно (двойники устройств); `applied` без подъёма по точке гостя — события
гостя не применяются, основа в отъезде не видит его решений.
"""
from __future__ import annotations

import threading

import pytest

from awgbot.infra import awg

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_jrn_duo import Duo, checked, sends
from tests.integration.test_reserve_jrn_follow_pair import _FUTURE, _START, _guest_jev

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv


@pytest.fixture()
def duo(mv, tmp_path, monkeypatch, fake_awg):
    monkeypatch.setattr(awg, "blocked_ips", lambda: set(fake_awg.blocked), raising=False)
    d = Duo(mv, tmp_path, monkeypatch)
    yield d
    d.close()


def test_a_jack_in_the_window_before_the_pass_neither_pollutes_the_tail_nor_blocks_following(duo):
    """Иван подтверждён; Пётр отправлен и сохранён резервом, `jack` в пути; гость открыт
    из хранилища резерва (с Петром). Основа уходит в следование по старому `acked`,
    `jack` ложится в окно перед `jrn_pass`: в хвосте нет подтверждённого; точка гостя
    из `st` поднимает `applied`; событие гостя применяется."""
    duo.journal_on()
    duo.edit("Иван")
    duo.tick()
    a = duo.a
    acked_before = int(a.db.jrn_meta()["acked"])
    duo.edit("Пётр")
    answers = duo.a_to_b(a.reserve_jrn_tick())                # резерв сохранил, ответы основе ещё в пути
    jack = [(k, b) for k, b in sends(answers) if k == "jack"]
    assert jack, answers
    top = int(jack[-1][1]["i"])
    assert top > acked_before
    duo.open_guest(button=False)                              # гость — из хранилища резерва: его точка = top

    real_pass, landed = a.db.jrn_pass, threading.Event()

    def pass_with_jack_in_flight(**kw):
        t = threading.Thread(target=lambda: (a.reserve_jrn_in("jack", checked("jack", jack[-1][1])), landed.set()),
                             name="word")
        t.start()
        t.join(1)
        return real_pass(**kw)

    a.db.jrn_pass = pass_with_jack_in_flight
    try:
        rm.go_away(a)
        a.reserve_epoch_sync()
    finally:
        a.db.jrn_pass = real_pass
    assert landed.wait(3)
    meta = a.db.jrn_meta()
    assert meta["role"] == "follower" and meta["holder"] == "peer" and int(meta["acked"]) == top
    held = a.db._connection().execute("SELECT MIN(i) FROM jrn_tail").fetchone()[0]
    assert held is None or int(held) > top, "в хвосте старой эпохи — подтверждённые резервом события"

    duo.st_b()                                                # точка гостя пришла
    assert not a.db.jrn_tail_held(), "хвост остался отложенным после точки гостя"
    body = _guest_jev(duo, lambda db: db.create_client("Вера", 1, _START, _FUTURE, "INV-DUMMY-V"))
    acts = a.reserve_jrn_in("jev", body)
    if sends(acts, "jneed"):
        # `applied` ниже подтверждённого (jack лёг после чтения): свои же события — одним кругом
        # досылки из хранилища резерва, дальше события гостя ложатся
        assert sends(acts, "jneed") == [("jneed", {"line": duo.line, "from": acked_before + 1})], acts
        duo.b_to_a(duo.a_to_b(acts))
        assert int(a.db.jrn_meta()["applied"]) == top, a.db.jrn_meta()
        acts = a.reserve_jrn_in("jev", body)
    assert [k for k, _ in sends(acts)] == ["jack"], acts
    assert [c for c in a.db.list_clients() if c.name == "Вера"], "решение гостя не применилось у основы"
    assert len([c for c in a.db.list_clients() if c.name == "Пётр"]) == 1, "досылка своих событий дала двойника"
