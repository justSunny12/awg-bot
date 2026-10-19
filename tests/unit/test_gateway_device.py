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

from tests.botscript import flat

from awgbot.core import config
from awgbot.domain import configgen
from awgbot.infra import gwguard
from awgbot.util import gwsign
from tests.conftest import SH, fake_cmd

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
    token = gwsign.PREFIX + gwsign.b64u(payload) + "." + gwsign.b64u(mac)
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


def test_client_subnet_from_conf_or_unit(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GW_CLIENT_SUBNET", "10.8.1.0/24")
    assert gwguard.client_subnet() == "10.8.1.0/24"
    monkeypatch.setattr(config, "GW_CLIENT_SUBNET", "")
    unit = tmp_path / "awg-link-gw.service"
    unit.write_text("[Service]\nEnvironment=LINK_IF=awglink\nEnvironment=CLIENT_SUBNET=10.8.1.0/24\n",
                    encoding="utf-8")
    monkeypatch.setattr(gwguard, "unit_path", lambda: unit)
    gwguard.unit._unit_cache = None
    assert gwguard.client_subnet() == "10.8.1.0/24", "подсеть приезжает в юните из бандла"
    unit.unlink()
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
    text = flat()
    fn = lambda name: re.search(rf"^{name}\(\) \{{.*?^\}}$", text, re.S | re.M).group(0)
    conf = tmp_path / "conf"; conf.mkdir()
    app = conf / "app.yaml"
    app.write_text((ROOT / "conf" / "app.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    fake_cmd(bin_dir / "sed",          # BSD sed не понимает `-i -E`, как пишет скрипт под GNU
        '#!/bin/sh\nif /usr/bin/sed --version >/dev/null 2>&1; then exec /usr/bin/sed "$@"; fi\n'
        'if [ "$1" = "-i" ] && [ "$2" = "-E" ]; then shift 2; exec /usr/bin/sed -i "" -E "$@"; fi\n'
        'exec /usr/bin/sed "$@"\n')
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


def _uplink_funcs(script: str) -> str:
    """Установка аплинка — общие функции шага 0 и режима --uplink."""
    start = script.index("uplink_render() {")
    return script[start:script.index("allow_list_path() {", start)]


def test_script_installs_uplink_only_to_the_machine_with_that_key(script):
    step0 = script.split('step "0. Шлюзовое устройство"', 1)[1].split('# ── 1. конфиг и подъём', 1)[0]
    funcs = _uplink_funcs(script)
    assert 'uplink_render "$_tmp" "$_tmp.conf"' in step0 and 'uplink_install "$_tmp.conf"' in step0, (
        "шаг 0 ставит аплинк не общими функциями")
    assert 'iface_by_pubkey "$GATEWAY_PUBKEY"' in step0 and 'iface_by_pubkey "$GATEWAY_PREV_PUBKEY"' in step0
    assert "GW_FOREIGN=1" in step0, "чужой шлюз → линк не поднимаем"
    assert '/^Table = off$/ {' in funcs, "PostUp вставляется в [Interface], а не в конец файла"
    assert "ip route replace default dev %%i table" in funcs
    assert 'if ! systemctl start "awg-quick@$UPLINK_IF"' in funcs and "откатываю на прежний" in funcs
    assert '"$AWG_QUICK" up "$UPLINK_IF"' not in funcs + step0, "аплинк снова поднимается мимо юнита awg-quick@"
    assert "cmp -s" in funcs, "неизменившийся конфиг аплинка не переподнимаем"
    assert 'printf \'GW_STATUS=%s' in step0, "решение пишется для агента"


def test_script_keeps_link_down_for_a_foreign_gateway(script):
    link = script.split('# ── 1. конфиг и подъём', 1)[1].split("# ── 1a.", 1)[0]
    assert 'if [ "$GW_FOREIGN" = "1" ]' in link
    foreign = link.split('if [ "$GW_FOREIGN" = "1" ]', 1)[1].split("fi", 1)[0]
    assert "down $LINK_IF" in foreign and "exit 0" in foreign
    assert script.index('if [ "$GW_FOREIGN" = "1" ]') < script.index('run "$AWG_QUICK up $LINK_IF"')


def test_unit_carries_gateway_env(script):
    """Ключи шлюза закрепляются в юните: по ним реассерт на загрузке узнаёт
    свою машину. Конфиг аплинка — нет (он с приватным ключом и едет только на
    время применения); это проверяет прогон блока в test_gw_unit_uplink.py."""
    for var in ("GATEWAY_PUBKEY", "GATEWAY_PREV_PUBKEY"):
        assert f"Environment={var}=${var}" in script, var


def test_uplink_postup_lands_inside_interface_section(tmp_path):
    """Прогоняем сам awk из скрипта: PostUp обязан стоять до [Peer]."""
    import subprocess as sp
    src = (ROOT / "install" / "routing-gw-setup.sh").read_text(encoding="utf-8")
    awk = src.split("awk -v mark=\"$TG_MARK\" -v tbl=\"$UPLINK_TABLE\" '", 1)[1].split("' \"$1\" > \"$2\"", 1)[0]
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
    fresh = step0.split("чистое устройство", 1)[0].rsplit("if ", 1)[1]
    assert '[ -z "$UPLINK_IF" ]' in fresh and '[ -z "$_others" ]' in fresh and '[ -n "$UPLINK_B64" ]' in fresh
    assert '! -f "$HOST_CONF_DIR/${UPLINK_IF_DEFAULT}.conf"' in fresh
    assert 'UPLINK_IF="$UPLINK_IF_DEFAULT"' in step0
    assert '_others="$(uplink_list' in step0
    assert 'case "$_i" in "$LINK_IF"|awglink*) continue ;; esac' in script, "свой линк и линк соседнего слота аплинком не считаются"
    assert re.search(r'^UPLINK_IF_DEFAULT="?\$\{UPLINK_IF_DEFAULT:-awg0\}"?|UPLINK_IF_DEFAULT=.*awg0', script, re.M)


# ── политика «Telegram → аплинк»: правило по метке + маршрут в таблице ───────

def _ip_stub(rules_json: str, routes_json: str, calls: list):

    class CP:
        def __init__(self, out=""):
            self.returncode, self.stdout, self.stderr = 0, out.encode(), b""

    def run(argv, **kw):
        a = list(argv)
        calls.append(a)
        if a[:3] == ["ip", "-j", "rule"]:
            return CP(rules_json)
        if a[:3] == ["ip", "-j", "route"]:
            return CP(routes_json)
        return CP()
    return run


def test_uplink_policy_survives_foreign_rules_with_masked_fwmark(monkeypatch):
    """Чужие правила на той же машине печатают метку с маской («0x1/0xff») или
    вовсе без неё; разбор одного такого правила не должен ронять весь тик."""
    import subprocess
    rules = ('[{"priority":100,"src":"all","fwmark":"0x2/0xff","table":"200"},'
             '{"priority":101,"src":"all","table":"main"},'
             '{"priority":102,"src":"all","fwmark":"0x1","table":"100"}]')
    monkeypatch.setattr(subprocess, "run", _ip_stub(rules, '[{"dst":"default","dev":"awg0"}]', []))
    assert gwguard.uplink_policy("awg0") == {"rule": True, "route": True}
    rules = '[{"priority":100,"src":"all","fwmark":"garbage","table":"100"}]'
    monkeypatch.setattr(subprocess, "run", _ip_stub(rules, "[]", []))
    assert gwguard.uplink_policy("awg0") == {"rule": False, "route": False}


def test_uplink_policy_detects_missing_rule_and_route_and_restores_them(monkeypatch):
    """systemd-networkd при перезапуске сносит чужие ip rule и маршруты: аплинк
    жив, метка стоит, а Telegram агента уходит локальному провайдеру. Агент
    должен увидеть пропажу и перевыставить оба — idempotent-командами ip."""
    import subprocess
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _ip_stub("[]", "[]", calls))
    assert gwguard.uplink_policy("awg0") == {"rule": False, "route": False}
    fixed = gwguard.uplink_policy_ensure("awg0")
    assert fixed == ["правило по метке", "маршрут в аплинк"]
    assert ["ip", "rule", "add", "fwmark", "1", "lookup", "100"] in calls
    assert ["ip", "route", "replace", "default", "dev", "awg0", "table", "100"] in calls

    calls.clear()
    present_rules = '[{"priority":32765,"src":"all","fwmark":"0x1","table":"100"}]'
    present_routes = '[{"dst":"default","dev":"awg0","scope":"link","flags":[]}]'
    monkeypatch.setattr(subprocess, "run", _ip_stub(present_rules, present_routes, calls))
    assert gwguard.uplink_policy("awg0") == {"rule": True, "route": True}
    assert gwguard.uplink_policy_ensure("awg0") == []
    assert not any(a[:2] in (["ip", "rule"], ["ip", "route"]) and a[2] in ("add", "replace")
                   for a in calls)
    # маршрут есть, но в другой интерфейс (ошибочно повёрнут в линк) — чинить
    wrong = '[{"dst":"default","dev":"awglink","scope":"link","flags":[]}]'
    monkeypatch.setattr(subprocess, "run", _ip_stub(present_rules, wrong, calls))
    assert gwguard.uplink_policy("awg0")["route"] is False


def test_gateway_conf_allowed_ips_carry_peer_subnets(tmp_path):
    """Чужие подсети — в AllowedIPs пира ВПС в конфиге шлюза: awg-quick сам
    ставит маршруты. Мусор из окружения отсеивается; без чужих подсетей —
    клиентская подсеть и /30 линка."""
    link = (ROOT / "install" / "routing-link-setup.sh").read_text(encoding="utf-8")
    assert "AllowedIPs = $(gw_allowed_ips)" in link
    start = link.index('PEER_LAN_NETS="$(printf')
    end = link.index("\n}\n", link.index("gw_allowed_ips() {")) + 3
    prog = link[start:end] + 'gw_allowed_ips\n'

    def _run(nets):
        r = subprocess.run([SH, "-c", prog], capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin", "CLIENT_SUBNET": "10.9.1.0/24",
                                "LINK_CIDR": "10.99.99.0/30", "PEER_LAN_NETS": nets})
        assert r.returncode == 0, r.stderr
        return r.stdout
    assert _run("192.168.1.0/24 192.168.2.0/24; rm -rf /") == \
        "10.9.1.0/24, 10.99.99.0/30, 192.168.1.0/24, 192.168.2.0/24"
    assert _run("") == "10.9.1.0/24, 10.99.99.0/30"
