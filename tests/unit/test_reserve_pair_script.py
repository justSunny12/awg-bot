"""install/reserve-pair-setup.sh — линк пары серверов AWG (awgpair).

Скрипт гоняется целиком под sh: `id`, `awg`, `awg-quick`, `ip`, `systemctl`,
`install` — подставные команды с журналом; поднятость линка — файл-флаг,
который ведут подставные `awg-quick` и `ip link show`. Конфиг, юнит и копия
скрипта — во временном каталоге (CONF_DIR, UNIT, SELF_PATH). Ключи —
заглушки DUMMY… по счётчику, вида ключа WireGuard (44 символа base64): иное
скрипт не пишет в конфиг.

Цена ошибки: конфиг пары с `Table = off` или с широким AllowedIPs —
пересылка через линк пары (второй сервер ходит к моим пользователям); штамп не
снят после кода — файервол держит карантин и канал пары мёртв; коммит новых
ключей без возврата прежних при отказе — линк пары на ключах, которых нет у
второй стороны; откат, оставивший юнит, — линк пары поднимается после
перезагрузки у сервера без пары.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import SH, fake_cmd

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "install" / "reserve-pair-setup.sh"
PID = "0123456789abcdef"
OBF = {"JC": "5", "JMIN": "10", "JMAX": "50", "S1": "20", "S2": "30", "S3": "40", "S4": "50",
       "H1": "100-200", "H2": "600000000-600100000", "H3": "1100000000-1100100000",
       "H4": "1600000000-1600100000"}


def _k(name: str) -> str:
    """Заглушка ключа вида WireGuard: DUMMY<имя>, добитая до 43 символов, и «=»."""
    return f"DUMMY{name}".ljust(43, "A") + "="


def _pub_of(priv: str) -> str:
    """Что «выводит» подставной `awg pubkey` для ключа."""
    return "DUMMYPUB" + priv[5:40] + "="


def _kv(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


class _Host:
    """Сервер для скрипта линка пары. `up_fail(n)` — столько первых
    `awg-quick up` упадут."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.bin = tmp / "bin"; self.bin.mkdir()
        self.conf_dir = tmp / "amneziawg"; self.conf_dir.mkdir()
        self.conf = self.conf_dir / "awgpair.conf"
        self.unit = tmp / "awg-pair.service"
        self.self_path = tmp / "sbin" / "reserve-pair-setup.sh"
        self.log = tmp / "calls.log"; self.log.write_text("", encoding="utf-8")
        self.up = tmp / "link.up"
        n = tmp / "keyno"; n.write_text("0", encoding="utf-8")
        self._fails = tmp / "up.fail"; self._fails.write_text("0", encoding="utf-8")
        fake_cmd(self.bin / "id", "echo 0\n")
        fake_cmd(self.bin / "awg",
                 f'echo "awg $*" >> {self.log}\n'
                 f'case "$1" in\n'
                 f'  genkey|genpsk) i=$(( $(cat {n}) + 1 )); echo $i > {n}; k="DUMMY$1$i"; '
                 f'while [ ${{#k}} -lt 43 ]; do k="${{k}}A"; done; echo "$k=" ;;\n'
                 f'  pubkey) read k; echo "DUMMYPUB$(printf %s "$k" | cut -c6-40)=" ;;\n'
                 f'esac\nexit 0\n')
        fake_cmd(self.bin / "awg-quick",
                 f'echo "awg-quick $*" >> {self.log}\n'
                 f'if [ "$1" = up ]; then f=$(cat {self._fails}); '
                 f'if [ "$f" -gt 0 ]; then echo $((f - 1)) > {self._fails}; exit 1; fi; touch {self.up}; fi\n'
                 f'if [ "$1" = down ]; then rm -f {self.up}; fi\nexit 0\n')
        fake_cmd(self.bin / "ip", f'echo "ip $*" >> {self.log}\n'
                                  f'if [ "$1 $2" = "link show" ]; then [ -f {self.up} ]; exit $?; fi\nexit 0\n')
        fake_cmd(self.bin / "systemctl", f'echo "systemctl $*" >> {self.log}\nexit 0\n')
        fake_cmd(self.bin / "install", f'echo "install $*" >> {self.log}\nexit 0\n')

    def up_fail(self, n: int) -> None:
        self._fails.write_text(str(n), encoding="utf-8")

    def run(self, mode: str, **env) -> subprocess.CompletedProcess:
        base = {"PATH": f"{self.bin}:/usr/bin:/bin", "PAIR_IF": "awgpair", "PAIR_CIDR": "10.99.98.0/30",
                "PAIR_PORT": "47101", "CONF_DIR": str(self.conf_dir), "UNIT": str(self.unit),
                "SELF_PATH": str(self.self_path), "_APP_YAML": str(self.tmp / "нет.yaml")}
        base.update({k: str(v) for k, v in env.items()})
        self.log.write_text("", encoding="utf-8")
        return subprocess.run([SH, str(SCRIPT), mode], capture_output=True, text=True, env=base, timeout=30)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def p(self, suffix: str) -> Path:
        return self.conf.with_name(self.conf.name + suffix)

    def text(self, suffix: str = "") -> str:
        return self.p(suffix).read_text(encoding="utf-8")


