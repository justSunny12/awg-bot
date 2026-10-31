"""Правки по ревью сети, файервола и скриптов резерва и кворума:

- пакет `awgbot.infra.nftguard` отдаёт SET_TG_OUT и TG_OUT4 — рендер таблицы с tg-блоком
  зоны и сбор `collect.zone_tg()` берут их через пакет;
- install/quorum-probe-setup.sh: порт ответчика каждого стоящего зонда (`<имя>.port`),
  выход таблицы `awg_quo` — на порты всех стоящих зондов;
- отпечатки разделов `quorum` (timing) и `reserve` (peer_name);
- install/reserve-zone-setup.sh `tg_path_up`: правило к Telegram ищется без /32;
- `awg-bot.sh reserve status` — `rfpeer=offer,slot`; снятие бота — правило 151;
- проба аплинка агента — к DNS, вписанному в конфиг аплинка руками, иначе к .1 и к .0 подсети
  пользователей (раскладки новая и прежняя);
- чистые правила: эпоха голосующего по фактам (`Voter.fact`, `raise_to`), смена состава
  кандидата снимает «решено».

Скрипты — прогоном функций под sh/bash с подставными командами в PATH (хосты
tests/unit/test_quorum_probe_script.py и tests/unit/test_reserve_zone_netmap.py).

Цена ошибки: рендер таблицы с tg-блоком падает — файервол хоста резерва не
перерисовывается вовсе, пока оболочка зоны стоит; зонд к линии второго сервера с другим
портом ответчика — выход таблицы его режет, кандидат слеп; смена цифр лидера или имени
второго сервера без нового документа — шлюзы считают по старым цифрам и пишут чужое имя;
правило зоны к Telegram копится на каждый `--shell`; `reserve status` прячет слот при
согласии — у терминала не видно, что сервер сам ходит через чужие шлюзы; правило 151
после снятия бота — РФ-трафик второй стороны маршрутизируется в пустую таблицу; проба
аплинка к .1 при прежней раскладке — аплинк живой, а агент считает его мёртвым.
"""
from __future__ import annotations

import ipaddress
import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import fake_cmd
from tests.unit.test_quorum_probe_script import _Host, _chain, probe_conf
from tests.unit.test_reserve_zone_netmap import _Zone

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
AWG_BOT = (ROOT / "awg-bot.sh").read_text(encoding="utf-8")


# ── nftguard: tg-блок зоны через пакет (net-1) ──────────────────────────────

def test_the_package_exports_the_telegram_set_and_ranges():
    from awgbot.infra import nftguard
    from awgbot.infra.nftguard import common
    assert nftguard.SET_TG_OUT == common.SET_TG_OUT == "tg_out4"
    assert tuple(nftguard.TG_OUT4) == tuple(common.TG_OUT4) and nftguard.TG_OUT4
    assert {"SET_TG_OUT", "TG_OUT4"} <= set(nftguard.__all__)


def test_the_table_with_the_zone_telegram_block_renders_through_the_package():
    from awgbot.infra import nftguard
    spec = nftguard.GuardSpec(ssh_port=2222, ssh_open=True, tunnel_nets4=["10.8.1.0/24"], tunnel_ifs=["awg0"],
                              udp_ports=[22222], wan_ifs=["eth0"], zone_tg_if="awggx-h", zone_tg_net4="169.254.77.8/30")
    text = nftguard.render(spec)
    assert "set tg_out4" in text, "в таблице нет набора Telegram для выхода агентов из зоны"
    tg_set = next(ln for ln in text.splitlines() if "set tg_out4" in ln or "tg_out4 {" in ln)
    assert tg_set
    assert re.search(r'iifname \{? ?"awggx-h" ?\}? .*ip daddr @tg_out4 tcp dport 443 accept', text), \
        f"агентам из зоны нет пути к Telegram 443: {[ln for ln in text.splitlines() if 'awggx' in ln]}"
    assert "ip saddr 169.254.77.8/30 masquerade" in text, "выход агентов из зоны без маскарада"


