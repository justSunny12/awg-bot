"""
Агент шлюза: локальная сеть без VPN — блок панели,
проверки монитора здоровья по счётчикам таблицы awg_home, задача списков.
"""
from __future__ import annotations

from awgbot.domain import gwchecks as gc

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
    assert "IPv6 на LAN" not in by, "IPv6 у клиентов режет резолвер (filter-AAAA), не sysctl на шлюзе"
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
    svc._last_reassert = float("-inf")   # «давно»: monotonic после перезагрузки меньше интервала
    _, checks = svc.lan_status()
    by = {c.name: c for c in checks}
    assert by[gc.CHECK_LAN_TABLE].ok is False and gc.SELF_HEAL_TAIL in by[gc.CHECK_LAN_TABLE].detail
    assert calls == [1]
    assert by["резолвер"].ok is False and "journalctl -u dnsmasq" in by["резолвер"].detail
    assert by["апстрим через аплинк"].ok is None and "не запущен" in by["апстрим через аплинк"].detail
    assert by["списки"].ok is False
    assert "трафик с роутера" not in by, "без таблицы счётчиков нет"
    _, checks = svc.lan_status()                              # второй такт — троттлинг
    assert calls == [1] and gc.SELF_HEAL_TAIL in {c.name: c for c in checks}[gc.CHECK_LAN_TABLE].detail


def test_script_error_is_shown_instead_of_a_reissue_advice(svc, monkeypatch):
    _lan_on(monkeypatch, home=None, status={"LAN_IF": "", "LAN_ADDR": "", "LAN_ERROR": "порт 53 занят: pihole-FTL"})
    calls = []
    monkeypatch.setattr(gwguard, "reassert", lambda: calls.append(1) or (True, ""))
    _, checks = svc.lan_status()
    by = {c.name: c for c in checks}
    assert by[gc.CHECK_TRANSIT].ok is False and "pihole-FTL" in by[gc.CHECK_TRANSIT].detail
    assert calls == [], "скрипт сам сказал, что не так — реассерт не поможет"


def test_moved_lan_interface_triggers_a_reassert(svc, monkeypatch):
    """OMV собрал bridge: адрес подсети теперь на br0, правила стоят на end0 —
    весь LAN шёл бы мимо маркировки, пока юнит не перевыставит обвязку."""
    calls = []
    _lan_on(monkeypatch, home=_home(), live=("br0", "192.168.68.222"))
    monkeypatch.setattr(gwguard, "reassert", lambda: calls.append(1) or (True, ""))
    svc._last_reassert = float("-inf")   # «давно»: monotonic после перезагрузки меньше интервала
    _, checks = svc.lan_status()
    by = {c.name: c for c in checks}
    assert calls == [1] and "br0" in by[gc.CHECK_TRANSIT].detail


def test_status_carries_lan_block_and_panel_shows_it(svc, monkeypatch):
    from awgbot.bot import texts
    st = GwStatus(link_up=True, handshake_age=5.0, checks=[GwCheck("резолвер", True)],
                  lan={"iface": "end0", "addr": "192.168.68.222", "resolver": "10.9.1.1", "domains": 1180,
                       "nets": 412, "updated_at": "2026-09-20T10:00:00+05:00",
                       "own_vpn": 2, "own_ru": 1, "lan_pkts": 12345})
    lines = texts.gateway_panel(st).splitlines()
    # VPN-транзит — строкой состояния с трафиком, списки — одной строкой со
    # своими; числа с разрядами; время обновления, адрес и DNS — на экране
    # «🔀 VPN-транзит», не на панели
    assert "🔀 VPN-транзит 🟢 · 12 345 пакетов с роутера" in lines, lines
    assert "📋 Списки: 1 180 доменов, 412 подсетей · свои: 2 в туннель, 1 напрямую" in lines, lines
    assert not any("192.168.68.222" in ln or ln.startswith("DNS") or "обн." in ln for ln in lines), lines
    assert not any(ln.startswith("🗂") for ln in lines), "svc не активен — строки SMB в панели быть не должно"
    st.checks.append(GwCheck("трафик с роутера", False, "пакетов нет", group="lan"))
    lines = texts.gateway_panel(st).splitlines()
    assert "🔀 VPN-транзит 🔴 трафик с роутера · 12 345 пакетов с роутера" in lines, lines
    # снимок переживает JSON
    assert GwStatus.from_json(st.to_json()).lan["addr"] == "192.168.68.222"


