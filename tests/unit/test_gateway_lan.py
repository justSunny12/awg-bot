"""
Агент шлюза: локальная сеть без VPN (концепт «локальная сеть» §3.5) — блок панели,
проверки монитора здоровья по счётчикам таблицы awg_home, задача списков.
"""
from __future__ import annotations

import datetime
import json

import pytest

from awgbot.domain.gateway import GatewayServices, GwStatus, GwCheck
from awgbot.infra import gwguard
from awgbot.infra.db import Database
from awgbot.util import timeutil


@pytest.fixture()
def svc(tmp_path):
    db = Database(tmp_path / "gw.db"); db.init_schema()
    return GatewayServices(db)


def _lan_on(monkeypatch, *, home=None, active=True, up=True, status=None, own=(2, 1),
            lists=None, live=("end0", "192.168.68.222")):
    monkeypatch.setattr(gwguard, "lan_mode", lambda: True)
    monkeypatch.setattr(gwguard, "unit_env", lambda k: {"RESOLVER": "10.9.1.1", "HOME_SUBNETS": "192.168.68.0/24"}.get(k, ""))
    monkeypatch.setattr(gwguard, "script_status", lambda: status or {"LAN_IF": "end0", "LAN_ADDR": "192.168.68.222"})
    monkeypatch.setattr(gwguard, "dnsmasq_active", lambda: active)
    monkeypatch.setattr(gwguard, "resolve_via_local", lambda name="github.com": up)
    monkeypatch.setattr(gwguard, "home_table_info", lambda: home)
    monkeypatch.setattr(gwguard, "lists_status", lambda: lists or {"domains": "1180", "nets": "412",
                                                                    "updated_at": "2026-09-20T10:00:00+05:00", "rc": "0"})
    monkeypatch.setattr(gwguard, "lan_own_lists", lambda: own)
    monkeypatch.setattr(gwguard, "iface_for_subnet", lambda net: live)
    monkeypatch.setattr(gwguard, "reassert", lambda: (True, ""))


def _home(lan=100, dns=20, nets=412, resolved=50):
    return {"sets": {"lan_vpn4": resolved, "lan_vpn_nets4": nets, "lan_ru4": 3},
            "chains": {"prerouting", "input", "forward", "postrouting"}, "lan_pkts": lan, "dns_pkts": dns}


def test_lan_off_adds_nothing(svc, monkeypatch):
    monkeypatch.setattr(gwguard, "lan_mode", lambda: False)
    assert svc.lan_status() == ({}, [])


def test_lan_status_block_and_checks_when_healthy(svc, monkeypatch):
    _lan_on(monkeypatch, home=_home())
    info, checks = svc.lan_status()
    assert info["iface"] == "end0" and info["addr"] == "192.168.68.222" and info["resolver"] == "10.9.1.1"
    assert info["domains"] == 1180 and info["nets"] == 412
    assert (info["own_vpn"], info["own_ru"]) == (2, 1)
    by = {c.name: c for c in checks}
    assert by["резолвер"].ok is True and by["апстрим через аплинк"].ok is True
    assert by["таблица локальной сети"].ok is True and by["списки"].ok is True
    assert all(c.group == "lan" for c in checks), "свой стрик, не критичный"
    assert "IPv6 на LAN" not in by, "IPv6 у клиентов режет резолвер (filter-AAAA), не sysctl на малине"
    assert by["трафик с роутера"].ok is None and by["DNS с роутера"].ok is None, "первый замер — не с чем сравнить"


