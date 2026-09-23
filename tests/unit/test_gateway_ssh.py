"""Доступ по SSH на шлюзе (domain/gwssh): порт как факт, чужой владелец,
фильтр снаружи, наборы с динамикой. Инфраструктура подменена: ни ss, ни
sshd, ни nft, ни systemctl."""
from __future__ import annotations

import pytest

from awgbot.domain.gateway import GatewayServices
from awgbot.domain.gwssh import SshOwnerRefusal
from awgbot.domain.services import ServiceError
from awgbot.infra import gwguard, nftguard, sshd
from awgbot.infra.db import Database


@pytest.fixture()
def svc(tmp_path):
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    return GatewayServices(d)


class _Host:
    """Состояние малины: что слушает sshd, кто владеет конфигом, что в
    таблице; и журнал действий агента."""

    def __init__(self, listening=(22,), owner=None, chains=("input", "tunnel_in", "forward", "ssh_in"),
                 table_ports=None, sets=None):
        self.listening = list(listening)
        self.owner = owner or sshd.SshdOwner()
        self.chains = set(chains)
        self.table_ports = dict(table_ports if table_ports is not None else {"tunnel_in": 22})
        self.sets = sets if sets is not None else {"ssh_allow4": set(), "server4": set(),
                                                   "lan4": {"192.168.1.0/24"}}
        self.acts: list = []

    def info(self):
        return {"sets": {k: set(v) for k, v in self.sets.items()}, "chains": set(self.chains),
                "masq_ifaces": set(), "ssh_ports": dict(self.table_ports)}


@pytest.fixture()
def host(tmp_path, monkeypatch):
    h = _Host()
    monkeypatch.setattr(gwguard, "FW_ENV", str(tmp_path / "firewall.env"))
    monkeypatch.setattr(sshd, "listening_ports", lambda: list(h.listening))
    monkeypatch.setattr(sshd, "effective_ports", lambda: list(h.listening))
    monkeypatch.setattr(sshd, "owner", lambda: h.owner)
    monkeypatch.setattr(sshd, "omv_firewall_rules", lambda: 0)
    monkeypatch.setattr(sshd, "port_busy", lambda p: "nginx" if p == 8443 else "")
    monkeypatch.setattr(sshd, "set_port", lambda p: h.acts.append(("sshd", p)) or h.listening.__setitem__(0, p) or [])
    monkeypatch.setattr(gwguard, "table_info", h.info)

    def reassert():
        h.acts.append(("reassert",))
        env = gwguard.read_env()
        port = int(env.get("SSH_PORT") or 22)
        h.table_ports = {"tunnel_in": port}
        if env.get("SSH_FILTER") == "1":
            h.table_ports["input"] = port
        return True, ""
    monkeypatch.setattr(gwguard, "reassert", reassert)

    def set_sync(name, desired, info=None):
        if name not in h.sets:
            return False
        if h.sets[name] == set(desired):
            return False
        h.sets[name] = set(desired)
        h.acts.append(("set", name, set(desired)))
        return True
    monkeypatch.setattr(gwguard, "set_sync", set_sync)
    monkeypatch.setattr(gwguard, "server_host", lambda: "203.0.113.10")
    monkeypatch.setattr(gwguard, "plumbing_installed", lambda: True)
    monkeypatch.setattr(gwguard, "lan_nets", lambda iface="": ["192.168.1.0/24"])
    monkeypatch.setattr(gwguard, "unit_admin_ips", lambda: ["10.9.1.2"])
    monkeypatch.setattr(nftguard, "ufw_active", lambda: False)
    monkeypatch.setattr(nftguard, "_resolve", lambda host: {"home2.dyn.example": ["198.51.100.4"]}.get(host, []))
    return h


# ── порт как факт: OMV сменил порт → правила следуют ──────────────────────────

def test_port_changed_by_omv_moves_the_table_and_notifies_once(svc, host):
    """Главный сценарий: OMV перевёл sshd на 2222. Тик видит факт, пишет
    SSH_PORT, реассертит сразу (правило для сервера по линку и переход на
    ssh_in — на новом порту) и уведомляет один раз; следующие тики молчат."""
    gwguard.write_env(SSH_PORT="22", SSH_FILTER="1")
    host.table_ports = {"tunnel_in": 22, "input": 22}
    host.owner = sshd.SshdOwner("omv", "OMV: Службы → SSH", 2222)
    host.listening = [2222]
    notes = svc.ssh_reconcile(host.info())
    assert gwguard.read_env()["SSH_PORT"] == "2222"
    assert ("reassert",) in host.acts
    assert host.table_ports == {"tunnel_in": 2222, "input": 2222}, "все правила про SSH на новом порту"
    assert len(notes) == 1 and "22 → 2222" in notes[0].text and "OMV" in notes[0].text
    assert notes[0].critical is False
    host.acts.clear()
    assert svc.ssh_reconcile(host.info()) == [] and ("reassert",) not in host.acts
    # монитор: таблица держит порт sshd
    checks = {c.name: c for c in svc.ssh_checks(host.info())}
    assert checks["порт SSH"].ok is True and checks["фильтр SSH снаружи"].ok is True