def test_without_vpn_transit_the_panel_has_no_transit_lists_or_smb_lines():
    """VPN-транзит выключен — ни «🔀», ни «📋», ни «🗂», и кнопки нет: про
    функцию, которой на шлюзе нет, панель не говорит."""
    from awgbot.bot import keyboards as kb, texts
    st = GwStatus(link_up=True, handshake_age=5.0)
    lines = texts.gateway_panel(st).splitlines()
    assert not any(ln.startswith(("🔀", "📋", "🗂")) for ln in lines), lines
    labels = [b.text for row in kb.gateway_panel_kb(bool(st.lan)).inline_keyboard for b in row]
    assert "🔀 VPN-транзит" not in labels and len(labels) == 3, labels


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
    """dnsmasq на шлюзе: «отправлено / отказов» по апстримам; None — dig нет."""

    def __init__(self, monkeypatch, sent=0, failed=0):
        self.value: dict | None = {"10.9.1.1#53": (sent, failed)}
        monkeypatch.setattr(gwguard, "upstream_stats", lambda: self.value)

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
    """dnsmasq только что запустился, локальная сеть спит — отправок ноль. Сказать
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
    апстрим, по UPLINK_IF из статуса скрипта: с двумя интерфейсами на шлюзе
    «через аплинк» не говорит, куда смотреть. Скрипт не назвал — слово
    «аплинк»; имя экранировано (статус пишет скрипт, не бот)."""
    from awgbot.bot import texts
    _lan_on(monkeypatch, home=_home(), status=status)
    info, _ = svc.lan_status()
    assert info["uplink"] == status.get("UPLINK_IF", ""), info
    out = texts.gateway_transit_text(GwStatus(link_up=True, lan=info))
    shown = f"<code>{shown}</code>" if status.get("UPLINK_IF") else shown     # имя интерфейса моноширинным, подпись — нет
    assert f"DNS — <code>10.9.1.1</code> через {shown}" in out.splitlines(), out


# ── сервисы соседних сетей: проверки группы «svc» ──

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


def test_no_avahi_is_off_but_missing_browse_is_red(svc, monkeypatch):
    """Шлюз без NAS и без avahi — нормальное состояние: функция выключена
    (off), строки в здоровье нет. Демон есть, а avahi-browse нет — сам не
    появится: 🔴 и в счёт проблем."""
    from awgbot.domain import gwchecks as gc
    _svc_on(monkeypatch, avahi=False)
    c = {c.name: c for c in svc.services_status()[1]}["SMB этой подсети"]
    assert c.ok is None and c.detail == ("avahi-daemon не запущен: SMB-серверы этой подсети по именам не "
                                         "видны ни в подсетях других шлюзов, ни на устройствах админа в туннеле"), c.detail
    _svc_on(monkeypatch, browse=False)
    c = {c.name: c for c in svc.services_status()[1]}["SMB этой подсети"]
    assert c.ok is False and c.detail == "нет avahi-browse, пакет avahi-utils (🚑 Восстановить)", c.detail
    c.group = "svc"
    assert gc.check_mark(c) == "🔴" and gc.health_problems([c]) == [c]


def test_services_add_nothing_where_the_function_does_not_work(svc, monkeypatch):
    """Без своих подсетей в юните обзора нет: ни блока, ни проверок, ни dig."""
    env = _svc_on(monkeypatch)
    env["HOME_SUBNETS"] = ""
    monkeypatch.setattr(gwguard, "dns_local", lambda *a, **k: pytest.fail("dig без функции"))
    assert svc.services_status() == ({"active": False}, [])


