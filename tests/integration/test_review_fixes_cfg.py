"""Документ конфигурации и копия настроек шлюза после перепроверки выпуска
(накат 3.3.1 → 3.4.0 на стороне сервера).

- документ сменил отпечаток без расхождения со стоящим (раздел ушёл из
  документа сервера): сохранённый ответ шлюза переписывается на новый
  отпечаток — карточка не теряет «конфигурация применена», удержание
  прежних отказов не снимается; временный отказ и расхождение — не трогаются;
- агент прежнего выпуска (без keep1), у которого висит вопрос о ключевой
  фразе (backup в wait/declined), получает документ один раз на отпечаток —
  иначе вопрос висел бы в его чате вечно;
- повторный ответ шлюза на тот же документ фиды заново не будит;
- первый блок настроек слота (агент только что получил keep1) — не событие
  бэкапа; блок нового устройства — событие.

Цена ошибки: карточка без даты применения после обновления сервера;
отказы, которые снова шлются каждым тактом; вечный вопрос о фразе у агента
3.3.1; лишние списки на каждый ответ; бэкап по событию «новое устройство»
у каждого шлюза сразу после наката.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tests.integration import test_gwcfg_delivery as _d
from tests.integration.test_gwcfg_delivery import _connect, _gw, _have, _snap
from tests.integration.test_gwlink_channel import _until

pytestmark = pytest.mark.integration
host, slot, srv = _d.host, _d.slot, _d.srv              # фикстуры сцены

OLD = "ab" * 8                       # отпечаток документа, в котором ещё был раздел backup


def _answered_old(services, **extra) -> None:
    """Шлюз применил прежний документ (всё, что в нём было)."""
    doc = services.gwcfg_doc(_gw(services))
    services.gwcfg_ack_in(1, {"gen": doc["gen"], "hash": OLD, "applied": list(doc["prints"]), **extra})


def _stored_by_the_old_server(services, held: str) -> None:
    """Ответ агента 3.3.1, сохранённый сервером 3.3.1 до наката: раздел
    backup тогда был в документе — и в ответе он есть (нынешний разбор
    ответа его уже не принимает, а сохранённое остаётся)."""
    doc = services.gwcfg_doc(_gw(services))
    services.db.set_state("gwcfg_ack_1", json.dumps({
        "gen": doc["gen"], "hash": OLD, "applied": list(doc["prints"]), "wait": {}, "declined": {},
        "failed": {}, "retry": False, held: {"backup": "cd" * 8}, "at": "2026-10-17T10:00:00+05:00"}))


# ── upd-3: ответ следует за документом без расхождения ──────────────────────

def test_an_answer_to_the_previous_document_follows_it_when_nothing_differs(services, slot):
    gw = _gw(services)
    services.gwlink_session_opened(1, "3.4.0", 2, ["cfg1", "keep1"])
    _answered_old(services, declined={"mail": "cd" * 8}, failed={"token": "бот не ответил"})
    _snap(services, _have(services))
    assert services.gwcfg_due(gw) is None, "без расхождения документ ушёл"
    doc = services.gwcfg_doc(gw)
    ack = services.gwcfg_ack(1)
    assert ack["hash"] == doc["hash"], "ответ остался «на другой документ»"
    assert ack["declined"] == {"mail": "cd" * 8}, "удержание прежнего «оставить своё» снялось"
    assert ack["failed"] == {}, "отказ прежнего документа выдан за отказ текущего (расхождения нет)"
    card = services.gwcfg_card(gw, services.gwlink_snapshot(1))
    assert card["applied_at"] and card["due"] == [] and card["failed"] == {}, card


def test_the_card_alone_keeps_the_applied_date_after_the_server_update(services, slot):
    """Карточку открыли раньше первого такта доставки (сразу после наката
    сервера): дата применения на месте и без него."""
    gw = _gw(services)
    services.gwlink_session_opened(1, "3.4.0", 2, ["cfg1", "keep1"])
    _answered_old(services)
    _snap(services, _have(services))
    card = services.gwcfg_card(gw, services.gwlink_snapshot(1))
    assert card["applied_at"] and card["due"] == [], card
    assert services.gwcfg_ack(1)["hash"] == services.gwcfg_doc(gw)["hash"]


def test_a_temporary_refusal_or_a_real_drift_is_not_rewritten(services, slot):
    gw = _gw(services)
    services.gwlink_session_opened(1, "3.4.0", 2, ["cfg1", "keep1"])
    _answered_old(services, retry=True)
    _snap(services, _have(services))
    services.gwcfg_due(gw)
    assert services.gwcfg_ack(1)["hash"] == OLD, "«повтори» выдан за применённый текущий документ"
    _answered_old(services)
    _snap(services, _have(services, env="0" * 16))
    assert services.gwcfg_due(gw) is not None
    assert services.gwcfg_ack(1)["hash"] == OLD, "расхождение есть, а ответ переписан на новый документ"
    assert services.gwcfg_card(gw, services.gwlink_snapshot(1))["applied_at"] == ""


# ── upd-3 / live-2: досылка агенту прежнего выпуска ─────────────────────────

@pytest.mark.parametrize("held", ["wait", "declined"])
def test_an_old_agent_holding_the_phrase_question_gets_the_document_once(services, slot, host, held):
    gw = _gw(services)
    services.gwlink_session_opened(1, "3.3.1", 2, ["cfg1"])
    _stored_by_the_old_server(services, held)
    _snap(services, _have(services))
    body = services.gwcfg_due(gw)
    doc = services.gwcfg_doc(gw)
    assert body is not None and body["hash"] == doc["hash"], "вопрос о фразе остаётся висеть у агента 3.3.1"
    assert "backup" not in body, "раздел, которого у сервера нет, вернулся в документ"
    assert services.gwcfg_due(gw) is None, "тот же документ досылается каждым тактом"
    host.hostname = "awg-srv-moved"                      # раздел server — новый документ
    gw = _gw(services)
    _snap(services, _have(services))
    assert services.gwcfg_due(gw) is not None, "новый документ — новая досылка, пока вопрос висит"
    assert services.gwcfg_due(gw) is None


@pytest.mark.parametrize("case", ["keep1", "nothing_held"])
def test_no_extra_document_without_a_hanging_phrase_question(services, slot, case):
    gw = _gw(services)
    services.gwlink_session_opened(1, "3.4.0", 2, ["cfg1", "keep1"] if case == "keep1" else ["cfg1"])
    if case == "keep1":
        _stored_by_the_old_server(services, "wait")
    else:
        _answered_old(services)
    _snap(services, _have(services))
    assert services.gwcfg_due(gw) is None


# ── вне оси: фиды — по смене документа, не по каждому ответу ────────────────

async def test_a_repeated_answer_to_the_same_document_does_not_wake_the_feeds(services, srv, monkeypatch):
    woke: list[str] = []

    async def lists(slot_id):
        woke.append("lists")

    async def peers(slot_id):
        woke.append("peers")
    monkeypatch.setattr(srv, "deliver_lists", lists)
    monkeypatch.setattr(srv, "deliver_peer_services", peers)
    gw = await _connect(srv, services, cfg=_have(services))
    await _until(lambda: services.gwlink_snapshot(1))
    await asyncio.sleep(0.1)
    woke.clear()
    doc = services.gwcfg_doc(services.db.gateway(1))
    for _ in range(3):
        await gw.send("cfg_ack", {"gen": doc["gen"], "hash": doc["hash"], "applied": ["env"]})
        await _until(lambda: services.gwcfg_ack(1))
        await asyncio.sleep(0.1)
    assert woke.count("lists") == 1 and woke.count("peers") == 1, woke
    await gw.send("cfg_ack", {"gen": doc["gen"] + 1, "hash": "ef" * 8, "applied": ["env"]})
    assert await _until(lambda: woke.count("lists") == 2), "ответ на новый документ фиды не разбудил"
    await gw.close()


# ── upd-2: первый блок настроек слота — не событие бэкапа ───────────────────

def _keep_event(services) -> list:
    raw = services.db.get_state_json("backup_event_due", {}) or {}
    return [w.get("kind") for w in raw.get("why") or []]


def test_the_first_settings_block_of_a_slot_is_not_a_backup_event(services, slot):
    services.backup_set_passphrase("correct horse battery staple")
    block = {"items": {"conf": {"quiet_hours.quiet_hours_start": 21}, "fw": {}, "mail": None},
             "at": "2026-10-19T10:00:00+05:00"}
    services.gwkeep_in(1, block)
    assert _keep_event(services) == [], "первый блок обновлённого агента — бэкап по событию «новое устройство»"
    services.gwkeep_in(1, {**block, "items": {**block["items"], "fw": {"ssh": "on"}}})
    assert _keep_event(services) == [], "блок того же устройства — событие бэкапа"
    services._gwkeep_save(1, {**services.gwkeep(1), "dev": "DUMMY-OTHER-DEVICE-PUB="})
    services.gwkeep_in(1, block)
    assert _keep_event(services) == ["keep"], "первый блок нового устройства без бэкапа по событию"
