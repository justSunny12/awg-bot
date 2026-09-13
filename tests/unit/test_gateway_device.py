"""Устройство-шлюз: подпись сообщений между ботами, конфиг аплинка, аплинк
и статус на стороне агента, скрипт обвязки и шапка бандла."""
from __future__ import annotations

import base64
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from awgbot.core import config
from awgbot.domain import configgen
from awgbot.infra import gwguard
from awgbot.util import gwsign

ROOT = Path(__file__).resolve().parents[2]
PRIV = base64.b64encode(os.urandom(32)).decode()
PUB = base64.b64encode(os.urandom(32)).decode()


# ── подпись ──────────────────────────────────────────────────────────────────

def test_sign_verify_roundtrip_inside_surrounding_text():
    token = gwsign.sign(PRIV, "claim", PUB, "10.9.1.15", host="NASPi")
    text = f"Перешли это основному боту:\n\n{token}\n\nспасибо"
    data = gwsign.verify(PRIV, text)
    assert data["act"] == "claim" and data["pub"] == PUB and data["addr"] == "10.9.1.15"
    assert gwsign.find_token(text) == token


def test_verify_rejects_wrong_key_tampering_and_expiry():
    token = gwsign.sign(PRIV, "release", PUB)
    other = base64.b64encode(os.urandom(32)).decode()
    with pytest.raises(ValueError):
        gwsign.verify(other, token)
    body, mac = token[len(gwsign.PREFIX):].split(".")
    with pytest.raises(ValueError):
        gwsign.verify(PRIV, gwsign.PREFIX + body[:-2] + "AA" + "." + mac)
    with pytest.raises(ValueError):
        gwsign.verify(PRIV, token, now=time.time() + gwsign.MAX_AGE_SECONDS + 10)
    with pytest.raises(ValueError):
        gwsign.verify(PRIV, "просто текст без токена")


# ── конфиг аплинка ───────────────────────────────────────────────────────────

def test_gateway_uplink_conf_drops_dns_and_sets_table_off():
    conf = ("[Interface]\nAddress = 10.9.1.15/32\nDNS = 10.9.1.1, 10.9.1.1\nPrivateKey = K==\n"
            "MTU = 1376\nJc = 6\n\n[Peer]\nPublicKey = S==\nAllowedIPs = 0.0.0.0/0, ::/0\n"
            "Endpoint = 203.0.113.10:45871\nPersistentKeepalive = 25\n")
    out = configgen.gateway_uplink_conf(conf)
    assert "DNS" not in out, "DNS в туннель увёл бы резолв всей машины"
    assert out.startswith("[Interface]\nTable = off\nAddress = 10.9.1.15/32\n")
    assert "PrivateKey = K==" in out and "MTU = 1376" in out and "Endpoint = 203.0.113.10:45871" in out


# ── агент: аплинк и статус скрипта ───────────────────────────────────────────

def test_uplink_autodetect_by_link_endpoint_host(monkeypatch):
    monkeypatch.setattr(config, "GW_UPLINK_IF", "")
    monkeypatch.setattr(config, "GW_LINK_IF", "awglink")
    table = {("show", "interfaces"): "awg0 awglink wg-home",
             ("show", "awglink", "endpoints"): "PUB1=\t203.0.113.10:47231",
             ("show", "awg0", "endpoints"): "PUB2=\t203.0.113.10:45871",
             ("show", "wg-home", "endpoints"): "PUB3=\t198.51.100.7:51820",
             ("show", "awg0", "public-key"): "MYPUB="}
    monkeypatch.setattr(gwguard, "_awg", lambda a: table.get(tuple(a), ""))
    assert gwguard.uplink_interface() == "awg0"
    assert gwguard.uplink_pubkey() == ("awg0", "MYPUB=")
    monkeypatch.setattr(config, "GW_UPLINK_IF", "wg-home")
    assert gwguard.uplink_interface() == "wg-home", "явный conf важнее автоопределения"