def test_services_work_with_own_subnets_alone(svc, monkeypatch):
    """Обзор своей сети нужен и устройствам админа в туннеле («SMB в туннеле»
    на сервере): условие — свои подсети заданы, без VPN-транзита и без
    подсетей соседей. Иначе шлюз без связи подсетей молчал бы, и имена его
    SMB-серверов админу не доехали бы никогда."""
    env = _svc_on(monkeypatch)
    env.update(LAN_MODE="0", PEER_HOME_NETS="")
    monkeypatch.setattr(gwguard, "lan_mode", lambda: False)
    assert svc.services_active() is True
    info, checks = svc.services_status()
    assert info["active"] is True
    assert [c.name for c in checks] == ["SMB этой подсети"], "проверка записей соседей без соседей"
    assert info["ever"] is True, "без подсетей соседей ждать от сервера нечего — не «обновляю…»"


def test_a_red_services_check_sends_neither_plumbing_nor_lan_alerts(svc, monkeypatch):
    """Соседи, которых нет, — не авария: 🔴 группы «svc» видна в
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


# ── свои списки: проверка группы «own» ──

def test_a_red_own_lists_check_sends_neither_plumbing_nor_lan_alerts(svc, monkeypatch):
    """Свои списки не применились (dnsmasq отверг канон) — 🔴 в мониторе, но
    не авария: доступ локальной сети держат прежние файлы. Ни «Обвязка шлюза
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


# ── маскарад соседей к роутеру: сверка с маршрутом по умолчанию и самопочинка ──

ROUTER_CHECK = "маскарад к роутеру"
CLOSED = "сам роутер подсетям других шлюзов не открыт"


def _router_env(monkeypatch, *, lan=True, peers="10.241.0.0/16", home="192.168.1.0/24",
                status=None, script_new=True):
    """VPN-транзит, соседи и свои подсети из юнита, статус скрипта, копия скрипта."""
    env = {"PEER_HOME_NETS": peers, "LAN_MODE": "1" if lan else "0", "HOME_SUBNETS": home}
    monkeypatch.setattr(gwguard, "lan_mode", lambda: lan)
    monkeypatch.setattr(gwguard, "unit_env", lambda k: env.get(k, ""))
    monkeypatch.setattr(gwguard, "script_status",
                        lambda: {"GW_STATUS": "confirmed", "ROUTER": "192.168.1.1"} if status is None else status)
    monkeypatch.setattr(gwguard, "setup_script_has", lambda marker: script_new and marker == "ROUTER=%s")


def _route(via="192.168.1.1", present=True):
    return {"present": present, "dev": "end0" if present else "", "via": via}


def _info(*router):
    return {"sets": {}, "chains": set(), "masq_ifaces": {"end0"}, "router_masq": set(router)}


def _stale(svc):
    return svc.__dict__.get("_router_masq_stale")


def test_router_masquerade_matches_the_default_route(svc, monkeypatch):
    """Правило к адресу роутера, роутер тот же — зелёная, без реассерта;
    группа «svc»: уведомлений нет, как у прочих проверок соседей."""
    _router_env(monkeypatch)
    c = svc._router_masq_check(_info("192.168.1.1"), _route())
    assert (c.name, c.ok, c.group, c.detail) == (ROUTER_CHECK, True, "svc", "роутер 192.168.1.1")
    assert _stale(svc) is False


