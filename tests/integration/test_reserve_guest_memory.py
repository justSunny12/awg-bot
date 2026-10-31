"""Потолок памяти и хранилище бота основы на резерве — по базе, с которой он
стартует сейчас (`rsvzone.guest_fit`), а не по той, что ушла в пакете.

Юнит гостя пишется при `--take` с MemoryMax от базы на тот момент; за
обслуживание база растёт, а гость стартует заново с реплики при передаче бота
(`released`), после сорванного возврата (`resume`) и при повторе захвата
Telegram — и подхватывает снимок `rep.bin`, чья база больше. Перед каждым своим
стартом гостя бот резерва перемонтирует хранилище (`store_mb`) и переписывает
потолок юнита скриптом зоны (`--guest-mem`, `guest_memory_mb`) по большей из
баз: в хранилище, в открытом заголовке `rep.bin` (ключа у бота резерва нет).

Цена ошибки: гость с базой больше потолка гибнет по OOM на старте и
перезапускается юнитом по кругу — пользователи основы у резерва с VPN, но без
бота, а починить потолок руками админ резерва не знает как; хранилище меньше
базы — `.prev` и копия для снимка не пишутся, гость стартует без отката;
сбой скрипта, сорвавший старт, — гость не поднят вовсе.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awgbot.domain.services.reserve import inbound_back
from awgbot.infra import rsvzone
from awgbot.util.rsvpkg import seal

from tests.conftest import fake_cmd
from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration.test_reserve_hosting import K, _REAL_GUEST_FIT, _taking, host
from tests.integration.test_reserve_resume import _age, _released
from tests.integration.test_reserve_vpn_without_bot import _emergency_done, _past_taken_wait

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["host"]
MB = 1024 * 1024
START = ["systemctl", "start", rsvzone.GUEST_UNIT]
SCRIPT = Path(__file__).resolve().parents[2] / "install" / "reserve-zone-setup.sh"


def _replica(db_size: int) -> bytes:
    return seal.snapshot_seal(K, {"pkg_gen": 7, "at": "2026-11-01T10:00:00+00:00"}, b"\0" * db_size)


class _Script:
    def __init__(self, fail: str = ""):
        self.calls: list[tuple] = []
        self.fail = fail

    def store(self, size_mb: int = 0) -> str:
        self.calls.append(("store", size_mb))
        return ""

    def run(self, mode: str, *, env=None, **_kw) -> str:
        self.calls.append((mode, dict(env or {})))
        if self.fail:
            raise rsvzone.ZoneError(self.fail)
        return ""


@pytest.fixture()
def script(monkeypatch):
    s = _Script()
    monkeypatch.setattr(rsvzone, "store", s.store)
    monkeypatch.setattr(rsvzone, "run", s.run)
    monkeypatch.setattr(rsvzone, "guest_fit", _REAL_GUEST_FIT)
    return s


# ── размер базы и сам расчёт ────────────────────────────────────────────────

def test_the_guest_base_is_the_biggest_of_the_store_the_snapshot_and_the_replica_being_laid():
    assert rsvzone.guest_db_size() == 0
    (rsvzone.RUN_DIR / "data").mkdir(parents=True, exist_ok=True)
    (rsvzone.RUN_DIR / "data" / "bot.db").write_bytes(b"\0" * (3 * MB))
    assert rsvzone.guest_db_size() == 3 * MB
    rsvzone.REP_PATH.parent.mkdir(parents=True, exist_ok=True)
    rsvzone.REP_PATH.write_bytes(_replica(5 * MB))
    assert rsvzone.guest_db_size() == 5 * MB, "база снимка, которую гость подхватит на старте, не учтена"
    assert rsvzone.guest_db_size(extra=9 * MB) == 9 * MB


def test_the_fit_remounts_the_store_and_rewrites_the_unit_ceiling_by_that_base(script):
    rsvzone.REP_PATH.parent.mkdir(parents=True, exist_ok=True)
    rsvzone.REP_PATH.write_bytes(_replica(120 * MB))
    assert rsvzone.guest_fit() == rsvzone.guest_memory_mb(120 * MB) == 96 + 120 + 16 + 360
    assert script.calls == [("store", rsvzone.store_mb(120 * MB)),
                            ("guest-mem", {"GUEST_MEMORY_MB": str(rsvzone.guest_memory_mb(120 * MB))})], script.calls


def test_a_failing_zone_script_is_logged_and_does_not_raise(script):
    script.fail = "юнита гостя нет — зона не поднята"
    assert rsvzone.guest_fit() == 0


# ── старты гостя ботом резерва ──────────────────────────────────────────────

def test_the_replica_at_hand_over_raises_the_guest_ceiling_before_the_guest_starts(host):
    """Реплика при передаче бота — база основы на момент передачи: больше ушедшей в пакете.
    Потолок по ней — до `systemctl start`, иначе гость стартует под старым."""
    _taking(host)
    host.zone.events.clear()
    host.zone.store_sizes.clear()
    blob = _replica(80 * MB)
    assert host.svc.reserve_released_store(blob) == []
    assert host.zone.guest_mem == [str(rsvzone.guest_memory_mb(80 * MB))], host.zone.guest_mem
    assert host.zone.store_sizes == [rsvzone.store_mb(80 * MB)], host.zone.store_sizes
    assert host.zone.events == ["store", "guest-mem", "start"], host.zone.events
    assert host.spawn.runs[-1] == START


def test_a_failing_ceiling_rewrite_still_starts_the_guest(host):
    _taking(host)
    host.zone.fail_on["guest-mem"] = "нужен root"
    assert host.svc.reserve_released_store(_replica(2 * MB)) == []
    assert host.spawn.runs[-1] == START and host.svc.reserve_in() == "taking", "сбой потолка сорвал старт гостя"


def test_the_guest_resumed_after_a_failed_return_starts_under_the_ceiling_of_its_snapshot(host):
    """Возврат сорвался у основы — гость снова у резерва; база — его последний `rep.bin`."""
    _released(host)
    host.zone.rep.parent.mkdir(parents=True, exist_ok=True)
    host.zone.rep.write_bytes(_replica(60 * MB))
    host.zone.events.clear()
    rm.st_in(host.svc, out="away", **{"in": "idle"})
    _age(host, inbound_back.RESUME_AFTER_S + 1)
    host.svc.reserve_in_tick()
    assert host.spawn.runs == [START], host.spawn.runs
    assert host.zone.guest_mem == [str(rsvzone.guest_memory_mb(60 * MB))]
    assert host.zone.events.index("guest-mem") < host.zone.events.index("start"), host.zone.events


def test_the_takeover_retry_starts_an_inactive_guest_under_a_fresh_ceiling(host, monkeypatch):
    from awgbot.domain.services.reserve import inbound_tgwait
    monkeypatch.setattr(inbound_tgwait.random, "uniform", lambda a, b: 1.0)
    _emergency_done(host, monkeypatch, active=False)
    _past_taken_wait(host)
    host.zone.events.clear()
    host.zone.guest_mem.clear()
    (rsvzone.RUN_DIR / "data").mkdir(parents=True, exist_ok=True)
    (rsvzone.RUN_DIR / "data" / "bot.db").write_bytes(b"\0" * (30 * MB))
    host.clock.advance(inbound_tgwait.TG_RETRY_S + 5)
    host.svc.reserve_in_tick()
    assert START in host.spawn.runs, host.spawn.runs
    assert host.zone.guest_mem == [str(rsvzone.guest_memory_mb(30 * MB))], host.zone.guest_mem
    assert host.zone.events.index("guest-mem") < host.zone.events.index("start"), host.zone.events


# ── скрипт зоны: --guest-mem ────────────────────────────────────────────────

def _funcs(*names: str) -> str:
    src = SCRIPT.read_text()
    out = []
    for name in names:
        start = src.index(f"{name}() {{")
        out.append(src[start:src.index("\n}\n", start) + 3])
    return "\n".join(out)


def _guest_mem(tmp_path: Path, unit_text: str | None, **env: str) -> subprocess.CompletedProcess:
    bin_ = tmp_path / "gbin"
    bin_.mkdir(exist_ok=True)
    calls = tmp_path / "systemctl.log"
    fake_cmd(bin_ / "systemctl", f'echo "$*" >> "{calls}"\n')
    unit = tmp_path / "guest.service"
    if unit_text is not None:
        unit.write_text(unit_text)
    prog = (f'UNIT="{unit}"\nsay() {{ printf "%s\\n" "$*"; }}\ndie() {{ printf "ОШИБКА: %s\\n" "$*" >&2; exit 1; }}\n'
            f'need_root() {{ :; }}\n{_funcs("guest_mem_mb", "cmd_guest_mem")}\ncmd_guest_mem\n')
    return subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=30,
                          env={"PATH": f"{bin_}:/usr/bin:/bin", **env})


UNIT = "[Service]\nMemorySwapMax=0\nMemoryHigh=128M\nMemoryMax=160M\nLimitCORE=0\n"


@pytest.mark.parametrize("mb, want", [("700", 700), ("", 160), ("90", 160), ("много", 160), ("99999999999", 160)])
def test_the_script_rewrites_only_the_memory_lines_of_the_guest_unit(tmp_path, mb, want):
    r = _guest_mem(tmp_path, UNIT, GUEST_MEMORY_MB=mb)
    assert r.returncode == 0, r.stderr
    text = (tmp_path / "guest.service").read_text()
    assert text == UNIT.replace("MemoryHigh=128M", f"MemoryHigh={want - 32}M").replace("MemoryMax=160M", f"MemoryMax={want}M")
    assert (tmp_path / "systemctl.log").read_text().split() == ["daemon-reload"], "юнит переписан без daemon-reload"
    assert not (tmp_path / "guest.service.tmp").exists()


def test_the_script_refuses_without_the_guest_unit(tmp_path):
    r = _guest_mem(tmp_path, None, GUEST_MEMORY_MB="700")
    assert r.returncode == 1 and "юнита гостя нет" in r.stderr
    assert not (tmp_path / "systemctl.log").exists()