@pytest.fixture()
def host(tmp_path):
    return _Host(tmp_path)


def _stage(host) -> dict:
    r = host.run("--stage", PAIR_ID=PID)
    assert r.returncode == 0, r.stdout + r.stderr
    return _kv(r.stdout)


def _join(host, **over) -> subprocess.CompletedProcess:
    env = {"PAIR_ID": PID, "PAIR_PRIV": _k("ACCPRIV"), "PAIR_PEER_PUB": _k("INVPUB"),
           "PAIR_ENDPOINT": "vpn-a.example.org:47101", **OBF}
    env.update(over)
    return host.run("--join", **env)


def _peer_block(text: str) -> str:
    return text.split("[Peer]", 1)[1]


# ── --stage: пригласивший ────────────────────────────────────────────────────

def test_stage_prints_both_keypairs_and_the_obfuscation_for_the_invitation(host):
    kv = _stage(host)
    for k in ("PAIR_VPS_PRIV", "PAIR_VPS_PUB", "PAIR_PEER_PRIV", "PAIR_PEER_PUB", "JC", "JMIN", "JMAX",
              "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4"):
        assert kv.get(k), f"бот не соберёт приглашение: нет {k} в выводе"
    assert "PAIR_PSK" not in kv, "PSK до кода — его не должно быть вовсе"
    assert kv["PAIR_VPS_PUB"] == _pub_of(kv['PAIR_VPS_PRIV'])
    assert int(kv["S2"]) != int(kv["S1"]) + 56, "S2 = S1 + 56 — сигнатура обфускации"
    lows = [int(kv[f"H{i}"].split("-")[0]) for i in range(1, 5)]
    highs = [int(kv[f"H{i}"].split("-")[1]) for i in range(1, 5)]
    assert all(lo < hi for lo, hi in zip(lows, highs)) and all(h < lo for h, lo in zip(highs, lows[1:])), \
        f"диапазоны H пересекаются: {kv['H1']} {kv['H2']} {kv['H3']} {kv['H4']}"


def test_stage_writes_a_stamped_quarantined_config_without_psk_and_without_forwarding(host):
    kv = _stage(host)
    text = host.text()
    assert text.splitlines()[1] == f"# awg-bot: пара {PID}", "нет штампа — файервол не узнает карантин"
    conf = _kv(text)
    assert conf["Address"] == "10.99.98.2/30", "пригласивший — второй адрес /30"
    assert conf["ListenPort"] == "47101" and conf["PrivateKey"] == kv["PAIR_VPS_PRIV"]
    peer = _peer_block(text)
    assert f"PublicKey = {kv['PAIR_PEER_PUB']}" in peer
    assert "PresharedKey" not in peer and "Endpoint" not in peer
    assert re.search(r"(?m)^AllowedIPs = 10\.99\.98\.1/32$", peer), "AllowedIPs шире адреса второй стороны"
    assert "Table" not in text, "Table = off — конфиг пары опознался бы линком слота"
    assert "PersistentKeepalive = 25-35" in peer, "без keepalive пригласившего вторая сторона его не видит"
    assert host.conf.stat().st_mode & 0o777 == 0o600, "закрытый ключ читается не только root"


def test_stage_brings_the_link_up_and_installs_the_unit(host):
    _stage(host)
    c = host.calls()
    assert "awg-quick up awgpair" in c, c
    unit = host.unit.read_text(encoding="utf-8")
    assert f"ExecStart={host.self_path} --reassert" in unit and f"ExecStop={host.self_path} --down" in unit
    assert "Environment=PAIR_IF=awgpair" in unit
    assert "systemctl enable awg-pair.service" in c and "systemctl daemon-reload" in c, c


def test_stage_without_an_invitation_id_refuses_and_writes_nothing(host):
    r = host.run("--stage")
    assert r.returncode != 0 and "ОШИБКА: нет PAIR_ID" in r.stdout
    assert not host.conf.exists() and not host.unit.exists()


def test_stage_that_cannot_bring_the_link_up_fails_loudly(host):
    host.up_fail(1)
    r = host.run("--stage", PAIR_ID=PID)
    assert r.returncode != 0 and "ОШИБКА: линк пары не поднялся" in r.stdout, r.stdout
    assert "PAIR_PEER_PRIV" not in r.stdout, "ключи напечатаны при отказе — бот собрал бы приглашение"


