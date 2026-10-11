"""
infra/admindns.py — обёртка бота над скриптом резолвера админа: адрес из
настроек, «поставлен ли», подтягивание к резолверу клиентов на старте,
файл записей. Скрипт подменён (subprocess.run), хост не трогается.

Цена ошибки: кривой адрес из app.yaml в DNS конфига админа — устройство без
DNS; install на каждом старте без нужды — рестарт экземпляра и окно без
имён; временный файл записей, оставленный в /tmp, — мусор на каждый тик.
"""
from __future__ import annotations

import subprocess

import pytest

from awgbot.core import settings
from awgbot.infra import admindns

pytestmark = pytest.mark.unit


class _Script:
    """Подставной скрипт: что ему передали (режим, аргумент, окружение,
    содержимое файла записей на момент вызова) и что он ответит."""

    def __init__(self, monkeypatch, rc=0, out=b""):
        self.calls: list[tuple] = []
        self.rc, self.out = rc, out
        self.paths: list[str] = []
        monkeypatch.setattr(subprocess, "run", self._run)

    def _run(self, args, **kw):
        mode = args[2]
        arg = args[3] if len(args) > 3 else ""
        body = ""
        if mode == "records" and arg != "-":
            self.paths.append(arg)
            with open(arg, encoding="utf-8") as f:
                body = f.read()
        self.calls.append((mode, arg if mode != "records" or arg == "-" else body, kw["env"]))
        return subprocess.CompletedProcess(args, self.rc, self.out, b"")


@pytest.fixture()
def conf(monkeypatch, tmp_path):
    p = tmp_path / "dnsmasq.conf"
    monkeypatch.setattr(admindns, "CONF_PATH", p)
    return p


def _addr_setting(monkeypatch, value):
    real = settings.get
    monkeypatch.setattr(settings, "get", lambda k, d=None: value if k == "app.client_config.admin_dns" else real(k, d))


@pytest.mark.parametrize("raw,want", [("10.255.53.53", "10.255.53.53"), (" 10.255.9.9 ", "10.255.9.9"),
                                      ("172.31.53.53", "172.31.53.53"),
                                      ("", "10.255.53.53"), (None, "10.255.53.53"),
                                      ("10.255.53", "10.255.53.53"), ("resolver.example.org", "10.255.53.53"),
                                      ("1.1.1.1", "10.255.53.53"), ("127.0.0.53", "10.255.53.53"),
                                      ("10.8.1.1", "10.255.53.53"), ("10.8.1.200", "10.255.53.53")])
def test_the_address_falls_back_to_the_default_unless_private_and_outside_client_nets(monkeypatch, raw, want):
    """Опечатка в app.yaml не должна уехать в DNS конфига админа и в правило
    nft. Адрес из подсети клиентов (например, резолвер клиентов 10.8.1.1)
    правило «:53 — только админу» отрезало бы от DNS всех клиентов; публичный
    увёл бы DNS админа в интернет; loopback из туннеля недостижим."""
    from awgbot.infra import nftguard
    monkeypatch.setattr(nftguard, "tunnel_nets", lambda: ["10.8.1.0/24"])
    _addr_setting(monkeypatch, raw)
    assert admindns.addr() == want


@pytest.mark.parametrize("conf_exists,active,want", [(True, True, True), (True, False, False),
                                                    (True, None, False), (False, True, False)])
def test_serving_means_installed_and_running(conf, monkeypatch, conf_exists, active, want):
    """Только поставленный и живой отдаёт адрес в конфиги админа: лежащий
    оставил бы новые устройства админа без DNS; «не знаю» (None) — не «жив»."""
    if conf_exists:
        conf.write_text("server=10.9.1.1\n", encoding="utf-8")
    monkeypatch.setattr(admindns, "service_active", lambda: active)
    assert admindns.serving() is want


def test_installed_and_upstream_follow_the_config_file(conf):
    assert admindns.installed() is False and admindns.upstream() == ""
    conf.write_text("listen-address=10.255.53.53\nserver=10.9.1.1\nconf-dir=/x,*.conf\n", encoding="utf-8")
    assert admindns.installed() is True and admindns.upstream() == "10.9.1.1"


@pytest.mark.parametrize("state", ["absent", "moved", "dead", "in_place"])
def test_ensure_always_runs_the_idempotent_install(conf, monkeypatch, state):
    if state != "absent":
        conf.write_text("server=" + ("10.8.1.1" if state == "moved" else "10.9.1.1") + "\n", encoding="utf-8")
    monkeypatch.setattr(admindns, "service_active", lambda: state != "dead")
    s = _Script(monkeypatch)
    assert admindns.ensure("10.9.1.1") is True
    # скрипт идемпотентен — зовётся всегда: подтягивает и адрес из настройки, и юнит поставки
    assert [(m, a) for m, a, _ in s.calls] == [("install", "10.9.1.1")]
    env = s.calls[0][2]
    assert env["ADMIN_ADDR"] == admindns.addr() and env["ADMIN_DIR"] == str(admindns.ADMIN_DIR)


