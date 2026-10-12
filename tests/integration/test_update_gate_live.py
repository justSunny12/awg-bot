"""Сверка версий ролей в движении: уведомление только о цели новее
объявленной; версии другой стороны — после ответа GitHub; выбор и запись под
одним замком (гонка проверки и пересчёта, два параллельных пересчёта —
одно уведомление); пересчёт при снятии слота и страховочно в такте канала;
исключение пересчёта не рвёт канал; окно ожидания после старта у обеих
ролей; «последний раз на связи» — правда и для живой сессии.
"""
from __future__ import annotations

import datetime
import threading

import pytest

from awgbot.core import config
from awgbot.core import settings
from awgbot.domain.gateway import GatewayServices
from awgbot.domain.selfupdate import UPDATE_HOLD_MINUTES, UPDATE_PEER_FRESH_DAYS
from awgbot.infra import updates
from awgbot.infra.db import Database
from awgbot.runtime import linkclient, linkserver
from awgbot.util import timeutil

pytestmark = pytest.mark.integration


def _r(tag: str, roles: dict) -> updates.Release:
    meta = {"schema": 1, "version": tag.lstrip("v"), "awg_gen": 1, "roles": roles}
    return updates.Release(tag=tag, version=updates.parse_version(tag), body="- пункт",
                           asset_url=f"https://api/assets/{tag}", sha256="a" * 64,
                           meta=updates.meta.check(meta, tag))


RELEASES = [
    _r("v3.3.0", {"main": {"requires": "3.2.0"}, "gw": {"requires": "3.2.0"}}),
    _r("v3.3.1", {"main": {"requires": "3.3.0", "needs_gw": "3.3.0"}}),
    _r("v3.3.2", {"main": {"requires": "3.3.0", "needs_gw": "3.3.1"}}),
]


def _github(monkeypatch, releases=RELEASES, during=None):
    def lr(**_):
        if during is not None:
            during()
        return list(releases)
    monkeypatch.setattr(updates, "list_releases", lr)


def _offline(monkeypatch):
    def boom(**_):
        raise AssertionError("пересчёт пошёл на GitHub")
    monkeypatch.setattr(updates, "list_releases", boom)


def _ago(**kw) -> str:
    return timeutil.to_iso(timeutil.now() - datetime.timedelta(**kw))


@pytest.fixture()
def vps(services, make_active_client, monkeypatch):
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.3.0")
    monkeypatch.setattr(config, "ROLE", "client")
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID, device_limit=0)
    pi = services.add_device(admin.id, "Pi5")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    return services


def _agent_version(vps, ver: str) -> None:
    vps.gwlink_session_closed(1)
    vps.gwlink_session_opened(1, ver, 2, [])


# ── уведомление: только о цели новее объявленной ─────────────────────────────

def test_a_target_that_drops_and_comes_back_is_not_announced_twice(vps, monkeypatch):
    _github(monkeypatch)
    _agent_version(vps, "3.3.1")
    found = vps.update_scan()
    assert found.tag == "v3.3.2" and vps.update_to_notify(found).tag == "v3.3.2"
    _offline(monkeypatch)
    _agent_version(vps, "3.3.0")                               # шлюз откатили
    assert vps.update_regate() is None and vps.update_available_tag() == "v3.3.1", \
        "цель ниже объявленной — без уведомления"
    _agent_version(vps, "3.3.1")
    assert vps.update_regate() is None and vps.update_available_tag() == "v3.3.2", \
        "вернулась к уже объявленной — второго уведомления нет"


def test_versions_of_the_other_side_are_read_after_the_network_answers(vps, monkeypatch):
    """Шлюз обновился, пока GitHub отвечал: решение — по версии на момент
    ответа, а не на момент запроса."""
    _agent_version(vps, "3.2.1")
    _github(monkeypatch, during=lambda: _agent_version(vps, "3.3.1"))
    assert vps.update_scan().tag == "v3.3.2"
    assert vps.update_waiting() is None


