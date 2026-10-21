"""Правки скриптов поставки резерва после двух ревью и отметки monotonic.

- install/reserve-zone-setup.sh: `--plan` говорит про dnsmasq; таблица зоны
  рисуется целиком и со входом вебхука с veth, когда порт известен
  (`--serve`); `--wipe` в фазе relay оставляет фазу и адрес основы;
- install/lib/update.sh и restore.sh: сторож `reserve_busy_guard` (tools.rsvbusy,
  код 3 — отказ) до остановки сервиса;
- awg-bot.sh `reserve teardown` и `reserve tgpool show|refresh`;
- util/livestep.py: mono_since / mono_due / mono_expired — отметки из прежней
  загрузки хоста.

Скрипты гоняются bash'ем с подставными бинарями в PATH (ip, nft, id, tc,
systemctl, python-модуль сторожа — функцией), хост не трогается.

Цена ошибки: таблица зоны без входа вебхука — Telegram не достучится до бота
основы на резерве, переезд «прошёл», а бот молчит; таблица, дописанная
повторным вызовом, — правила множатся; `--wipe` в relay, сбросивший фазу, —
файервол снимает пересылку хвоста, клиенты, не сменившие запись, без сервера;
обновление посреди переезда — код меняется под гостем, возврат сливает
чужую схему; отметка из прошлой загрузки, принятая за свежую, — проба или
шаг автомата висят до следующей перезагрузки.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from awgbot.util import livestep
from tests.conftest import fake_cmd
from tests.unit.test_reserve_zone_script import _Box, _plan
from tests.unit.test_reserve_zone_serve_wipe import _Host

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


# ── livestep: отметки monotonic ─────────────────────────────────────────────

@pytest.mark.parametrize("mark, now, want", [
    (100.0, 160.0, 60.0),            # обычная отметка
    (0, 160.0, float("inf")),        # отметки нет — давно
    (None, 160.0, float("inf")),
    ("мусор", 160.0, float("inf")),
    (5_000_000.0, 160.0, float("inf")),   # из прежней загрузки (в будущем)
    (160.5, 160.0, -0.5),            # секунда дрожи — ещё своя отметка
])
def test_mono_since_treats_a_mark_from_a_previous_boot_as_old(mark, now, want):
    assert livestep.mono_since(mark, now) == want


@pytest.mark.parametrize("mark, now, interval, want", [
    (100.0, 150.0, 60, False), (100.0, 160.0, 60, True), (None, 1.0, 60, True), (10 ** 7, 5.0, 60, True),
])
def test_mono_due_is_due_without_a_mark_or_with_one_from_a_previous_boot(mark, now, interval, want):
    assert livestep.mono_due(mark, now, interval) is want


@pytest.mark.parametrize("deadline, now, want", [
    (200.0, 100.0, False),                                   # срок впереди
    (200.0, 201.0, True),                                    # вышел
    (0, 100.0, False), (None, 100.0, False), ("x", 100.0, False),   # срока нет — не вышел
    (100.0 + livestep.MONO_SANE_S - 1, 100.0, False),        # далеко, но в пределах 30 дней
    (100.0 + livestep.MONO_SANE_S + 1, 100.0, True),         # дальше 30 дней — прежняя загрузка
])
def test_mono_expired_counts_a_deadline_beyond_thirty_days_as_from_a_previous_boot(deadline, now, want):
    assert livestep.mono_expired(deadline, now) is want


# ── скрипт зоны: dnsmasq в плане ────────────────────────────────────────────

def test_the_plan_says_dnsmasq_yes_when_it_is_installed(tmp_path):
    box = _Box(tmp_path)
    fake_cmd(box.bin / "dnsmasq", "exit 0\n")
    p = _plan(box.run("--plan"))
    assert p["dnsmasq"] == "yes" and list(p)[-1] == "dnsmasq", "строка dnsmasq — последней"


@pytest.mark.skipif(os.path.exists("/usr/sbin/dnsmasq") or bool(shutil.which("dnsmasq", path="/usr/bin:/bin")),
                    reason="на машине прогона dnsmasq стоит")
def test_the_plan_says_dnsmasq_no_without_it(tmp_path):
    assert _plan(_Box(tmp_path).run("--plan"))["dnsmasq"] == "no", "без dnsmasq план обещает резолвер зоны"


# ── скрипт зоны: таблица целиком и вход вебхука ─────────────────────────────

class _Zone(_Host):
    """Хост из test_reserve_zone_serve_wipe, где `ip netns exec` исполняет
    команду, а `nft -f -` кладёт текст таблицы в файл (последний вызов)."""

    def __init__(self, tmp: Path):
        super().__init__(tmp)
        self.nft = tmp / "nft.txt"
        os.replace(self.bin / "ip", self.bin / "ip.real")
        fake_cmd(self.bin / "ip", f'if [ "$1 $2" = "netns exec" ]; then shift 3; exec "$@"; fi\n'
                                  f'exec {self.bin / "ip.real"} "$@"\n')
        fake_cmd(self.bin / "nft", f'if [ "$1 $2" = "-f -" ]; then cat > {self.nft}; fi\nexit 0\n')

    def table(self) -> str:
        return self.nft.read_text(encoding="utf-8") if self.nft.exists() else ""


@pytest.fixture()
def zone(tmp_path):
    z = _Zone(tmp_path)
    z.put_state(phase="taking", iface="awg0")
    return z


WEBHOOK_IN = 'iifname "awggv-n" ip daddr 169.254.77.2 tcp dport 8443 accept'


def test_serve_redraws_the_zone_table_with_the_webhook_door(zone):
    r = zone.run("--serve", WEBHOOK_PORT="8443")
    assert r.returncode == 0, r.stdout + r.stderr
    lines = [ln.strip() for ln in zone.table().splitlines()]
    assert WEBHOOK_IN in lines, f"вход вебхука с veth не нарисован:\n{zone.table()}"
    assert lines[:2] == ["table inet awg_guest", "delete table inet awg_guest"], \
        "таблица не пересоздаётся целиком — повторный вызов допишет правила"
    assert sum(1 for ln in lines if ln.startswith("table inet awg_guest {")) == 1


def test_without_a_known_port_there_is_no_webhook_door(zone):
    assert zone.run("--serve").returncode == 0
    assert zone.table() and "tcp dport" not in zone.table().replace("tcp dport 53", "").replace("tcp dport 8787", ""), \
        zone.table()


def test_a_repeated_serve_gives_the_same_table(zone):
    zone.run("--serve", WEBHOOK_PORT="8443")
    first = zone.table()
    zone.run("--serve", WEBHOOK_PORT="8443")
    assert zone.table() == first and first.count(WEBHOOK_IN) == 1


# ── скрипт зоны: wipe в relay ───────────────────────────────────────────────

def test_wipe_during_relay_keeps_the_phase_and_the_target(tmp_path):
    h = _Host(tmp_path)
    h.put_state(phase="relay", relay_to="198.51.100.10:45871", webhook_port="443", iface="awg0")
    r = h.run("--wipe")
    assert r.returncode == 0, r.stdout + r.stderr
    st = h.state()
    assert (st.get("phase"), st.get("relay_to")) == ("relay", "198.51.100.10:45871"), \
        f"wipe в relay снял пересылку хвоста: {st}"
    assert st.get("webhook_port") == "" and st.get("wiped_at"), st


def test_wipe_outside_relay_resets_the_zone_to_idle(tmp_path):
    h = _Host(tmp_path)
    h.put_state(phase="serving", relay_to="198.51.100.10:45871", webhook_port="443", iface="awg0")
    assert h.run("--wipe").returncode == 0
    st = h.state()
    assert (st.get("phase"), st.get("relay_to"), st.get("iface")) == ("idle", "", ""), st


# ── сторож обновления и восстановления ──────────────────────────────────────

def _func(text: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", text, re.S | re.M)
    assert m, f"нет функции {name}()"
    return m.group(0)


UPDATE = (ROOT / "install" / "lib" / "update.sh").read_text(encoding="utf-8")
RESTORE = (ROOT / "install" / "lib" / "restore.sh").read_text(encoding="utf-8")
SCRIPT = (ROOT / "awg-bot.sh").read_text(encoding="utf-8")


def _guard(tool: str | None) -> subprocess.CompletedProcess:
    """Вызов сторожа так, как его зовут cmd_update/cmd_restore, под set -euo pipefail."""
    lines = ["set -euo pipefail", "die() { echo \"DIE: $*\"; exit 1; }"]
    if tool is not None:
        lines.append(f"bot_tool() {{ {tool}; }}")
    lines += [_func(UPDATE, "reserve_busy_guard"),
              'declare -F reserve_busy_guard >/dev/null 2>&1 && reserve_busy_guard "обновление"', "echo continued"]
    return subprocess.run(["bash", "-c", "\n".join(lines)], capture_output=True, text=True)


def test_the_guard_stops_the_update_when_the_server_is_busy():
    r = _guard('echo "клиенты этого сервера сейчас у резерва «Beta» (away)"; return 3')
    assert r.returncode != 0 and "continued" not in r.stdout, "обновление пошло посреди переезда"
    assert "DIE: обновление сейчас нельзя: клиенты этого сервера сейчас у резерва «Beta» (away)" in r.stdout, r.stdout


@pytest.mark.parametrize("tool", ["return 0", "return 1", None], ids=["free", "old-code-without-the-tool", "no-bot-tool"])
def test_the_guard_lets_the_update_go_when_free_or_unknown(tool):
    """Свободен — да; прежний код без tools/rsvbusy.py (python -m падает) или
    без bot_tool — сторож не мешает обновлению."""
    r = _guard(tool)
    assert r.returncode == 0 and "continued" in r.stdout, r.stdout + r.stderr


@pytest.mark.parametrize("text, func", [(UPDATE, "cmd_update"), (RESTORE, "cmd_restore")])
def test_the_guard_runs_before_the_service_is_stopped(text, func):
    body = _func(text, func)
    guard = body.index("reserve_busy_guard")
    stop = body.index('systemctl stop "$SERVICE"')
    assert guard < stop, f"{func}: сторож после остановки сервиса — бот уже лёг, а отказ пришёл"


# ── awg-bot reserve teardown | tgpool ───────────────────────────────────────

def _reserve(tmp_path: Path, *args: str, zone_fail: str = "", root: bool = True) -> subprocess.CompletedProcess:
    inst = tmp_path / "inst"
    (inst / "install").mkdir(parents=True, exist_ok=True)
    log = tmp_path / "zone.log"
    fail = f'[ "$1" = "{zone_fail}" ] && exit 1\n' if zone_fail else ""
    (inst / "install" / "reserve-zone-setup.sh").write_text(f'echo "$*" >> {log}\n{fail}exit 0\n', encoding="utf-8")
    harness = "\n".join(["set -euo pipefail", f'INSTALL_DIR="{inst}"', f'DATA_DIR="{tmp_path / "data"}"',
                         'ENV_FILE=/dev/null CONF_DIR=/dev/null', "die() { echo \"DIE: $*\"; exit 1; }",
                         ("require_root() { :; }" if root else "require_root() { die нужен root; }"), _func(SCRIPT, "cmd_reserve"), f"cmd_reserve {' '.join(args)}"])
    return subprocess.run(["bash", "-c", harness], capture_output=True, text=True)


def _zone_calls(tmp_path: Path) -> list[str]:
    p = tmp_path / "zone.log"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def test_teardown_takes_the_zone_down_and_wipes_the_store(tmp_path):
    r = _reserve(tmp_path, "teardown")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _zone_calls(tmp_path) == ["--teardown", "--wipe"]


def test_teardown_wipes_the_store_even_if_the_zone_does_not_come_down(tmp_path):
    """Аварийный глагол: зона не снялась (полусобранная) — данные основы в
    хранилище гостя всё равно должны быть стёрты."""
    _reserve(tmp_path, "teardown", zone_fail="--teardown")
    assert "--wipe" in _zone_calls(tmp_path), f"хранилище с данными основы не стёрто: {_zone_calls(tmp_path)}"


def test_tgpool_show_prints_the_pool_file_or_an_empty_pool(tmp_path):
    r = _reserve(tmp_path, "tgpool", "show")
    assert r.returncode == 0 and r.stdout.strip() == '{"pool": []}', r.stdout + r.stderr
    (tmp_path / "data" / "reserve").mkdir(parents=True)
    (tmp_path / "data" / "reserve" / "tg_pool.json").write_text('{"pool": ["149.154.160.0/20"]}', encoding="utf-8")
    assert "149.154.160.0/20" in _reserve(tmp_path, "tgpool").stdout, "show — по умолчанию"


@pytest.mark.parametrize("verb", ["show", "refresh"])
def test_tgpool_needs_root(tmp_path, verb):
    r = _reserve(tmp_path, "tgpool", verb, root=False)
    assert r.returncode != 0 and "нужен root" in r.stdout, r.stdout
    assert not (tmp_path / "data" / "reserve" / "tg_pool.refresh").exists()


def test_tgpool_refresh_puts_the_mark_for_the_bot(tmp_path):
    r = _reserve(tmp_path, "tgpool", "refresh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "data" / "reserve" / "tg_pool.refresh").exists()


def test_an_unknown_tgpool_verb_is_a_usage_error(tmp_path):
    r = _reserve(tmp_path, "tgpool", "drop")
    assert r.returncode != 0 and "show | refresh" in r.stdout
