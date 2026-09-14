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
    token = gwsign.sign(PRIV, "claim", PUB, host="NASPi")
    text = f"Перешли это основному боту:\n\n{token}\n\nспасибо"
    data = gwsign.verify(PRIV, text)
    assert data["act"] == "claim" and data["pub"] == PUB and data["host"] == "NASPi"
    assert gwsign.find_token(text) == token


def test_verify_tolerates_extra_fields_from_older_agents():
    """Прежние агенты подписывали ещё и "addr": подпись покрывает весь payload,
    лишний ключ проверке не мешает — шлюз на старом агенте помечается."""
    import json
    payload = json.dumps({"act": "claim", "pub": PUB, "addr": "10.9.1.15",
                          "ts": int(time.time()), "nonce": "ab" * 8, "host": "NASPi"},
                         separators=(",", ":")).encode()
    import hashlib, hmac
    mac = hmac.new(gwsign._key(PRIV), payload, hashlib.sha256).digest()[:20]
    token = gwsign.PREFIX + gwsign._b64u(payload) + "." + gwsign._b64u(mac)
    assert gwsign.verify(PRIV, token)["pub"] == PUB


def test_verify_accepts_only_claim():
    """Действие одно — claim; release-токены сняты вместе со сценарием замены,
    и подписанный «release» больше ничего не значит."""
    with pytest.raises(ValueError):
        gwsign.verify(PRIV, gwsign.sign(PRIV, "release", PUB))


def test_verify_rejects_wrong_key_tampering_and_expiry():
    token = gwsign.sign(PRIV, "claim", PUB)
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


def test_gateway_mark_outcome(tmp_path, monkeypatch):
    from awgbot.domain import gateway as gw
    from awgbot.domain.gateway import GatewayServices
    from awgbot.infra.db import Database
    db = Database(tmp_path / "gw.db"); db.init_schema()
    svc = GatewayServices(db)
    monkeypatch.setattr(gw, "pathlib_read", lambda p: "[Interface]\nPrivateKey = " + PRIV + "\n")
    monkeypatch.setattr(gwguard, "uplink_pubkey", lambda: ("awg0", PUB))
    # не помечен → claim (запасной путь); помечен другой → claim; свой → тишина
    for status, expect in (("unmarked", True), ("foreign", True), ("confirmed", False)):
        monkeypatch.setattr(gwguard, "script_status", lambda s=status: {"GW_STATUS": s})
        out = svc.gateway_mark_outcome()
        assert out["status"] == status and bool(out["claim"]) is expect, status
        assert svc.gateway_mark_status() == status
        if out["claim"]:
            assert gwsign.verify(PRIV, out["claim"])["pub"] == PUB


def test_gateway_apply_report_is_human_text(tmp_path, monkeypatch):
    from awgbot.bot import texts
    from awgbot.domain.gateway import GatewayServices
    from awgbot.infra.db import Database
    db = Database(tmp_path / "gw.db"); db.init_schema()
    svc = GatewayServices(db)
    cases = {
        ("confirmed", "installed", "up"): "Аплинк обновлён и поднят, линк поднят, шлюз подтверждён.",
        ("confirmed", "unchanged", "up"): "Аплинк без изменений, линк поднят, шлюз подтверждён.",
        ("foreign", "", "foreign"): "Линк лежит: в основном боте назначен другой шлюз.",
        ("unmarked", "", ""): "Шлюз в основном боте не назначен.",
    }
    for (gs, up, link), expect in cases.items():
        st = {k: v for k, v in (("GW_STATUS", gs), ("UPLINK", up), ("LINK", link)) if v}
        assert texts.gateway_apply_report(st) == expect, st
        monkeypatch.setattr(gwguard, "script_status", lambda s=st: s)
        assert svc.gateway_apply_report() == expect
    assert texts.gateway_apply_report({}) == "", "нет статуса — нет отчёта, останется хвост"


def test_client_subnet_from_conf_or_unit(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GW_CLIENT_SUBNET", "10.8.1.0/24")
    assert gwguard.client_subnet() == "10.8.1.0/24"
    monkeypatch.setattr(config, "GW_CLIENT_SUBNET", "")
    monkeypatch.setattr(config, "GW_UNIT", "awg-link-gw.service")
    real = Path.read_text

    def fake_read(self, *a, **k):
        if str(self) == "/etc/systemd/system/awg-link-gw.service":
            return "[Service]\nEnvironment=LINK_IF=awglink\nEnvironment=CLIENT_SUBNET=10.8.1.0/24\n"
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", fake_read)
    assert gwguard.client_subnet() == "10.8.1.0/24", "подсеть приезжает в юните из бандла"
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (_ for _ in ()).throw(OSError()))
    assert gwguard.client_subnet() == ""


