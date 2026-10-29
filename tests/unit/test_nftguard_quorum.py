"""Файервол хоста и ответчик кворума резерва (infra/nftguard: collect.quorum_probe,
render._probe_input/_probe_forward; концепт «кворум» §3.1, таблица «Файервол»).

Ответчик — второй адрес интерфейса AWG из блока зондов 169.254.88.0/24,
отдельной строкой `Address`. На хост с интерфейса — только udp на порт
ответчика с подблока своей линии; всё прочее с блока зондов — drop; пересылки
с блока нет вовсе. Без второго адреса правил нет.

Цена ошибки: accept шире подблока — зонд чужой линии (или адрес, подделанный
в обход AllowedIPs) считается голосом; нет drop блока — с адреса зонда
открыты DNS, SSH и прочие порты хоста; нет drop в пересылке — зонд
становится выходом в интернет через сервер (у зонда выхода нет по
построению); правила при конфиге без второго адреса — таблица ссылается на
ответчика, которого нет, и прячет ошибку конфигурации.
"""
from __future__ import annotations

import pytest

from awgbot.core import config, settings
from awgbot.infra import nftguard

pytestmark = pytest.mark.unit

CLIENTS = "[Interface]\nAddress = 10.9.1.1/24\nListenPort = 42755\nPrivateKey = DUMMY\n"
WITH_A = CLIENTS.replace("Address = 10.9.1.1/24\n", "Address = 10.9.1.1/24\nAddress = 169.254.88.1/26\n")
WITH_B = CLIENTS.replace("Address = 10.9.1.1/24\n", "Address = 10.9.1.1/24\nAddress = 169.254.88.65/26\n")
Q = 'iifname { "awg1" }'
ACCEPT_A = f"{Q} ip saddr 169.254.88.0/26 ip daddr 169.254.88.1 udp dport 8789 accept"
DROP = "ip saddr 169.254.88.0/24 drop"


@pytest.fixture()
def awgdir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AWG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg1")
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "")
    monkeypatch.setattr(config, "SUBNET_PREFIX", "10.9.1")
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "")
    monkeypatch.setattr(config, "SERVER_PORT", 42755)
    monkeypatch.setattr(nftguard, "_tunnel_ifs", lambda: ["awg1"])
    monkeypatch.setattr(nftguard, "wan_ifaces", lambda: [])
    return tmp_path


def _spec(awgdir, monkeypatch, conf: str | None, **kw):
    vals = {"app.firewall.enabled": True, "app.firewall.ssh_allow": [], "app.firewall.open_tcp": [8080],
            "app.firewall.open_udp": [5353], "app.network.ssh_port": 2222, "app.reserve.quorum": {"port": 8789}}
    vals.update(kw)
    monkeypatch.setattr(settings, "get", lambda k, d=None: vals.get(k, d))
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(vals.get(k, d)))
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: bool(vals.get(k, d)))
    if conf is not None:
        (awgdir / "awg1.conf").write_text(conf, encoding="utf-8")
    return nftguard.build_spec(["10.9.1.2"])


def _chain(text: str, name: str) -> list[str]:
    body = text.split(f"chain {name} {{", 1)[1].split("\n    }", 1)[0]
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


def test_the_responder_is_read_from_the_second_address_line(awgdir, monkeypatch):
    spec = _spec(awgdir, monkeypatch, WITH_A)
    assert (spec.probe_if, spec.probe_net4, spec.probe_resp4, spec.probe_port) == ("awg1", "169.254.88.0/26", "169.254.88.1", 8789)


def test_the_line_b_responder_gives_the_upper_subblock(awgdir, monkeypatch):
    spec = _spec(awgdir, monkeypatch, WITH_B)
    assert (spec.probe_net4, spec.probe_resp4) == ("169.254.88.64/26", "169.254.88.65")


def test_the_port_follows_the_setting(awgdir, monkeypatch):
    spec = _spec(awgdir, monkeypatch, WITH_A, **{"app.reserve.quorum": {"port": 9911}})
    assert spec.probe_port == 9911
    assert "udp dport 9911 accept" in nftguard.render(spec)


@pytest.mark.parametrize("conf", [CLIENTS, None, "мусор"], ids=["без второго адреса", "нет конфига", "битый"])
def test_without_a_responder_there_are_no_probe_rules(awgdir, monkeypatch, conf):
    spec = _spec(awgdir, monkeypatch, conf)
    assert spec.probe_resp4 == "" and spec.probe_if == ""
    for enabled in (True, False):
        spec = _spec(awgdir, monkeypatch, None, **{"app.firewall.enabled": enabled})
        assert "169.254.88" not in nftguard.render(spec), "правила ответчика без второго адреса"


@pytest.mark.parametrize("enabled", [True, False], ids=["фильтр", "NAT-only"])
def test_into_the_host_only_probes_of_the_own_line_reach_the_responder(awgdir, monkeypatch, enabled):
    text = nftguard.render(_spec(awgdir, monkeypatch, WITH_A, **{"app.firewall.enabled": enabled}))
    lines = _chain(text, "input")
    assert ACCEPT_A in lines, f"нет разрешения проб к ответчику: {lines}"
    at = lines.index(ACCEPT_A)
    assert lines[at + 1] == DROP, "после разрешения проб нет drop блока зондов"
    general = [i for i, ln in enumerate(lines) if ln == "ip protocol icmp accept" or ln.startswith(("udp dport", "tcp dport"))]
    assert all(i > at + 1 for i in general), f"общие разрешения раньше drop блока — с зонда открыты порты хоста: {lines}"


@pytest.mark.parametrize("enabled", [True, False], ids=["фильтр", "NAT-only"])
def test_forwarding_from_the_probe_block_is_dropped_before_any_accept(awgdir, monkeypatch, enabled):
    text = nftguard.render(_spec(awgdir, monkeypatch, WITH_A, **{"app.firewall.enabled": enabled}))
    assert "chain forward {" in text
    lines = _chain(text, "forward")
    assert DROP in lines, f"пересылка с блока зондов не закрыта: {lines}"
    first_accept = next((i for i, ln in enumerate(lines) if ln.endswith("accept") and "policy" not in ln), len(lines))
    assert lines.index(DROP) < first_accept, f"пересылка разрешена раньше drop блока зондов: {lines}"


def test_the_accept_is_only_from_the_subblock_never_the_whole_block(awgdir, monkeypatch):
    text = nftguard.render(_spec(awgdir, monkeypatch, WITH_A))
    accepts = [ln for ln in _chain(text, "input") if "169.254.88" in ln and ln.endswith("accept")]
    assert accepts == [ACCEPT_A], f"к ответчику пускается шире подблока линии: {accepts}"


def test_the_rendering_is_stable(awgdir, monkeypatch):
    a = nftguard.render(_spec(awgdir, monkeypatch, WITH_A))
    b = nftguard.render(_spec(awgdir, monkeypatch, WITH_A))
    assert a == b