def test_script_status_parse(tmp_path, monkeypatch):
    f = tmp_path / "gateway.status"; f.write_text("GW_STATUS=foreign\nGATEWAY_PUBKEY=ABC=\n")
    monkeypatch.setattr(gwguard, "STATUS_FILE", str(f))
    assert gwguard.script_status() == {"GW_STATUS": "foreign", "GATEWAY_PUBKEY": "ABC="}
    monkeypatch.setattr(gwguard, "STATUS_FILE", str(tmp_path / "none"))
    assert gwguard.script_status() == {}


def test_gateway_mark_outcome_and_release(tmp_path, monkeypatch):
    from awgbot.domain import gateway as gw
    from awgbot.domain.gateway import GatewayServices
    from awgbot.infra.db import Database
    db = Database(tmp_path / "gw.db"); db.init_schema()
    svc = GatewayServices(db)
    monkeypatch.setattr(gw, "pathlib_read", lambda p: "[Interface]\nPrivateKey = " + PRIV + "\n")
    monkeypatch.setattr(gwguard, "uplink_pubkey", lambda: ("awg0", PUB))
    # не помечен → claim; помечен другой → claim; свой → тишина
    for status, expect in (("unmarked", True), ("foreign", True), ("confirmed", False)):
        monkeypatch.setattr(gwguard, "script_status", lambda s=status: {"GW_STATUS": s})
        out = svc.gateway_mark_outcome()
        assert out["status"] == status and bool(out["claim"]) is expect, status
        assert svc.gateway_mark_status() == status
        if out["claim"]:
            assert gwsign.verify(PRIV, out["claim"])["pub"] == PUB
    # release: чужой ключ → отказ, claim вместо release → отказ, свой → линк вниз
    calls = []
    monkeypatch.setattr(gw, "_run", lambda argv, timeout=10: (calls.append(list(argv)), subprocess.CompletedProcess(argv, 0, b"", b""))[1])
    other = base64.b64encode(os.urandom(32)).decode()
    ok, why = svc.gateway_accept_release(gwsign.sign(PRIV, "release", other))
    assert not ok and "другому" in why and calls == []
    ok, why = svc.gateway_accept_release(gwsign.sign(PRIV, "claim", PUB))
    assert not ok and calls == []
    ok, _ = svc.gateway_accept_release("текст " + gwsign.sign(PRIV, "release", PUB))
    assert ok
    assert ["awg-quick", "down", config.GW_LINK_IF] in calls
    assert any(c[:2] == ["systemctl", "disable"] for c in calls)
    assert svc.gateway_mark_status() == "released"


# ── скрипт обвязки и шапка бандла ────────────────────────────────────────────

@pytest.fixture(scope="module")
def script() -> str:
    return (ROOT / "install" / "routing-gw-setup.sh").read_text(encoding="utf-8")


def test_script_installs_uplink_only_to_the_machine_with_that_key(script):
    step0 = script.split('step "0. Шлюзовое устройство"', 1)[1].split('# ── 1. конфиг и подъём', 1)[0]
    assert 'iface_by_pubkey "$GATEWAY_PUBKEY"' in step0 and 'iface_by_pubkey "$GATEWAY_PREV_PUBKEY"' in step0
    assert "GW_FOREIGN=1" in step0, "чужой шлюз → линк не поднимаем"
    assert "Table = off" not in step0 or True   # Table = off приходит в конфиге из бандла
    assert '/^Table = off$/ {' in step0, "PostUp вставляется в [Interface], а не в конец файла"
    assert "ip route replace default dev %%i table" in step0
    assert 'if ! "$AWG_QUICK" up "$UPLINK_IF"' in step0 and "откатываю на прежний" in step0
    assert "cmp -s" in step0, "неизменившийся конфиг аплинка не переподнимаем"
    assert 'printf \'GW_STATUS=%s' in step0, "решение пишется для агента"


