"""
`routing-gw-setup.sh --join <конфиг>` на шлюзе (п.8 шапки скрипта): линк по
команде подключения — только линк и
карантинная таблица inet awg_gw_join; ни аплинка, ни транзита, ни юнита.
Полное применение после сопряжения — обычным --apply: оно снимает карантин.

Конфиг линка строит `awg-bot join` (tools/join.py) из токена: ключи и
обфускация, AllowedIPs — только /30 линка. Проверка скрипта (wg_conf_check)
принимает его видом join: подсеть клиентов шлюз узнает из документа после
кода.

Цена ошибки: линк сопряжения, через который ходят пакеты в сеть шлюза или
из неё до верного кода; конфиг по токену, который проверка отвергает, —
подключение не встаёт никогда; аплинк, тронутый до кода.

Блок режима прогоняется под sh из настоящего скрипта; nft, awg-quick, ip —
подставные с журналом вызовов.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awgbot.util import jointoken, wgconf
from tests.conftest import SH, fake_cmd
from tests.unit._gw_setup_common import _helpers

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "install" / "routing-gw-setup.sh"


def _token() -> jointoken.JoinToken:
    return jointoken.JoinToken(slot=2, version="3.4.0", join_id="0123456789abcdef", expires=1_800_000_000,
                               host="vpn-server.example.org", link_port=8443, channel_port=8787,
                               gw_addr="10.99.99.6", vps_pub=bytes(32), gw_priv=bytes(range(32)), jc=4, jmin=40,
                               jmax=70, s=(10, 20, 0, 0), h=((100, 200), (300, 400), (500, 600), (700, 800)))


def _link_conf() -> str:
    from tools import join as tjoin
    return tjoin._link_conf(_token())


def _core(script: str) -> str:
    start = script.index("allow_list_path() {")
    return script[start:script.index('if [ "$MODE" = "link-apply" ]; then')]


def _check(tmp_path, text: str, kind: str, **env) -> subprocess.CompletedProcess:
    conf = tmp_path / "c.conf"; conf.write_text(text, encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    allow = wgconf.ALLOW_LIST
    prog = (_core(script) + f'\nallow_list_path() {{ printf "%s" "{allow}"; [ -f "{allow}" ]; }}\n'
            f'wg_conf_check "{conf}" {kind}\n')
    return subprocess.run([SH, "-c", prog], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", **env})


def test_the_link_config_from_the_token_passes_the_join_check(tmp_path):
    text = _link_conf()
    conf = wgconf.parse(text)
    assert conf.get("Peer", "AllowedIPs") == "10.99.99.4/30" and conf.get("Peer", "PresharedKey") in (None, "")
    r = _check(tmp_path, text, "join")
    assert r.returncode == 0, r.stderr
    r = _check(tmp_path, text, "link", CLIENT_SUBNET="10.8.1.0/24")
    assert r.returncode != 0 and "нет обязательной записи 10.8.1.0/24" in r.stderr, \
        "вид link без подсети клиентов принял бы что угодно — проверка поменялась"


@pytest.mark.parametrize("extra, why", [
    ("PostUp = id\n", "вне белого списка"),
    ("", "AllowedIPs"),
])
def test_the_join_check_refuses_what_a_link_must_not_have(tmp_path, extra, why):
    text = _link_conf()
    if extra:
        text = text.replace("[Peer]\n", extra + "\n[Peer]\n")
    else:
        text = text.replace("AllowedIPs = 10.99.99.4/30", "AllowedIPs = 10.99.99.4/30, 192.168.1.0/24")
    r = _check(tmp_path, text, "join")
    assert r.returncode != 0 and why in r.stderr, r.stderr


class _Gw:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"; self.bin.mkdir()
        self.conf_dir = tmp_path / "amneziawg"; self.conf_dir.mkdir()
        self.etc = tmp_path / "awg-gw"
        self.log = tmp_path / "calls.log"; self.log.write_text("", encoding="utf-8")
        for name in ("nft", "awg-quick", "ip", "systemctl"):
            fake_cmd(self.bin / name, f'echo "{name} $*" >> {self.log}\nexit 0\n')

    def run(self, conf_text: str | None) -> subprocess.CompletedProcess:
        script = SCRIPT.read_text(encoding="utf-8")
        src = self.tmp / "link.conf"
        if conf_text is not None:
            src.write_text(conf_text, encoding="utf-8")
        else:
            src.unlink(missing_ok=True)
        block = script.index('if [ "$MODE" = "join" ]; then')
        end = script.index("\nfi\n", block) + 4
        allow = wgconf.ALLOW_LIST
        prog = ("set -e\n" + _helpers(script)
                + f'\nMODE=join\nSRC_CONF="{src}"\nLINK_IF=awglink\nHOST_CONF_DIR="{self.conf_dir}"\n'
                f'GW_ETC="{self.etc}"\nAWG_QUICK=awg-quick\nJOIN_TABLE="inet awg_gw_join"\nIFACE_NETS=""\n'
                + _core(script)
                + f'\nallow_list_path() {{ printf "%s" "{allow}"; [ -f "{allow}" ]; }}\n'
                + 'install_self() { echo "install_self" >> "$CALLS"; }\n'
                + script[block:end])
        return subprocess.run([SH, "-c", prog], capture_output=True, text=True,
                              env={"PATH": f"{self.bin}:/usr/bin:/bin", "CALLS": str(self.log)})

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()


def test_join_brings_up_only_the_link_behind_the_quarantine_table(tmp_path):
    gw = _Gw(tmp_path)
    text = _link_conf()
    r = gw.run(text)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.rstrip().splitlines()[-1] == "join: linked"
    assert (gw.conf_dir / "awglink.conf").read_text(encoding="utf-8") == text
    assert oct((gw.conf_dir / "awglink.conf").stat().st_mode)[-3:] == "600"
    table = (gw.etc / "join.nft").read_text(encoding="utf-8")
    inp = table.split("chain input {", 1)[1].split("}", 1)[0]
    fwd = table.split("chain forward {", 1)[1].split("}", 1)[0]
    assert [ln.strip() for ln in inp.splitlines() if "awglink" in ln] == [
        'iifname "awglink" ct state established,related accept', 'iifname "awglink" drop']
    assert [ln.strip() for ln in fwd.splitlines() if "awglink" in ln] == [
        'iifname "awglink" drop', 'oifname "awglink" drop']
    calls = gw.calls()
    nft_at = next(i for i, c in enumerate(calls) if c.startswith("nft -f"))
    up_at = calls.index("awg-quick up awglink")
    assert nft_at < up_at, "линк поднят раньше карантина"
    assert not any("awg0" in c or "uplink" in c for c in calls), f"аплинк тронут: {calls}"
    assert not any(c.startswith("systemctl") for c in calls), f"юнит до кода: {calls}"
    assert not list(gw.conf_dir.glob("awg0*")), "аплинк положен"


def test_a_bad_link_config_is_not_installed(tmp_path):
    gw = _Gw(tmp_path)
    r = gw.run(_link_conf().replace("[Peer]\n", "PostUp = id\n\n[Peer]\n"))
    assert r.returncode != 0 and "не прошёл проверку" in r.stdout
    assert not (gw.conf_dir / "awglink.conf").exists() and "awg-quick up awglink" not in gw.calls()
    r = gw.run(None)
    assert r.returncode == 2 and "нужен файл" in r.stdout


def test_the_full_apply_and_rollback_take_the_quarantine_away():
    """Полное применение после сопряжения (тот же --apply) и снятие обвязки
    снимают карантинную таблицу: иначе линк на постоянных ключах остался бы
    без пересылки навсегда."""
    script = SCRIPT.read_text(encoding="utf-8")
    full = script.split('run "nft -f $GUARD_FILE"', 1)[1].split("# ── 3.", 1)[0]
    assert 'run "nft delete table $JOIN_TABLE 2>/dev/null || true"' in full and "join.nft" in full
    rollback = script.split('if [ "$MODE" = "rollback" ]; then', 1)[1].split("\nfi\n", 1)[0]
    assert 'nft delete table $JOIN_TABLE' in rollback and "join.nft" in rollback


def _apply_quarantine_block(tmp_path, *, finish: bool) -> tuple[subprocess.CompletedProcess, list[str], Path]:
    """Кусок --apply о карантине подключения — под sh, nft подставной."""
    script = SCRIPT.read_text(encoding="utf-8")
    bin_ = tmp_path / "bin"; bin_.mkdir(parents=True)
    log = tmp_path / "calls.log"; log.write_text("", encoding="utf-8")
    fake_cmd(bin_ / "nft", f'echo "nft $*" >> {log}\nexit 0\n')
    etc = tmp_path / "awg-gw"; etc.mkdir()
    (etc / "join.nft").write_text("table inet awg_gw_join\n", encoding="utf-8")
    start = script.index('if [ -f "$GW_ETC/join.nft" ] || nft list table $JOIN_TABLE')
    end = script.index("\nfi\n", script.index("    fi\n", start)) + 4
    prog = (_helpers(script) + f'\nGW_ETC="{etc}"\nJOIN_TABLE="inet awg_gw_join"\n'
            + ("JOIN_FINISH=1\n" if finish else "") + script[start:end])
    r = subprocess.run([SH, "-c", prog], capture_output=True, text=True, env={"PATH": f"{bin_}:/usr/bin:/bin"})
    return r, log.read_text(encoding="utf-8").splitlines(), etc / "join.nft"


def test_the_quarantine_is_lifted_only_by_the_end_of_pairing(tmp_path):
    """Вне оси (агент уже стоял): прежний юнит установленного шлюза, перезапущенный
    посреди сопряжения (--apply без JOIN_FINISH), карантин не снимает — ставит
    заново; снимает только --apply агента после смены ключей по коду."""
    r, calls, f = _apply_quarantine_block(tmp_path / "a", finish=False)
    assert r.returncode == 0, r.stderr
    assert f.exists() and not any("delete table" in c for c in calls) and f"nft -f {f}" in calls
    r, calls, f = _apply_quarantine_block(tmp_path / "b", finish=True)
    assert r.returncode == 0, r.stderr
    assert not f.exists() and any("delete table inet awg_gw_join" in c for c in calls)


def test_the_agent_finishes_with_the_mark(tmp_path, monkeypatch):
    """join_finish (агент после кода) передаёт JOIN_FINISH=1 — иначе карантин
    не снялся бы никогда."""
    from awgbot.core import config
    from awgbot.infra.gwguard import join as gj
    link = tmp_path / "awglink.conf"; link.write_text(_link_conf(), encoding="utf-8")
    monkeypatch.setattr(config, "GW_LINK_CONF", str(link))
    got = {}
    monkeypatch.setattr(gj, "_run", lambda script, args, env, timeout: (got.update(env), (True, ""))[1])
    gj.join_finish(8787)
    assert got["JOIN_FINISH"] == "1"


def test_no_text_sends_the_human_to_a_configuration_file(monkeypatch, tmp_path):
    """flow-8: файла первого применения больше нет — ни подсказка старта, ни
    диалоги, ни терминал не отправляют к нему; новое устройство — командой."""
    from awgbot.bot.texts.routing.dialogs import GATEWAY_KEYS_PICK
    from awgbot.core import config
    from awgbot.runtime import preflight
    from tools.snapshot import ON_GATEWAY
    monkeypatch.setattr(config, "GW_LINK_CONF", str(tmp_path / "нет.conf"))
    monkeypatch.setattr(preflight, "_unit_enabled", lambda unit: "enabled")
    monkeypatch.setattr(preflight, "_service_autostart_warning", lambda: [])
    warns = [w for w in preflight.collect_warnings_gateway() if "конфига линка" in w]
    assert warns and "«🛰 Подключить шлюз»" in warns[0] and "--bundle" not in warns[0]
    assert "«🛰 Подключить шлюз»" in ON_GATEWAY and "<файл>" not in ON_GATEWAY
    assert "файл конфигурации" not in GATEWAY_KEYS_PICK
    sh = (Path(__file__).resolve().parents[2] / "awg-bot.sh").read_text(encoding="utf-8")
    assert "времён файла конфигурации" not in sh
    gw = SCRIPT.read_text(encoding="utf-8")
    says = [ln for ln in gw.splitlines() if ln.strip().startswith("say ")]
    assert not [ln for ln in says if "из файла конфигурации" in ln or "нужен только для первоначальной" in ln]
