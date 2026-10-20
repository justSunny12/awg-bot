"""install/reserve-gw-setup.sh — постоянный линк шлюза к резерву (awgrsv) и
его обвязка: таблица inet awg_rsv_guard (вход, пересылка по набору
rsv_open4, connmark), маршруты таблицы 152, юнит.

Скрипт гоняется целиком под sh: `id`, `nft`, `ip`, `awg-quick`, `awg`,
`systemctl`, `install` — подставные команды с журналом; программа, которую
скрипт подаёт `nft -f -`, сохраняется целиком и разбирается здесь;
поднятость линка — файл-флаг; правила `ip rule` — файл. Конфиг, состояние,
юнит, копия скрипта — во временном каталоге. Ключи — DUMMY-заглушки.

Цена ошибки: вход с awgrsv без явного drop — резерв видит все службы шлюза
(policy accept, а в tunnel_in адреса сторон зоны не прыгают); пересылка с
пустым набором открыта — резерв ходит через шлюз основы без переезда;
пересылка в частные сети — резерв в локальной сети шлюза; SSH админов без
открытого набора — вход с линка резерва всегда; марка маршрута на исходящих
из awgrsv — РФ-трафик клиентов возвращается в тот же линк и наружу не
выходит; набор, не восстановленный после перезагрузки, — клиенты у резерва
без РФ-доступа до следующего слова.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import SH, fake_cmd

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "install" / "reserve-gw-setup.sh"
GW_SETUP = Path(__file__).resolve().parents[2] / "install" / "routing-gw-setup.sh"

CONF = ("[Interface]\nAddress = 10.99.97.2/30\nPrivateKey = DUMMY-RSV-PRIV=\nTable = off\nJc = 5\n\n"
        "[Peer]\nPublicKey = DUMMY-ZONE-PUB=\nPresharedKey = DUMMY-RSV-PSK=\nAllowedIPs = 10.99.97.1/32\n"
        "Endpoint = 198.51.100.7:45001\nPersistentKeepalive = 25-35\n")


class _Gw:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.bin = tmp / "bin"; self.bin.mkdir()
        self.etc = tmp / "etc"; self.etc.mkdir()
        self.state = tmp / "var" / "rsv.status"
        self.unit = tmp / "awg-rsv-gw.service"
        self.self_path = tmp / "sbin" / "reserve-gw-setup.sh"
        self.gw_status = tmp / "gateway.status"
        self.log = tmp / "calls.log"; self.log.write_text("", encoding="utf-8")
        self.progs = tmp / "nft.progs"; self.progs.write_text("", encoding="utf-8")
        self.up = tmp / "link.up"
        self.rules = tmp / "rules"; self.rules.write_text("", encoding="utf-8")
        fake_cmd(self.bin / "id", f'[ -f {tmp}/notroot ] && echo 1000 || echo 0\n')
        fake_cmd(self.bin / "nft",
                 f'echo "nft $*" >> {self.log}\n'
                 f'if [ "$1" = "-f" ] && [ "$2" = "-" ]; then echo "=== PROG" >> {self.progs}; cat >> {self.progs}; fi\n'
                 'exit 0\n')
        fake_cmd(self.bin / "ip",
                 f'echo "ip $*" >> {self.log}\n'
                 f'if [ "$1 $2" = "link show" ]; then [ -f {self.up} ]; exit $?; fi\n'
                 f'if [ "$1 $2" = "rule list" ]; then cat {self.rules}; exit 0; fi\n'
                 f'if [ "$1 $2" = "rule add" ]; then echo "100: from all fwmark $4 lookup $6" >> {self.rules}; exit 0; fi\n'
                 f'if [ "$1 $2" = "rule del" ]; then : > {self.rules}; exit 0; fi\n'
                 'exit 0\n')
        fake_cmd(self.bin / "awg-quick",
                 f'echo "awg-quick $*" >> {self.log}\n'
                 f'[ "$1" = up ] && touch {self.up}\n[ "$1" = down ] && rm -f {self.up}\nexit 0\n')
        fake_cmd(self.bin / "awg", f'echo "awg $*" >> {self.log}\n'
                                   'if [ "$2" = awgrsv ] && [ "$3" = transfer ]; then printf "DUMMY-ZONE-PUB=\\t1200\\t3400\\n"; fi\n'
                                   'exit 0\n')
        fake_cmd(self.bin / "systemctl", f'echo "systemctl $*" >> {self.log}\nexit 0\n')
        fake_cmd(self.bin / "install",
                 f'echo "install $*" >> {self.log}\n'
                 'for last; do :; done\n'
                 'src=""; prev=""; for a; do [ "$a" = "$last" ] && src="$prev"; prev="$a"; done\n'
                 'mkdir -p "$(dirname "$last")" && cp "$src" "$last"\nexit 0\n')

    def run(self, *args: str, **env) -> subprocess.CompletedProcess:
        base = {"PATH": f"{self.bin}:/usr/bin:/bin", "RSV_CONF_DIR": str(self.etc), "RSV_STATE": str(self.state),
                "UNIT": str(self.unit), "SELF_PATH": str(self.self_path), "GW_STATUS": str(self.gw_status)}
        base.update({k: str(v) for k, v in env.items()})
        self.log.write_text("", encoding="utf-8")
        self.progs.write_text("", encoding="utf-8")
        return subprocess.run([SH, str(SCRIPT), *args], capture_output=True, text=True, env=base, timeout=30)

    def up_with(self, text: str = CONF, **env) -> subprocess.CompletedProcess:
        src = self.tmp / "in.conf"
        src.write_text(text, encoding="utf-8")
        return self.run("--up", str(src), **env)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def prog(self) -> str:
        """Последняя программа, поданная nft -f -."""
        text = self.progs.read_text(encoding="utf-8")
        return text.rsplit("=== PROG\n", 1)[-1] if "=== PROG" in text else ""

    def kv(self) -> dict:
        out = {}
        for line in self.run("--status").stdout.splitlines():
            k, sep, v = line.partition("=")
            if sep:
                out[k] = v
        return out


@pytest.fixture()
def gw(tmp_path):
    return _Gw(tmp_path)


def _chain(prog: str, name: str) -> list[str]:
    body = prog.split(f"chain {name} {{", 1)[1].split("}", 1)[0]
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


def _set(prog: str, name: str) -> str:
    return prog.split(f"set {name} {{", 1)[1].split("\n    }", 1)[0]


def _priority(rule: str) -> int:
    m = re.search(r"priority (filter|mangle)\s*(?:([-+])\s*(\d+))?", rule) or re.search(r"priority (-?\d+)", rule)
    base = {"filter": 0, "mangle": -150}
    if m.group(1) in base:
        return base[m.group(1)] + (int(m.group(3)) * (1 if m.group(2) == "+" else -1) if m.group(2) else 0)
    return int(m.group(1))


# ── --up и таблица ──────────────────────────────────────────────────────────

def test_up_installs_a_private_conf_raises_the_link_and_draws_the_table(gw):
    r = gw.up_with()
    assert r.returncode == 0 and "up: ok" in r.stdout, r.stdout + r.stderr
    assert (gw.etc / "awgrsv.conf").read_text(encoding="utf-8") == CONF
    assert any("install -m 0600" in c for c in gw.calls()), "конфиг с ключом линка ставится не 0600"
    assert "awg-quick up" in " ".join(gw.calls())
    prog = gw.prog()
    assert "table inet awg_rsv_guard {" in prog and "delete table inet awg_rsv_guard" in prog
    assert gw.unit.exists() and "--reassert" in gw.unit.read_text(encoding="utf-8")
    assert "fwmark 0x52 lookup 152" in gw.rules.read_text(encoding="utf-8")
    assert gw.kv()["open"] == "0", "после подъёма линка пересылка открыта"


def test_input_from_awgrsv_is_only_replies_and_drops_before_the_guard_table_jumps_to_tunnel_in(gw):
    gw.up_with()
    rules = _chain(gw.prog(), "rsv_input")
    assert rules[1] == 'iifname "awgrsv" ct state established,related accept'
    assert rules[-1] == 'iifname "awgrsv" drop', f"вход с awgrsv не закрыт явным drop: {rules}"
    guard = next(ln for ln in GW_SETUP.read_text(encoding="utf-8").splitlines()
                 if "hook input priority filter" in ln)
    assert _priority(rules[0]) < _priority(guard), "drop с awgrsv не раньше цепочки input таблицы обвязки"


def test_ssh_of_the_admins_only_with_an_open_set_and_only_to_the_ssh_port(gw):
    gw.gw_status.write_text("SSH_PORT=2222\n", encoding="utf-8")
    gw.up_with(ADMIN_IPS="10.9.1.2,10.9.1.3")
    prog = gw.prog()
    ssh = [r for r in _chain(prog, "rsv_input") if "dport" in r]
    assert ssh == ['iifname "awgrsv" ip saddr @rsv_open4 ip saddr @rsv_admin4 tcp dport 2222 accept'], ssh
    assert "elements = { 10.9.1.2, 10.9.1.3 }" in _set(prog, "rsv_admin4")


def test_forwarding_with_an_empty_set_is_closed_and_with_an_open_one_only_outwards(gw):
    gw.up_with()
    prog = gw.prog()
    assert "elements" not in _set(prog, "rsv_open4"), "набор пересылки не пуст после подъёма"
    fwd = _chain(prog, "rsv_forward")
    accepts = [r for r in fwd if r.endswith("accept") and r.startswith('iifname "awgrsv"')]
    assert accepts == ['iifname "awgrsv" ip saddr @rsv_open4 ip daddr != @rsv_private4 accept'], accepts
    assert 'iifname "awgrsv" drop' in fwd and fwd.index('iifname "awgrsv" drop') > fwd.index(accepts[0])
    private = _set(prog, "rsv_private4")
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert net in private, f"частная сеть {net} не закрыта для пересылки с awgrsv"


def test_only_replies_are_steered_back_into_awgrsv_by_the_connmark(gw):
    """Соединение с awgrsv помечается connmark; марка маршрута (таблица 152 →
    awgrsv) — только ответам. Поставленная и исходящим из awgrsv пакетам,
    она уводила бы РФ-трафик клиентов обратно в линк вместо WAN."""
    gw.up_with()
    mark = _chain(gw.prog(), "rsv_mark")
    assert 'iifname "awgrsv" ct state new ct mark set 0x52' in mark
    steer = [r for r in mark if "meta mark set" in r]
    assert steer, "ответы в awgrsv по connmark не направляются"
    for r in steer:
        assert "ct direction reply" in r or 'iifname != "awgrsv"' in r, (
            f"марка маршрута ставится и пакетам из awgrsv — они уйдут обратно в линк: {r}")


def test_a_conf_with_hooks_or_without_table_off_is_refused_and_nothing_is_installed(gw):
    for bad in (CONF.replace("Table = off\n", "Table = off\nPostUp = curl evil\n"),
                CONF.replace("Table = off\n", ""), CONF.replace("[Peer]", "[Pear]")):
        r = gw.up_with(bad)
        assert r.returncode != 0 and "не прошёл проверку" in r.stdout, r.stdout
        assert not (gw.etc / "awgrsv.conf").exists() and not gw.prog(), "плохой конфиг поставлен"


def test_up_with_the_same_conf_does_not_touch_the_link(gw):
    gw.up_with()
    r = gw.up_with()
    assert "up: same" in r.stdout
    assert not [c for c in gw.calls() if c.startswith("awg-quick")], "тот же конфиг переподнял линк"


def test_up_without_root_is_refused(gw):
    (gw.tmp / "notroot").write_text("", encoding="utf-8")
    r = gw.up_with()
    assert r.returncode != 0 and "root" in r.stdout and not gw.prog()


# ── --open / --close / --reassert ───────────────────────────────────────────

def test_open_and_close_change_only_the_set_and_the_state(gw):
    gw.up_with()
    r = gw.run("--open", "10.9.1.0/24")
    assert r.returncode == 0
    assert "nft add element inet awg_rsv_guard rsv_open4 { 10.9.1.0/24 }" in gw.calls()
    assert not gw.prog(), "открытие пересобрало таблицу вместо набора"
    kv = gw.kv()
    assert (kv["open"], kv["src"]) == ("1", "10.9.1.0/24")
    gw.run("--close")
    assert "nft flush set inet awg_rsv_guard rsv_open4" in gw.calls()
    assert gw.kv()["open"] == "0"


def test_after_a_reboot_reassert_restores_the_open_set_from_the_state(gw):
    gw.up_with()
    gw.run("--open", "10.9.1.0/24")
    gw.up.unlink()                                      # перезагрузка: линка и таблицы нет
    r = gw.run("--reassert")
    assert r.returncode == 0 and "reassert: ok" in r.stdout
    assert "elements = { 10.9.1.0/24 }" in _set(gw.prog(), "rsv_open4"), "набор не восстановлен после перезагрузки"
    assert gw.up.exists()


def test_status_reports_counters(gw):
    gw.up_with()
    kv = gw.kv()
    assert (kv["link"], kv["rx"], kv["tx"], kv["conf"]) == ("up", "1200", "3400", "present")


# ── --down / --remove ───────────────────────────────────────────────────────

def test_down_keeps_the_conf_and_remove_takes_everything(gw):
    gw.up_with()
    gw.run("--open", "10.9.1.0/24")
    gw.run("--down")
    assert (gw.etc / "awgrsv.conf").exists() and "nft delete table inet awg_rsv_guard" in gw.calls()
    assert "fwmark 0x52" not in gw.rules.read_text(encoding="utf-8")
    r = gw.run("--remove")
    assert r.returncode == 0 and "remove: ok" in r.stdout
    assert not (gw.etc / "awgrsv.conf").exists() and not gw.state.exists() and not gw.unit.exists()


def test_the_uninstall_of_the_gateway_removes_the_reserve_link():
    text = (Path(__file__).resolve().parents[2] / "awg-bot.sh").read_text(encoding="utf-8")
    assert "reserve-gw-setup.sh" in text and "--remove" in text.split("reserve-gw-setup.sh", 1)[1][:200], (
        "удаление агента не снимает линк к резерву")


# ── адреса админов в состоянии ──────────────────────────────────────────────

def _state(gw) -> dict:
    out = {}
    for line in gw.state.read_text(encoding="utf-8").splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k] = v
    return out


def test_the_admin_addresses_survive_a_reboot_without_the_unit_passing_them(gw):
    """Юнит при загрузке зовёт --reassert без ADMIN_IPS: адреса — из
    состояния, иначе после перезагрузки SSH админов через резерв закрыт."""
    gw.up_with(ADMIN_IPS="10.9.1.2,10.9.1.3")
    assert _state(gw)["admins"] == "10.9.1.2,10.9.1.3"
    gw.up.unlink()
    r = gw.run("--reassert")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "elements = { 10.9.1.2, 10.9.1.3 }" in _set(gw.prog(), "rsv_admin4"), "адреса админов потеряны на загрузке"


def test_open_and_close_keep_the_admin_addresses(gw):
    gw.up_with(ADMIN_IPS="10.9.1.2")
    gw.run("--open", "10.9.1.0/24")
    assert _state(gw) == {"open": "1", "src": "10.9.1.0/24", "admins": "10.9.1.2"}, _state(gw)
    gw.run("--close")
    assert _state(gw)["admins"] == "10.9.1.2", "закрытие пересылки стёрло адреса админов"
    gw.up.unlink()
    gw.run("--reassert")
    assert "elements = { 10.9.1.2 }" in _set(gw.prog(), "rsv_admin4")


def test_up_with_new_admin_addresses_replaces_them_and_keeps_the_forwarding(gw):
    gw.up_with(ADMIN_IPS="10.9.1.2")
    gw.run("--open", "10.9.1.0/24")
    gw.up_with(ADMIN_IPS="10.9.1.7")
    st = _state(gw)
    assert (st["admins"], st["open"], st["src"]) == ("10.9.1.7", "1", "10.9.1.0/24"), \
        "смена адресов админов закрыла пересылку в переезде"
    assert "elements = { 10.9.1.7 }" in _set(gw.prog(), "rsv_admin4")


# ── живой линк: syncconf без пересоздания ───────────────────────────────────

def test_a_new_conf_on_a_live_link_is_synced_without_recreating_it(gw):
    gw.up_with()
    gw.up_with(CONF.replace("DUMMY-ZONE-PUB=", "DUMMY-ZONE-PUB-2="))
    calls = gw.calls()
    assert any(c.startswith("awg syncconf awgrsv ") for c in calls), calls
    assert not [c for c in calls if c.startswith("awg-quick down") or c.startswith("awg-quick up")], \
        "живой линк к резерву пересоздан — гостевой канал рвётся на каждом документе"


def test_reassert_of_a_live_link_syncs_too(gw):
    gw.up_with()
    r = gw.run("--reassert")
    assert r.returncode == 0
    calls = gw.calls()
    assert any(c.startswith("awg syncconf awgrsv ") for c in calls) and not [c for c in calls if "awg-quick up" in c]


def test_a_failing_syncconf_falls_back_to_down_and_up(gw):
    gw.up_with()
    fake_cmd(gw.bin / "awg", f'echo "awg $*" >> {gw.log}\n[ "$1" = syncconf ] && exit 1\nexit 0\n')
    r = gw.up_with(CONF.replace("DUMMY-ZONE-PUB=", "DUMMY-ZONE-PUB-2="))
    assert r.returncode == 0, r.stdout + r.stderr
    calls = gw.calls()
    i_sync = next(i for i, c in enumerate(calls) if c.startswith("awg syncconf"))
    assert any(c.startswith("awg-quick down") for c in calls[i_sync:]) and any(c.startswith("awg-quick up")
                                                                                 for c in calls[i_sync:]), calls
    assert gw.up.exists()


def test_the_uninstall_takes_the_copy_of_the_script_from_sbin():
    """Каталог установки к этому моменту снят: снимать линк к резерву —
    копией скрипта в /usr/local/sbin, и копию — тоже."""
    text = (Path(__file__).resolve().parents[2] / "awg-bot.sh").read_text(encoding="utf-8")
    body = text.split("cmd_post_uninstall()", 1)[1].split("\n}\n", 1)[0]
    assert "sh /usr/local/sbin/reserve-gw-setup.sh --remove" in body
    assert "$INSTALL_DIR/install/reserve-gw-setup.sh" not in body, "uninstall ищет скрипт в снятом каталоге"
    assert "rm -f /usr/local/sbin/reserve-gw-setup.sh" in body
    assert body.index("reserve-gw-setup.sh --remove") < body.index("routing-gw-setup.sh --rollback"), \
        "линк к резерву снимается после обвязки — его таблица переживает откат"