def test_first_tick_on_a_fresh_machine_records_22_quietly(svc, host):
    """Файла нет, sshd на 22: записать SSH_PORT без реассерта и без
    уведомления — таблица и так на 22."""
    notes = svc.ssh_reconcile(host.info())
    assert notes == [] and gwguard.read_env()["SSH_PORT"] == "22"
    assert ("reassert",) not in host.acts


def test_first_tick_with_sshd_already_moved_reasserts_without_a_notice(svc, host):
    host.listening = [2222]
    notes = svc.ssh_reconcile(host.info())
    assert notes == [], "менять некому не с чего: прежнего значения не было"
    assert ("reassert",) in host.acts and host.table_ports["tunnel_in"] == 2222


def test_sshd_down_is_a_third_state(svc, host):
    host.listening = []
    gwguard.write_env(SSH_PORT="22")
    assert svc.ssh_reconcile(host.info()) == []
    assert gwguard.read_env()["SSH_PORT"] == "22", "нет факта — файл не трогаем"
    checks = {c.name: c for c in svc.ssh_checks(host.info())}
    assert checks["порт SSH"].ok is None and "не запущен" in checks["порт SSH"].detail


def test_monitor_flags_a_table_that_lost_the_port(svc, host):
    gwguard.write_env(SSH_PORT="2222", SSH_FILTER="1")
    host.listening = [2222]
    host.table_ports = {"tunnel_in": 22}                 # реассерт не прошёл, перехода нет
    checks = {c.name: c for c in svc.ssh_checks(host.info())}
    assert checks["порт SSH"].ok is False and "2222" in checks["порт SSH"].detail
    assert checks["фильтр SSH снаружи"].ok is False
    host.chains.discard("ssh_in")
    checks = {c.name: c for c in svc.ssh_checks(host.info())}
    assert "перевыпусти" in checks["фильтр SSH снаружи"].detail


# ── смена порта из чата ──────────────────────────────────────────────────────

def test_port_change_refused_when_omv_owns_the_config(svc, host):
    host.owner = sshd.SshdOwner("omv", "OMV: Службы → SSH → «Порт»", 22)
    with pytest.raises(SshOwnerRefusal) as ei:
        svc.ssh_port_change(2222)
    assert ei.value.owner.kind == "omv" and ei.value.listening == 22
    assert not host.acts and "SSH_PORT" not in gwguard.read_env()


def test_port_change_table_first_then_sshd_and_rollback(svc, host, monkeypatch):
    gwguard.write_env(SSH_PORT="22")
    assert svc.ssh_port_change(2222) == 22
    assert host.acts[0] == ("reassert",) and host.acts[1] == ("sshd", 2222), "таблица раньше sshd"
    assert gwguard.read_env()["SSH_PORT"] == "2222" and host.table_ports["tunnel_in"] == 2222
    assert svc.db.get_state("gw_ssh_port_seen") == "2222", "свою смену тик не переуведомит"
    host.acts.clear()

    def boom(p):
        raise sshd.SshdError("sshd -t: Bad configuration option")
    monkeypatch.setattr(sshd, "set_port", boom)
    with pytest.raises(ServiceError, match="Bad configuration"):
        svc.ssh_port_change(2200)
    assert gwguard.read_env()["SSH_PORT"] == "2222" and host.table_ports["tunnel_in"] == 2222, \
        "sshd не принял — файл и таблица вернулись"
    with pytest.raises(ServiceError, match="занят процессом nginx"):
        svc.ssh_port_change(8443)
    with pytest.raises(ServiceError, match="текущий"):
        svc.ssh_port_change(2222)
    host.listening = []
    with pytest.raises(ServiceError, match="не запущен"):
        svc.ssh_port_change(2200)


# ── адреса снаружи и фильтр ──────────────────────────────────────────────────

