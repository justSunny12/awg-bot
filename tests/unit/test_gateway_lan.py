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
    # апстрим — по статистике dnsmasq: отвечает / все запросы без ответа / dig нет
    monkeypatch.setattr(gwguard, "upstream_stats",
                        lambda: None if up is None else {"10.9.1.1#53": (10, 0 if up else 10)})
    monkeypatch.setattr(gwguard, "home_table_info", lambda: home)
    monkeypatch.setattr(gwguard, "lists_status", lambda: lists or {"domains": "1180", "nets": "412",
                                                                    "updated_at": "2026-09-20T10:00:00+05:00", "rc": "0"})
    monkeypatch.setattr(gwguard, "lan_own_lists", lambda: own)
    monkeypatch.setattr(gwguard, "iface_for_subnet", lambda net: live)
    monkeypatch.setattr(gwguard, "reassert", lambda: (True, ""))
    # реассерт смотрит, не запущен ли юнит уже, — без настоящего systemctl
    monkeypatch.setattr(gwguard, "unit_state", lambda: {"ActiveState": "active"})


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
    assert "🏠 Локальная сеть без VPN: 🟢 работает" in out
    assert "сеть: end0, <code>192.168.68.222</code>" in out and "12 345 пакетов" in out
    assert "локальная сеть: end0" not in out, "подпись строки интерфейса — «сеть:» (вычитка 3.1.0)"
    assert "DNS — <code>10.9.1.1</code> через " in out and "апстрим" not in out, out
    # списки — своей группой после пустой строки, дата обновления — в скобках
    assert "\n\n📋 Списки: 1180 доменов, 412 подсетей (обн. " in out, out
    assert "Свои списки: 2 в туннель, 1 напрямую" in out, out
    assert "🗂" not in out, "svc не активен — строки SMB в панели быть не должно"
    st.checks.append(GwCheck("трафик с роутера", False, "пакетов нет", group="lan"))
    assert "🏠 Локальная сеть без VPN: 🔴 трафик с роутера" in texts.gateway_panel(st)
    assert "Локальная сеть без VPN" not in texts.gateway_panel(GwStatus()), "выключено — блока нет"
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


# ── апстрим по статистике dnsmasq, без запроса наружу ───────────────────────

class _Stats:
    """dnsmasq на малине: «отправлено / отказов» по апстримам; None — dig нет."""

    def __init__(self, monkeypatch, sent=0, failed=0):
        self.value: dict | None = {"10.9.1.1#53": (sent, failed)}
        monkeypatch.setattr(gwguard, "upstream_stats", lambda: self.value)
        monkeypatch.setattr(gwguard, "resolve_via_local", lambda name="github.com":
                            pytest.fail("апстрим проверен живым запросом наружу"))

    def set(self, sent, failed):
        self.value = {"10.9.1.1#53": (sent, failed)}


def test_the_first_look_judges_by_what_dnsmasq_accumulated(svc, monkeypatch):
    """Первый взгляд после старта агента — по накопленному с запуска dnsmasq:
    на все отправленные ни одного ответа — апстрим молчит."""
    st = _Stats(monkeypatch, sent=40, failed=3)
    assert svc._upstream_verdict() is True
    svc2 = GatewayServices(svc.db)
    st.set(40, 40)
    assert svc2._upstream_verdict() is False, "ни одного ответа на 40 запросов — это отказ"


def test_a_fresh_dnsmasq_that_sent_nothing_yet_is_not_an_unreadable_statistic(svc, monkeypatch):
    """dnsmasq только что запустился, квартира спит — отправок ноль. Сказать
    «статистика не прочиталась» (серый вердикт) здесь неправда: прочиталась, и
    отказов в ней нет. Прошлого вердикта нет — значит считаем, что всё хорошо,
    как и в простое после первого взгляда."""
    _Stats(monkeypatch, sent=0, failed=0)
    assert svc._upstream_verdict() is True


def test_failures_growing_without_sends_are_a_silent_upstream(svc, monkeypatch):
    """Аплинк лёг: dnsmasq пробует, отказы растут, успешных отправок нет."""
    st = _Stats(monkeypatch, sent=100, failed=2)
    assert svc._upstream_verdict() is True
    st.set(100, 9)
    assert svc._upstream_verdict() is False
    st.set(130, 9)                                  # ожил: отправки пошли, отказов не прибавилось
    assert svc._upstream_verdict() is True