def test_a_regate_during_a_github_request_and_the_scan_notify_once(vps, monkeypatch):
    """Проверка ждёт GitHub, а в это время шлюз обновился и пересчёт уже
    объявил цель: проверка, дойдя до решения, ту же цель второй раз не
    объявляет, и тег — по свежим версиям."""
    _github(monkeypatch)
    _agent_version(vps, "3.2.1")
    assert vps.update_scan() is None                           # список релизов сохранён
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        assert release.wait(5)
    _github(monkeypatch, during=slow)
    out: dict = {}
    scan = threading.Thread(target=lambda: out.update(found=vps.update_scan()))
    scan.start()
    assert started.wait(5)
    _agent_version(vps, "3.3.0")
    first = vps.update_regate()                               # замок не держится на время сети
    release.set()
    scan.join(5)
    assert first is not None and first.tag == "v3.3.1"
    assert out["found"].tag == "v3.3.1" and vps.update_to_notify(out["found"]) is None
    assert vps.update_available_tag() == "v3.3.1" and vps.update_waiting()["tag"] == "v3.3.2"


def test_two_parallel_regates_notify_once(vps, monkeypatch):
    _github(monkeypatch)
    _agent_version(vps, "3.2.1")
    vps.update_scan()
    _offline(monkeypatch)
    _agent_version(vps, "3.3.1")
    barrier, got = threading.Barrier(2), []

    def run():
        barrier.wait(5)
        got.append(vps.update_regate())
    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert sorted(x.tag if x else "" for x in got) == ["", "v3.3.2"]


# ── где пересчитывается ──────────────────────────────────────────────────────

def test_removing_the_slot_regates_without_announcing(vps, monkeypatch):
    """Снятый шлюз больше не держит: тег пересчитан сразу, а уведомит
    страховочный пересчёт такта канала (у снятия своего бота нет)."""
    _github(monkeypatch)
    _agent_version(vps, "3.2.1")
    assert vps.update_scan() is None
    _offline(monkeypatch)
    monkeypatch.setattr(vps, "_run_link_script", lambda *a, **k: None)
    monkeypatch.setattr(settings, "set_value", lambda k, v: [k])
    vps.gateway_remove(1)
    assert vps.update_available_tag() == "v3.3.2" and vps.update_waiting() is None
    assert vps.update_regate().tag == "v3.3.2", "объявление — следующим пересчётом"


class _Srv:
    """Слушатель канала без сокетов: такт живости зовёт только это."""

    def __init__(self, live=()):
        self._sessions = {sid: type("S", (), {"hello": True})() for sid in live}
        self.delivered = 0

    async def rekey_boot(self): pass
    async def ensure(self): pass
    async def sweep_dead(self): pass
    async def send_roles(self): pass
    async def rekey_tick(self): pass

    def deliver_soon(self):
        self.delivered += 1


async def test_the_liveness_tick_touches_live_sessions_and_regates_every_20th(vps, monkeypatch):
    _github(monkeypatch)
    _agent_version(vps, "3.2.1")
    vps.update_scan()
    _offline(monkeypatch)
    srv = _Srv(live=[1])
    monkeypatch.setattr(linkserver, "_server", srv)
    got = []

    async def on_update(nxt):
        got.append(nxt.tag)
    monkeypatch.setattr(linkserver, "_on_update", on_update)
    vps.db.set_state("gwlink_seen_1", _ago(hours=1))
    sess = vps.db.get_state("gwlink_session_1")
    vps.db.set_state("gwlink_session_1", sess.replace("3.2.1", "3.3.1"))   # событие о версии потерялось
    for _ in range(19):
        await linkserver.liveness_tick(vps)
    assert got == [] and srv.delivered == 4
    seen = timeutil.parse_iso(vps.db.get_state("gwlink_seen_1"))
    assert (timeutil.now() - seen).total_seconds() < 60, "живая сессия не освежила «последний раз на связи»"
    await linkserver.liveness_tick(vps)
    assert got == ["v3.3.2"], "страховочный пересчёт на 20-м такте"


async def test_an_exception_in_the_regate_stays_at_the_call_site(vps, monkeypatch, caplog):
    def boom(notify=True):
        raise RuntimeError("подставной сбой")
    monkeypatch.setattr(vps, "update_regate", boom)
    await linkserver.update_regate(vps)                         # не бросает
    assert "подставной сбой" in caplog.text
    agent = GatewayServices.__new__(GatewayServices)
    agent.update_regate = boom
    await linkclient.update_regate(agent)
    assert caplog.text.count("подставной сбой") == 2


# ── «последний раз на связи» ─────────────────────────────────────────────────

