"""Помечен ли шлюз — по решению скрипта обвязки при последнем прогоне
(gateway.status), а не по записи в базе: её клал только итог применения файла
перевыпуска из чата, снятого в 3.3.1, и без живого чтения она застывала."""
from awgbot.domain.gateway import GatewayServices
from awgbot.infra import gwguard
from awgbot.infra.db import Database


def _svc(tmp_path):
    d = Database(tmp_path / "gw.db"); d.init_schema()
    return GatewayServices(d)


def test_mark_status_is_read_live_from_the_plumbing_status(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    svc.db.set_state(svc._GW_MARK_KEY, "unmarked")                # застывшая запись прошлого выпуска
    monkeypatch.setattr(gwguard, "script_status", lambda: {"GW_STATUS": "confirmed"})
    assert svc.gateway_mark_status() == "confirmed"
    assert svc.gateway_claim_if_needed() is None, "помеченный шлюз шлёт claim по застывшей записи"


def test_mark_status_falls_back_to_the_db_and_then_unknown(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    monkeypatch.setattr(gwguard, "script_status", lambda: {})
    assert svc.gateway_mark_status() == "?"
    svc.db.set_state(svc._GW_MARK_KEY, "foreign")
    assert svc.gateway_mark_status() == "foreign"
    monkeypatch.setattr(gwguard, "script_status", lambda: {"GW_STATUS": "мусор"})
    assert svc.gateway_mark_status() == "foreign", "непонятное значение файла перекрыло запись"