def test_link_script_rekey_mode_regenerates_keys():
    text = (ROOT / "install" / "routing-link-setup.sh").read_text(encoding="utf-8")
    assert re.search(r"--rekey\)\s+MODE=\"apply\"; REKEY=1", text)
    block = text.split('if [ "$REKEY" = "1" ] && [ -f "$CONF" ]; then', 1)[1].split("fi", 1)[0]
    assert "awg-quick down $LINK_IF" in block and "rm -f $CONF" in block
    assert text.index('if [ "$REKEY" = "1" ]') < text.index('if [ -f "$CONF" ] && [ "$MODE" = "apply" ]'), \
        "снятие конфига — раньше проверки «уже настроено», иначе rekey выходит ни с чем"


def test_installer_gateway_role_rewrites_the_template_without_questions(tmp_path):
    """configure_gateway на шаблоне conf/app.yaml: роль и секция gateway
    раскомментированы, runtime — host, клиентскую подсеть не спрашивает и не
    пишет (она приезжает в юните обвязки из бандла). Вопросов нет вовсе:
    stdin закрыт, любой read упал бы."""
    text = (ROOT / "awg-bot.sh").read_text(encoding="utf-8")
    fn = lambda name: re.search(rf"^{name}\(\) \{{.*?^\}}$", text, re.S | re.M).group(0)
    conf = tmp_path / "conf"; conf.mkdir()
    app = conf / "app.yaml"
    app.write_text((ROOT / "conf" / "app.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    (bin_dir / "sed").write_text(          # BSD sed не понимает `-i -E`, как пишет скрипт под GNU
        '#!/bin/sh\nif /usr/bin/sed --version >/dev/null 2>&1; then exec /usr/bin/sed "$@"; fi\n'
        'if [ "$1" = "-i" ] && [ "$2" = "-E" ]; then shift 2; exec /usr/bin/sed -i "" -E "$@"; fi\n'
        'exec /usr/bin/sed "$@"\n', encoding="utf-8")
    (bin_dir / "sed").chmod(0o755)
    prog = "\n".join([
        "set -e", 'ok(){ :; }', 'die(){ echo "$*" >&2; exit 1; }',
        f'CONF_DIR="{conf}"', fn("yaml_set"), fn("configure_gateway"), "configure_gateway",
    ])
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       env={"PATH": f"{bin_dir}:/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    out = app.read_text(encoding="utf-8")
    assert re.search(r'^role: "gateway"$', out, re.M)
    assert re.search(r'^gateway:$', out, re.M) and re.search(r'^  link_interface: "awglink"', out, re.M)
    assert re.search(r'^  client_subnet: ""', out, re.M), "подсеть приезжает в юните из бандла"
    assert re.search(r'^  runtime: "host"', out, re.M)


# ── скрипт обвязки и шапка бандла ────────────────────────────────────────────

@pytest.fixture(scope="module")
def script() -> str:
    return (ROOT / "install" / "routing-gw-setup.sh").read_text(encoding="utf-8")


def test_script_installs_uplink_only_to_the_machine_with_that_key(script):
    step0 = script.split('step "0. Шлюзовое устройство"', 1)[1].split('# ── 1. конфиг и подъём', 1)[0]
    assert 'iface_by_pubkey "$GATEWAY_PUBKEY"' in step0 and 'iface_by_pubkey "$GATEWAY_PREV_PUBKEY"' in step0
    assert "GW_FOREIGN=1" in step0, "чужой шлюз → линк не поднимаем"
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


def test_script_installs_uplink_on_a_fresh_machine(script):
    """Новая машина: аплинков нет — ставим из бандла под именем по умолчанию;
    машина с чужим аплинком в эту ветку не попадает."""
    step0 = script.split('step "0. Шлюзовое устройство"', 1)[1].split('# ── 1. конфиг и подъём', 1)[0]
    fresh = step0.split("чистая машина", 1)[0].rsplit("if ", 1)[1]
    assert '[ -z "$UPLINK_IF" ]' in fresh and '[ -z "$_others" ]' in fresh and '[ -n "$UPLINK_B64" ]' in fresh
    assert '! -f "$HOST_CONF_DIR/${UPLINK_IF_DEFAULT}.conf"' in fresh
    assert 'UPLINK_IF="$UPLINK_IF_DEFAULT"' in step0
    assert 'grep -vx "$LINK_IF"' in step0, "свой линк не считается чужим аплинком"
    assert re.search(r'^UPLINK_IF_DEFAULT="?\$\{UPLINK_IF_DEFAULT:-awg0\}"?|UPLINK_IF_DEFAULT=.*awg0', script, re.M)
