"""Ответчик кворума бота основы на резерве — второй адрес клона в гостевой зоне
(install/reserve-zone-setup.sh `--take` с GUEST_PROBE_ADDR, таблица зоны
`inet awg_guest`, состояние `probe_*`, `--teardown`) и обёртка
(infra/rsvzone.take, tools/rsvopen.py) — концепт «кворум» §3.1, §2.6.

Функции скрипта гоняются тем же хостом-моделью, что и NETMAP зоны
(tests/unit/test_reserve_zone_netmap.py): `ip`, `nft` и прочие подменены,
каждый `nft -f` оставляет текст таблицы.

Цена ошибки: второй адрес не встал на клон — бот основы на резерве не
слышит проб, шлюзы основы «не видят» её клон, пока пользователи там; вход к
ответчику шире подблока линии — голос подделывается с любого адреса зоны;
пересылка с блока зондов из зоны — зонд выходит в интернет через резерв;
`probe_addr`, оставшийся после снятия зоны, — следующий `--take` без
кворума рисует правило к ответчику, которого нет.
"""
from __future__ import annotations

import pytest

from awgbot.infra import rsvzone
from tests.unit.test_reserve_zone_netmap import _take, _Zone
from tests.unit.test_rsvzone import _Proc

pytestmark = pytest.mark.unit

PROBE = {"GUEST_PROBE_ADDR": "169.254.88.2/26", "GUEST_PROBE_NET": "169.254.88.0/26", "GUEST_PROBE_PORT": "8789"}
ACCEPT = 'iifname { "awg0" } ip saddr 169.254.88.0/26 ip daddr 169.254.88.2 udp dport 8789 accept'
DROP = "ip saddr 169.254.88.0/24 drop"


@pytest.fixture()
def zone(tmp_path):
    return _Zone(tmp_path)


def _chain(text: str, name: str) -> list[str]:
    body = text.split(f"chain {name} {{", 1)[1].split("\n    }", 1)[0]
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


def test_take_puts_the_second_address_on_the_clone_and_keeps_it_in_the_state(zone):
    r = _take(zone, extra=False, **PROBE)
    assert r.returncode == 0 and "take: ok" in r.stdout, r.stdout + r.stderr
    adds = [c for c in zone.calls() if "addr replace 169.254.88.2/26" in c]
    assert adds and adds[0].endswith("ip addr replace 169.254.88.2/26 dev awg0"), \
        f"второй адрес клона не встал (или не на клон): {zone.calls()}"
    assert any("netns exec" in c for c in adds), "второй адрес клона поставлен у хоста, а не в зоне"
    st = zone.read_state()
    assert (st["probe_addr"], st["probe_net"], st["probe_port"]) == ("169.254.88.2/26", "169.254.88.0/26", "8789")


def test_the_zone_table_lets_probes_of_the_line_only_to_the_responder(zone):
    assert _take(zone, extra=False, **PROBE).returncode == 0
    table = zone.tables()[-1][0]
    inp = _chain(table, "input")
    assert ACCEPT in inp, f"нет входа к ответчику клона: {inp}"
    assert inp[inp.index(ACCEPT) + 1] == DROP, "после входа к ответчику нет drop блока зондов"
    accepts = [ln for ln in inp if "169.254.88" in ln and ln.endswith("accept")]
    assert accepts == [ACCEPT], f"к ответчику клона пускается шире подблока: {accepts}"


def test_nothing_else_of_the_zone_is_open_to_the_probe_block(zone):
    """Как во входе хоста сервера: с блока зондов — только проба ответчику; DNS и
    ICMP клона зонду не нужны, и drop блока должен стоять раньше них."""
    assert _take(zone, extra=False, **PROBE).returncode == 0
    inp = _chain(zone.tables()[-1][0], "input")
    at = inp.index(DROP)
    general = [ln for ln in inp if ln == "ip protocol icmp accept" or "dport 53 accept" in ln]
    early = [ln for ln in general if inp.index(ln) < at]
    assert not early, f"с блока зондов в зоне открыто раньше drop: {early}"


def test_forwarding_from_the_probe_block_is_dropped_in_the_zone(zone):
    assert _take(zone, extra=False, **PROBE).returncode == 0
    fwd = _chain(zone.tables()[-1][0], "forward")
    first_accept = next(i for i, ln in enumerate(fwd) if ln.endswith("accept") and "established" not in ln)
    assert DROP in fwd and fwd.index(DROP) < first_accept, f"пересылка с блока зондов из зоны: {fwd}"