def test_an_idle_tick_keeps_the_previous_verdict(svc, monkeypatch):
    """Ночью запросов нет вовсе: счётчики стоят. Спрашивать наружу ради
    проверки значит завести маячок — держим прошлый вердикт, в обе стороны."""
    st = _Stats(monkeypatch, sent=100, failed=2)
    svc._upstream_verdict()
    st.set(100, 9)
    assert svc._upstream_verdict() is False
    for _ in range(3):
        assert svc._upstream_verdict() is False, "простой перевернул отказ в «работает»"
    st.set(150, 9)
    assert svc._upstream_verdict() is True
    for _ in range(3):
        assert svc._upstream_verdict() is True


def test_a_dnsmasq_restart_starts_the_count_over(svc, monkeypatch):
    """Счётчики dnsmasq сбрасываются рестартом (новые фиды — рестарт). Разница
    «ушла вниз» — не отказ: судим по накопленному с нового старта."""
    st = _Stats(monkeypatch, sent=500, failed=10)
    svc._upstream_verdict()
    st.set(20, 0)
    assert svc._upstream_verdict() is True
    st.set(3, 3)
    svc3 = GatewayServices(svc.db)
    svc3._upstream_verdict()
    st.set(0, 0)                                    # рестарт, отправок ещё нет
    assert svc3._upstream_verdict() is False, "рестарт без отправок перевернул прошлый отказ"


def test_no_statistic_is_the_third_state(svc, monkeypatch):
    """dig нет или dnsmasq не ответил — «нечем проверить», а не «работает»."""
    st = _Stats(monkeypatch)
    st.value = None
    assert svc._upstream_verdict() is None
    st.value = {}
    assert svc._upstream_verdict() is None


def test_the_panel_says_what_is_wrong_with_the_upstream(svc, monkeypatch):
    _lan_on(monkeypatch, home=_home())
    st = _Stats(monkeypatch, sent=10, failed=10)
    by = {c.name: c for c in svc.lan_status()[1]}
    assert by["апстрим через аплинк"].ok is False
    assert "10.9.1.1 не отвечает через аплинк" in by["апстрим через аплинк"].detail
    st.value = None
    by = {c.name: c for c in GatewayServices(svc.db).lan_status()[1]}
    assert by["апстрим через аплинк"].ok is None
    assert by["апстрим через аплинк"].detail == "статистика dnsmasq не прочиталась"


def test_missing_lists_script_is_a_failed_check_with_a_reissue_advice(svc, monkeypatch, tmp_path):
    """Скрипта списков нет (конфигурация старого образца, скрипт стёрли) —
    применить фиды нечем, ни свои, ни привезённые каналом. Монитор обязан
    сказать это прямо, а не показывать «списки не загружены» без причины."""
    _lan_on(monkeypatch, home=_home())
    monkeypatch.setattr(gwguard, "LAN_LISTS_SCRIPT", str(tmp_path / "нет-такого.sh"))
    _, checks = svc.lan_status()
    by = {c.name: c for c in checks}
    assert "скрипт списков" in by, [c.name for c in checks]
    assert by["скрипт списков"].ok is False and "перевыпусти конфигурацию шлюза" in by["скрипт списков"].detail
    assert by["скрипт списков"].group == "lan", "своим стриком, не критичным"


def test_present_lists_script_adds_no_check(svc, monkeypatch, tmp_path):
    """Скрипт на месте — строки о нём нет вовсе: зелёная «скрипт списков: ок»
    в мониторе была бы шумом."""
    script = tmp_path / "awg-lan-lists.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    _lan_on(monkeypatch, home=_home())
    monkeypatch.setattr(gwguard, "LAN_LISTS_SCRIPT", str(script))
    _, checks = svc.lan_status()
    assert "скрипт списков" not in {c.name for c in checks}


def test_lan_block_carries_the_first_local_subnet_for_the_recovery_note(svc, monkeypatch):
    """Подсеть в блоке — для отбоя «Локальная сеть без VPN (хост, подсеть)
    снова в порядке»: у двух шлюзов отбои иначе не различить. Подсетей в
    юните нет — пустая строка, а не падение."""
    _lan_on(monkeypatch, home=_home())
    info, _ = svc.lan_status()
    assert info["subnet"] == "192.168.68.0/24"
    monkeypatch.setattr(gwguard, "unit_env", lambda k: {"RESOLVER": "10.9.1.1"}.get(k, ""))
    info, _ = svc.lan_status()
    assert info["subnet"] == ""


