"""Проверка обновлений основного бота после набора: цель — выпуск набора
(новейший для любой роли), признак сбоя сети — у своего вызова, тег не новее
выпуска набора строкой «⬆️ Доступна» не показывается; «последний раз на
связи» — правда и для живой сессии.

Окна ожидания после старта и пересчёта цели по версиям шлюзов больше нет:
порядок набора — по hello шлюзов (правило «ждать факта»).
"""
from __future__ import annotations

import datetime
import threading

import pytest

from awgbot.core import config
from awgbot.infra import awglock, updates
from awgbot.util import timeutil

pytestmark = pytest.mark.integration


def _r(tag: str, roles: dict) -> updates.Release:
    meta = {"schema": 1, "version": tag.lstrip("v"), "awg_gen": 1, "roles": roles}
    return updates.Release(tag=tag, version=updates.parse_version(tag), body="- пункт",
                           asset_url=f"https://api/assets/{tag}", sha256="a" * 64,
                           meta=updates.meta.check(meta, tag))


BOTH = {"main": {"requires": "3.4.0"}, "gw": {"requires": "3.4.0"}}
RELEASES = [_r("v3.4.0", BOTH), _r("v3.4.1", {"main": {"requires": "3.4.0"}}),
            _r("v3.4.2", {"gw": {"requires": "3.4.0"}})]


def _ago(**kw) -> str:
    return timeutil.to_iso(timeutil.now() - datetime.timedelta(**kw))


@pytest.fixture()
def vps(services, make_active_client, monkeypatch):
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.4.0")
    monkeypatch.setattr(config, "ROLE", "client")
    monkeypatch.setattr(awglock, "generation", lambda: 1)
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID, device_limit=0)
    pi = services.add_device(admin.id, "Pi5")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    return services


def test_the_target_is_the_newest_release_for_any_role(vps, monkeypatch):
    """v3.4.2 — только для шлюзов: кнопка сервера её зажигает (раньше основной
    бот искал только свою цель, и выпуск для шлюзов молчал)."""
    monkeypatch.setattr(updates, "list_releases", lambda **_: list(RELEASES))
    nxt = vps.update_scan()
    assert nxt.tag == "v3.4.2" and vps.update_available_tag() == "v3.4.2"


def test_a_network_failure_belongs_to_its_own_check(vps, monkeypatch):
    """Признак сбоя — у вызова, не у экземпляра: сбой чужой проверки в
    другом потоке не выдаёт себя за свой."""
    monkeypatch.setattr(updates, "list_releases", lambda **_: list(RELEASES))
    assert vps.update_check()[1] is False

    def boom(**_):
        raise updates.UpdateError("GitHub не ответил")
    out = {}
    monkeypatch.setattr(updates, "list_releases", boom)
    t = threading.Thread(target=lambda: out.update(r=vps.update_check()))
    t.start()
    t.join(5)
    assert out["r"] == (None, True)
    monkeypatch.setattr(updates, "list_releases", lambda **_: list(RELEASES))
    vps.update_scan_failed = True                                # след чужой проверки на экземпляре
    nxt, failed = vps.update_check()
    assert nxt.tag == "v3.4.2" and failed is False


def test_a_tag_not_newer_than_the_fleet_release_is_not_shown(vps, monkeypatch):
    """Тег, записанный до обновления, строкой «⬆️ Доступна» не показывается;
    после хода к выпуску только для шлюзов выпуск набора — его тег, и тот же
    тег уже не «доступен»."""
    monkeypatch.setattr(updates, "list_releases", lambda **_: list(RELEASES))
    vps.update_scan()
    vps.db.set_state(vps._AVAILABLE_KEY, "v3.4.0")
    assert vps.update_available_tag() == ""
    vps.db.set_state(vps._AVAILABLE_KEY, "v3.4.2")
    assert vps.update_available_tag() == "v3.4.2"
    vps.fleet_run_set({"id": "a1b2c3d4", "tag": "v3.4.2", "state": "done", "steps": []})
    vps.fleet_release_set("v3.4.2")                    # пишет фиксация хода
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.4.1")
    assert vps.fleet_release() == "v3.4.2" and vps.update_available_tag() == ""


def test_seen_is_touched_only_for_live_sessions_with_a_fresh_handshake(vps, make_active_client, monkeypatch):
    from awgbot.infra import routing
    from awgbot.domain.services.gwchannel.session import GWLINK_FRESH_HANDSHAKE_S
    other = make_active_client(name="Сосед", tg_id=777002, device_limit=0)
    dev = vps.add_device(other.id, "Pi4")
    vps.db.gateway_add(dev.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    vps.gwlink_session_opened(1, "3.4.0", 2, [])
    vps.gwlink_session_opened(2, "3.4.0", 2, [])
    for slot in (1, 2):
        vps.db.set_state(f"gwlink_seen_{slot}", _ago(days=3))
    ages = {"awglink": 30, "awglink2": GWLINK_FRESH_HANDSHAKE_S + 1}
    # подмена — в части пакета: на самом пакете откат оставил бы имя в его
    # globals, и оно заслонило бы подмены probes в следующих тестах процесса
    monkeypatch.setattr(routing.probes, "link_handshake_age", lambda iface="": ages.get(iface))
    vps.gwlink_seen_touch([1, 2])
    assert vps.db.get_state("gwlink_seen_2") < vps.db.get_state("gwlink_seen_1"), \
        "полуоткрытая сессия (хендшейк протух) время связи не двигает"
    vps.gwlink_session_closed(2)
    vps.db.set_state("gwlink_seen_2", _ago(days=3))
    ages["awglink2"] = 10
    vps.gwlink_seen_touch([2])
    assert vps._gwlink_seen_recent(2, 3600) is False, "без сессии время не трогается — это была бы подделка"