def test_take_without_a_responder_draws_no_responder_rule(zone):
    assert _take(zone, extra=False).returncode == 0
    table = zone.tables()[-1][0]
    assert "169.254.88.2" not in table, "правило к ответчику без второго адреса"
    assert not [c for c in zone.calls() if "169.254.88" in c and "addr" in c], "второй адрес поставлен без GUEST_PROBE_ADDR"
    assert zone.read_state()["probe_addr"] == ""
    assert DROP in _chain(table, "forward"), "пересылка с блока зондов открыта, пока ответчика нет"


def test_the_responder_port_follows_the_environment(zone):
    assert _take(zone, extra=False, **{**PROBE, "GUEST_PROBE_PORT": "9911"}).returncode == 0
    assert "udp dport 9911 accept" in zone.tables()[-1][0]


def test_teardown_forgets_the_responder(zone):
    zone.put_state(phase="serving", iface="awg0", ifaces="awg0", probe_addr="169.254.88.2/26",
                   probe_net="169.254.88.0/26", probe_port="8789", netmap="", port="45877", ports="45877")
    r = zone.sh("cmd_teardown")
    assert r.returncode == 0, r.stdout + r.stderr
    st = zone.read_state()
    assert (st.get("probe_addr", ""), st.get("probe_net", "")) == ("", ""), f"ответчик пережил снятие зоны: {st}"


# ── обёртка ──────────────────────────────────────────────────────────────────

def test_rsvzone_take_passes_the_responder_address_its_subblock_and_port(monkeypatch):
    p = _Proc(monkeypatch, out=b"take: ok")
    rsvzone.take("[Interface]\nPrivateKey = DUMMY\n", iface="awg0", addr="10.8.1.1/24", dns=[], upstreams=[], admin=[],
                 probe_addr="169.254.88.66/26", probe_port=8789)
    env = p.calls[0][1]["env"]
    assert (env["GUEST_PROBE_ADDR"], env["GUEST_PROBE_NET"], env["GUEST_PROBE_PORT"]) == \
        ("169.254.88.66/26", "169.254.88.64/26", "8789")


def test_rsvzone_take_without_a_responder_passes_nothing(monkeypatch):
    p = _Proc(monkeypatch, out=b"take: ok")
    for k in PROBE:
        monkeypatch.delenv(k, raising=False)
    rsvzone.take("[Interface]\nPrivateKey = DUMMY\n", iface="awg0", addr="10.8.1.1/24", dns=[], upstreams=[], admin=[])
    env = p.calls[0][1]["env"]
    assert not any(k in env for k in PROBE), "переменные ответчика без адреса"


def test_rsvzone_take_with_a_broken_address_passes_an_empty_subblock(monkeypatch):
    p = _Proc(monkeypatch, out=b"take: ok")
    rsvzone.take("[Interface]\nPrivateKey = DUMMY\n", iface="awg0", addr="10.8.1.1/24", dns=[], upstreams=[], admin=[],
                 probe_addr="мусор")
    env = p.calls[0][1]["env"]
    assert env["GUEST_PROBE_NET"] == "" and "GUEST_PROBE_PORT" not in env


def test_the_open_process_hands_the_clone_responder_to_take(monkeypatch):
    """tools/rsvopen: адрес ответчика клона — из части VPN (`probe_resp`), порт —
    из входа процесса открытия (`probe_port`)."""
    import inspect
    from tools import rsvopen
    src = inspect.getsource(rsvopen.run)
    assert 'probe_addr=str(iface.get("probe_resp") or "")' in src and 'probe_port=int(data.get("probe_port") or 0)' in src


def test_the_open_payload_carries_the_probe_port(services, monkeypatch):
    from awgbot.core import settings
    vals = {"app.reserve.quorum": {"port": 9911}}
    real = settings.get
    monkeypatch.setattr(settings, "get", lambda k, d=None: vals[k] if k in vals else real(k, d))
    monkeypatch.setattr(rsvzone, "plan", lambda: {"webhook_port": "443"})
    monkeypatch.setattr(services, "reserve_own_ip", lambda: "203.0.113.10")
    payload = services._open_payload("button")
    assert payload is not None and payload.get("probe_port") == 9911, payload
