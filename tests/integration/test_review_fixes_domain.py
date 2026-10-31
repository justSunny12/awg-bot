"""Правки по ревью домена основной роли: учёт трафика, фиды подсетей сервисов,
ключи устройств, подписки.

- опрос трафика сериализован: дамп и сравнение с сэмплом — под одним замком,
  второй опрос (кнопка «🔄», передача резерву, гость) снимает дамп только после
  записи первого.

Цена ошибки: дамп, снятый раньше, записывался после более позднего — дельта
выходила отрицательной, принималась за рестарт awg, и весь счётчик интерфейса
(десятки гигабайт с подъёма) уходил в трафик месяца — лимит срабатывал зря.
"""
from __future__ import annotations

import threading

import pytest

from awgbot.infra import awg

pytestmark = pytest.mark.integration


# ── опрос трафика: дамп и запись под одним замком ────────────────────────────

def test_poll_traffic_second_dump_waits_for_first_commit(services, make_active_client, monkeypatch):
    """Первый опрос снял дамп и задержался — второй не снимает свой, пока первый
    не записал сэмпл; итог месяца — ровно прирост, без «рестарта»."""
    c = make_active_client(name="a", tg_id=7001)
    d = services.add_device(c.id, "Тел")
    pub = services.db.get_device(d.device_id).public_key
    base = 10 * 2**30
    rx = {"v": base}
    first_dumped, release_first = threading.Event(), threading.Event()
    calls: list[int] = []

    def dump(name=None):
        v = rx["v"]
        calls.append(v)
        if len(calls) == 2:              # дамп опроса A — снят, но запись задержана
            first_dumped.set()
            assert release_first.wait(5)
        return [dict(public_key=pub, rx=v, tx=0, last_handshake=None, allowed_ips="10.8.1.2/32")]

    monkeypatch.setattr(awg, "show_dump", dump)
    monkeypatch.setattr(awg, "read_file", lambda p: "")
    services.poll_traffic()                                  # первая база
    rx["v"] = base + 1000
    a = threading.Thread(target=services.poll_traffic)
    a.start()
    assert first_dumped.wait(5)
    rx["v"] = base + 2000                                    # опрос B пришёл позже A
    b = threading.Thread(target=services.poll_traffic)
    b.start()
    b.join(0.3)
    assert len(calls) == 2, "второй опрос снял дамп до записи первого"
    release_first.set()
    a.join(5)
    b.join(5)
    assert calls == [base, base + 1000, base + 2000]
    assert services.db.get_device(d.device_id).traffic_rx_month == 2000