def test_router_redirect_is_judged_by_counter_growth(svc, monkeypatch):
    """Счётчик из LAN наружу растёт — роутер заворачивает; не растёт дольше
    суток — нет, с временем последнего пакета и отсылкой к рецепту."""
    _lan_on(monkeypatch, home=_home(lan=100, dns=5))
    svc.lan_status()                                            # первый замер
    _lan_on(monkeypatch, home=_home(lan=150, dns=5))
    _, checks = svc.lan_status()
    by = {c.name: c for c in checks}
    assert by["трафик с роутера"].ok is True
    assert by["DNS с роутера"].ok is True, "не вырос, но тишина недавняя"
    # состарить последний рост DNS
    last = json.loads(svc.db.get_state(svc._LAN_LAST_KEY))
    last["dns_at"] = timeutil.to_iso(timeutil.now() - datetime.timedelta(hours=25))
    svc.db.set_state(svc._LAN_LAST_KEY, json.dumps(last))
    _lan_on(monkeypatch, home=_home(lan=151, dns=5))
    _, checks = svc.lan_status()
    by = {c.name: c for c in checks}
    assert by["DNS с роутера"].ok is False and "DHCP роутера раздаёт не адрес шлюза" in by["DNS с роутера"].detail
    assert by["трафик с роутера"].ok is True
    # сброс счётчика (реассерт таблицы) — не тревога, а новая точка отсчёта
    _lan_on(monkeypatch, home=_home(lan=3, dns=5))
    _, checks = svc.lan_status()
    assert {c.name: c for c in checks}["трафик с роутера"].ok is True


def test_missing_table_is_reasserted_not_reissued(svc, monkeypatch):
    """Таблицу снёс кто-то посторонний — вернёт юнит обвязки (тот же троттлинг,
    что у guard), а не перевыпуск конфигурации на ВПС."""
    calls = []
    _lan_on(monkeypatch, home=None, active=False, up=None)
    monkeypatch.setattr(gwguard, "reassert", lambda: calls.append(1) or (True, ""))
    svc._last_reassert = 0.0
    _, checks = svc.lan_status()
    by = {c.name: c for c in checks}
    assert by["таблица локальной сети"].ok is False and "перевыставляю" in by["таблица локальной сети"].detail
    assert calls == [1]
    assert by["резолвер"].ok is False and "journalctl -u dnsmasq" in by["резолвер"].detail
    assert by["апстрим через аплинк"].ok is None and "не запущен" in by["апстрим через аплинк"].detail
    assert by["списки"].ok is False
    assert "трафик с роутера" not in by, "без таблицы счётчиков нет"
    _, checks = svc.lan_status()                              # второй такт — троттлинг
    assert calls == [1] and "в ближайший такт" in {c.name: c for c in checks}["таблица локальной сети"].detail


def test_script_error_is_shown_instead_of_a_reissue_advice(svc, monkeypatch):
    _lan_on(monkeypatch, home=None, status={"LAN_IF": "", "LAN_ADDR": "", "LAN_ERROR": "порт 53 занят: pihole-FTL"})
    calls = []
    monkeypatch.setattr(gwguard, "reassert", lambda: calls.append(1) or (True, ""))
    _, checks = svc.lan_status()
    by = {c.name: c for c in checks}
    assert by["применение локальной сети"].ok is False and "pihole-FTL" in by["применение локальной сети"].detail
    assert calls == [], "скрипт сам сказал, что не так — реассерт не поможет"


def test_moved_lan_interface_triggers_a_reassert(svc, monkeypatch):
    """OMV собрал bridge: адрес подсети теперь на br0, правила стоят на end0 —
    весь LAN шёл бы мимо маркировки, пока юнит не перевыставит обвязку."""
    calls = []
    _lan_on(monkeypatch, home=_home(), live=("br0", "192.168.68.222"))
    monkeypatch.setattr(gwguard, "reassert", lambda: calls.append(1) or (True, ""))
    svc._last_reassert = 0.0
    _, checks = svc.lan_status()
    by = {c.name: c for c in checks}
    assert calls == [1] and "br0" in by["применение локальной сети"].detail


def test_status_carries_lan_block_and_panel_shows_it(svc, monkeypatch):
    from awgbot.bot import texts
    st = GwStatus(link_up=True, handshake_age=5.0, checks=[GwCheck("резолвер", True)],
                  lan={"iface": "end0", "addr": "192.168.68.222", "resolver": "10.9.1.1", "domains": 1180,
                       "nets": 412, "updated_at": "2026-09-20T10:00:00+05:00",
                       "own_vpn": 2, "own_ru": 1, "lan_pkts": 12345})
    out = texts.gateway_panel(st)
    assert "🏠 За шлюзом — без VPN: 🟢 работает" in out
    assert "локальная сеть: end0, 192.168.68.222" in out and "12 345 пакетов" in out
    assert "1180 доменов, 412 подсетей" in out and "свои: 2 в туннель, 1 напрямую" in out
    st.checks.append(GwCheck("трафик с роутера", False, "пакетов нет", group="lan"))
    assert "🏠 За шлюзом — без VPN: 🔴 трафик с роутера" in texts.gateway_panel(st)
    assert "За шлюзом — без VPN" not in texts.gateway_panel(GwStatus()), "выключено — блока нет"
    # снимок переживает JSON
    assert GwStatus.from_json(st.to_json()).lan["addr"] == "192.168.68.222"