def test_seen_is_touched_only_for_live_sessions_with_a_fresh_handshake(vps, make_active_client, monkeypatch):
    from awgbot.infra import routing
    from awgbot.domain.services.gwchannel.session import GWLINK_FRESH_HANDSHAKE_S
    other = make_active_client(name="Сосед", tg_id=777002, device_limit=0)
    dev = vps.add_device(other.id, "Pi4")
    vps.db.gateway_add(dev.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    vps.gwlink_session_opened(1, "3.3.0", 2, [])
    vps.gwlink_session_opened(2, "3.3.0", 2, [])
    for slot in (1, 2):
        vps.db.set_state(f"gwlink_seen_{slot}", _ago(days=3))
    ages = {"awglink": 30, "awglink2": GWLINK_FRESH_HANDSHAKE_S + 1}
    monkeypatch.setattr(routing, "link_handshake_age", lambda iface="": ages.get(iface))
    vps.gwlink_seen_touch([1, 2])
    assert vps.db.get_state("gwlink_seen_2") < vps.db.get_state("gwlink_seen_1"), \
        "полуоткрытая сессия (хендшейк протух) время связи не двигает"
    vps.gwlink_session_closed(2)
    vps.db.set_state("gwlink_seen_2", _ago(days=3))
    ages["awglink2"] = 10
    vps.gwlink_seen_touch([2])
    assert vps._gwlink_seen_recent(2, 3600) is False, "без сессии время не трогается — это была бы подделка"


# ── окно ожидания после старта ───────────────────────────────────────────────

def test_the_server_waits_for_gateways_seen_before_the_stop(vps, monkeypatch, make_active_client):
    other = make_active_client(name="Сосед", tg_id=777003, device_limit=0)
    dev = vps.add_device(other.id, "Pi4")
    vps.db.gateway_add(dev.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    vps.gwlink_snapshot_in(1, {"agent_version": "3.2.1"}, 1, True)
    vps.db.set_state("gwlink_seen_1", _ago(minutes=5))           # был на связи перед остановкой
    vps.db.set_state("gwlink_seen_2", _ago(days=UPDATE_PEER_FRESH_DAYS + 1))   # лежит давно — не ждём
    vps.db.set_state(vps._AVAILABLE_KEY, "v3.3.0")
    vps.gwlink_sessions_reset()
    vps.update_hold_start()
    hold = vps.update_hold()
    assert hold["waiting"] == ["Pi5"], hold
    _github(monkeypatch)
    assert vps.update_scan() is None and vps.update_to_notify(None) is None
    assert vps.update_available_tag() == "" and vps.update_waiting() is None, "в окно строки «Доступна» нет"
    assert vps.db.get_state(vps._AVAILABLE_KEY) == "v3.3.0", "отложенное решение тег не трогает"
    assert vps._update_cached_releases(), "список релизов сохранён и в окно"
    _offline(monkeypatch)
    vps.gwlink_session_opened(1, "3.3.1", 2, [])                 # шлюз переподключился обновлённым
    nxt = vps.update_regate()
    assert vps.update_hold() is None and nxt.tag == "v3.3.2"


def test_the_window_closes_by_the_limit_lazily_with_a_guaranteed_regate(vps, monkeypatch):
    """Предел вышел: читатели видят окно закрытым, но не закрывают его сами —
    закрывает пересчёт (такт по update_hold_due), и цель посчитана."""
    _github(monkeypatch)
    vps.gwlink_snapshot_in(1, {"agent_version": "3.3.1"}, 1, True)
    vps.db.set_state("gwlink_seen_1", _ago(minutes=5))
    vps.update_hold_start()
    assert vps.update_scan() is None, "в окно решение отложено"
    assert vps.update_hold() is not None and vps.update_hold_due() is False
    later = timeutil.now() + datetime.timedelta(minutes=UPDATE_HOLD_MINUTES + 1)
    monkeypatch.setattr(timeutil, "now", lambda: later)
    assert vps.update_hold() is None and vps.update_available_tag() == ""
    assert vps.update_hold() is None and vps.update_hold_due() is True, \
        "читатели окно не закрыли — пересчёт всё ещё нужен"
    _offline(monkeypatch)
    assert vps.update_regate().tag == "v3.3.2", "пересчёт по последней известной версии"
    assert vps.update_hold_due() is False, "флаг снял тот, кто пересчитал"


def test_a_window_closed_by_the_button_still_gets_its_regate(vps, monkeypatch):
    """Окно закрыл выбор не пересчётом (кнопка, проверка): флаг due остаётся,
    такт пересчитает и объявит."""
    _github(monkeypatch)
    vps.gwlink_snapshot_in(1, {"agent_version": "3.3.1"}, 1, True)
    vps.db.set_state("gwlink_seen_1", _ago(minutes=5))
    vps.update_hold_start()
    vps.gwlink_session_opened(1, "3.3.1", 2, [])
    assert vps.update_next().tag == "v3.3.2"                     # кнопка: выбор закрыл окно
    assert vps.update_hold_due() is True
    _offline(monkeypatch)
    assert vps.update_regate().tag == "v3.3.2" and vps.update_hold_due() is False


def test_a_gateway_that_came_and_went_after_the_start_is_not_awaited(vps, monkeypatch):
    """Ждём тех, кто после начала окна ещё не выходил на связь, а не «у кого
    сессия прямо сейчас»: переподключившийся и отвалившийся шлюз версию уже
    сказал."""
    vps.db.set_state("gwlink_seen_1", _ago(minutes=5))
    vps.update_hold_start()
    assert vps.update_hold()["waiting"] == ["Pi5"]
    vps.gwlink_session_opened(1, "3.3.1", 2, [])
    vps.gwlink_session_closed(1)
    assert vps.update_hold() is None and vps.update_hold_due() is True


def test_no_window_when_nobody_was_on_line(vps):
    vps.db.set_state("gwlink_seen_1", _ago(days=UPDATE_PEER_FRESH_DAYS + 1))
    vps.update_hold_start()
    assert vps.update_hold() is None


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.3.0")
    monkeypatch.setattr(config, "ROLE", "gateway")
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    yield GatewayServices(d)
    d.close()


def test_the_agent_waits_for_the_first_role(agent, monkeypatch):
    gw = [RELEASES[0], _r("v3.3.1", {"gw": {"requires": "3.3.0", "needs_main": "3.3.0"}})]
    agent.set_link_role(True, server="3.2.1")                     # прежний процесс знал старый сервер
    agent.__dict__.pop("_role_since_start")                       # новый процесс
    agent.update_hold_start()
    assert agent.update_hold()["role"] == "gateway"
    _github(monkeypatch, gw)
    assert agent.update_scan() is None and agent.update_available_tag() == ""
    _offline(monkeypatch)
    agent.set_link_role(True, server="3.3.0")
    assert agent.update_hold() is None and agent.update_regate().tag == "v3.3.1"


def test_a_scan_result_already_replaced_by_a_regate_is_not_announced(vps, monkeypatch):
    """Объявляется только текущая цель: проверка решила v3.3.1, а пересчёт до
    её объявления уже поднял цель — итог проверки не объявляется."""
    _github(monkeypatch)
    _agent_version(vps, "3.3.0")
    found = vps.update_scan()
    assert found.tag == "v3.3.1"
    _offline(monkeypatch)
    _agent_version(vps, "3.3.1")
    assert vps.update_regate(notify=False) is None and vps.update_available_tag() == "v3.3.2"
    assert vps.update_to_notify(found) is None, "устаревший итог проверки"
    assert vps.update_regate().tag == "v3.3.2"


def test_a_network_failure_belongs_to_its_own_check(vps, monkeypatch):
    """Признак сбоя — у вызова, не у экземпляра: сбой чужой проверки в
    другом потоке не выдаёт себя за свой."""
    _github(monkeypatch)
    _agent_version(vps, "3.3.1")
    assert vps.update_check() == (vps.update_check()[0], False)

    def boom(**_):
        raise updates.UpdateError("GitHub не ответил")
    out = {}
    monkeypatch.setattr(updates, "list_releases", boom)
    t = threading.Thread(target=lambda: out.update(r=vps.update_check()))
    t.start()
    t.join(5)
    assert out["r"] == (None, True)
    _github(monkeypatch)
    vps.update_scan_failed = True                                # след чужой проверки на экземпляре
    nxt, failed = vps.update_check()
    assert nxt.tag == "v3.3.2" and failed is False


def test_a_tag_not_newer_than_the_installed_version_is_not_shown(vps, monkeypatch):
    """Тег, записанный до обновления (прежний процесс нашёл v3.3.1, мы на нём
    и стоим), строкой «⬆️ Доступна» не показывается до первой проверки."""
    vps.db.set_state(vps._AVAILABLE_KEY, "v3.3.1")
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.3.1")
    assert vps.update_available_tag() == ""
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.3.0")
    assert vps.update_available_tag() == "v3.3.1"