def test_zone_tg_with_the_veth_up_gives_the_fields(monkeypatch):
    from awgbot.infra import rsvzone
    from awgbot.infra.nftguard import collect, common
    monkeypatch.setattr(rsvzone, "state", lambda: {"tg": "up", "zone_tg_cidr": "169.254.77.12/30"})
    got = collect.zone_tg()
    assert got == {"zone_tg_if": "awggx-h", "zone_tg_net4": "169.254.77.12/30", "tg_out4": list(common.TG_OUT4)}, got


@pytest.mark.parametrize("st", [{}, {"tg": "down"}, {"tg": "up", "zone_tg_cidr": "мусор"}], ids=["none", "down", "bad_cidr"])
def test_zone_tg_without_the_veth_gives_nothing(monkeypatch, st):
    from awgbot.infra import rsvzone
    from awgbot.infra.nftguard import collect
    monkeypatch.setattr(rsvzone, "state", lambda: dict(st))
    assert collect.zone_tg() == {}


# ── зонды: порты всех стоящих (net-3, скрипт) ───────────────────────────────

@pytest.fixture()
def host(tmp_path):
    return _Host(tmp_path)


def _up(host, name, resp, **env):
    f = host.put(f"{name}.in", probe_conf(resp=resp))
    return host.sh(f'cmd_up "{f}"', QUO_IF=name, **env)


OUT = "ip daddr @resp4 udp dport {{ {} }} accept"


def test_two_probes_with_different_responder_ports_are_both_let_out(host):
    assert _up(host, "awgqah", "169.254.88.1/32").returncode == 0
    r = _up(host, "awgqbh", "169.254.88.65/32", QUO_PORT="9911")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (host.conf_dir / "awgqah.port").read_text().strip() == "8789"
    assert (host.conf_dir / "awgqbh.port").read_text().strip() == "9911", "порт ответчика зонда не записан"
    out = _chain(host.tables()[-1], "output")
    assert OUT.format("8789, 9911") in out, f"выход таблицы не на порты обоих ответчиков: {out}"


def test_down_takes_its_port_out_of_the_table(host):
    _up(host, "awgqah", "169.254.88.1/32")
    _up(host, "awgqbh", "169.254.88.65/32", QUO_PORT="9911")
    r = host.sh("cmd_down awgqbh")
    assert r.returncode == 0 and "down: ok" in r.stdout
    assert not (host.conf_dir / "awgqbh.port").exists(), "порт снятого зонда остался"
    out = _chain(host.tables()[-1], "output")
    assert OUT.format("8789") in out, f"порт снятого зонда остался в выходе: {out}"


def test_the_same_port_twice_is_listed_once(host):
    _up(host, "awgqah", "169.254.88.1/32")
    _up(host, "awgqbh", "169.254.88.65/32")
    assert OUT.format("8789") in _chain(host.tables()[-1], "output")


def test_wipe_takes_every_port_file(host):
    _up(host, "awgqah", "169.254.88.1/32")
    _up(host, "awgqbh", "169.254.88.65/32", QUO_PORT="9911")
    assert host.sh("cmd_wipe").returncode == 0
    assert not list(host.d.glob("quorum/*.port")), "после --wipe файлы портов остались"


# ── отпечатки разделов (net-4) ──────────────────────────────────────────────

QSEC = {"l": "a", "g": 2, "members": ["a", "b", "a1"], "t": 2, "port": 8789, "probes": {},
        "point": {"x": 2, "y": "ab" * 16, "q_gen": 3}, "e": 5}
TIMING = {"up_seconds": 100, "down_seconds": 60}


def test_the_quorum_print_follows_the_leader_timing():
    from awgbot.domain.gwcfg import prints
    base = prints.quorum_print(QSEC)
    t1 = prints.quorum_print({**QSEC, "timing": dict(TIMING)})
    t2 = prints.quorum_print({**QSEC, "timing": {**TIMING, "up_seconds": 120}})
    assert base and t1 and t2 and len({base, t1, t2}) == 3, "смена цифр лидера не меняет отпечаток раздела quorum"
    assert prints.quorum_print({**QSEC, "timing": dict(reversed(list(TIMING.items())))}) == t1, \
        "отпечаток зависит от порядка ключей timing — документ по кругу"


