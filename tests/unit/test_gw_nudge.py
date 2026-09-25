"""Повтор после поломки на шлюзе: агент просит сервер прислать канон своих
списков (записи SMB) заново, когда отказ стоит дольше интервала.

Сервер за сессию один и тот же канон второй раз не шлёт. Поломку на малине
(нет места, диск только для чтения) чинят руками — и без просьбы агента
шлюз ждал бы переподключения канала (дни). Просьба — трафик в туннеле,
поэтому не раньше чем через интервал после отказа и не чаще интервала.

Цена ошибки: без просьбы — Finder и свои списки пусты до переподключения при
починенном диске; просьба на каждом тике — маячок в туннеле и рестарт-попытки
на каждом такте; просьба при отложенном из-за обвязки — двойной повтор того,
что и так повторяется само.
"""
from __future__ import annotations

import builtins
import datetime
import errno
import json

import pytest

from awgbot.domain import gateway as gw_mod
from awgbot.domain.gateway import GatewayServices
from awgbot.infra import gwguard
from awgbot.infra.db import Database
from awgbot.util import timeutil
from tests.unit.test_gwownlists import _canon, _Host
from tests.unit.test_gwservices import H_NAS, NAS, _Peer

OWN, SVC = GatewayServices._OWN_NUDGE_KEY, GatewayServices._SVC_NUDGE_KEY
AFTER = GatewayServices._NUDGE_AFTER_S
FULL = "ошибка записи файла: нет места на диске"


class _Clock:
    """Часы агента: сдвиг в секундах от настоящего момента."""

    def __init__(self, monkeypatch):
        self.base = timeutil.now()
        self.shift = 0.0
        monkeypatch.setattr(timeutil, "now", lambda: self.base + datetime.timedelta(seconds=self.shift))

    def at(self, seconds: float) -> None:
        self.shift = float(seconds)


class _Disk:
    """Диск малины: `full` — запись файла `path()` падает ENOSPC, прочее
    (БД, состояние) пишется как обычно."""

    def __init__(self, monkeypatch, path):
        self.full = False
        real_open = builtins.open

        def fake_open(p, mode="r", *a, **k):
            if self.full and str(p) == path() and "w" in mode:
                raise OSError(errno.ENOSPC, "No space left on device", str(p))
            return real_open(p, mode, *a, **k)
        monkeypatch.setattr(gw_mod, "open", fake_open, raising=False)


@pytest.fixture()
def clock(monkeypatch):
    return _Clock(monkeypatch)


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr(GatewayServices, "_own_run", "")
    db = Database(tmp_path / "gw.db"); db.init_schema()
    a = GatewayServices(db)
    a._last_reassert = -1e9
    yield a
    db.close()


def _due(agent, key: str) -> bool:
    return agent._nudge_due(key)


# ── интервалы ────────────────────────────────────────────────────────────────

def test_a_fresh_refusal_is_not_nudged_before_the_interval(agent, clock):
    """Первые десять минут после отказа — ни одной просьбы: поломку ещё не
    починили, а просьба сразу дала бы тот же отказ."""
    agent._nudge_note(OWN, FULL)
    for t in (0, 60, AFTER - 1):
        clock.at(t)
        assert _due(agent, OWN) is False, f"просьба через {t} с после отказа"
    clock.at(AFTER)
    assert _due(agent, OWN) is True, "отказ стоит интервал — просьбы нет"


def test_after_a_nudge_the_next_one_waits_a_full_interval(agent, clock):
    """Просьба ушла, сервер повторил, отказ тот же — следующая не раньше
    интервала: иначе на каждом тике по пакету в туннель."""
    agent._nudge_note(OWN, FULL)
    clock.at(AFTER)
    assert _due(agent, OWN) is True
    for t in (AFTER, AFTER + 1, 2 * AFTER - 1):
        clock.at(t)
        assert _due(agent, OWN) is False, f"повторная просьба через {t - AFTER} с после прошлой"
    clock.at(2 * AFTER)
    assert _due(agent, OWN) is True, "просьбы прекратились, хотя отказ стоит"
    assert _due(agent, OWN) is False, "два вызова в один момент — две просьбы"