# ── --join: принявший ────────────────────────────────────────────────────────

def test_join_writes_the_invitation_keys_and_the_endpoint(host):
    r = _join(host)
    assert r.returncode == 0 and r.stdout.strip().endswith("join: ok"), r.stdout + r.stderr
    text = host.text()
    assert text.splitlines()[1] == f"# awg-bot: пара {PID}"
    conf = _kv(text)
    assert conf["Address"] == "10.99.98.1/30", "принявший — первый адрес /30"
    assert conf["PrivateKey"] == _k("ACCPRIV")
    peer = _peer_block(text)
    assert f"PublicKey = {_k('INVPUB')}" in peer and "Endpoint = vpn-a.example.org:47101" in peer
    assert re.search(r"(?m)^AllowedIPs = 10\.99\.98\.2/32$", peer)
    assert all(f"{k[0] + k[1:].lower() if k.startswith('J') else k} = {v}" in text for k, v in OBF.items()), \
        "обфускация из приглашения не легла в конфиг — линк не встанет"
    assert "awg-quick up awgpair" in host.calls()


@pytest.mark.parametrize("missing", ["PAIR_PRIV", "PAIR_ENDPOINT", "H4"])
def test_join_without_what_the_invitation_carries_refuses(host, missing):
    r = _join(host, **{missing: ""})
    assert r.returncode != 0 and "ОШИБКА" in r.stdout, r.stdout
    assert not host.conf.exists(), "конфиг записан без ключей или обфускации"


# ── смена ключей после кода ─────────────────────────────────────────────────

def test_rekey_stage_leaves_the_live_link_alone_and_prints_the_psk(host):
    _stage(host)
    live = host.text()
    r = host.run("--rekey-stage")
    assert r.returncode == 0 and r.stdout.strip().endswith("rekey: staged"), r.stdout
    kv = _kv(r.stdout)
    assert kv.get("PAIR_PSK"), "PSK не напечатан — принявшему нечего прислать"
    assert host.text() == live, "живой конфиг тронут стадией"
    nxt = host.text(".next")
    assert f"PresharedKey = {kv['PAIR_PSK']}" in nxt and "# awg-bot: пара" not in nxt, "в .next штамп карантина"
    assert _kv(nxt)["Address"] == "10.99.98.2/30" and "AllowedIPs = 10.99.98.1/32" in nxt
    assert not [c for c in host.calls() if c.startswith("awg-quick")], host.calls()


def test_rekey_commit_switches_to_the_new_keys_and_drops_the_stamp(host):
    _stage(host)
    host.run("--rekey-stage")
    nxt = host.text(".next")
    r = host.run("--rekey-commit")
    assert r.returncode == 0 and r.stdout.strip().endswith("rekey: committed"), r.stdout
    assert host.text() == nxt and not host.p(".next").exists()
    assert "# awg-bot: пара" in host.text(".prev"), "прежний конфиг не сохранён для возврата"
    assert "awg-quick up awgpair" in host.calls()


def test_rekey_commit_that_cannot_come_up_returns_the_previous_keys(host):
    _stage(host)
    live = host.text()
    host.run("--rekey-stage")
    host.up_fail(1)
    r = host.run("--rekey-commit")
    assert r.returncode != 0 and "прежние возвращены" in r.stdout, r.stdout
    assert host.text() == live, "на диске остались ключи, которых у второй стороны нет"
    assert host.up.exists(), "прежний линк не поднят обратно"


def test_rekey_revert_and_drop(host):
    _stage(host)
    live = host.text()
    host.run("--rekey-stage")
    host.run("--rekey-commit")
    r = host.run("--rekey-revert")
    assert r.returncode == 0 and host.text() == live and not host.p(".prev").exists()
    host.run("--rekey-stage")
    host.run("--rekey-drop")
    assert not host.p(".next").exists() and not host.p(".prev").exists()


def test_set_peer_puts_the_new_keys_and_psk_and_keeps_the_endpoint(host):
    _join(host)
    r = host.run("--set-peer", PAIR_PRIV=_k("ACCNEW"), PAIR_PEER_PUB=_k("INVNEW"), PAIR_PSK=_k("PSK"),
                 **{**OBF, "JC": "7"})
    assert r.returncode == 0 and r.stdout.strip().endswith("peer: set"), r.stdout
    text = host.text()
    assert "# awg-bot: пара" not in text, "штамп карантина остался — канал пары закрыт файерволом"
    conf = _kv(text)
    assert (conf["PrivateKey"], conf["PublicKey"], conf["PresharedKey"]) == (_k("ACCNEW"), _k("INVNEW"),
                                                                             _k("PSK"))
    assert conf["Endpoint"] == "vpn-a.example.org:47101" and conf["Address"] == "10.99.98.1/30"
    assert conf["Jc"] == "7", "новая обфускация не легла"
    assert not host.p(".prev").exists()