@pytest.mark.parametrize("have, route, detail", [
    (("192.168.1.1",), _route("192.168.1.254"),
     "правило к 192.168.1.1, а роутер теперь 192.168.1.254; обвязка перевыставится в ближайшие минуты"),
    ((), _route(), "правила нет: сам роутер 192.168.1.1 подсетям других шлюзов не открыт; "
                   "обвязка перевыставится в ближайшие минуты"),
    (("192.168.1.1",), _route(""),
     "у маршрута по умолчанию нет шлюза: правило к 192.168.1.1 лишнее; обвязка перевыставится в ближайшие минуты"),
    (("192.168.1.1",), _route("10.0.0.1"),
     "роутер вне локальной сети шлюза: правило к 192.168.1.1 лишнее; обвязка перевыставится в ближайшие минуты"),
], ids=["moved", "missing", "default-without-via", "router-left-the-lan"])
def test_router_masquerade_diverged_is_red_and_flags_a_reassert(svc, monkeypatch, have, route, detail):
    """Адрес роутера сменился (DHCP шлюза), правила нет, у маршрута по
    умолчанию пропал шлюз или роутер ушёл из своей сети, а правило стоит, —
    красная, флаг перевыставить обвязку."""
    _router_env(monkeypatch)
    c = svc._router_masq_check(_info(*have), route)
    assert (c.ok, c.detail) == (False, detail)
    assert _stale(svc) is True


@pytest.mark.parametrize("route, home, detail", [
    (_route(""), "192.168.1.0/24",
     f"адрес роутера не определён: у маршрута по умолчанию нет шлюза — {CLOSED}"),
    (_route("10.0.0.1"), "192.168.1.0/24", f"роутер вне локальной сети шлюза: 10.0.0.1 — {CLOSED}"),
    (_route("192.168.1.1"), "", f"роутер вне локальной сети шлюза: 192.168.1.1 — {CLOSED}"),
], ids=["no-via", "router-outside-the-lan", "no-own-subnets"])
def test_no_router_address_without_a_rule_is_unknown(svc, monkeypatch, route, home, detail):
    """Шлюза у маршрута по умолчанию нет или он вне своих подсетей, правила
    нет — скрипт его и не поставит: «не проверено», перевыставлять нечего."""
    _router_env(monkeypatch, home=home)
    c = svc._router_masq_check(_info(), route)
    assert (c.ok, c.detail) == (None, detail)
    assert _stale(svc) is False


@pytest.mark.parametrize("have", [(), ("192.168.1.1",)], ids=["no-rule", "rule"])
def test_no_default_route_at_all_is_unknown(svc, monkeypatch, have):
    """Маршрута по умолчанию нет вовсе (или `ip -j` пуст): адрес роутера не
    узнать, и стоящее правило — не повод перевыставлять обвязку вслепую."""
    _router_env(monkeypatch)
    c = svc._router_masq_check(_info(*have), _route("", present=False))
    assert (c.ok, c.detail) == (None, "маршрута по умолчанию нет: адрес роутера не узнать")
    assert _stale(svc) is False


def test_plumbing_from_the_previous_script_is_reasserted_once_the_new_copy_is_there(svc, monkeypatch):
    """Файл статуса без ROUTER= — обвязку поставил прежний скрипт, а копия на
    шлюзе уже новая (awg-bot update юнит не перезапускает): правило встанет
    только после прогона — флаг, агент перевыставит."""
    _router_env(monkeypatch, status={"GW_STATUS": "confirmed", "LAN_ADDR": "192.168.1.2"})
    c = svc._router_masq_check(_info(), _route())
    assert (c.ok, c.detail) == (False, "обвязка старого образца: сам роутер из подсетей других шлюзов недоступен; "
                                   "обвязка перевыставится в ближайшие минуты")
    assert _stale(svc) is True


def test_an_old_script_copy_is_unknown_and_not_reasserted(svc, monkeypatch):
    """Копия скрипта на шлюзе сама прежняя: реассерт поставил бы ту же
    таблицу без правила — «не проверено», без флага."""
    _router_env(monkeypatch, status={"GW_STATUS": "confirmed"}, script_new=False)
    c = svc._router_masq_check(_info(), _route())
    assert (c.ok, c.detail) == (None, "скрипт обвязки старого образца: сам роутер из подсетей других шлюзов не "
                                  "открыт — перевыпусти конфигурацию шлюза с сервера AWG")
    assert _stale(svc) is False