def test_allow_list_is_ipv4_only_and_feeds_the_live_set(svc, host):
    cur = svc.ssh_allow_add("home2.dyn.example, 203.0.113.7 198.51.101.0/24")
    assert cur == ["home2.dyn.example", "203.0.113.7", "198.51.101.0/24"]
    env = gwguard.read_env()
    assert env["SSH_ALLOW"] == "home2.dyn.example 203.0.113.7 198.51.101.0/24"
    assert env["SSH_ALLOW_RESOLVED"] == "198.51.100.4", "резолв имени — для следующей загрузки"
    assert host.sets["ssh_allow4"] == {"198.51.100.4", "203.0.113.7", "198.51.101.0/24"}
    assert host.sets["server4"] == {"203.0.113.10"}
    assert host.sets["lan4"] == {"192.168.1.0/24"}
    assert ("reassert",) not in host.acts, "список — живой набор, юнит не дёргаем"
    with pytest.raises(ServiceError, match="IPv6"):
        svc.ssh_allow_add("2001:db8::1")
    with pytest.raises(ServiceError, match="не адрес, не подсеть и не имя$"):
        svc.ssh_allow_add("мусор")
    assert svc.ssh_allow_remove("203.0.113.7") == ["home2.dyn.example", "198.51.101.0/24"]
    assert host.sets["ssh_allow4"] == {"198.51.100.4", "198.51.101.0/24"}


def test_overlapping_entries_are_merged_and_resolved_names_inside_subnets_are_not_duplicated(svc, host):
    """nft отвергает пересекающиеся интервалы — и в транзакции, и в файле при
    загрузке (после ребута обвязки не было бы). Пересечения схлопываются:
    остаётся покрывающая подсеть; имя, резолвящееся в подсеть из списка, в
    SSH_ALLOW_RESOLVED не попадает."""
    assert svc.ssh_allow_add("198.51.100.0/24") == ["198.51.100.0/24"]
    assert svc.ssh_allow_add("198.51.100.5") == ["198.51.100.0/24"], "адрес внутри подсети поглощён"
    assert svc.ssh_allow_add("203.0.113.9 203.0.113.0/24") == ["198.51.100.0/24", "203.0.113.0/24"]
    assert svc.ssh_allow_add("198.51.0.0/16") == ["203.0.113.0/24", "198.51.0.0/16"], \
        "подсеть поверх прежней — прежняя уходит"
    svc.ssh_allow_add("home2.dyn.example")                 # резолвится в 198.51.100.4 — внутри /16
    env = gwguard.read_env()
    assert env["SSH_ALLOW"] == "203.0.113.0/24 198.51.0.0/16 home2.dyn.example"
    assert env.get("SSH_ALLOW_RESOLVED", "") == "", "адрес покрыт подсетью — в файл не пишем"


def test_dyndns_change_is_followed_on_tick_without_a_reassert(svc, host, monkeypatch):
    svc.ssh_allow_add("home2.dyn.example")
    host.acts.clear()
    monkeypatch.setattr(nftguard, "_resolve", lambda h: ["198.51.100.9"])
    svc.ssh_reconcile(host.info())
    assert host.sets["ssh_allow4"] == {"198.51.100.9"}
    assert gwguard.read_env()["SSH_ALLOW_RESOLVED"] == "198.51.100.9"
    assert ("reassert",) not in host.acts


def test_silent_dns_keeps_the_last_known_address(svc, host, monkeypatch):
    """После ребута/рестарта агента кэш резолвера пуст; DNS роутера ещё не
    поднялся — прошлый адрес берётся из SSH_ALLOW_RESOLVED, набор и файл не
    пустеют (fail-closed, а не «снаружи никого»)."""
    gwguard.write_env(SSH_ALLOW="home2.dyn.example", SSH_ALLOW_RESOLVED="198.51.100.9")
    host.sets["ssh_allow4"] = {"198.51.100.9"}
    monkeypatch.setattr(nftguard, "_resolve", lambda h: [])              # DNS молчит
    svc.ssh_reconcile(host.info())
    assert host.sets["ssh_allow4"] == {"198.51.100.9"}, "прошлый адрес удержан"
    assert gwguard.read_env()["SSH_ALLOW_RESOLVED"] == "198.51.100.9", "файл не затёрт"
    scr = svc.ssh_screen(host.info())
    assert scr["unresolved"] == ["home2.dyn.example"] and scr["held"] == ["198.51.100.9"]
    monkeypatch.setattr(nftguard, "_resolve", lambda h: ["198.51.100.10"])   # DNS ожил
    svc.ssh_reconcile(host.info())
    assert host.sets["ssh_allow4"] == {"198.51.100.10"}
    assert gwguard.read_env()["SSH_ALLOW_RESOLVED"] == "198.51.100.10"