def test_lists_job_warns_after_two_failures_only(svc, monkeypatch):
    monkeypatch.setattr(gwguard, "lan_mode", lambda: True)
    monkeypatch.setattr(gwguard, "run_lan_lists", lambda timeout=600: (False, "фид не скачался"))
    assert svc.lan_lists_update() == []
    notes = svc.lan_lists_update()
    assert len(notes) == 1 and "дважды подряд" in notes[0].text and not notes[0].critical
    assert svc.lan_lists_update() == [], "третий провал — молчим, не спамим"
    monkeypatch.setattr(gwguard, "run_lan_lists", lambda timeout=600: (True, ""))
    assert svc.lan_lists_now() == (True, "") and svc.db.get_state(svc._LAN_FAILS_KEY) == "0", \
        "ручной успех сбрасывает счётчик провалов"
    assert svc.lan_lists_update() == []
    monkeypatch.setattr(gwguard, "lan_mode", lambda: False)
    monkeypatch.setattr(gwguard, "run_lan_lists", lambda timeout=600: (_ for _ in ()).throw(AssertionError("не звать")))
    assert svc.lan_lists_update() == []


def test_home_table_info_parses_counters_and_sets(monkeypatch):
    doc = {"nftables": [
        {"set": {"name": "lan_vpn4", "elem": ["1.1.1.1", {"prefix": {"addr": "8.8.8.0", "len": 24}}]}},
        {"set": {"name": "lan_vpn_nets4", "elem": []}},
        {"chain": {"name": "prerouting"}}, {"chain": {"name": "input"}},
        {"rule": {"chain": "prerouting", "expr": [{"match": {}}, {"accept": None}]}},
        {"rule": {"chain": "prerouting", "expr": [{"match": {}}, {"counter": {"packets": 777, "bytes": 1}}]}},
        {"rule": {"chain": "prerouting", "expr": [{"counter": {"packets": 5, "bytes": 1}}]}},
        {"rule": {"chain": "input", "expr": [{"counter": {"packets": 42, "bytes": 1}}]}},
    ]}
    import subprocess
    monkeypatch.setattr(gwguard, "_nft", lambda args, timeout=10, check=True:
                        subprocess.CompletedProcess([], 0, stdout=json.dumps(doc).encode(), stderr=b""))
    info = gwguard.home_table_info()
    assert set(info["sets"]) == {"lan_vpn4", "lan_vpn_nets4"}
    assert info["lan_pkts"] == 777 and info["dns_pkts"] == 42, "первый счётчик цепочки, не последний"
    monkeypatch.setattr(gwguard, "_nft", lambda args, timeout=10, check=True:
                        subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"no such table"))
    assert gwguard.home_table_info() is None


def test_unit_env_reads_quoted_and_bare_values(tmp_path, monkeypatch):
    unit = tmp_path / "awg-link-gw.service"
    unit.write_text('[Service]\nEnvironment=LAN_MODE=1\nEnvironment="HOME_SUBNETS=192.168.68.0/24 10.0.0.0/24"\n'
                    "Environment=RESOLVER=10.9.1.1\n", encoding="utf-8")
    monkeypatch.setattr(gwguard.config, "GW_UNIT", unit.name)
    real = gwguard.Path
    monkeypatch.setattr(gwguard, "Path", lambda p: real(str(p).replace("/etc/systemd/system", str(tmp_path))))
    assert gwguard.unit_env("LAN_MODE") == "1" and gwguard.lan_mode()
    assert gwguard.unit_env("HOME_SUBNETS") == "192.168.68.0/24 10.0.0.0/24"
    assert gwguard.unit_env("RESOLVER") == "10.9.1.1" and gwguard.unit_env("NOPE") == ""