@pytest.mark.parametrize("status,shown", [
    ({"LAN_IF": "end0", "LAN_ADDR": "192.168.68.222", "UPLINK_IF": "wlan0"}, "wlan0"),
    ({"LAN_IF": "end0", "LAN_ADDR": "192.168.68.222"}, "аплинк"),
    ({"LAN_IF": "end0", "LAN_ADDR": "192.168.68.222", "UPLINK_IF": "<br&0>"}, "&lt;br&amp;0&gt;"),
], ids=["named", "unknown", "escaped"])
def test_the_lan_screen_names_the_uplink_the_script_reported(svc, monkeypatch, status, shown):
    """Строка DNS на экране локальной сети — через какой интерфейс идёт
    апстрим, по UPLINK_IF из статуса скрипта: с двумя интерфейсами на малине
    «через аплинк» не говорит, куда смотреть. Скрипт не назвал — слово
    «аплинк»; имя экранировано (статус пишет скрипт, не бот)."""
    from awgbot.bot import texts
    _lan_on(monkeypatch, home=_home(), status=status)
    info, _ = svc.lan_status()
    assert info["uplink"] == status.get("UPLINK_IF", ""), info
    out = texts.gateway_lan_text(GwStatus(link_up=True, lan=info))
    assert f"\nDNS — <code>10.9.1.1</code> через {shown}\n" in out, out


# ── сервисы соседних сетей: проверки группы «svc» (концепт «сервисы соседних сетей» §4.2, §7.1) ──

def _svc_on(monkeypatch, *, avahi=True, dig=("naspi5._smb._tcp.awg.internal.",), browse=True):
    import shutil
    env = {"LAN_MODE": "1", "HOME_SUBNETS": "192.168.68.0/24", "PEER_HOME_NETS": "192.168.1.0/24",
           "LINK_CHANNEL": "1", "RESOLVER": "10.9.1.1"}
    monkeypatch.setattr(gwguard, "lan_mode", lambda: True)
    monkeypatch.setattr(gwguard, "unit_env", lambda k: env.get(k, ""))
    monkeypatch.setattr(gwguard, "avahi_active", lambda: avahi)
    monkeypatch.setattr(gwguard, "dns_local", lambda name, qtype="PTR": None if dig is None else list(dig))
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: ("/usr/bin/avahi-browse" if browse else None)
                        if n == "avahi-browse" else real(n, *a, **k))
    return env


def _peer_applied(svc, n=2, err=""):
    items = [{"t": "_smb._tcp", "n": f"nas{i}", "h": f"nas{i}", "p": 445, "a": f"192.168.1.{i + 1}"}
             for i in range(n)]
    svc.db.set_state("gw_peer_svc", json.dumps({"hash": "ab" * 32, "items": items}))
    svc.db.set_state("gw_peer_svc_err", err)


def test_services_checks_say_what_the_monitor_should(svc, monkeypatch):
    _svc_on(monkeypatch)
    _peer_applied(svc)
    info, checks = svc.services_status()
    by = {c.name: c for c in checks}
    assert all(c.group == "svc" for c in checks), "своя группа — без уведомлений"
    assert by["SMB подсетей других шлюзов"].ok is True and by["SMB подсетей других шлюзов"].detail == "2 SMB доступны"
    assert by["SMB этой подсети"].ok is True and by["SMB этой подсети"].detail == "0 SMB"
    assert info["active"] and info["peer"] == ["nas0", "nas1"]
    # резолвер не отдаёт — 🔴 с подсказкой; dig не ответил — ⚪, а не «всё хорошо»
    _svc_on(monkeypatch, dig=())
    c = {c.name: c for c in svc.services_status()[1]}["SMB подсетей других шлюзов"]
    assert c.ok is False and c.detail == "резолвер не отдаёт записи: journalctl -u dnsmasq -e", c.detail
    _svc_on(monkeypatch, dig=None)
    c = {c.name: c for c in svc.services_status()[1]}["SMB подсетей других шлюзов"]
    assert c.ok is None and c.detail == "не проверено: dig не ответил", c.detail
    # ошибка применения — 🔴 с её хвостом
    _peer_applied(svc, err="dnsmasq отверг записи соседей")
    c = {c.name: c for c in svc.services_status()[1]}["SMB подсетей других шлюзов"]
    assert c.ok is False and "записи не применились: dnsmasq отверг" in c.detail