def test_the_quorum_print_without_timing_is_the_one_of_4_1():
    """Шлюз 4.1.x считает отпечаток без timing — документ без поля не должен тянуться заново."""
    from awgbot.domain.gwcfg import prints
    text = ("l=a\ng=2\nmembers=a,b,a1\nt=2\nport=8789\nh:\nr:"
            f"\npoint=2:{'ab' * 16}:3\ne=5\n")
    assert prints.quorum_print(QSEC) == prints.section_print("quorum", text)
    assert prints.quorum_print({**QSEC, "timing": {}}) == prints.quorum_print(QSEC)


RSEC = {"link": {}, "self": "10.99.97.2", "state": "idle", "move": 0, "chan": 1, "peer_host": "203.0.113.5",
        "record": "vpn.example.org", "name": "Beta", "slot": 1}


def test_the_reserve_print_follows_the_peer_name():
    from awgbot.domain.gwcfg import prints
    base = prints.reserve_print(RSEC)
    a = prints.reserve_print({**RSEC, "peer_name": "Gamma"})
    b = prints.reserve_print({**RSEC, "peer_name": "Delta"})
    assert len({base, a, b}) == 3, "смена имени второго сервера не меняет отпечаток раздела reserve"


def test_the_reserve_print_without_peer_name_is_unchanged():
    from awgbot.domain.gwcfg import prints
    text = ("self=10.99.97.2\nstate=idle\nmove=0\nchan=1\npeer_host=203.0.113.5\nrecord=vpn.example.org\n"
            "name=Beta\nslot=1\n")
    assert prints.reserve_print(RSEC) == prints.section_print("reserve", text)
    assert prints.reserve_print({**RSEC, "peer_name": ""}) == prints.reserve_print(RSEC)


# ── зона: правило к Telegram не копится (net-7) ─────────────────────────────

LINK = "[Interface]\nPrivateKey = DUMMY\nAddress = {addr}\n"


@pytest.fixture()
def zone(tmp_path):
    """Хост зоны с `ip rule` моделью: `rule list` печатает /32 без маски, как ядро."""
    z = _Zone(tmp_path)
    (z.d / "links").mkdir()
    rules = z.d / "rules"
    fake_cmd(z.bin / "ip", (
        f'echo "ip $*" >> "{z.log}"\n'
        'if [ "$1" = netns ] && [ "$2" = exec ]; then shift 3; exec "$@"; fi\n'
        f'if [ "$1 $2" = "rule list" ]; then cat "{rules}" 2>/dev/null; exit 0; fi\n'
        'if [ "$1 $2" = "rule add" ] && [ "$3" = from ]; then\n'
        f'  echo "32765:\tfrom ${{4%/32}} lookup $6" >> "{rules}"; exit 0; fi\n'
        'case "$*" in "-j route show default") echo \'[{"dev": "eth0"}]\' ;;\n'
        f'  "-4 route show") cat "{tmp_path}/routes" 2>/dev/null ;;\n'
        '  "rule del"*) exit 1 ;; esac\n'
        'exit 0\n'))
    z.rules = rules
    return z


def test_tg_path_up_twice_keeps_one_rule_per_source(zone):
    for i, a in enumerate(("10.99.97.1/30", "10.99.97.5/30"), 1):
        (zone.d / "links" / f"awglink{i}.conf").write_text(LINK.format(addr=a), encoding="utf-8")
    for _ in range(3):
        r = zone.sh("tg_path_up")
        assert r.returncode == 0, r.stdout + r.stderr
    lines = zone.rules.read_text(encoding="utf-8").splitlines()
    assert lines == ["32765:\tfrom 10.99.97.2 lookup 153", "32765:\tfrom 10.99.97.6 lookup 153"], \
        f"правило зоны к Telegram копится на каждый --shell: {lines}"