def test_multiple_sshd_ports_keep_the_recorded_one(svc, host):
    """sshd на двух портах: порядок `ss` не контракт — держим порт из файла,
    без него — меньший; уведомлений от перестановки быть не должно."""
    gwguard.write_env(SSH_PORT="2222")
    host.listening = [22, 2222]
    assert svc.ssh_port_fact()[0] == 2222
    gwguard.write_env(SSH_PORT="")
    assert svc.ssh_port_fact()[0] == 22


def test_failed_reassert_is_reported_and_retried(svc, host, monkeypatch):
    gwguard.write_env(SSH_PORT="22")
    host.listening = [2222]
    monkeypatch.setattr(gwguard, "reassert", lambda: host.acts.append(("reassert",)) or (False, "timeout"))
    notes = svc.ssh_reconcile(host.info())
    assert len(notes) == 1 and "не удалось" in notes[0].text and "переведён" not in notes[0].text
    assert gwguard.read_env()["SSH_PORT"] == "2222"
    host.acts.clear()
    svc.ssh_reconcile(host.info())                        # таблица всё ещё на 22
    assert ("reassert",) not in host.acts, "повтор не чаще раза в 10 минут"
    svc._ssh_last_reassert = 0.0
    svc.ssh_reconcile(host.info())
    assert ("reassert",) in host.acts, "троттлинг истёк — повтор"


def test_nothing_is_written_after_rollback(svc, host, monkeypatch):
    monkeypatch.setattr(gwguard, "plumbing_installed", lambda: False)
    host.listening = [2222]
    assert svc.ssh_reconcile(host.info()) == []
    assert gwguard.read_env() == {} and not host.acts


def test_filter_toggle_reasserts_and_needs_the_new_plumbing(svc, host):
    svc.ssh_filter_on()
    assert gwguard.read_env()["SSH_FILTER"] == "1" and host.table_ports.get("input") == 22
    svc.ssh_filter_off()
    assert gwguard.read_env()["SSH_FILTER"] == "0" and "input" not in host.table_ports
    host.chains.discard("ssh_in")
    with pytest.raises(ServiceError, match="старого образца"):
        svc.ssh_filter_on()
    assert gwguard.read_env()["SSH_FILTER"] == "0"


def test_screen_collects_port_owner_and_plumbing_state(svc, host):
    host.owner = sshd.SshdOwner("omv", "OMV: Службы → SSH", 2222)
    host.listening = [22, 2200]
    scr = svc.ssh_screen(host.info())
    assert scr["port"] == 22 and scr["ports"] == [22, 2200] and scr["owner"] == "omv"
    assert scr["owner_port"] == 2222 and scr["new_plumbing"] is True
    assert scr["admin_ips"] == ["10.9.1.2"] and scr["server"] == "203.0.113.10"
    host.listening = []
    scr = svc.ssh_screen(host.info())
    assert scr["sshd_down"] is True and scr["port"] == 22


# ── подсети других шлюзов (peer_nets4) на экране ─────────────────────────────

def test_screen_reports_peer_nets_from_the_table_set(svc, host):
    """Экран показывает то, что реально пускает ssh_in, — набор peer_nets4
    таблицы, а не то, что обещал бандл; отсортировано, чтобы строка не
    прыгала от порядка элементов в `nft -j`."""
    host.sets["peer_nets4"] = {"192.168.68.0/24", "10.20.0.0/16"}
    assert svc.ssh_screen(host.info())["peer_nets"] == ["10.20.0.0/16", "192.168.68.0/24"]


def test_screen_peer_nets_empty_when_the_set_is_empty_absent_or_unreadable(svc, host, monkeypatch):
    """Доступ выключен (набор пуст), обвязка старого образца (набора нет),
    nft недоступен — пустой список, без исключения: экран SSH не должен
    падать из-за функции, которой у человека нет."""
    host.sets["peer_nets4"] = set()
    assert svc.ssh_screen(host.info())["peer_nets"] == []
    del host.sets["peer_nets4"]
    assert svc.ssh_screen(host.info())["peer_nets"] == []

    def _broken():
        raise gwguard.GwGuardError("nft: нет доступа")
    monkeypatch.setattr(gwguard, "table_info", _broken)
    assert svc.ssh_screen()["peer_nets"] == []
