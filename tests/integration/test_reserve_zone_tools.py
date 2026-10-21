"""Пакеты гостевой зоны ставятся сами (reserve/invite.py: reserve_tools_missing,
reserve_tools_ensure, reserve_tools_tick, reserve_require_zone_tools) — план
скрипта зоны и apt-get подменой, база настоящая.

Цена ошибки: пакетов нет, а бот молчит — пара собрана и «готова», но в день
аварии резерв не поднимет зону для клиентов второго сервера; установка в
потоке бота — экран и канал пары стоят минутами, пока apt-get качает пакеты;
две установки разом — замок dpkg и отказ обеих; уведомление об отказе каждую
минуту — админ перестаёт читать бота; уведомление, не снятое после успеха,
— следующий отказ уже никто не увидит; кэш плана зоны, не сброшенный после
установки, — направление «ко мне» ещё десять минут закрыто «нет dnsmasq».
"""
from __future__ import annotations

import threading

import pytest

from awgbot.core import config, settings
from awgbot.domain.services.reserve import compat
from awgbot.domain.services.reserve import invite as invitemod
from awgbot.infra import rsvzone
from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration import test_reserve_actions as _actions

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
act, wired = _actions.act, _actions.wired            # фикстуры такта планировщика

# настоящие функции — до автоподмен conftest (там пакетам «всего хватает»)
_REAL_MISSING = invitemod.ReserveInviteMixin.reserve_tools_missing
_REAL_ZONE_WHY = compat.zone_why


class _Host:
    """Сервер глазами установки: что говорит план скрипта зоны, что делает
    apt-get (журнал вызовов, отказ, «долгая» установка до сигнала теста)."""

    def __init__(self, monkeypatch) -> None:
        self.plan: dict = {"dnsmasq": "no", "conntrack": "no", "python": "yes", "webhook_port": "443"}
        self.plan_calls = 0
        self.installs: list[list[str]] = []
        self.fail = ""
        self.gate: threading.Event | None = None
        self.started = threading.Event()
        monkeypatch.setattr(invitemod.ReserveInviteMixin, "reserve_tools_missing", _REAL_MISSING)
        monkeypatch.setattr(rsvzone, "plan", self._plan)
        monkeypatch.setattr(rsvzone, "install_tools", self._install)

    def _plan(self) -> dict:
        self.plan_calls += 1
        return dict(self.plan)

    def _install(self, names, timeout=900):
        self.installs.append(list(names))
        self.started.set()
        if self.gate is not None:
            self.gate.wait(10)
        if self.fail:
            raise rsvzone.ZoneError(self.fail)
        for n in names:
            self.plan[n] = "yes"                      # поставлено — план зоны это видит
        return " ".join(names)


def _join() -> None:
    """Дождаться фоновой установки (поток «rsv-tools»)."""
    for t in threading.enumerate():
        if t.name == "rsv-tools":
            t.join(10)
            assert not t.is_alive(), "фоновая установка не закончилась за 10 с"


@pytest.fixture()
def host(services, rsv, monkeypatch):
    h = _Host(monkeypatch)
    yield h
    if h.gate is not None:
        h.gate.set()
    _join()


def _ago(hours: float) -> str:
    return timeutil.to_iso(timeutil.shift(timeutil.now(), -hours * 3600))


# ── чего не хватает ─────────────────────────────────────────────────────────

def test_missing_tools_are_read_from_the_zone_plan(services, host):
    assert services.reserve_tools_missing() == ["dnsmasq", "conntrack"]
    host.plan["dnsmasq"] = "yes"
    assert services.reserve_tools_missing() == ["conntrack"]
    host.plan["conntrack"] = "yes"
    assert services.reserve_tools_missing() == []


def test_an_unread_plan_or_a_silent_key_is_not_a_reason_to_run_apt_get(services, host):
    """План не прочитан (скрипт зоны отказал) или ключа нет — «нет пакета» не
    выводится: установка по догадке ставила бы пакеты на каждом такте."""
    host.plan = {"error": "скрипт зоны не запустился"}
    assert services.reserve_tools_missing() == []
    host.plan = {"dnsmasq": "", "python": "yes"}
    assert services.reserve_tools_missing() == []


# ── установка ───────────────────────────────────────────────────────────────

def test_ensure_installs_in_the_background_and_answers_at_once(services, host):
    """Ответ — сразу (строка для всплывашки), apt-get — в фоне; пока он идёт,
    вызывающий не ждёт."""
    host.gate = threading.Event()
    assert services.reserve_tools_ensure() == "ставлю dnsmasq, conntrack"
    assert host.started.wait(5), "установка не запустилась"
    assert services.rsv_get("rsv_view").get("tools_at") in (None, ""), \
        "отметка установки записана до её конца — значит, вызов ждал apt-get"
    host.gate.set()
    _join()
    assert host.installs == [["dnsmasq", "conntrack"]]
    view = services.rsv_get("rsv_view")
    assert view.get("tools_at") and not view.get("tools_fail"), view