@pytest.mark.parametrize("gw_status", ["foreign", "unconfirmed"])
def test_an_unconfirmed_gateway_is_unknown_and_not_reasserted(svc, monkeypatch, gw_status):
    """Слот помечен другому устройству или аплинка не видно — скрипт выходит
    до таблицы, и реассерт ничего не поставит: «не проверено», без флага."""
    _router_env(monkeypatch, status={"GW_STATUS": gw_status, "ROUTER": "192.168.1.1"})
    c = svc._router_masq_check(_info(), _route())
    assert (c.ok, c.detail) == (None, "шлюз слота не подтверждён, обвязка не установлена")
    assert _stale(svc) is False


@pytest.mark.parametrize("env", [{"lan": False}, {"peers": ""}], ids=["no-transit", "no-peers"])
def test_no_router_masquerade_check_without_transit_or_peers(svc, monkeypatch, env):
    """Без VPN-транзита или без соседей правила нет и быть не должно — проверки
    нет вовсе, флага тоже (даже если бы маршрут разошёлся)."""
    _router_env(monkeypatch, **env)
    assert svc._router_masq_check(_info("192.168.1.1"), _route("192.168.1.254")) is None
    assert _stale(svc) is False
    assert svc._router_masq_check(None, _route()) is None, "таблицы нет — о ней скажет своя проверка"


def test_router_masquerade_is_part_of_the_plumbing_checks(svc, monkeypatch):
    """Проверка стоит в обвязке сразу после «маскарад клиентов» и берёт
    маршрут по умолчанию из того же разбора — один `ip -j` на тик."""
    from awgbot.domain.gateway import base
    _router_env(monkeypatch)
    calls = []
    monkeypatch.setattr(base, "pathlib_read", lambda p: "1\n")
    monkeypatch.setattr(gwguard, "table_info", lambda: _info("192.168.1.1"))
    monkeypatch.setattr(gwguard, "client_subnet", lambda: "")
    monkeypatch.setattr(gwguard, "iptables_forward_policy", lambda: None)
    monkeypatch.setattr(gwguard, "uplink_interface", lambda: "")
    monkeypatch.setattr(gwguard, "_ip_json", lambda args: calls.append(args) or (
        [{"dst": "default", "gateway": "192.168.1.254", "dev": "end0"}] if args == ["route", "show", "default"] else []))
    monkeypatch.setattr(svc, "_unit_enabled", lambda: True)
    checks = svc.plumbing_checks()
    names = [c.name for c in checks]
    assert names.index(ROUTER_CHECK) == names.index("маскарад клиентов") + 1, names
    assert {c.name: c for c in checks}[ROUTER_CHECK].ok is False and _stale(svc) is True
    assert calls.count(["route", "show", "default"]) == 1, calls


def test_a_diverged_router_masquerade_is_reasserted_at_most_every_ten_minutes(svc, monkeypatch):
    """Тик перевыставляет обвязку по флагу — не чаще раза в 10 минут (общий
    троттлинг с прочими починками), без уведомления."""
    from tests.unit.test_gateway import _quiet_status
    _router_env(monkeypatch)
    calls = []
    monkeypatch.setattr(gwguard, "unit_state", lambda: {"ActiveState": "active"})
    monkeypatch.setattr(gwguard, "reassert", lambda **kw: calls.append(1) or (True, ""))
    monkeypatch.setattr(gwguard, "table_info", lambda: _info("192.168.1.1"))
    monkeypatch.setattr(gwguard, "default_route", lambda: _route("192.168.1.254"))

    def status():
        st = _quiet_status()
        st.checks.append(svc._router_masq_check(_info("192.168.1.1"), _route("192.168.1.254")))
        return st
    monkeypatch.setattr(svc, "status", status)
    monkeypatch.setattr(svc, "uplink_policy_heal", lambda: [])
    monkeypatch.setattr(svc, "tg_mark_ensure", lambda missing=None: 0)
    svc._last_reassert = float("-inf")   # «давно»: monotonic после перезагрузки меньше интервала
    notes = svc.monitor_tick()
    assert calls == [1], "разошлось — обвязка не перевыставлена"
    assert not [n for n in notes if "роутер" in n.text], "проверка соседей не уведомляет"
    svc.monitor_tick()
    assert calls == [1], "второй реассерт раньше 10 минут"
    svc._last_reassert -= 10 * 60 + 1
    svc.monitor_tick()
    assert calls == [1, 1], "через 10 минут — снова"


