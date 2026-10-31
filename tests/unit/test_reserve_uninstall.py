"""Снятие резерва вместе с установкой: awg-bot.sh `cmd_uninstall` копирует
скрипт гостевой зоны до удаления кода и отдаёт копию хвосту (`AWG_ZONE_SCRIPT`);
`cmd_post_uninstall` снимает зону этой копией (`--teardown`, затем `--wipe`),
останавливает журнал гостя, снимает каждое `ip rule iif awggr-h` и стирает
журнал гостя; установщик с флагом сноса (install/awg-bot-install.sh
`wipe_previous`) делает то же скриптом из каталога установки, плюс линк пары.

Функции гоняются bash'ем из настоящих скриптов. Абсолютные пути функции
(`/usr/local/sbin`, `/etc`, `/run`, `/var/lib`) переписаны во временный корень;
`rm`, `systemctl`, `ip`, `nft`, `sh`, `mountpoint`, `umount`, `awg-quick` —
функции-журналы: ничего на машине прогона не удаляется и не снимается. Скрипт
зоны — подставной, пишет режимы в тот же журнал.

Цена ошибки: зона, снятая «руками» мимо своего скрипта, — CAKE на внешнем
интерфейсе остаётся навсегда, клоны с ключами второй стороны живут в
пространстве, а правило iif awggr-h ловит трафик несуществующего veth;
`--wipe` не позван — расшифрованные данные основы в хранилище в памяти до
перезагрузки; копия скрипта, снятая раньше хвоста, или скрипт из уже
удалённого каталога — зона не снимается вовсе; журнал гостя с данными второй
стороны — в /run до перезагрузки.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CLI = (ROOT / "awg-bot.sh").read_text(encoding="utf-8")
INSTALLER = (ROOT / "install" / "awg-bot-install.sh").read_text(encoding="utf-8")


def _func(text: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", text, re.S | re.M)
    assert m, f"нет функции {name}()"
    return m.group(0)


def _rooted(body: str) -> str:
    """Абсолютные пути функции — под $R (временный корень)."""
    for prefix in ("/usr/local/sbin/", "/etc/", "/run/", "/var/lib/", "/root/", "/opt/"):
        body = re.sub(rf'(?<![\w$./-]){re.escape(prefix)}', f'$R{prefix}', body)
    return body


class _Host:
    """Временный корень и журнал вызовов; правило iif awggr-h стоит `rules` раз."""

    def __init__(self, tmp: Path, *, rules: int = 2):
        self.tmp = tmp
        self.root = tmp / "root"
        self.log = tmp / "calls.log"
        self.log.write_text("", encoding="utf-8")
        self.rules = tmp / "rules"
        self.rules.write_text(f"{rules}\n", encoding="utf-8")
        self.zone = tmp / "zone-copy.sh"

    def put(self, rel: str, text: str = "", *, exe: bool = False) -> Path:
        p = self.root / rel.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        if exe:
            p.chmod(0o755)
        return p

    def zone_script(self, *, fail: bool = False) -> Path:
        self.zone.write_text(f'echo "zone $*" >> "{self.log}"\n' + ("exit 1\n" if fail else "exit 0\n"),
                             encoding="utf-8")
        return self.zone

    def stubs(self) -> str:
        log = self.log
        return "\n".join([
            f'R="{self.root}"',
            f'_j() {{ echo "$*" | sed "s#$R##g" >> "{log}"; }}',
            'rm() { _j "rm $*"; }', 'systemctl() { _j "systemctl $*"; }', 'nft() { _j "nft $*"; return 1; }',
            'sh() { _j "sh $*"; }', 'umount() { _j "umount $*"; }', 'awg-quick() { _j "awg-quick $*"; }',
            'mountpoint() { return 0; }', 'setsid() { :; }',
            # ip rule del: правило снимается, пока стоит; остальное — журнал
            f'ip() {{ _j "ip $*"; if [ "$1 $2" = "rule del" ]; then n=$(cat "{self.rules}"); '
            f'[ "$n" -gt 0 ] || return 1; echo $((n - 1)) > "{self.rules}"; fi; return 0; }}',
            'ok() { echo "OK $*"; }', 'warn() { echo "WARN $*"; }', 'log() { echo "LOG $*"; }',
            'confirm() { return 1; }',
        ])

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def rules_left(self) -> int:
        return int(self.rules.read_text(encoding="utf-8").strip())


@pytest.fixture()
def host(tmp_path):
    h = _Host(tmp_path)
    h.put("/usr/local/sbin/reserve-pair-setup.sh", "exit 0\n", exe=True)
    h.put("/etc/systemd/system/awg-bot-guest.service")
    h.put("/run/log/journal/0123.awg-guest/system.journal", "DUMMY")
    return h


# ── awg-bot uninstall: хвост ────────────────────────────────────────────────

def _post(h: _Host, *, zone: str | None, role: str = "client") -> subprocess.CompletedProcess:
    env_zone = f'AWG_ZONE_SCRIPT="{zone}"' if zone is not None else "unset AWG_ZONE_SCRIPT"
    prog = "\n".join(["set -uo pipefail", h.stubs(), f'DATA_DIR="{h.tmp}/data"; ETC_DIR="{h.tmp}/etc-awg-bot"',
                      f'SELF_PATH="{h.tmp}/self"', f"AWG_ROLE={role}", env_zone,
                      _rooted(_func(CLI, "cmd_post_uninstall")), "cmd_post_uninstall"])
    return subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=30,
                          env={"PATH": "/usr/bin:/bin"})


def test_the_uninstall_tail_takes_the_zone_down_with_its_own_script_copy(host):
    zone = host.zone_script()
    r = _post(host, zone=str(zone))
    assert r.returncode == 0, r.stdout + r.stderr
    calls = host.calls()
    zone_calls = [c for c in calls if c.startswith("zone ")]
    assert zone_calls == ["zone --teardown", "zone --wipe"], f"зона снята не своим скриптом: {zone_calls}"
    assert f"rm -f {zone}" in calls, "копия скрипта зоны осталась во временном каталоге"
    assert calls.index("zone --wipe") < calls.index("ip netns del awg-guest"), \
        "пространство снято раньше скрипта зоны — клоны и CAKE ему уже не найти"
    assert "OK линк пары и гостевая зона резерва сняты" in r.stdout, r.stdout


def test_the_uninstall_tail_stops_the_guest_journal_and_removes_it(host):
    _post(host, zone=str(host.zone_script()))
    calls = host.calls()
    assert "systemctl stop systemd-journald@awg-guest.service" in calls, "журнал гостя держит данные второй стороны"
    rms = [c for c in calls if c.startswith("rm -rf ")]
    assert any("/run/log/journal/0123.awg-guest" in c for c in rms), f"журнал гостя не стёрт: {rms}"
    assert any("/run/awg-bot-guest" in c and "/var/lib/awg-bot-guest" in c for c in rms), rms


def test_the_uninstall_tail_removes_every_rf_rule_of_the_zone_veth(host):
    _post(host, zone=str(host.zone_script()))
    assert host.rules_left() == 0, "правило iif awggr-h осталось"
    assert [c for c in host.calls() if c.startswith("ip rule del")].count("ip rule del iif awggr-h") == 3, \
        "цикл не дошёл до отказа ip rule del"


def test_the_uninstall_tail_does_not_delete_the_zone_table_from_the_host(host):
    """Таблица awg_guest живёт в пространстве зоны, а не у хоста: холостой
    `nft delete table inet awg_guest` у хоста ничего не снимал."""
    _post(host, zone=str(host.zone_script()))
    assert not [c for c in host.calls() if "awg_guest" in c and c.startswith("nft")], host.calls()


def test_a_failing_zone_script_does_not_stop_the_rest_of_the_tail(host):
    r = _post(host, zone=str(host.zone_script(fail=True)))
    assert r.returncode == 0, r.stdout + r.stderr
    calls = host.calls()
    assert "zone --wipe" in calls, "teardown отказал — а хранилище с данными основы не стёрто"
    assert "ip netns del awg-guest" in calls and host.rules_left() == 0


@pytest.mark.parametrize("zone", [None, "", "missing"], ids=["unset", "empty", "no-file"])
def test_without_the_zone_script_copy_the_zone_is_taken_down_by_hand(host, zone):
    path = str(host.tmp / "nope.sh") if zone == "missing" else zone
    r = _post(host, zone=path)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = host.calls()
    assert not [c for c in calls if c.startswith("zone ")]
    assert "ip netns del awg-guest" in calls and host.rules_left() == 0
    assert "systemctl stop systemd-journald@awg-guest.service" in calls


def test_nothing_of_the_reserve_is_touched_where_it_never_was(tmp_path):
    h = _Host(tmp_path)
    r = _post(h, zone=str(h.zone_script()))
    assert r.returncode == 0, r.stdout + r.stderr
    assert not [c for c in h.calls() if c.startswith("zone ") or "awg-guest" in c], h.calls()
    assert h.rules_left() == 2


# ── awg-bot uninstall: копия скрипта зоны до удаления кода ─────────────────

def _uninstall(tmp: Path, *, with_zone: bool) -> subprocess.CompletedProcess:
    inst = tmp / "opt"
    (inst / "install").mkdir(parents=True)
    if with_zone:
        (inst / "install" / "reserve-zone-setup.sh").write_text("# DUMMY zone script\n", encoding="utf-8")
    seen = tmp / "seen.txt"
    self_path = tmp / "awg-bot.sh"
    # части скрипта — рядом, как в поставке: хвост уезжает во временный каталог вместе с ними
    (tmp / "install" / "lib").mkdir(parents=True)
    (tmp / "install" / "lib" / "update.sh").write_text("# DUMMY lib\n", encoding="utf-8")
    # «хвост» — запоминает, что ему передали, и убирает за собой обе временные копии
    self_path.write_text('#!/bin/bash\n'
                         f'{{ echo "args=$*"; echo "role=$AWG_ROLE"; echo "zone=${{AWG_ZONE_SCRIPT:-}}"; '
                         f'[ -n "${{AWG_ZONE_SCRIPT:-}}" ] && cat "$AWG_ZONE_SCRIPT"; '
                         f'[ -d "{inst}" ] && echo "install-dir=present" || echo "install-dir=gone"; }} > "{seen}"\n'
                         'rm -f "${AWG_ZONE_SCRIPT:-}" "$0"\n', encoding="utf-8")
    self_path.chmod(0o755)
    (tmp / "etc" / "conf").mkdir(parents=True)
    (tmp / "etc" / "conf" / "app.yaml").write_text('role: "client"\n', encoding="utf-8")
    prog = "\n".join([
        "set -euo pipefail",
        f'INSTALL_DIR="{inst}"; ETC_DIR="{tmp / "etc"}"; SELF_PATH="{self_path}"; SELF_DIR="{tmp}"; SERVICE=awg-bot',
        f'UNIT_PATH="{tmp / "unit"}"; SELF_LINK="{tmp / "link"}"',
        "require_root() { :; }; confirm() { return 0; }; systemctl() { :; }; update_guard_disarm() { :; }",
        'warn() { :; }; ok() { :; }; log() { :; }',
        _func(CLI, "cmd_uninstall"), "cmd_uninstall"])
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", "TMPDIR": str(tmp)})
    r.seen = seen.read_text(encoding="utf-8") if seen.exists() else ""
    return r


def test_the_uninstall_hands_a_copy_of_the_zone_script_to_the_tail(tmp_path):
    r = _uninstall(tmp_path, with_zone=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "args=__post_uninstall" in r.seen and "role=client" in r.seen, r.seen
    assert "install-dir=gone" in r.seen, "предусловие: хвост идёт после удаления кода"
    m = re.search(r"^zone=(.+)$", r.seen, re.M)
    assert m and not m.group(1).startswith(str(tmp_path / "opt")), f"хвосту не отдана копия скрипта зоны: {r.seen}"
    assert "# DUMMY zone script" in r.seen, "копия — не скрипт зоны (или снята вместе с кодом)"


def test_the_uninstall_without_a_zone_script_gives_the_tail_an_empty_path(tmp_path):
    r = _uninstall(tmp_path, with_zone=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert re.search(r"^zone=$", r.seen, re.M), r.seen


# ── установщик со сносом прошлой установки ─────────────────────────────────

def _wipe(h: _Host, *, role: str = "main", with_zone: bool = True) -> subprocess.CompletedProcess:
    inst = h.tmp / "inst"
    (inst / "install").mkdir(parents=True, exist_ok=True)
    if with_zone:
        (inst / "install" / "reserve-zone-setup.sh").write_text(
            f'echo "zone $*" >> "{h.log}"\nexit 0\n', encoding="utf-8")
    (h.tmp / "etc" / "conf").mkdir(parents=True, exist_ok=True)
    (h.tmp / "etc" / "conf" / "app.yaml").write_text(f'role: "{role}"\n', encoding="utf-8")
    prog = "\n".join(["set -uo pipefail", h.stubs(),
                      f'INSTALL_DIR="{inst}"; ETC_DIR="{h.tmp / "etc"}"; DATA_DIR="{h.tmp / "data"}"',
                      f'SELF_LINK="{h.tmp / "link"}"',
                      _func(INSTALLER, "installed_role"),
                      _rooted(_func(INSTALLER, "wipe_previous")), "wipe_previous"])
    return subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=30,
                          env={"PATH": "/usr/bin:/bin"})


def test_the_wiping_install_takes_the_zone_and_the_pair_link_down(host):
    r = _wipe(host)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = host.calls()
    assert [c for c in calls if c.startswith("zone ")] == ["zone --teardown", "zone --wipe"], calls
    assert "sh /usr/local/sbin/reserve-pair-setup.sh --down" in calls, "линк пары живёт после сноса установки"
    assert "systemctl disable --now awg-pair.service awg-bot-guest.service awg-bot-guest-dns.service" in calls
    assert "systemctl stop systemd-journald@awg-guest.service" in calls
    assert host.rules_left() == 0, "правило iif awggr-h пережило снос"
    assert "ip netns del awg-guest" in calls
    rms = " ".join(c for c in calls if c.startswith("rm "))
    for path in ("/usr/local/sbin/reserve-pair-setup.sh", "/etc/systemd/system/awg-pair.service",
                 "/etc/systemd/journald@awg-guest.conf", "/run/awg-bot-guest", "/var/lib/awg-bot-guest",
                 "/run/awg-bot/rsv", "/run/log/journal/0123.awg-guest"):
        assert path in rms, f"после сноса осталось {path}"


def test_the_wiping_install_runs_the_zone_script_before_the_code_is_removed(host):
    _wipe(host)
    calls = host.calls()
    last_rm = max(i for i, c in enumerate(calls) if c.startswith("rm -rf ") and "/inst" in c)
    assert calls.index("zone --wipe") < last_rm, "каталог установки снят раньше скрипта зоны"
    assert calls.index("zone --wipe") < calls.index("ip netns del awg-guest")


def test_the_wiping_install_without_the_zone_script_still_cleans_the_reserve(host):
    r = _wipe(host, with_zone=False)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = host.calls()
    assert not [c for c in calls if c.startswith("zone ")]
    assert "ip netns del awg-guest" in calls and host.rules_left() == 0


def test_a_gateway_wipe_does_not_touch_the_reserve_zone(host):
    """Гостевая зона бывает только у ВПС; на шлюзе её скрипт не зовётся."""
    _wipe(host, role="gateway")
    assert not [c for c in host.calls() if c.startswith("zone ") or "awg-guest" in c], host.calls()