def test_set_peer_that_cannot_come_up_returns_the_invitation_keys(host):
    _join(host)
    live = host.text()
    host.up_fail(1)
    r = host.run("--set-peer", PAIR_PRIV=_k("ACCNEW"), PAIR_PEER_PUB=_k("INVNEW"), PAIR_PSK=_k("PSK"), **OBF)
    assert r.returncode != 0 and "прежние возвращены" in r.stdout
    assert host.text() == live


_INJECT = "\nPostUp = touch /tmp/awg-pair-injected"


@pytest.mark.parametrize("field", ["PAIR_PRIV", "PAIR_PEER_PUB", "PAIR_PSK", "H1", "JC"])
def test_set_peer_refuses_a_value_that_would_become_a_config_line(host, field):
    """Значения `--set-peer` — от второго сервера (pair_ok): перевод строки в
    любом из них стал бы своей строкой awgpair.conf, `PostUp` — командой от
    root при подъёме линка. Отказ — до записи и до .prev."""
    _join(host)
    live = host.text()
    env = {"PAIR_PRIV": _k("ACCNEW"), "PAIR_PEER_PUB": _k("INVNEW"), "PAIR_PSK": _k("PSK"), **OBF}
    env[field] = env[field].rstrip("=") + _INJECT if field.startswith("PAIR") else env[field] + _INJECT
    r = host.run("--set-peer", **env)
    assert r.returncode != 0 and "не того вида" in r.stdout, r.stdout + r.stderr
    assert host.text() == live and "PostUp" not in host.text()
    assert not host.p(".prev").exists()
    assert not [c for c in host.calls() if c.startswith("awg-quick")], "линк переподнимали с отвергнутыми значениями"


@pytest.mark.parametrize("field,value", [("PAIR_ENDPOINT", "vpn-a.example.org:47101" + _INJECT),
                                         ("PAIR_PRIV", "DUMMY-short="), ("S3", "20 30")])
def test_join_refuses_values_of_the_wrong_shape(host, field, value):
    env = {"PAIR_ID": PID, "PAIR_PRIV": _k("ACCPRIV"), "PAIR_PEER_PUB": _k("INVPUB"),
           "PAIR_ENDPOINT": "vpn-a.example.org:47101", **OBF, field: value}
    r = host.run("--join", **env)
    assert r.returncode != 0 and "не того вида" in r.stdout, r.stdout
    assert not host.conf.exists(), "конфиг записан с отвергнутым значением"


# ── снятие, подъём, состояние ───────────────────────────────────────────────

def test_rollback_removes_the_config_the_leftovers_and_the_unit(host):
    _stage(host)
    host.run("--rekey-stage")
    r = host.run("--rollback")
    assert r.returncode == 0 and r.stdout.strip().endswith("rollback: ok")
    assert not any(host.conf_dir.iterdir()), f"остались файлы: {list(host.conf_dir.iterdir())}"
    assert not host.unit.exists(), "юнит остался — линк поднимется после перезагрузки"
    c = host.calls()
    assert "systemctl disable --now awg-pair.service" in c and "awg-quick down awgpair" in c, c


def test_rollback_twice_is_harmless(host):
    _stage(host)
    host.run("--rollback")
    r = host.run("--rollback")
    assert r.returncode == 0, r.stdout + r.stderr


def test_adopt_brings_up_a_restored_config_and_refuses_without_one(host):
    r = host.run("--adopt")
    assert r.returncode != 0 and "ОШИБКА: конфига линка пары нет" in r.stdout
    assert not host.unit.exists()
    _join(host)
    host.unit.unlink()
    (host.tmp / "link.up").unlink()
    conf = host.text()
    r = host.run("--adopt")
    assert r.returncode == 0 and host.unit.exists() and host.up.exists(), r.stdout
    assert host.text() == conf, "ключи из копии тронуты"


def test_reassert_without_a_config_is_a_quiet_no_op(host):
    r = host.run("--reassert")
    assert r.returncode == 0 and not [c for c in host.calls() if c.startswith("awg-quick")]


def test_status_reports_the_stamp_and_the_link(host):
    assert host.run("--status").stdout.split() == ["conf=absent", "link=down"]
    _stage(host)
    assert host.run("--status").stdout.split() == ["conf=present", f"stamp={PID}", "link=up"]


def test_an_unknown_mode_prints_usage(host):
    r = host.run("--bogus")
    assert r.returncode == 2 and "использование" in r.stdout
