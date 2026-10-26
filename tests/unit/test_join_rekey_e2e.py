"""Смена ключей после верного кода сопряжения на шлюзе — сквозь настоящие
функции (FIXES-uupd-3, flow-1): стоящий конфиг линка — по токену
(tools/join._link_conf: AllowedIPs только /30), новый — как его собирает
сервер (подсеть пользователей, /30, префиксы соседей); агент — настоящий
GatewayServices (rekey_next_in → rekey_go), проверка — настоящая
wg_conf_check из routing-gw-setup.sh с окружением, которое агент передаёт
в --link-apply.

Цена ошибки: после верного кода линк на постоянные ключи не переходит
никогда — проверка скрипта требует подсеть пользователей, которой в [Peer] по
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


LINK_SCRIPT = Path(__file__).resolve().parents[2] / "install" / "routing-link-setup.sh"


def _script_next(tmp: Path, standing: str, peers: str = "") -> str:
    """Шлюзовая сторона новыми ключами — выводом настоящего --rekey-stage
    скрипта линка ВПС (LINK_I1, PEER_LAN_NETS, ENDPOINT_HOST, как их
    передаёт бот) и в том виде, в каком её отдаёт link_next
    (gateway_rekey_next_body: без пояснений, штамп контракта). awg — подставной:
    genkey/genpsk — настоящие ключи по очереди, pubkey — их пары."""
    import base64
    import os
    from tests.conftest import fake_cmd
    run = tmp / "rekey-stage"; run.mkdir()
    bin_, conf_dir, root = run / "bin", run / "amneziawg", run / "root"
    for d in (bin_, conf_dir, root):
        d.mkdir()
    pairs = [keypair() for _ in range(2)]
    (run / "keys").write_text("".join(f"{priv}\n" for priv, _ in pairs), encoding="utf-8")
    (run / "pubs").write_text("".join(f"{priv} {pub}\n" for priv, pub in pairs), encoding="utf-8")
    (run / "psk").write_text(base64.b64encode(os.urandom(32)).decode() + "\n", encoding="utf-8")
    fake_cmd(bin_ / "id", "echo 0\n")
    fake_cmd(bin_ / "awg",
             f'case "$1" in\n'
             f'  genkey) head -n1 "{run}/keys"; sed -i.bak 1d "{run}/keys" ;;\n'
             f'  genpsk) cat "{run}/psk" ;;\n'
             f'  pubkey) read k; awk -v k="$k" \'$1 == k {{ print $2 }}\' "{run}/pubs" ;;\n'
             f'esac\n')
    vps_priv, _ = keypair()
    gw_pub = wgconf.pubkey_of(wgconf.parse(standing).get("Interface", "PrivateKey"))
    (conf_dir / "awglink.conf").write_text(
        f"[Interface]\nAddress = 10.99.99.1/30\nListenPort = 443\nPrivateKey = {vps_priv}\nTable = off\n\n"
        f"[Peer]\nPublicKey = {gw_pub}\nAllowedIPs = 0.0.0.0/0\n", encoding="utf-8")
    (root / "gw-awglink.conf").write_text(standing, encoding="utf-8")
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "LINK_IF": "awglink", "LINK_CIDR": "10.99.99.0/30", "LINK_PORT": "443",
           "CONF_DIR": str(conf_dir), "GW_CONF_OUT": str(root / "gw-awglink.conf"), "_APP_YAML": str(run / "нет.yaml"),
           "CLIENT_SUBNET": "10.8.1.0/24", "LINK_KEEPALIVE": "25-35", "ENDPOINT_HOST": "vpn-server.example.org",
           "LINK_I1": I1, "PEER_LAN_NETS": peers.replace(",", " ")}
    r = _RUN([SH, str(LINK_SCRIPT), "--rekey-stage"], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout.strip().endswith("rekey: staged"), r.stdout + r.stderr
    conf = wgconf.parse((root / "gw-awglink.conf.next").read_text(encoding="utf-8"))
    conf.comments = []
    return wgconf.render(conf, "link", stamp=gwcfg.LINK_CONTRACT)


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
def test_the_keys_after_the_code_pass_the_scripts_check(joined, peers, tmp_path):
    """link_next auto=join после кода: проверка ключей агентом, переключение,
    --link-apply — настоящая проверка скрипта принимает конфиг, и на месте —
    [Peer] сервера (подсеть пользователей, соседи, PSK), [Interface] нового (I1)."""
    nxt = _script_next(tmp_path, joined.link.read_text(encoding="utf-8"), peers)
    res = joined.rekey_next_in({"t": "link_next", "id": "a1b2c3d4e5f6", "conf": nxt, "fp": "x", "auto": "join"})
    assert res["reply"]["state"] == "ready" and not res["ask"], res
    out = joined.rekey_go({"id": "a1b2c3d4e5f6"})
    assert out["ok"], (out, joined.runs)
    conf = wgconf.parse(joined.link.read_text(encoding="utf-8"))
    want = "10.8.1.0/24, 10.99.99.0/30" + (f", {peers}" if peers else "")
    assert conf.get("Peer", "AllowedIPs") == want, conf.get("Peer", "AllowedIPs")
    assert conf.get("Peer", "PresharedKey") == wgconf.parse(nxt).get("Peer", "PresharedKey")
    assert conf.get("Interface", "I1") == I1


def test_the_old_merge_would_have_been_refused_by_the_script(joined, tmp_path):
    """Тот же переход прежним слиянием ([Peer] стоящего, только /30) с
    подсетью пользователей — скрипт отказывает: ради этого [Peer] при сопряжении
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
    assert seen[-1] == {"CLIENT_SUBNET": "10.8.1.0/24", "PEER_LAN_NETS": "10.241.0.0/16"}
    monkeypatch.setattr(joined, "join_active", lambda: False)
    joined._rk_link_apply(str(p))
    assert seen[-1] == {}