def test_a_second_ensure_during_the_install_does_not_start_another_apt_get(services, host):
    """Два apt-get разом — замок dpkg и отказ обоих: повтор (такт, второе
    нажатие) только говорит «ставлю»."""
    host.gate = threading.Event()
    services.reserve_tools_ensure()
    assert host.started.wait(5)
    assert services.reserve_tools_ensure() == "ставлю dnsmasq, conntrack"
    host.gate.set()
    _join()
    assert host.installs == [["dnsmasq", "conntrack"]], f"установка запущена дважды: {host.installs}"


def test_after_the_install_the_lock_is_free_for_a_later_retry(services, host):
    host.fail = "apt-get: E: Unable to locate package dnsmasq"
    services.reserve_tools_ensure()
    _join()
    host.fail = ""
    services.reserve_tools_ensure()
    _join()
    assert len(host.installs) == 2, f"после отказа замок не снят — повтора не будет: {host.installs}"


def test_a_successful_install_clears_the_failure_and_the_notice_mark(services, host):
    services.rsv_update("rsv_view", tools_fail="apt-get: старый отказ", tools_told=True)
    services.reserve_tools_ensure()
    _join()
    view = services.rsv_get("rsv_view")
    assert not view.get("tools_fail") and view.get("tools_told") is False, \
        f"после успеха старый отказ остался — следующий отказ админ не увидит: {view}"


def test_a_successful_install_drops_the_cached_zone_plan(services, host, monkeypatch):
    """Кэш zone_why (10 минут) держит «нет dnsmasq» — после установки он
    сброшен, и направление «ко мне» открывается на следующем st."""
    monkeypatch.setattr(compat, "zone_why", _REAL_ZONE_WHY)
    assert compat.zone_why() == "на сервере нет dnsmasq — резолвер зоны не поднимется"
    services.reserve_tools_ensure()
    _join()
    assert compat.zone_why() == "", "кэш плана зоны пережил установку — «нет dnsmasq» ещё 10 минут"


def test_a_failed_install_writes_the_reason_and_the_time(services, host):
    host.fail = "apt-get: E: Unable to locate package dnsmasq"
    assert services.reserve_tools_ensure() == "ставлю dnsmasq, conntrack"
    _join()
    view = services.rsv_get("rsv_view")
    assert view.get("tools_fail") == "apt-get: E: Unable to locate package dnsmasq", view
    assert view.get("tools_at"), "время отказа не записано — повтор пойдёт на каждом такте"


def test_a_long_failure_reason_is_cut(services, host):
    host.fail = "apt-get: " + "x" * 1000
    services.reserve_tools_ensure()
    _join()
    assert len(services.rsv_get("rsv_view")["tools_fail"]) == 200


def test_nothing_missing_means_no_install_and_an_old_failure_is_cleared(services, host):
    host.plan.update(dnsmasq="yes", conntrack="yes")
    services.rsv_update("rsv_view", tools_fail="apt-get: поставили руками", tools_told=True)
    assert services.reserve_tools_ensure() == ""
    _join()
    assert host.installs == [], "всё на месте — а apt-get запускался"
    view = services.rsv_get("rsv_view")
    assert not view.get("tools_fail") and view.get("tools_told") is False, \
        f"пакеты поставили руками, а отказ висит: {view}"


def test_pairing_and_invite_hand_the_missing_tools_to_the_background_install(services, host):
    """Сопряжение (обе стороны) зовёт reserve_require_zone_tools: не отказ,
    а установка в фоне."""
    services.reserve_require_zone_tools()
    _join()
    assert host.installs == [["dnsmasq", "conntrack"]]


# ── такт ────────────────────────────────────────────────────────────────────

def test_the_tick_installs_at_once_when_it_never_tried(services, host):
    rc.pair_row(services)
    assert services.reserve_tools_tick() == []
    _join()
    assert host.installs == [["dnsmasq", "conntrack"]]


def test_the_tick_retries_no_sooner_than_six_hours(services, host):
    rc.pair_row(services)
    host.fail = "apt-get: E: сеть недоступна"
    services.rsv_update("rsv_view", tools_at=_ago(5.9), tools_fail=host.fail, tools_told=True)
    services.reserve_tools_tick()
    _join()
    assert host.installs == [], "повтор раньше шести часов"
    services.rsv_update("rsv_view", tools_at=_ago(6.1))
    services.reserve_tools_tick()
    _join()
    assert host.installs == [["dnsmasq", "conntrack"]], "через шесть часов повтора не было"