def test_no_avahi_is_grey_not_red(svc, monkeypatch):
    """Малина без NAS и без avahi — нормальное состояние (§12.3): ⚪, не 🔴."""
    _svc_on(monkeypatch, avahi=False)
    c = {c.name: c for c in svc.services_status()[1]}["SMB этой подсети"]
    assert c.ok is None and c.detail == ("avahi-daemon не запущен: SMB-серверы этой подсети не видны "
                                         "из подсетей других шлюзов"), c.detail
    _svc_on(monkeypatch, browse=False)
    c = {c.name: c for c in svc.services_status()[1]}["SMB этой подсети"]
    assert c.ok is None and c.detail == "нет avahi-browse, пакет avahi-utils (🔧 Мастер восстановления)", c.detail


def test_services_add_nothing_where_the_function_does_not_work(svc, monkeypatch):
    """Без соседей в юните функции нет: ни блока, ни проверок, ни обзора."""
    env = _svc_on(monkeypatch)
    env["PEER_HOME_NETS"] = ""
    monkeypatch.setattr(gwguard, "dns_local", lambda *a, **k: pytest.fail("dig без функции"))
    assert svc.services_status() == ({"active": False}, [])


def test_a_red_services_check_sends_neither_plumbing_nor_lan_alerts(svc, monkeypatch):
    """Соседи, которых нет, — не авария (§4.2): 🔴 группы «svc» видна в
    мониторе, но не поднимает ни «Обвязка шлюза неисправна», ни «Локальная
    сеть без VPN», сколько бы тиков ни держалась."""
    from awgbot.bot import texts
    from tests.unit.test_gateway import _quiet_status
    _svc_on(monkeypatch, dig=())
    _peer_applied(svc)
    _, checks = svc.services_status()
    red = [c for c in checks if c.ok is False]
    assert red and red[0].group == "svc"
    st = _quiet_status(checks=[GwCheck("MASQUERADE", True)] + checks,
                       lan={"subnet": "192.168.68.0/24", "svc": {"active": True}})
    monkeypatch.setattr(svc, "status", lambda: st)
    monkeypatch.setattr(svc, "uplink_policy_heal", lambda: [])
    notes = []
    for _ in range(6):
        notes += svc.monitor_tick()
    assert notes == [], f"проверка сервисов соседей подняла уведомление: {[n.text for n in notes]}"
    assert "🔴 SMB подсетей других шлюзов" in texts.gateway_health(st), "в мониторе проверку видно"


# ── свои списки: проверка группы «own» (концепт «синхронизация своих списков» §7.1) ──

def test_a_red_own_lists_check_sends_neither_plumbing_nor_lan_alerts(svc, monkeypatch):
    """Свои списки не применились (dnsmasq отверг канон) — 🔴 в мониторе, но
    не авария: доступ квартиры держат прежние файлы. Ни «Обвязка шлюза
    неисправна», ни «Локальная сеть без VPN» — сколько бы тиков ни держалось."""
    from awgbot.bot import texts
    from tests.unit.test_gateway import _quiet_status
    env = {"LAN_MODE": "1", "LINK_CHANNEL": "1"}
    monkeypatch.setattr(gwguard, "lan_mode", lambda: True)
    monkeypatch.setattr(gwguard, "unit_env", lambda k: env.get(k, ""))
    monkeypatch.setattr(gwguard, "lan_domain_has_sync", lambda: True)
    svc.db.set_state("gw_own_err", "dnsmasq --test отверг списки — откатываю")
    info, checks = svc.own_status()
    red = [c for c in checks if c.ok is False]
    assert red and red[0].group == "own" and info["state"] == "failed", checks
    st = _quiet_status(checks=[GwCheck("MASQUERADE", True)] + checks,
                       lan={"subnet": "192.168.68.0/24", "own": info})
    monkeypatch.setattr(svc, "status", lambda: st)
    monkeypatch.setattr(svc, "uplink_policy_heal", lambda: [])
    notes = []
    for _ in range(6):
        notes += svc.monitor_tick()
    assert notes == [], f"проверка своих списков подняла уведомление: {[n.text for n in notes]}"
    assert "🔴 свои списки — не применились: dnsmasq --test отверг" in texts.gateway_health(st), \
        texts.gateway_health(st)
