"""Смена ключей после верного кода сопряжения на шлюзе — сквозь настоящие
функции (FIXES-uupd-3, flow-1): стоящий конфиг линка — по токену
(tools/join._link_conf: AllowedIPs только /30), новый — как его собирает
сервер (подсеть клиентов, /30, префиксы соседей); агент — настоящий
GatewayServices (rekey_next_in → rekey_go), проверка — настоящая
wg_conf_check из routing-gw-setup.sh с окружением, которое агент передаёт
в --link-apply.

Цена ошибки: после верного кода линк на постоянные ключи не переходит
никогда — проверка скрипта требует подсеть клиентов, которой в [Peer] по
токену нет, юнита с CLIENT_SUBNET у шлюза в сопряжении ещё нет.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from awgbot.domain import gwcfg
from awgbot.util import jointoken, wgconf
from tests.conftest import SH
from tests.unit import _link_txn_common as _c
from tests.unit.test_gwlink_rekey import keypair

pytestmark = pytest.mark.unit

conf_dir, svc = _c.conf_dir, _c.svc
SCRIPT = Path(__file__).resolve().parents[2] / "install" / "routing-gw-setup.sh"
I1 = "<b 0xc70000000108ce1bc7e1d8a3b4a9><r 16><t><rc 8>"
_RUN = subprocess.run                  # настоящий: gwguard.subprocess.run в фикстуре подменён


def _token(gw_priv: bytes, vps_pub: bytes) -> jointoken.JoinToken:
    return jointoken.JoinToken(slot=1, version="3.4.0", join_id="0123456789abcdef", expires=1_800_000_000,
                               host="vpn-server.example.org", link_port=443, channel_port=8787,
                               gw_addr="10.99.99.2", vps_pub=vps_pub, gw_priv=gw_priv, jc=4, jmin=40, jmax=70,
                               s=(10, 20, 0, 0), h=((100, 200), (300, 400), (500, 600), (700, 800)))


def _server_next(peers: str = "") -> str:
    """Шлюзовая сторона новыми ключами — как её пишет write_gw_conf скрипта
    линка в --rekey-stage (gw_allowed_ips, PSK, I1)."""
    priv, _ = keypair()
    _, vpub = keypair()
    allowed = "10.8.1.0/24, 10.99.99.0/30" + (f", {peers}" if peers else "")
    return (f"# awg-bot: контракт линка 1\n[Interface]\nAddress = 10.99.99.2/30\nPrivateKey = {priv}\n"
            "Jc = 4\nJmin = 40\nJmax = 70\nS1 = 10\nS2 = 20\nS3 = 0\nS4 = 0\n"
            "H1 = 100-200\nH2 = 300-400\nH3 = 500-600\nH4 = 700-800\n"
            f"I1 = {I1}\n\n[Peer]\nPublicKey = {vpub}\nPresharedKey = DUMMY-NEW-PSK=\n"
            f"Endpoint = vpn-server.example.org:443\nAllowedIPs = {allowed}\nPersistentKeepalive = 25-35\n")


def _script_check(path: str, env: dict) -> subprocess.CompletedProcess:
    """Проверка --link-apply настоящего скрипта: вид — как в нём (LINK_CHECK_KIND)."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert '_lk=link; [ "${LINK_CHECK_KIND:-}" = "join" ] && _lk=join' in text
    core = text[text.index("allow_list_path() {"):text.index('if [ "$MODE" = "link-apply" ]; then')]
    allow = wgconf.ALLOW_LIST
    prog = (core + f'\nallow_list_path() {{ printf "%s" "{allow}"; [ -f "{allow}" ]; }}\n'
            '_lk=link; [ "${LINK_CHECK_KIND:-}" = "join" ] && _lk=join\n'
            f'wg_conf_check "{path}" "$_lk"\n')
    keep = {k: v for k, v in env.items() if k in ("CLIENT_SUBNET", "PEER_LAN_NETS", "RESOLVER", "LAN_SUBNETS",
                                                  "IFACE_NETS", "LINK_CHECK_KIND")}
    return _RUN([SH, "-c", prog], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", **keep})


@pytest.fixture()
def joined(svc, tmp_path, monkeypatch):
    """Шлюз в сопряжении: код подтверждён (join.json accepted), стоит конфиг
    линка по токену, юнита обвязки нет; --link-apply — настоящая проверка
    скрипта с переданным окружением, при успехе файл встаёт на место."""
    import base64
    from awgbot.core import config
    from awgbot.infra import gwguard
    from tools import join as tjoin
    gpriv, _ = keypair()
    _, vpub = keypair()
    token_conf = tjoin._link_conf(_token(base64.b64decode(gpriv), base64.b64decode(vpub)))
    link = tmp_path / "awglink.conf"
    link.write_text(token_conf, encoding="utf-8")
    etc = tmp_path / "awg-gw"; etc.mkdir()
    monkeypatch.setattr(config, "GW_LINK_CONF", str(link))
    monkeypatch.setattr(gwguard, "GW_ETC", str(etc))
    monkeypatch.setattr(gwguard, "unit_env", lambda key: "")          # юнита ещё нет
    jfile = tmp_path / "join.json"
    jfile.write_text(json.dumps({"id": "0123456789abcdef", "slot": 1, "state": "accepted"}), encoding="utf-8")
    monkeypatch.setattr(svc, "_join_file", str(jfile))
    runs = []

    class _P:
        def __init__(self, rc, out):
            self.returncode, self.stdout, self.stderr = rc, out, b""

    def run(argv, **kw):
        if len(argv) >= 3 and argv[1] == "--link-apply":
            r = _script_check(argv[2], kw.get("env") or {})
            runs.append((argv[2], r.returncode, r.stderr))
            if r.returncode == 0:
                link.write_text(Path(argv[2]).read_text(encoding="utf-8"), encoding="utf-8")
                return _P(0, b"link: restarted\n")
            return _P(1, ("ОШИБКА: " + r.stderr).encode())
        return _P(0, b"")
    monkeypatch.setattr(gwguard, "subprocess", __import__("types").SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired, SubprocessError=subprocess.SubprocessError))
    svc.runs = runs
    svc.link = link
    return svc