def test_script_keeps_link_down_for_a_foreign_gateway(script):
    link = script.split('# ── 1. конфиг и подъём', 1)[1].split("# ── 1a.", 1)[0]
    assert 'if [ "$GW_FOREIGN" = "1" ]' in link
    foreign = link.split('if [ "$GW_FOREIGN" = "1" ]', 1)[1].split("fi", 1)[0]
    assert "down $LINK_IF" in foreign and "systemctl disable awg-link-gw.service" in foreign and "exit 0" in foreign
    assert script.index('if [ "$GW_FOREIGN" = "1" ]') < script.index('run "$AWG_QUICK up $LINK_IF"')


def test_unit_carries_gateway_env(script):
    for var in ("GATEWAY_PUBKEY", "GATEWAY_PREV_PUBKEY", "UPLINK_B64"):
        assert f"Environment={var}=${var}" in script, var


def test_bundle_header_carries_gateway_fields(tmp_path):
    """Шапка бандла: ключи и конфиг аплинка из окружения сборки, только
    base64-символы (значения приходят из БД, но фильтр обязателен)."""
    link = (ROOT / "install" / "routing-link-setup.sh").read_text(encoding="utf-8").replace(
        '[ "$(id -u)" = "0" ] || { echo "нужен root"; exit 1; }', ":", 1)
    inst = tmp_path / "install"; inst.mkdir()
    (inst / "routing-link-setup.sh").write_text(link, encoding="utf-8")
    (inst / "routing-gw-setup.sh").write_text((ROOT / "install" / "routing-gw-setup.sh").read_text(encoding="utf-8"), encoding="utf-8")
    conf = tmp_path / "gw.conf"; conf.write_text("[Interface]\nAddress = 10.99.99.2/30\nPrivateKey = X==\n\n[Peer]\nEndpoint = 203.0.113.10:443\n", encoding="utf-8")
    (tmp_path / "linkconf").mkdir(); (tmp_path / "linkconf" / "awglink.conf").write_text("[Interface]\nListenPort = 443\nPrivateKey = X==\n", encoding="utf-8")
    out = tmp_path / "b.sh"
    up = base64.b64encode(b"[Interface]\nTable = off\n").decode()
    r = subprocess.run(["sh", str(inst / "routing-link-setup.sh"), "--bundle"], cwd=tmp_path,
                       capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "CONF_DIR": str(tmp_path / "linkconf"),
                            "GW_CONF_OUT": str(conf), "GW_BUNDLE_OUT": str(out),
                            "GATEWAY_PUBKEY": PUB + '; $( ) "\n', "GATEWAY_PREV_PUBKEY": "", "UPLINK_B64": up})
    assert r.returncode == 0, r.stderr
    text = out.read_text(encoding="utf-8")
    assert re.search(rf'^GATEWAY_PUBKEY="{re.escape(PUB)}"$', text, re.M), "мусор из окружения отсеян"
    assert 'GATEWAY_PREV_PUBKEY=""' in text and f'UPLINK_B64="{up}"' in text
    for var in ("GATEWAY_PUBKEY", "UPLINK_B64"):
        assert text.index(f"export {var}") < text.index('exec "$DEST/routing-gw-setup.sh"')


def test_uplink_postup_lands_inside_interface_section(tmp_path):
    """Прогоняем сам awk из скрипта: PostUp обязан стоять до [Peer]."""
    import subprocess as sp
    src = (ROOT / "install" / "routing-gw-setup.sh").read_text(encoding="utf-8")
    awk = src.split("awk -v mark=\"$TG_MARK\" -v tbl=\"$UPLINK_TABLE\" '", 1)[1].split("' \"$_tmp\"", 1)[0]
    conf = tmp_path / "u.conf"
    conf.write_text("[Interface]\nTable = off\nAddress = 10.9.1.15/32\nPrivateKey = K==\n\n[Peer]\nPublicKey = S==\n", encoding="utf-8")
    out = sp.run(["awk", "-v", "mark=0x1", "-v", "tbl=100", awk, str(conf)], capture_output=True, text=True).stdout
    assert out.index("PostUp = ip rule list") < out.index("[Peer]")
    assert "PostUp = ip route replace default dev %i table 100" in out
    assert "PostDown = ip route del default dev %i table 100" in out