def test_the_same_refusal_again_does_not_restart_the_wait(agent, clock):
    """Сервер повторил, шлюз ответил тем же — отсчёт «с какого момента
    отказ» не сдвигается: иначе повтор откладывал бы повтор бесконечно."""
    agent._nudge_note(OWN, FULL)
    clock.at(AFTER - 100)
    agent._nudge_note(OWN, FULL)
    clock.at(AFTER)
    assert _due(agent, OWN) is True, "тот же отказ перезапустил ожидание"


def test_a_different_refusal_restarts_the_wait(agent, clock):
    """Отказ сменился (диск почистили, но стал только для чтения) — новая
    поломка, новый отсчёт; прошлая просьба не в счёт."""
    agent._nudge_note(OWN, FULL)
    clock.at(AFTER)
    assert _due(agent, OWN) is True
    clock.at(AFTER + 100)
    agent._nudge_note(OWN, "ошибка записи файла: диск только для чтения")
    clock.at(2 * AFTER + 99)
    assert _due(agent, OWN) is False, "новая поломка — просьба раньше интервала от её начала"
    clock.at(2 * AFTER + 100)
    assert _due(agent, OWN) is True


def test_a_success_clears_the_nudge(agent, clock):
    """Применилось — просьба снята: ни одного пакета ни через интервал, ни
    через сутки."""
    agent._nudge_note(OWN, FULL)
    agent._nudge_note(OWN, "")
    assert (agent.db.get_state(OWN) or "") == "", "ключ просьбы пережил успех"
    for t in (AFTER, 86400):
        clock.at(t)
        assert _due(agent, OWN) is False, f"после успеха просьба через {t} с"


@pytest.mark.parametrize("err", [
    "скрипт старого образца — обвязка перевыставляется",
    "скрипт старого образца — обвязка перевыставится в ближайшие минуты",
    "отсутствует скрипт — обвязка перевыставляется",
    "отсутствует скрипт — обвязка перевыставится в ближайшие минуты",
])
def test_a_refusal_for_the_old_plumbing_is_never_nudged(agent, clock, err):
    """Обвязка старого образца — не поломка: отложенное повторяется само на
    тике (own_retry / services_retry). Просьба к серверу дала бы второй повтор
    того же — и лишний пакет в туннеле."""
    agent._nudge_note(OWN, err)
    for t in (AFTER, 10 * AFTER, 86400):
        clock.at(t)
        assert _due(agent, OWN) is False, f"просьба при отложенном из-за обвязки ({t} с)"


@pytest.mark.parametrize("err", [
    "не найдены файлы своих списков — функционал локальной сети без VPN недоступен",
    "отсутствует скрипт — перевыпусти конфигурацию шлюза",
    "не пройдена проверка строк",
    "dnsmasq --test отверг списки — откатываю",
    "скрипт не ответил за 150 с",
    # «ошибка записи файла» не в начале — чужой хвост, не поломка агента
    "rc=1: ошибка записи файла",
])
def test_a_refusal_that_is_not_a_breakage_is_never_nudged(agent, clock, err):
    """Просьба — только при поломке на шлюзе («ошибка записи файла…»):
    остальные отказы от повтора того же канона не пройдут, а просьба раз в
    10 минут — вечный маячок в туннеле и перезапись файлов на малине."""
    agent._nudge_note(OWN, err)
    agent._nudge_note(SVC, err)
    for t in (AFTER, 10 * AFTER, 86400):
        clock.at(t)
        assert _due(agent, OWN) is False and _due(agent, SVC) is False, f"просьба при «{err}» ({t} с)"