@pytest.mark.parametrize("peers", ["", "10.241.0.0/16"])
def test_the_keys_after_the_code_pass_the_scripts_check(joined, peers):
    """link_next auto=join после кода: проверка ключей агентом, переключение,
    --link-apply — настоящая проверка скрипта принимает конфиг, и на месте —
    [Peer] сервера (подсеть клиентов, соседи, PSK), [Interface] нового (I1)."""
    nxt = _server_next(peers)
    res = joined.rekey_next_in({"t": "link_next", "id": "a1b2c3d4e5f6", "conf": nxt, "fp": "x", "auto": "join"})
    assert res["reply"]["state"] == "ready" and not res["ask"], res
    out = joined.rekey_go({"id": "a1b2c3d4e5f6"})
    assert out["ok"], (out, joined.runs)
    conf = wgconf.parse(joined.link.read_text(encoding="utf-8"))
    assert conf.get("Peer", "AllowedIPs").startswith("10.8.1.0/24, 10.99.99.0/30")
    assert conf.get("Peer", "PresharedKey") == "DUMMY-NEW-PSK=" and conf.get("Interface", "I1") == I1


def test_the_old_merge_would_have_been_refused_by_the_script(joined, tmp_path):
    """Тот же переход прежним слиянием ([Peer] стоящего, только /30) с
    подсетью клиентов — скрипт отказывает: ради этого [Peer] при сопряжении
    берётся из нового."""
    standing = joined.link.read_text(encoding="utf-8")
    new = gwcfg.check_link_rekey(_server_next(), standing)
    old = wgconf.render(gwcfg.merge_link_keys(standing, new), "link", stamp="1")
    p = tmp_path / "old.conf"; p.write_text(old, encoding="utf-8")
    r = _script_check(str(p), {"CLIENT_SUBNET": "10.8.1.0/24"})
    assert r.returncode != 0 and "нет обязательной записи 10.8.1.0/24" in r.stderr


def test_the_way_back_to_the_token_conf_passes_too(joined):
    """Поиск рабочего конфига после неудачного переключения возвращает конфиг
    по токену — проверкой вида join (AllowedIPs только /30)."""
    standing = joined.link.read_text(encoding="utf-8")
    assert gwcfg.join_link_env(standing) == {"LINK_CHECK_KIND": "join"}
    prev = Path(joined._rk_path("rekey.prev"))
    prev.parent.mkdir(parents=True, exist_ok=True)
    prev.write_text(standing, encoding="utf-8")
    assert joined._rk_apply_file("rekey.prev"), joined.runs


def test_the_env_comes_from_the_conf_only_while_pairing(joined, monkeypatch):
    """Вне сопряжения окружение проверки — юнита, как было."""
    from awgbot.infra import gwguard
    seen = []
    monkeypatch.setattr(gwguard, "link_apply", lambda path, iface_nets=(), env_extra=None: (seen.append(env_extra), (True, ""))[1])
    p = Path(joined._rk_path("rekey.next")); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_server_next("10.241.0.0/16"), encoding="utf-8")
    joined._rk_link_apply(str(p))
    assert seen[-1] == {"CLIENT_SUBNET": "10.8.1.0/24", "PEER_LAN_NETS": "10.241.0.0/16", "RESOLVER": ""}
    monkeypatch.setattr(joined, "join_active", lambda: False)
    joined._rk_link_apply(str(p))
    assert seen[-1] == {}


def test_the_full_apply_after_pairing_gets_the_client_subnet(joined, monkeypatch):
    """Полная обвязка после сопряжения (--apply) — подсеть клиентов и соседи
    из конфига линка на постоянных ключах, а не /30 и не умолчание."""
    from awgbot.infra import gwguard
    from awgbot.infra.gwguard import join as gj
    joined.link.write_text(_server_next("10.241.0.0/16"), encoding="utf-8")
    got = {}
    monkeypatch.setattr(gj, "_run", lambda script, args, env, timeout: (got.update(env), (True, ""))[1])
    gwguard.join_finish(8787)
    assert got["CLIENT_SUBNET"] == "10.8.1.0/24" and got["PEER_LAN_NETS"] == "10.241.0.0/16"
    assert "LINK_CHECK_KIND" not in got