def test_a_broken_time_mark_means_retry_now(services, host):
    rc.pair_row(services)
    services.rsv_update("rsv_view", tools_at="не дата")
    services.reserve_tools_tick()
    _join()
    assert len(host.installs) == 1


def test_a_failure_is_told_to_the_admin_once(services, host):
    """Отказ — одно уведомление админу с причиной; следующие такты (и повтор
    через шесть часов с тем же отказом) молчат."""
    rc.pair_row(services)
    host.fail = "apt-get: E: Unable to locate package <dnsmasq>"
    assert services.reserve_tools_tick() == [], "уведомление раньше, чем установка закончилась"
    _join()
    notes = services.reserve_tools_tick()
    assert len(notes) == 1 and notes[0].tg_id == int(config.ADMIN_ID), notes
    assert "не поставились пакеты: apt-get: E: Unable to locate package &lt;dnsmasq&gt;" in notes[0].text, notes[0].text
    assert "<code>apt-get install dnsmasq conntrack</code>" in notes[0].text
    assert services.reserve_tools_tick() == [], "отказ сообщён второй раз"
    services.rsv_update("rsv_view", tools_at=_ago(7))
    services.reserve_tools_tick()
    _join()
    assert services.reserve_tools_tick() == [], "повтор с тем же отказом — снова уведомление"
    assert len(host.installs) == 2


def test_after_a_success_a_new_failure_is_told_again(services, host):
    rc.pair_row(services)
    host.fail = "apt-get: первый отказ"
    services.reserve_tools_tick()
    _join()
    assert len(services.reserve_tools_tick()) == 1
    host.fail = ""
    services.rsv_update("rsv_view", tools_at=_ago(7))
    services.reserve_tools_tick()
    _join()
    host.plan.update(dnsmasq="no")                   # пакет снова пропал (сняли руками)
    host.fail = "apt-get: второй отказ"
    services.rsv_update("rsv_view", tools_at=_ago(7))
    services.reserve_tools_tick()
    _join()
    notes = services.reserve_tools_tick()
    assert len(notes) == 1 and "второй отказ" in notes[0].text, notes


def test_the_tick_is_silent_and_installs_nothing_without_a_pair_and_without_readiness(services, host):
    """Резервом сервер не будет — пакеты зоны ему не нужны, ставить их и
    тревожить админа нечего."""
    settings.set_value("app.reserve.ready", False)
    services.rsv_update("rsv_view", tools_fail="apt-get: старый отказ", tools_told=False)
    assert services.reserve_tools_tick() == []
    _join()
    assert host.installs == []


def test_the_tick_works_with_readiness_on_even_before_a_pair(services, host):
    settings.set_value("app.reserve.ready", True)
    services.reserve_tools_tick()
    _join()
    assert host.installs == [["dnsmasq", "conntrack"]]


def test_with_everything_present_the_tick_neither_installs_nor_tells(services, host):
    rc.pair_row(services)
    host.plan.update(dnsmasq="yes", conntrack="yes")
    for _ in range(3):
        assert services.reserve_tools_tick() == []
    _join()
    assert host.installs == []


# ── медленный такт планировщика ─────────────────────────────────────────────

async def test_the_slow_tick_brings_the_tools_failure_to_the_admin_even_if_the_record_watch_fails(
        mv, wired, monkeypatch):
    """Такт раз в минуту: отказ установки доходит до админа уведомлением,
    сбой соседнего шага (надзор записи) его не глушит, и второй такт молчит."""
    svc = wired.svc
    h = _Host(monkeypatch)
    h.fail = "apt-get: E: сеть недоступна"

    def boom():
        raise RuntimeError("DNS упал")
    monkeypatch.setattr(svc, "reserve_record_watch_tick", boom)
    monkeypatch.setattr(svc, "reserve_return_notify_tick", lambda: [])
    await wired.jobs["rsv_slow_tick"][0]()
    _join()
    assert h.installs == [["dnsmasq", "conntrack"]], "медленный такт не запустил установку"
    wired.ran.clear()
    await wired.jobs["rsv_slow_tick"][0]()
    notes = [n for r in wired.ran for a in r if a[0] == "notify" for n in a[1]]
    assert len(notes) == 1 and "не поставились пакеты: apt-get: E: сеть недоступна" in notes[0].text, wired.ran
    wired.ran.clear()
    await wired.jobs["rsv_slow_tick"][0]()
    assert not [a for r in wired.ran for a in r if a[0] == "notify"], f"отказ сообщён второй раз: {wired.ran}"