# ── awg-bot.sh: reserve status и снятие (net-8, net-9) ──────────────────────

def _func(text: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", text, re.S | re.M)
    assert m, f"нет функции {name}()"
    return m.group(0)


def _status(tmp_path: Path, rfpeer: str | None) -> list[str]:
    inst = tmp_path / "inst"
    (inst / "install").mkdir(parents=True, exist_ok=True)
    (inst / "install" / "reserve-zone-setup.sh").write_text('echo "zone=$*"\n', encoding="utf-8")
    data = tmp_path / "data"
    if rfpeer is not None:
        (data / "reserve").mkdir(parents=True, exist_ok=True)
        (data / "reserve" / "rfpeer.json").write_text(rfpeer, encoding="utf-8")
    harness = "\n".join(["set -euo pipefail", f'INSTALL_DIR="{inst}"', f'DATA_DIR="{data}"',
                         "ENV_FILE=/dev/null CONF_DIR=/dev/null", 'die() { echo "DIE: $*"; exit 1; }',
                         "require_installed() { :; }", "require_root() { :; }",
                         _func(AWG_BOT, "cmd_reserve"), "cmd_reserve status"])
    r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stdout + r.stderr
    return [ln for ln in r.stdout.splitlines() if ln.startswith("rfpeer=")]


@pytest.mark.parametrize("text, want", [
    ('{"offer": true, "slot": true, "at": "x"}', "rfpeer=offer,slot"),
    ('{\n  "offer": true,\n  "slot": false\n}', "rfpeer=offer"),
    ('{"offer": false, "slot": true}', "rfpeer=slot"),
    ('{"offer": false, "slot": false}', "rfpeer=off"),
    (None, "rfpeer=off"),
], ids=["offer_and_slot", "offer", "slot", "neither", "no_file"])
def test_reserve_status_prints_both_offer_and_slot(tmp_path, text, want):
    assert _status(tmp_path, text) == [want]


def test_uninstall_takes_the_transit_rule_151_away():
    body = _func(AWG_BOT, "cmd_post_uninstall")
    assert "ip rule del pref 151" in body, "снятие бота оставляет правило 151 транзита второй стороны"
    assert re.search(r"while ip rule del pref 151 2>/dev/null; do :; done", body), \
        "правило 151 снимается не все копии"


# ── проба аплинка агента — к DNS конфига (net-10) ───────────────────────────

@pytest.fixture()
def probe(monkeypatch, tmp_path):
    from awgbot.core import config
    from awgbot.domain.gateway.reserve import tgvia as tgmod
    from awgbot.infra import gwguard
    asked: list[tuple[str, str]] = []
    monkeypatch.setattr(config, "GW_CONF_DIR", str(tmp_path))
    monkeypatch.setattr(gwguard, "unit_env", lambda k: {"CLIENT_SUBNET": "10.8.1.0/24"}.get(k, ""))
    monkeypatch.setattr(tgmod, "_dns_probe", lambda resolver, iface, timeout=None: asked.append((resolver, iface)) or True)
    return types_ns(asked=asked, dir=tmp_path, mixin=tgmod.ReserveTgViaMixin)


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


def test_a_hand_written_dns_line_of_the_uplink_config_is_asked_first(probe):
    """Выданный шлюзу конфиг DNS не несёт; строка, вписанная руками, — первой."""
    (probe.dir / "awgup.conf").write_text("[Interface]\nAddress = 10.8.1.7/32\nDNS = 10.8.1.0, 1.1.1.1\n", encoding="utf-8")
    assert probe.mixin._tgvia_probe("awgup") is True
    assert probe.asked == [("10.8.1.0", "awgup")], f"проба не к DNS конфига аплинка: {probe.asked}"


def test_the_issued_uplink_config_has_no_dns_and_the_old_layout_answers_on_dot_zero(probe, monkeypatch):
    """Конфиг аплинка — как его выдаёт сервер (configgen.gateway_uplink_conf: DNS вырезан). У прежней
    раскладки сервер на .0: .1 молчит — проба спрашивает .0; ответивший адрес — дальше только он."""
    from awgbot.domain import configgen
    from awgbot.domain.gateway.reserve import tgvia as tgmod
    dev = ("[Interface]\nPrivateKey = DUMMY\nAddress = 10.8.1.7/32\nDNS = 10.8.1.0, 10.8.1.0\n\n"
           "[Peer]\nPublicKey = DUMMY\nAllowedIPs = 0.0.0.0/0\nEndpoint = vpn.example.org:43125\n")
    issued = configgen.gateway_uplink_conf(dev)
    assert "DNS" not in issued, "подготовка: выданный конфиг аплинка несёт DNS"
    (probe.dir / "awgup.conf").write_text(issued, encoding="utf-8")
    monkeypatch.setattr(tgmod, "_dns_probe", lambda resolver, iface, timeout=None:
                        probe.asked.append((resolver, iface)) or resolver == "10.8.1.0")
    memo: dict = {}
    assert probe.mixin._tgvia_probe("awgup", memo) is True, "аплинк прежней раскладки никогда не отвечает пробе"
    assert probe.asked == [("10.8.1.1", "awgup"), ("10.8.1.0", "awgup")], probe.asked
    probe.asked.clear()
    assert probe.mixin._tgvia_probe("awgup", memo) is True
    assert probe.asked == [("10.8.1.0", "awgup")], f"ответивший адрес не запомнен — лишнее ожидание на .1: {probe.asked}"


def test_without_the_uplink_config_the_probe_asks_the_dot_one(probe):
    assert probe.mixin._tgvia_probe("awgup") is True
    assert probe.asked == [("10.8.1.1", "awgup")], probe.asked


def test_a_config_without_dns_falls_back_to_the_dot_one(probe):
    (probe.dir / "awgup.conf").write_text("[Interface]\nAddress = 10.8.1.7/32\n", encoding="utf-8")
    probe.mixin._tgvia_probe("awgup")
    assert probe.asked == [("10.8.1.1", "awgup")], probe.asked


def test_without_any_resolver_there_is_no_probe(probe, monkeypatch):
    from awgbot.infra import gwguard
    monkeypatch.setattr(gwguard, "unit_env", lambda k: "")
    assert probe.mixin._tgvia_probe("awgup") is False and probe.asked == []


# ── чистые правила кворума ──────────────────────────────────────────────────

def test_the_raised_epoch_is_not_a_fact():
    from awgbot.util.quorum.voter import Voter
    v = Voter("b", "a")
    v.epoch_in(7)
    assert (v.epoch, v.fact) == (7, 7)
    v.raise_to(8)
    assert (v.epoch, v.fact) == (8, 7), "поднятая собой эпоха стала фактом"
    v.raise_to(6)
    assert v.epoch == 8, "raise_to опустил эпоху пробы"
    assert v.epoch_in(8) is False and v.fact == 8, "факт той же эпохи не записан"
    assert v.epoch_in(9) is True and (v.epoch, v.fact) == (9, 9)


def test_a_change_of_the_members_drops_decided_and_keeps_the_count():
    from awgbot.util.quorum.candidate import Candidate
    from awgbot.util.quorum.timing import Timing
    c = Candidate(l="a", v=3, me="b", timing=Timing())
    c.set_members(("b", "a1", "a2"), 3)
    c.set_own("uns", 1000.0)
    c.view_in("a1", "uns", 1000.0)
    c.tick(1000.0)
    c.decided = True
    since = c.no_since
    c.set_members(("b", "a1", "a2"), 3)
    assert c.decided is True, "тот же состав снял «решено»"
    c.set_members(("b", "a1", "a2", "a3"), 4)
    assert c.decided is False, "смена состава не сняла «решено»"
    assert c.no_since == since, "смена состава сбросила счёт большинства"


def test_the_net_helpers_agree_on_the_telegram_ranges():
    from awgbot.infra.nftguard import common
    for n in common.TG_OUT4:
        ipaddress.ip_network(n)
