"""Скрипт гостевой зоны резерва (install/reserve-zone-setup.sh) после ревью:
python состояния — интерпретатор бота (venv), запасной — системный, без него
и при отказе — отказ без затирания состояния; `--plan` сообщает conntrack и
python; адреса админа основы (набор admin4) — в состоянии и заново при каждой
перерисовке таблицы; отвергнутая таблица — отказ режима; стороны линков
шлюзов — `awg setconf` без расширений awg-quick; хранилище — tmpfs без свопа
или ramfs, но никогда не свопируемый tmpfs; чужой CAKE не трогается;
resolv.conf гостя — на резолвер зоны; `--relay` снимает записи conntrack по
портам клонов.

Режимы гоняются bash'ем по функциям самого скрипта (обвязка `_Zone` из
test_reserve_zone_two_clones), CAKE — хостом `_Host` из
test_reserve_zone_serve_wipe; `nft`, `awg`, `ip`, `mount`, `conntrack` —
подставные в PATH, хост не трогается.

Цена ошибки: состояние, затёртое заглушкой без python, — бот не видит ни
клонов, ни фазы и снимает пересылку хвоста; набор admin4, опустевший после
`--serve`, — устройства админа основы в зоне не видят друг друга весь
переезд; таблица, отвергнутая nft, при «ok» — зона живёт со старыми
правилами без входа вебхука; Address в конфиге для `awg setconf` — сторона
линка шлюза не поднимается, РФ-доступ клиентов основы на резерве мёртв;
свопируемый tmpfs — ключи основы на диске резерва; снятый CAKE админа —
его настройки очереди потеряны; resolv.conf хоста в зоне (127.0.0.53) —
бот основы на резерве не резолвит api.telegram.org; записи conntrack от
клонов — живые потоки клиентов не попадают под DNAT хвоста.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.conftest import fake_cmd
from tests.unit.test_reserve_zone_script import _unit_text
from tests.unit.test_reserve_zone_serve_wipe import _Host
from tests.unit.test_reserve_zone_two_clones import LIB, _conf
from tests.unit.test_reserve_zone_two_clones import _Zone as _BaseZone

pytestmark = pytest.mark.unit

ADMIN = "198.51.100.20 198.51.100.21"


class _Zone(_BaseZone):
    """Зона из test_reserve_zone_two_clones, где `nft -f -` ещё и пишет «nft -f» в
    журнал (видно, что элементы набора добавлены после таблицы) и отвечает
    кодом NFT_RC; `awg setconf` кладёт полученный конфиг в setconfs/last.conf;
    каталог сторон линков — во временном каталоге."""

    def __init__(self, tmp: Path):
        super().__init__(tmp)
        self.links = tmp / "links"
        fake_cmd(self.bin / "nft", f'if [ "$1 $2" = "-f -" ]; then cat > {self.nft}; echo "nft -f" >> {self.log}; '
                                   'exit "${NFT_RC:-0}"; fi\n'
                                   f'echo "nft $*" >> {self.log}\nexit 0\n')
        fake_cmd(self.bin / "awg",
                 f'echo "awg $*" >> {self.log}\n'
                 'if [ "$1" = setconf ]; then\n'
                 '  case "$3" in *"${AWG_FAIL:-@@}"*) exit 1 ;; esac\n'
                 f'  cat "$3" > {self.setconfs}/last.conf\n'
                 f'  case "$3" in {self.run_dir}/*) cp "$3" {self.setconfs}/$(basename "$3") ;; esac\n'
                 'fi\nexit 0\n')

    def run(self, func: str, *, stdin: str = "", **env: str):
        env.setdefault("RSV_LINKS_DIR", str(self.links))
        env.setdefault("INSTALL_DIR", str(self.tmp / "opt"))
        return super().run(func, stdin=stdin, **env)

    def admin_adds(self) -> list[str]:
        return [c for c in self.calls("nft add element") if "admin4" in c]

    def tail_after_last_table(self) -> list[str]:
        calls = self.log.read_text(encoding="utf-8").splitlines()
        last = max(i for i, c in enumerate(calls) if c == "nft -f")
        return calls[last + 1:]


@pytest.fixture()
def zone(tmp_path):
    return _Zone(tmp_path)


def _take(zone: _Zone, **env):
    (zone.run_dir / "awg").mkdir(parents=True, exist_ok=True)
    return zone.run("cmd_take", stdin=_conf(45877, 1), GUEST_IF="awg0", GUEST_ADDR="198.51.100.1/24", **env)


# ── python для состояния ────────────────────────────────────────────────────

def test_state_is_written_by_the_bots_venv_python_when_it_is_there(zone):
    venv = zone.tmp / "opt" / "venv" / "bin"
    venv.mkdir(parents=True)
    used = zone.tmp / "venv-used"
    (venv / "python").write_text(f'#!/bin/sh\necho x >> "{used}"\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    (venv / "python").chmod(0o755)
    r = zone.run("state_write phase=taking")
    assert r.returncode == 0, r.stderr
    assert zone.state() == {"phase": "taking"}
    assert used.exists(), "состояние писал не python бота — на хосте без python3 зона без состояния"


def test_without_any_python_the_state_write_refuses_and_keeps_the_old_state(zone):
    zone.put_state(phase="relay", relay_to="203.0.113.5:45877", ports="45877")
    before = (zone.state_dir / "state.json").read_text(encoding="utf-8")
    r = zone.run('PY=""; state_write phase=idle; echo after')
    assert r.returncode != 0 and "after" not in r.stdout, "режим пошёл дальше без состояния"
    assert "нет python для состояния зоны" in r.stderr, r.stderr
    assert (zone.state_dir / "state.json").read_text(encoding="utf-8") == before, \
        "состояние затёрто — бот потерял фазу relay и адрес основы"


def test_a_failing_python_refuses_and_keeps_the_old_state(zone):
    zone.put_state(phase="serving", iface="awg0")
    before = (zone.state_dir / "state.json").read_text(encoding="utf-8")
    bad = zone.tmp / "badpy"
    bad.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    bad.chmod(0o755)
    r = zone.run(f'PY="{bad}"; state_write phase=idle; echo after')
    assert r.returncode != 0 and "after" not in r.stdout
    assert "состояние зоны не записано" in r.stderr, r.stderr
    assert (zone.state_dir / "state.json").read_text(encoding="utf-8") == before
    assert zone.run(f'PY="{bad}"; printf "[%s]" "$(state_get phase)"').stdout == "[]", \
        "state_get с отказавшим python — не пустое значение"


def test_the_plan_says_python_no_without_any_python(zone):
    out = zone.run('PY=""; cmd_plan').stdout.splitlines()
    assert "python=no" in out, out
    assert "python=yes" in zone.run("cmd_plan").stdout.splitlines()


def test_the_plan_says_whether_conntrack_is_there(zone):
    if (Path("/usr/bin/conntrack").exists() or Path("/bin/conntrack").exists()):
        pytest.skip("на машине прогона conntrack стоит")
    assert "conntrack=no" in zone.run("cmd_plan").stdout.splitlines()
    fake_cmd(zone.bin / "conntrack", "exit 0\n")
    assert "conntrack=yes" in zone.run("cmd_plan").stdout.splitlines()


# ── набор admin4 ────────────────────────────────────────────────────────────

def test_take_keeps_the_admin_addresses_in_the_state_and_puts_them_after_the_table(zone):
    r = _take(zone, GUEST_ADMIN=ADMIN)
    assert r.returncode == 0 and "take: ok" in r.stdout, r.stdout + r.stderr
    assert zone.state().get("admin") == ADMIN, zone.state()
    adds = [c for c in zone.tail_after_last_table() if c.startswith("nft add element") and "admin4" in c]
    assert adds == ["nft add element inet awg_guest admin4 { 198.51.100.20 }",
                    "nft add element inet awg_guest admin4 { 198.51.100.21 }"], \
        f"адреса админа не добавлены в свежую таблицу: {zone.calls('nft')}"


@pytest.mark.parametrize("func", ["cmd_serve", "cmd_shell", "cmd_relay"])
def test_every_redraw_of_the_table_puts_the_admin_addresses_again(zone, func):
    """Таблица рисуется целиком (delete + create): без повторного добавления
    набор admin4 пустеет на `--serve`, повторном `--shell` и снятии клонов."""
    _take(zone, GUEST_ADMIN=ADMIN)
    zone.log.write_text("", encoding="utf-8")
    r = zone.run(func, WEBHOOK_PORT="8443", RELAY_TO="203.0.113.5:45877")
    assert r.returncode == 0, r.stdout + r.stderr
    adds = [c for c in zone.tail_after_last_table() if c.startswith("nft add element") and "admin4" in c]
    assert len(adds) == 2 and all(a in " ".join(adds) for a in ADMIN.split()), \
        f"{func}: набор admin4 пуст после перерисовки: {zone.calls('nft')}"


def test_take_without_admin_addresses_puts_none_and_clears_the_previous(zone):
    zone.put_state(admin=ADMIN)
    r = _take(zone)
    assert r.returncode == 0, r.stdout + r.stderr
    assert zone.state().get("admin") == "", "адреса админа прошлого переезда остались в состоянии"
    assert not zone.admin_adds(), zone.admin_adds()


# ── отказ nft ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("func, ok_word", [("cmd_take", "take: ok"), ("cmd_serve", "serve: ok"), ("cmd_shell", "shell: ok")])
def test_a_table_refused_by_nft_fails_the_mode(zone, func, ok_word):
    zone.put_state(phase="taking", iface="awg0")
    if func == "cmd_take":
        r = _take(zone, NFT_RC="1")
    else:
        r = zone.run(func, NFT_RC="1", WEBHOOK_PORT="8443")
    assert r.returncode != 0 and ok_word not in r.stdout, f"{func}: таблица отвергнута, а режим «ok»: {r.stdout}"
    assert "nft отверг таблицу зоны" in r.stderr, r.stderr
    if func == "cmd_take":
        assert zone.state().get("phase") == "taking" and "taken_at" not in zone.state(), \
            "отказ take записал зону принятой"
        assert not zone.calls("systemctl restart"), "гость запущен поверх отвергнутой таблицы"
    if func == "cmd_shell":
        assert zone.state().get("zone") != "present"


# ── стороны линков шлюзов основы ────────────────────────────────────────────

LINK = ("[Interface]\nAddress = 198.51.100.2/32\nMTU = 1380\nDNS = 198.51.100.1\nTable = off\n"
        "PrivateKey = DUMMY-link-priv\nListenPort = 51900\nJc = 4\n\n"
        "[Peer]\nPublicKey = DUMMY-link-pub\nAllowedIPs = 198.51.100.0/24\n")


def test_link_sides_get_a_setconf_config_without_awg_quick_keys_but_keep_their_address(zone):
    zone.links.mkdir()
    (zone.links / "awglink1.conf").write_text(LINK, encoding="utf-8")
    r = zone.run("link_sides_up")
    assert r.returncode == 0 and "ВНИМАНИЕ" not in r.stdout, r.stdout + r.stderr
    text = (zone.setconfs / "last.conf").read_text(encoding="utf-8")
    keys = {ln.split("=")[0].strip() for ln in text.splitlines() if "=" in ln}
    assert not keys & {"Address", "MTU", "DNS", "Table"}, f"awg setconf получил расширения awg-quick: {sorted(keys)}"
    assert {"PrivateKey", "ListenPort", "Jc", "PublicKey", "AllowedIPs"} <= keys, f"снято лишнее: {keys}"
    assert "ip addr add 198.51.100.2/32 dev awglink1" in zone.calls("ip addr"), "адрес стороны линка не выставлен"
    assert (zone.links / "awglink1.conf").read_text(encoding="utf-8") == LINK, "файл стороны линка переписан"


def test_a_refused_link_side_is_a_warning_and_its_temp_link_is_removed(zone):
    zone.links.mkdir()
    (zone.links / "awglink1.conf").write_text(LINK, encoding="utf-8")
    r = zone.run("link_sides_up", AWG_FAIL="/dev/fd")
    assert r.returncode == 0 and "ВНИМАНИЕ: конфиг стороны линка awglink1 не принят" in r.stdout, r.stdout + r.stderr
    assert any(c.startswith("ip link del awgz") for c in zone.calls("ip link del")), "временный интерфейс остался"


# ── хранилище ───────────────────────────────────────────────────────────────

def _store(zone: _Zone, kernel: str, mount_fail: str = "") -> object:
    fake_cmd(zone.bin / "uname", f'case "$1" in -r) echo "{kernel}" ;; *) echo Linux ;; esac\n')
    fake_cmd(zone.bin / "mountpoint", "exit 1\n")
    fake_cmd(zone.bin / "mount", f'echo "mount $*" >> {zone.log}\ncase "$*" in {mount_fail or "@@"}) exit 32 ;; esac\nexit 0\n')
    # обвязка подменяет store_mount пустой функцией — возвращаем настоящую
    return zone.run(_store_mount_src() + '\nstore_mount; echo stored')


def _store_mount_src() -> str:
    start = LIB.index("store_mount() {")
    return LIB[start:LIB.index("\n}\n", start) + 2]


def test_the_store_falls_back_to_ramfs_never_to_a_swappable_tmpfs(zone):
    r = _store(zone, "6.12.47", mount_fail="*noswap*")
    assert r.returncode == 0 and "stored" in r.stdout, r.stdout + r.stderr
    mounts = zone.calls("mount ")
    assert mounts[0].startswith("mount -t tmpfs") and "noswap" in mounts[0], mounts
    assert mounts[1:] == [f"mount -t ramfs -o mode=0700 ramfs {zone.run_dir}"], mounts
    assert not [m for m in mounts if m.startswith("mount -t tmpfs") and "noswap" not in m], \
        "ключи основы в свопируемом tmpfs"


def test_the_store_on_an_old_kernel_is_ramfs_only(zone):
    r = _store(zone, "6.1.0-26-amd64")
    assert r.returncode == 0, r.stderr
    assert zone.calls("mount ") == [f"mount -t ramfs -o mode=0700 ramfs {zone.run_dir}"]


def test_a_store_that_mounts_nowhere_refuses(zone):
    r = _store(zone, "6.12.47", mount_fail="*")
    assert r.returncode != 0 and "stored" not in r.stdout and "хранилище гостя не смонтировалось" in r.stderr


# ── CAKE админа ─────────────────────────────────────────────────────────────

OWN_CAKE = "qdisc cake 8001: root refcnt 2 bandwidth 50Mbit diffserv4 triple-isolate nonat nowash no-ack-filter"


@pytest.fixture()
def caked(tmp_path):
    h = _Host(tmp_path)
    h.qdisc.write_text(OWN_CAKE + "\n", encoding="utf-8")
    h.put_state(phase="taking", iface="awg0")
    return h


def test_a_cake_set_by_the_admin_is_kept_on_serve_and_on_wipe(caked):
    r = caked.run("--serve", WEBHOOK_PORT="443")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not [c for c in caked.tc_calls() if " replace " in f" {c} " or " del " in f" {c} "], caked.tc_calls()
    assert caked.show() == OWN_CAKE, "CAKE админа с его скоростью заменён"
    st = caked.state()
    assert (st.get("cake"), st.get("cake_dev"), st.get("cake_prev")) == ("kept", "", ""), st
    r = caked.run("--wipe")
    assert r.returncode == 0, r.stdout + r.stderr
    assert caked.show() == OWN_CAKE, "стирание сняло CAKE, которого зона не ставила"
    assert not [c for c in caked.tc_calls() if "replace" in c or "del" in c], caked.tc_calls()
    assert caked.state().get("cake") == "off"


def test_a_repeated_serve_over_the_admins_cake_still_keeps_it(caked):
    caked.run("--serve", WEBHOOK_PORT="443")
    caked.run("--serve", WEBHOOK_PORT="443")
    assert caked.state().get("cake") == "kept" and caked.show() == OWN_CAKE


def test_our_own_cake_from_a_previous_serve_is_still_ours(tmp_path):
    """CAKE, поставленный зоной (cake=on), на повторном --serve — не «чужой»:
    иначе стирание оставило бы его навсегда."""
    h = _Host(tmp_path)
    h.put_state(phase="taking", iface="awg0")
    h.run("--serve", WEBHOOK_PORT="443")
    h.run("--serve", WEBHOOK_PORT="443")
    assert h.state().get("cake") == "on", h.state()
    h.run("--wipe")
    assert h.show().split()[1] == "fq_codel", h.show()


# ── resolv.conf гостя ───────────────────────────────────────────────────────

def test_the_guest_resolves_through_the_zone_resolver(zone):
    zone.run_dir.mkdir(parents=True)
    src = LIB[LIB.index("write_dnsmasq() {"):]
    src = src[:src.index("\n}\n") + 2]
    r = zone.run(f'{src}\nwrite_dnsmasq "198.51.100.53 198.51.100.54" "203.0.113.1"')
    assert r.returncode == 0, r.stderr
    assert (zone.run_dir / "resolv.conf").read_text(encoding="utf-8") == "nameserver 198.51.100.53\n", \
        "resolv.conf гостя не на первый адрес резолвера зоны"


def test_the_guest_unit_binds_the_zone_resolv_conf(tmp_path):
    text = _unit_text(tmp_path)
    assert "BindReadOnlyPaths=/run/awg-bot-guest/resolv.conf:/etc/resolv.conf" in text.splitlines(), \
        "бот основы в зоне резолвит через 127.0.0.53 хоста — там никого нет"


def test_take_writes_the_resolv_conf_before_the_guest_unit_starts(zone):
    """Без «-» привязка несуществующего файла роняет юнит гостя: resolv.conf
    обязан лечь до `systemctl restart awg-bot-guest`."""
    seen = zone.tmp / "seen"
    fake_cmd(zone.bin / "systemctl", f'echo "systemctl $*" >> {zone.log}\n'
                                     f'[ -f "{zone.run_dir}/resolv.conf" ] && echo "$*" >> {seen}\nexit 0\n')
    (zone.run_dir / "env").parent.mkdir(parents=True, exist_ok=True)
    (zone.run_dir / "env").write_text("BOT_TOKEN=DUMMY\n", encoding="utf-8")
    src = LIB[LIB.index("write_dnsmasq() {"):]
    src = src[:src.index("\n}\n") + 2]
    (zone.run_dir / "awg").mkdir(parents=True, exist_ok=True)
    r = zone.run(f'{src}\ncmd_take', stdin=_conf(45877, 1), GUEST_IF="awg0", GUEST_ADDR="198.51.100.1/24",
                 GUEST_DNS="198.51.100.53")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "restart awg-bot-guest.service" in seen.read_text(encoding="utf-8"), \
        "юнит гостя стартует раньше resolv.conf"


# ── conntrack: отдельный режим --relay-flush ────────────────────────────────
# Сброс — после того как бот дорисовал DNAT хвоста (иначе поток, пришедший между
# снятием клонов и перерисовкой, заведёт запись без NAT); `--relay` его не делает.

def _flush_host(tmp_path, *, conntrack: str | None = "exit 0"):
    h = _Host(tmp_path)
    if conntrack is not None:
        fake_cmd(h.bin / "conntrack", f'echo "conntrack $*" >> {h.log}\n{conntrack}\n')
    return h


def _conntrack_calls(h) -> list[str]:
    return [ln for ln in h.log.read_text(encoding="utf-8").splitlines() if ln.startswith("conntrack ")]


def test_relay_flush_drops_conntrack_for_every_clone_port(tmp_path):
    h = _flush_host(tmp_path)
    h.put_state(phase="relay", relay_to="203.0.113.5:45877", ports="45877 45878")
    r = h.run("--relay-flush")
    assert r.returncode == 0 and "relay-flush: ok" in r.stdout, r.stdout + r.stderr
    assert _conntrack_calls(h) == ["conntrack -D -p udp --orig-port-dst 45877",
                                   "conntrack -D -p udp --orig-port-dst 45878"], _conntrack_calls(h)
    assert h.state().get("phase") == "relay", "сброс conntrack тронул фазу"


def test_relay_itself_no_longer_flushes_conntrack(zone):
    fake_cmd(zone.bin / "conntrack", f'echo "conntrack $*" >> {zone.log}\nexit 0\n')
    zone.put_state(phase="serving", iface="awg0", ifaces="awg0 awg1", ports="45877 45878")
    r = zone.run("cmd_relay", RELAY_TO="203.0.113.5:45877")
    assert r.returncode == 0 and "relay: ok" in r.stdout, r.stdout + r.stderr
    assert not zone.calls("conntrack "), "сброс до перерисовки DNAT — живые потоки снова заводят записи без NAT"


def test_relay_flush_without_conntrack_warns_and_succeeds(tmp_path):
    if Path("/usr/bin/conntrack").exists() or Path("/bin/conntrack").exists() or Path("/usr/sbin/conntrack").exists():
        pytest.skip("на машине прогона conntrack стоит")
    h = _flush_host(tmp_path, conntrack=None)
    h.put_state(phase="relay", ports="45877")
    r = h.run("--relay-flush")
    assert r.returncode == 0 and "ВНИМАНИЕ: нет conntrack" in r.stdout, r.stdout + r.stderr


def test_relay_flush_goes_on_when_conntrack_has_nothing_to_delete(tmp_path):
    h = _flush_host(tmp_path, conntrack="exit 1")
    h.put_state(phase="relay", ports="45877 45878")
    r = h.run("--relay-flush")
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(_conntrack_calls(h)) == 2, "отказ на первом порту оборвал снятие остальных"


def test_relay_flush_without_known_ports_flushes_nothing(tmp_path):
    h = _flush_host(tmp_path)
    h.put_state(phase="relay")
    assert h.run("--relay-flush").returncode == 0
    assert _conntrack_calls(h) == [], "conntrack -D без порта снял бы все потоки UDP хоста"


def test_relay_flush_needs_root(tmp_path):
    h = _flush_host(tmp_path)
    fake_cmd(h.bin / "id", 'case "$1" in -u) echo 1000 ;; *) echo "uid=1000" ;; esac\n')
    h.put_state(phase="relay", ports="45877")
    r = h.run("--relay-flush")
    assert r.returncode == 1 and "root" in r.stderr and _conntrack_calls(h) == []


def test_the_harness_does_not_see_the_real_install_dir(zone):
    """Сторож обвязки: PY берётся из подставного INSTALL_DIR, а не из /opt/awg-bot
    машины прогона."""
    r = zone.run('printf "%s" "$PY"')
    assert not r.stdout.startswith("/opt/"), r.stdout
    assert os.path.basename(r.stdout).startswith("python"), r.stdout
