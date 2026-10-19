"""I1 линка в install/routing-link-setup.sh:
`--rekey-stage` с `LINK_I1` пишет строку I1 только в шлюзовую сторону — у
неё инициатор рукопожатия; сторона ВПС I1 не получает никогда; без
переменной — без I1; прочие режимы (ручной `--apply`, `--rekey`) — без I1.
Значение — как есть, с `<`, `>` и пробелами; правило `S1 + 56 ≠ S2` — как
раньше.
"""
from __future__ import annotations

import re

import pytest

from tests.unit import test_link_script_rekey as _rk
from tests.unit.test_link_script_rekey import SCRIPT, _kv

pytestmark = pytest.mark.unit

vps = _rk.vps

I1 = "<b 0xc70000000108ce1bc7e1d8a3b4a9><r 16><t><rc 8>"


def test_stage_with_link_i1_writes_it_only_to_the_gateway_side(vps):
    r = vps.run("--rekey-stage", LINK_I1=I1)
    assert r.returncode == 0, r.stdout + r.stderr
    gw_next = vps.p(vps.gw, ".next").read_text(encoding="utf-8")
    vps_next = vps.p(vps.conf, ".next").read_text(encoding="utf-8")
    assert f"\nI1 = {I1}\n" in gw_next, "шлюзовая сторона без I1"
    assert gw_next.index("H4 = ") < gw_next.index("I1 = ") < gw_next.index("[Peer]"), "I1 не в [Interface]"
    assert gw_next.count("I1 = ") == 1
    assert "I1" not in vps_next, "I1 у стороны ВПС"
    assert vps.p(vps.gw, ".next").stat().st_mode & 0o777 == 0o600
    assert not vps.p(vps.gw, ".next.i1").exists(), "временный файл остался"


def test_stage_without_link_i1_has_no_i1(vps):
    assert vps.run("--rekey-stage").returncode == 0
    assert "I1" not in vps.p(vps.gw, ".next").read_text(encoding="utf-8")
    assert vps.run("--rekey-stage", LINK_I1="").returncode == 0
    assert "I1" not in vps.p(vps.gw, ".next").read_text(encoding="utf-8")


def test_a_value_with_a_newline_stays_one_line(vps):
    assert vps.run("--rekey-stage", LINK_I1="<b 0x01>\nPostUp = rm -rf /").returncode == 0
    text = vps.p(vps.gw, ".next").read_text(encoding="utf-8")
    assert "\nPostUp" not in text and "I1 = <b 0x01>PostUp = rm -rf /" in text


def test_s1_plus_56_never_equals_s2(vps):
    for _ in range(5):
        assert vps.run("--rekey-stage", LINK_I1=I1).returncode == 0
        kv = _kv(vps.p(vps.gw, ".next").read_text(encoding="utf-8"))
        assert int(kv["S1"]) + 56 != int(kv["S2"])


def test_only_the_stage_mode_writes_i1():
    text = SCRIPT.read_text(encoding="utf-8")
    body = re.search(r"^gw_conf_i1\(\) \{.*?^\}", text, re.S | re.M).group(0)
    assert '[ "$MODE" = "rekey-stage" ] || return 0' in body, "I1 пишут и другие режимы"
    vps_writer = re.search(r"^write_vps_conf\(\) \{.*?^\}", text, re.S | re.M).group(0)
    assert "I1" not in vps_writer and "gw_conf_i1" not in vps_writer