def test_a_content_refusal_of_the_services_is_not_nudged(agent, clock, tmp_path, monkeypatch):
    """Помощник отверг записи (dnsmasq --test) — повтор даст тот же отказ:
    просьбы нет ни через интервал, ни через сутки."""
    peer = _Peer(tmp_path, monkeypatch)
    peer.helper.write_text("#!/bin/sh\n", encoding="utf-8")
    peer.rc_ok, peer.tail = False, "dnsmasq: bad option at line 3"
    res = agent.apply_peer_services(H_NAS, [NAS])
    assert res["ok"] is False and res["error"] == "dnsmasq: bad option at line 3", res
    for t in (AFTER, 86400):
        clock.at(t)
        assert agent.services_nudge_due() is False, f"просьба при отказе по содержимому ({t} с)"


def test_nothing_stored_or_a_broken_record_means_no_nudge(agent, clock):
    """Ключа нет или он битый (ручная правка, старая база) — просьбы нет и
    исключения нет: тик монитора не должен падать."""
    clock.at(86400)
    assert _due(agent, OWN) is False
    agent.db.set_state(OWN, json.dumps({"err": FULL, "since": "вчера", "asked": ""}))
    assert _due(agent, OWN) is False
    agent.db.set_state(OWN, "не json")
    assert _due(agent, OWN) is False


# ── через настоящие пути применения ──────────────────────────────────────────

def test_a_full_disk_on_the_canon_leads_to_one_nudge_and_a_fix_clears_it(agent, clock, tmp_path, monkeypatch):
    """Канон не записался (диск полон) — через интервал агент просит его
    снова; после починки канон применён — просьб больше нет. Записи SMB этим
    не задеты."""
    host = _Host(tmp_path, monkeypatch)
    disk = _Disk(monkeypatch, lambda: gwguard.OWN_LISTS_NEW)
    disk.full = True
    res = agent.apply_own_lists(_canon({"a.com": "vpn"}))
    assert res["ok"] is False and res["error"] == FULL, res
    assert agent.own_nudge_due() is False, "просьба сразу после отказа"
    clock.at(AFTER)
    assert agent.services_nudge_due() is False, "отказ своих списков запросил записи SMB"
    assert agent.own_nudge_due() is True
    assert agent.own_nudge_due() is False, "вторая просьба в тот же момент"
    disk.full = False
    res = agent.apply_own_lists(_canon({"a.com": "vpn"}))
    assert res["ok"] is True and host.lists() == {"a.com": "vpn"}, res
    clock.at(10 * AFTER)
    assert agent.own_nudge_due() is False, "после успешного применения просьба осталась"


def test_a_refused_services_file_leads_to_a_nudge_and_a_success_clears_it(agent, clock, tmp_path, monkeypatch):
    """Записи SMB: файл не записался — через интервал просьба; записались —
    снята. Свои списки этим не задеты."""
    peer = _Peer(tmp_path, monkeypatch)
    peer.helper.write_text("#!/bin/sh\n", encoding="utf-8")
    disk = _Disk(monkeypatch, lambda: gwguard.PEER_SERVICES_NEW)
    disk.full = True
    res = agent.apply_peer_services(H_NAS, [NAS])
    assert res["ok"] is False and res["error"].endswith("нет места на диске"), res
    clock.at(AFTER - 1)
    assert agent.services_nudge_due() is False
    clock.at(AFTER)
    assert agent.own_nudge_due() is False, "отказ записей SMB запросил свои списки"
    assert agent.services_nudge_due() is True
    disk.full = False
    res = agent.apply_peer_services(H_NAS, [NAS])
    assert res["ok"] is True, res
    clock.at(10 * AFTER)
    assert agent.services_nudge_due() is False, "после успеха просьба осталась"


def test_a_missing_helper_is_retried_by_itself_not_by_a_nudge(agent, clock, tmp_path, monkeypatch):
    """Помощника нет (обвязка старого образца) — записи отложены и
    повторяются на тике сами; просить сервер не нужно."""
    _Peer(tmp_path, monkeypatch)                     # помощника на диске нет
    res = agent.apply_peer_services(H_NAS, [NAS])
    assert res["ok"] is False and "обвязка перевыстав" in res["error"], res
    clock.at(10 * AFTER)
    assert agent.services_nudge_due() is False, "просьба при отложенном из-за обвязки"
