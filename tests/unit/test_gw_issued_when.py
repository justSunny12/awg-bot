"""routing-gw-setup.sh: время выпуска файла конфигурации в отказе — с меткой
системного пояса (скрипт идёт в терминале хоста). Функции — из НАСТОЯЩЕГО
скрипта, под dash (на шлюзе sh — dash); GNU date подменён: на машине
разработки его может не быть."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import SH

pytestmark = pytest.mark.unit

SRC = (Path(__file__).resolve().parents[2] / "install" / "routing-gw-setup.sh").read_text(encoding="utf-8")


def _func(name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", SRC, re.S | re.M)
    assert m, f"в routing-gw-setup.sh нет функции {name}()"
    return m.group(0)


@pytest.mark.parametrize("off,label", [("+0300", "UTC+3"), ("+0530", "UTC+5:30"), ("+0545", "UTC+5:45"),
                                       ("+0000", "UTC"), ("-0000", "UTC"), ("-0500", "UTC-5"),
                                       ("-0330", "UTC-3:30"), ("+1000", "UTC+10"), ("+1345", "UTC+13:45")])
def test_tz_label(off, label):
    r = subprocess.run([SH, "-c", _func("tz_label") + f'\ntz_label "{off}"'], capture_output=True, text=True,
                       timeout=10, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0 and r.stdout == label, (r.stdout, r.stderr)


def test_issued_when_has_the_label(tmp_path):
    fake = tmp_path / "date"
    fake.write_text('#!/bin/sh\n'
                    'case "$3" in +%z) echo "+0530" ;; '
                    '*) printf \'%s\\n\' "$3" | sed "s/^+//; s/%d.%m.%y %H:%M/15.09.26 12:00/" ;; esac\n',
                    encoding="utf-8")
    fake.chmod(0o755)
    prog = _func("tz_label") + "\n" + _func("issued_when") + '\nissued_when 1789462800'
    r = subprocess.run([SH, "-c", prog], capture_output=True, text=True, timeout=10,
                       env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
    assert r.stdout.rstrip("\n") == "15.09.26 12:00 (UTC+5:30)", (r.stdout, r.stderr)


def test_issued_when_without_gnu_date_prints_the_number():
    prog = _func("tz_label") + "\n" + _func("issued_when") + '\ndate() { return 1; }\nissued_when 1789462800'
    r = subprocess.run([SH, "-c", prog], capture_output=True, text=True, timeout=10, env={"PATH": "/usr/bin:/bin"})
    assert r.stdout == "1789462800"


def test_refusal_text():
    """Т21: «ОТКАЗ: файл конфигурации выпущен …, текущая конфигурация от …» —
    без скобок в скобках и без точки в конце."""
    assert ('say "ОТКАЗ: файл конфигурации выпущен $(issued_when "$_bundle_issued"), '
            'текущая конфигурация от $(issued_when "$_have")"') in SRC
