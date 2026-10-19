"""Агент шлюза после перепроверки выпуска: отложенное при выключенном
VPN-транзите, вопросы канала под одним замком, остаток записи юнита.

- отложенные свои списки и записи SMB соседей при выключенном VPN-транзите
  стираются: скрипта для них в этом режиме нет и не будет, а повтор
  перезапускал бы юнит обвязки раз в 10 минут без конца;
- вопрос вешает поток канала (применение документа), снимает — поток экрана
  (ответ человека): одновременные hold и ответ не теряют ни одной записи;
- остаток прерванной записи юнита (.tmp) убирается следующей записью.

Цена ошибки: обвязка шлюза, перезапускаемая без конца (обрыв трафика
локальной сети каждые 10 минут); вопрос о почте, воскресший после ответа,
или новый вопрос, пропавший молча; мусор с текстом юнита рядом с ним.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from awgbot.domain.gateway import GatewayServices
from awgbot.infra import gwguard
from awgbot.infra.db import Database
from tests.unit._gw_unit_common import UNIT_TEXT, _Unit

pytestmark = pytest.mark.unit


@pytest.fixture()
def agent(tmp_path):
    db = Database(tmp_path / "gw.db"); db.init_schema()
    yield GatewayServices(db)
    db.close()


class _Plumbing:
    """Обвязка шлюза для повторов: режим VPN-транзита, есть ли скрипт
    списков с sync и помощник сервисов; перезапуски юнита — счётчиком."""

    def __init__(self, agent, monkeypatch, tmp_path, *, lan: bool):
        self.lan = lan
        self.reasserts: list[str] = []
        monkeypatch.setattr(gwguard, "lan_mode", lambda: self.lan)
        monkeypatch.setattr(gwguard, "lan_domain_has_sync", lambda: False)
        monkeypatch.setattr(gwguard, "LAN_SERVICES_SCRIPT", str(tmp_path / "нет-помощника.sh"))
        monkeypatch.setattr(agent, "reassert_guarded", lambda why: (self.reasserts.append(why), (True, ""))[1])
        self.agent = agent

    def due(self) -> None:
        self.agent._last_reassert = time.monotonic() - 3600     # троттлинг не держит


def _defer(agent) -> None:
    agent.db.set_state(agent._OWN_DEFER_KEY, json.dumps({"gen": 3, "lists": []}))
    agent.db.set_state(agent._SVC_PENDING_KEY, json.dumps({"hash": "ab" * 8, "items": [{"n": "nas"}]}))


# ── live-1: отложенное при выключенном VPN-транзите ─────────────────────────

def test_deferred_lists_and_records_are_dropped_when_the_transit_is_off(agent, monkeypatch, tmp_path):
    p = _Plumbing(agent, monkeypatch, tmp_path, lan=False)
    _defer(agent)
    for _ in range(3):
        p.due()
        assert agent.own_retry() is None and agent.services_retry() is None
    assert p.reasserts == [], "обвязка перезапускается ради отложенного при выключенном транзите"
    assert agent.db.get_state(agent._OWN_DEFER_KEY) == "" and agent.db.get_state(agent._SVC_PENDING_KEY) == "", \
        "отложенное осталось — повтор снова пойдёт, как только транзит включат с другим содержимым"


def test_with_the_transit_on_the_deferred_still_waits_for_the_script(agent, monkeypatch, tmp_path):
    """Контроль: транзит включён, скрипта ещё нет — повтор просит перезапуск
    обвязки (с троттлингом) и хранит отложенное."""
    p = _Plumbing(agent, monkeypatch, tmp_path, lan=True)
    _defer(agent)
    p.due()
    assert agent.own_retry() is None
    p.due()
    assert agent.services_retry() is None
    assert len(p.reasserts) == 2, p.reasserts
    assert agent.db.get_state(agent._OWN_DEFER_KEY) and agent.db.get_state(agent._SVC_PENDING_KEY)


# ── live-4: вопросы канала под одним замком ─────────────────────────────────

def test_a_question_hung_by_the_channel_and_an_answer_at_once_lose_nothing(agent, monkeypatch):
    """Поток экрана отвечает «оставить своё» на вопрос о почте, в тот же миг
    поток канала вешает вопрос по другому разделу. Чтение и запись медленные
    (диск шлюза) — без общего замка одна запись затёрла бы другую. Сдвиг
    второго потока перебирается: какое-то окно чтение-запись совпадёт."""
    mail = {"login": "box@example.org", "password": "DUMMY-pass", "imap_host": "imap.example.org",
            "imap_port": 993, "smtp_host": "smtp.example.org", "smtp_port": 465}
    real = agent.db.get_state_json

    def slow(key, default=None):
        value = real(key, default)
        time.sleep(0.03)                                  # между чтением и записью — окно для другого потока
        return value
    lost = []
    for step in range(10):
        for key in (agent._CFG_PENDING_KEY, agent._CFG_WAIT_KEY, agent._CFG_KEEP_KEY):
            agent.db.set_state(key, "")
        monkeypatch.setattr(agent.db, "get_state_json", real)
        agent.cfg_hold("mail", mail, "ab" * 8, 2, "cd" * 8)
        monkeypatch.setattr(agent.db, "get_state_json", slow)
        errors: list = []

        def run(fn, delay):
            time.sleep(delay)
            try:
                fn()
            except Exception as e:                        # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=run, args=(lambda: agent.cfg_answer("mail", accept=False), 0)),
                   threading.Thread(target=run, args=(lambda: agent.cfg_hold("link", {"conf": "DUMMY"}, "ef" * 8,
                                                                             3, "01" * 8), step * 0.015))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        monkeypatch.setattr(agent.db, "get_state_json", real)
        assert errors == [], errors
        got = (sorted(agent.cfg_pending()), sorted(agent.cfg_wait()), agent.cfg_keep())
        if got != (["link"], ["link"], {"mail": "ab" * 8}):
            lost.append((step, got))
    assert lost == [], f"одна запись затёрла другую (сдвиг, pending, wait, keep): {lost}"


# ── вне оси: остаток прерванной записи юнита ────────────────────────────────

@pytest.mark.parametrize("change", [True, False])
def test_a_leftover_of_an_interrupted_unit_write_is_cleaned_up(tmp_path, monkeypatch, change):
    """Запись юнита упала между временным файлом и переименованием — рядом с
    юнитом остался .tmp с его текстом. Следующая запись (с правкой или без)
    его убирает; юнит — тот, что должен быть."""
    unit = _Unit(tmp_path, monkeypatch)
    leftover = unit.path.with_suffix(".tmp")
    leftover.write_text(UNIT_TEXT.replace("10.8.1.2", "10.8.1.99"), encoding="utf-8")
    want = "192.168.70.0/24" if change else "192.168.68.0/24"
    gwguard.unit_set_env({"LAN_SUBNETS": want})
    assert not leftover.exists(), "остаток прерванной записи юнита остался на диске"
    assert gwguard.unit_env("LAN_SUBNETS") == want and gwguard.unit_env("ADMIN_IPS") == "10.8.1.2"