def test_ensure_without_a_client_resolver_does_nothing(conf, monkeypatch):
    s = _Script(monkeypatch)
    assert admindns.ensure("") is False and s.calls == []


def test_a_failing_script_raises_with_its_last_lines_but_status_does_not(monkeypatch):
    _Script(monkeypatch, rc=1, out=b"one\ntwo\nthree\n[admin-dns:ERR] no dnsmasq\n")
    with pytest.raises(admindns.AdminDnsError, match="no dnsmasq") as e:
        admindns.run("install", "10.9.1.1")
    assert "one" not in str(e.value), "в ошибку — хвост, а не весь вывод"
    assert admindns.run("status").endswith("no dnsmasq"), "код status — состояние, не отказ"


def test_a_script_that_does_not_start_is_an_admin_dns_error(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("bash")
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(admindns.AdminDnsError, match="не запустился"):
        admindns.run("status")


def test_records_go_through_a_temp_file_that_is_removed_afterwards(monkeypatch):
    import os
    s = _Script(monkeypatch)
    admindns.apply_records("local=/awg.internal/\n")
    assert [(m, a) for m, a, _ in s.calls] == [("records", "local=/awg.internal/\n")]
    assert s.paths and not os.path.exists(s.paths[0]), "временный файл записей остался"


def test_records_temp_file_is_removed_even_when_the_script_refuses(monkeypatch):
    import os
    s = _Script(monkeypatch, rc=1, out=b"refused")
    with pytest.raises(admindns.AdminDnsError):
        admindns.apply_records("local=/awg.internal/\n")
    assert s.paths and not os.path.exists(s.paths[0])


def test_empty_records_ask_the_script_to_take_the_file_down(monkeypatch):
    s = _Script(monkeypatch)
    admindns.apply_records("")
    assert [(m, a) for m, a, _ in s.calls] == [("records", "-")]


class _Iptables:
    """iptables -t nat: -S отдаёт `rules`, -C — есть ли правило в `have`, -I — дописывает."""

    def __init__(self, monkeypatch, rules: str, fail_insert=False):
        self.rules, self.have, self.inserted = rules, set(), []
        self.fail_insert = fail_insert
        monkeypatch.setattr(subprocess, "run", self._run)

    def _run(self, argv, **kw):
        op = argv[3]
        if op == "-S":
            return subprocess.CompletedProcess(argv, 0, self.rules.encode(), b"")
        spec = " ".join(argv[5:])
        if op == "-C":
            return subprocess.CompletedProcess(argv, 0 if spec in self.have else 1, b"", b"")
        if self.fail_insert:
            raise subprocess.CalledProcessError(1, argv)
        self.have.add(spec)
        self.inserted.append(spec)
        return subprocess.CompletedProcess(argv, 0, b"", b"")


DNAT = ("-A PREROUTING -s 10.8.1.0/24 -p udp -m udp --dport 53 -j DNAT --to-destination 10.8.1.1:53\n"
        "-A PREROUTING -s 10.8.1.0/24 -p tcp -m tcp --dport 53 -j DNAT --to-destination 10.8.1.1:53\n")


def test_nat_exempt_puts_the_admin_resolver_above_the_dns_interception_once(monkeypatch):
    """Перехват :53 обвязки уводил бы запросы к резолверу админа на резолвер
    клиентов — без имён awg.internal. RETURN — по разу на протокол; второй
    старт бота ничего не дописывает."""
    from awgbot.infra import nftguard
    monkeypatch.setattr(nftguard, "tunnel_nets", lambda: ["10.8.1.0/24"])
    ipt = _Iptables(monkeypatch, DNAT)
    assert admindns.nat_exempt(["10.8.1.0/24"]) == ["10.8.1.0/24/udp", "10.8.1.0/24/tcp"]
    assert ipt.inserted == [f"-s 10.8.1.0/24 -d 10.255.53.53 -p {p} --dport 53 -j RETURN" for p in ("udp", "tcp")]
    assert admindns.nat_exempt(["10.8.1.0/24"]) == [] and len(ipt.inserted) == 2, "повтор дописал дубль"


def test_nat_exempt_does_nothing_without_the_interception(monkeypatch):
    ipt = _Iptables(monkeypatch, "-P PREROUTING ACCEPT\n")
    assert admindns.nat_exempt(["10.8.1.0/24"]) == [] and ipt.inserted == []


def test_nat_exempt_survives_a_missing_or_failing_iptables(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("iptables")
    monkeypatch.setattr(subprocess, "run", boom)
    assert admindns.nat_exempt(["10.8.1.0/24"]) == []
    _Iptables(monkeypatch, DNAT, fail_insert=True)
    assert admindns.nat_exempt(["10.8.1.0/24"]) == [], "отказ вставки выдан за поставленное правило"
