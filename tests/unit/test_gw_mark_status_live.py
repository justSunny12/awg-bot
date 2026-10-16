"""Помечен ли шлюз — по решению скрипта обвязки при последнем прогоне
(gateway.status). Записи в базе больше нет: её клал только итог применения
файла перевыпуска из чата, снятого в 3.3.1, и она застывала — файла статуса
нет или значение непонятное, ответ честный: «?»."""
from awgbot.domain.gateway import GatewayServices
from awgbot.infra import gwguard
from awgbot.infra.db import Database


def _svc(tmp_path):
    d = Database(tmp_path / "gw.db"); d.init_schema()
    return GatewayServices(d)


def test_mark_status_is_read_live_from_the_plumbing_status(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    svc.db.set_state("gw_mark_status", "unmarked")                 # застывшая запись прошлого выпуска
    monkeypatch.setattr(gwguard, "script_status", lambda: {"GW_STATUS": "confirmed"})
    assert svc.gateway_mark_status() == "confirmed"
    assert svc.gateway_claim_if_needed() is None, "помеченный шлюз шлёт claim по застывшей записи"


def test_without_the_status_file_the_mark_is_unknown_whatever_the_db_says(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    svc.db.set_state("gw_mark_status", "foreign")                  # застывшая запись прошлого выпуска
    monkeypatch.setattr(gwguard, "script_status", lambda: {})
    assert svc.gateway_mark_status() == "?", "ответ по застывшей записи базы"
    monkeypatch.setattr(gwguard, "script_status", lambda: {"GW_STATUS": "мусор"})
    assert svc.gateway_mark_status() == "?", "непонятное значение файла"