def test_the_full_apply_after_pairing_gets_the_client_subnet(joined, monkeypatch):
    """Полная обвязка после сопряжения (--apply) — подсеть пользователей и соседи
    из конфига линка на постоянных ключах, а не /30 и не умолчание."""
    from awgbot.infra import gwguard
    from awgbot.infra.gwguard import join as gj
    joined.link.write_text(_server_next("10.241.0.0/16"), encoding="utf-8")
    got = {}
    monkeypatch.setattr(gj, "_run", lambda script, args, env, timeout: (got.update(env), (True, ""))[1])
    gwguard.join_finish(8787)
    assert got["CLIENT_SUBNET"] == "10.8.1.0/24" and got["PEER_LAN_NETS"] == "10.241.0.0/16"
    assert "LINK_CHECK_KIND" not in got


def test_the_resolver_is_not_guessed_from_allowed_ips():
    """join-3: в AllowedIPs линка резолвера нет (gw_allowed_ips скрипта линка:
    подсеть пользователей, /30, префиксы соседей) — /32 не принимается за него;
    резолвер довезёт документ."""
    env = gwcfg.join_link_env(_server_next("10.241.0.0/16, 10.242.0.7/32"))
    assert env == {"CLIENT_SUBNET": "10.8.1.0/24", "PEER_LAN_NETS": "10.241.0.0/16 10.242.0.7/32"}
    assert "RESOLVER" not in env


def test_the_keys_under_the_pairing_key_reach_the_link_and_a_stranger_cannot_read_them(joined, tmp_path):
    """sec-1: в сессии сопряжения сервер шлёт конфиг новых ключей только под
    ключом сопряжения (box). Агент со своим Kp открывает его и переходит;
    чужой ключ (держатель токена, перетянувший endpoint) — отказ, линк на
    месте."""
    import os
    from awgbot.util import joinsas
    standing = joined.link.read_text(encoding="utf-8")
    nxt = _script_next(tmp_path, standing)
    kp = os.urandom(32)
    stranger = joinsas.seal(os.urandom(32), {"conf": nxt})
    joined.__dict__["_join_kp"] = kp
    res = joined.rekey_next_in({"t": "link_next", "id": "a1b2c3d4e5f6", "box": stranger, "fp": "x", "auto": "join"})
    assert res["reply"]["state"] == "declined", res
    assert joined.link.read_text(encoding="utf-8") == standing
    res = joined.rekey_next_in({"t": "link_next", "id": "a1b2c3d4e5f7", "box": joinsas.seal(kp, {"conf": nxt}),
                                "fp": "x", "auto": "join"})
    assert res["reply"]["state"] == "ready", res
    assert joined.rekey_go({"id": "a1b2c3d4e5f7"})["ok"], joined.runs
    assert wgconf.parse(joined.link.read_text(encoding="utf-8")).get("Interface", "I1") == I1