@pytest.mark.parametrize("state, fresh", [
    ("active", "fixed"),        # прогон после обновления уже всё поправил
    ("activating", "stale"),    # прогон идёт — свежий снимок даже не читается
    ("active", "error"),        # nft не ответил — тик не падает, реассерта нет
    ("active", "throttled"),    # окно троттлинга — свежий снимок не читается зря
])
def test_a_stale_snapshot_does_not_reassert_twice(svc, monkeypatch, state, fresh):
    """Флаг ставился по снимку начала тика; к реассерту обвязка уже
    перевыставлена (или перевыставляется) прогоном после обновления — второго
    прогона нет: решение — по свежему состоянию. Свежий снимок — exec'ы nft и
    ip: читается только когда реассерт не под троттлингом и юнит не activating;
    отказ nft не роняет тик."""
    from tests.unit.test_gateway import _quiet_status
    _router_env(monkeypatch)
    calls, reads = [], []
    monkeypatch.setattr(gwguard, "unit_state", lambda: {"ActiveState": state})
    monkeypatch.setattr(gwguard, "reassert", lambda **kw: calls.append(1) or (True, ""))

    def table_info():
        reads.append(1)
        if fresh == "error":
            raise gwguard.GwGuardError("nft: timeout")
        return _info("192.168.1.254" if fresh == "fixed" else "192.168.1.1")
    monkeypatch.setattr(gwguard, "table_info", table_info)
    monkeypatch.setattr(gwguard, "default_route", lambda: _route("192.168.1.254"))

    def status():
        st = _quiet_status()
        st.checks.append(svc._router_masq_check(_info("192.168.1.1"), _route("192.168.1.254")))
        return st
    monkeypatch.setattr(svc, "status", status)
    monkeypatch.setattr(svc, "uplink_policy_heal", lambda: [])
    monkeypatch.setattr(svc, "tg_mark_ensure", lambda missing=None: 0)
    svc._last_reassert = float("-inf") if fresh != "throttled" else __import__("time").monotonic()
    notes = svc.monitor_tick()
    assert calls == [], "реассерт по устаревшему снимку"
    assert bool(reads) == (fresh in ("fixed", "error")), reads
    assert svc.__dict__.get("_router_masq_stale") is None, "флаг не должен пережить тик"
    assert isinstance(notes, list)


def test_the_script_copy_marker_is_the_status_line_of_the_script():
    """Признак новой копии скрипта — та самая строка файла статуса, которую
    пишет write_status: переименуют — проверка перестанет её узнавать."""
    import pathlib
    script = (pathlib.Path(__file__).resolve().parents[2] / "install" / "routing-gw-setup.sh").read_text(encoding="utf-8")
    write_status = script.split("write_status() {", 1)[1].split("\n}\n", 1)[0]
    assert "\\nROUTER=%s\\n" in write_status and GatewayServices._ROUTER_MARK == "ROUTER=%s"


def test_setup_script_has_reads_the_installed_copy(tmp_path, monkeypatch):
    copy = tmp_path / "routing-gw-setup.sh"
    monkeypatch.setattr(gwguard, "SETUP_SCRIPT", str(copy))
    assert gwguard.setup_script_has("ROUTER=%s") is False, "копии нет — не знает"
    copy.write_text("printf 'GW_STATUS=%s\\nROUTER=%s\\n'\n", encoding="utf-8")
    assert gwguard.setup_script_has("ROUTER=%s") is True
