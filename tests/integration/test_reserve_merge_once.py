"""Реплика одного эпизода сливается один раз (merge_ops._return_merge, backup/merge/reserve.merge
`mark`, outbound_return.reserve_released_in).

Признак «эпизод слит» — поколение пакета из снимка (`rsv_merged_gen` в server_state), и
ложится он той же транзакцией, что и слияние. Прежний признак — sha256 шифротекста
(`merged_fp`): повторный `back_req` после COMMIT получает с резерва `rep.bin` с диска
под другим nonce — отпечаток другой, слияние шло второй раз: счётчики трафика
`c + (r − b)` складывались дважды, архивы дублировались; крах между COMMIT и записью
отпечатка давал то же.

Окружение — `merge_world` из test_reserve_return.

Цена ошибки: удвоенный расход через резерв в отчёте и архивах, двойные строки `lost`.
"""
from __future__ import annotations

import pytest

from awgbot.domain.backup.merge import reserve as merge_mod
from awgbot.domain.services.reserve import merge_ops
from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration.test_reserve_return import _reserve_edit, away, merge_world

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["away", "merge_world"]


def _traffic_edit(w, rx: int = 1000, tx: int = 500):
    """Правки резерва плюс расход устройства «Телефон» за месяц."""
    base = _reserve_edit(w)
    dev = w.keep.device_id

    def edit(db):
        base(db)
        db._connection().execute("UPDATE device_traffic SET traffic_rx_month = traffic_rx_month + ?, "
                                 "traffic_tx_month = traffic_tx_month + ? WHERE device_id = ?", (rx, tx, dev))
        db._connection().commit()
    return edit


@pytest.fixture()
def spy(merge_world, monkeypatch):
    calls = []
    real = merge_mod.merge
    monkeypatch.setattr(merge_mod, "merge", lambda *a, **k: calls.append(k) or real(*a, **k))
    merge_world.calls = calls
    return merge_world


def test_the_same_episode_under_another_nonce_is_not_merged_twice(spy):
    w = spy
    edit = _traffic_edit(w)
    first = rm.snapshot_blob(w.svc, edit, gen=7)
    assert w.svc.reserve_released_in(first) == [("restart",)]
    assert len(w.calls) == 1 and w.calls[0]["mark"] == (merge_ops.MERGED_KEY, "7")
    assert w.svc.db.get_state(merge_ops.MERGED_KEY) == "7", "отметка не легла с COMMIT"
    traffic = dict(w.svc.rsv_get("rsv_traffic").get("out") or {})
    clients = w.svc.db._connection().execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    # рестарт посреди шага restart → back_req снова; резерв отдаёт тот же rep.bin с диска под
    # другим nonce (снимок — тот же, что был слит: живая база основы его уже несёт)
    w.svc._out_step("back_req", 300)
    again = rm.snapshot_blob(w.svc, None, gen=7)
    assert again != first, "контроль теста: шифротекст другой"
    acts = w.svc.reserve_released_in(again)
    assert acts == [("restart",)] and w.svc.rsv_get("rsv_out")["step"] == "restart"
    assert len(w.calls) == 1, "реплика того же поколения слита второй раз"
    assert dict(w.svc.rsv_get("rsv_traffic").get("out") or {}) == traffic, "расход через резерв сложен дважды"
    assert w.svc.db._connection().execute("SELECT COUNT(*) FROM clients").fetchone()[0] == clients


def test_a_replica_of_a_newer_episode_is_merged(spy):
    w = spy
    assert w.svc.reserve_released_in(rm.snapshot_blob(w.svc, gen=7)) == [("restart",)]
    w.svc._out_step("back_req", 300)
    rm.put_base(w.svc, gen=8)
    w.svc.rsv_update("rsv_out", gen=8)
    assert w.svc.reserve_released_in(rm.snapshot_blob(w.svc, gen=8)) == [("restart",)]
    assert len(w.calls) == 2 and w.svc.db.get_state(merge_ops.MERGED_KEY) == "8"


def test_the_mark_is_cleared_when_the_return_ends(spy):
    w = spy
    w.svc.reserve_released_in(rm.snapshot_blob(w.svc, gen=7))
    w.svc._out_step("relay", 300)
    w.svc.rsv_update("rsv_out", record_fact_at=timeutil.now_iso())
    w.svc.reserve_wiped_in()
    assert w.svc.reserve_out() == "home" and not w.svc.db.get_state(merge_ops.MERGED_KEY)


def test_a_failed_merge_leaves_no_mark(spy, monkeypatch):
    """Откат транзакции — откат и отметки: следующая реплика того же поколения сольётся."""
    w = spy
    monkeypatch.setattr(merge_mod, "_merge_state", lambda con, rpt: (_ for _ in ()).throw(RuntimeError("database is locked")))
    w.svc.reserve_released_in(rm.snapshot_blob(w.svc, gen=7))
    assert w.svc.reserve_out() == "away" and not w.svc.db.get_state(merge_ops.MERGED_KEY)
