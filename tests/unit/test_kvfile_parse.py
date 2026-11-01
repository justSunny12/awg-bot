"""util/kvfile.parse — один разбор KEY=VALUE у всех читателей вывода скриптов и
файлов состояния.

Что стережём: `--status`/`--plan` зоны резерва, зондов кворума и линка шлюза к
резерву, `systemctl show` юнита обвязки, статус скрипта списков шлюза, вывод
скрипта линка пары и install/awg.lock кита разбираются одинаково: комментарии,
пустые строки, строки без «=» и строки, где слева от «=» не ключ (пробел внутри —
«ВНИМАНИЕ: …=…» скрипта), пропускаются; «=» в значении сохраняется; повтор
ключа — последнее значение.

Цена ошибки: копии расходились — одна брала «# x=1» ключом «# x», другая
резала строку предупреждения в ключ с пробелами; читатель, полагавшийся на
свою копию, видел поле, которого скрипт не печатал (или терял «=» в пути и
ключе), а правка разбора в одном месте не доходила до остальных.
"""
from __future__ import annotations

import types

import pytest

from awgbot.util import kvfile

pytestmark = pytest.mark.unit

# вывод, в котором есть всё, на чём копии расходились
OUT = ("# шапка=не ключ\n"
       "\n"
       "ВНИМАНИЕ: конфиг awg1 не найден, MTU=1420\n"
       "=пустой ключ\n"
       "строка без знака\n"
       "  link = up  \n"
       "src=10.0.0.1\n"
       "url=https://x/?a=1&b=2\n"
       "open=0\n"
       "open=1\n")
WANT = {"link": "up", "src": "10.0.0.1", "url": "https://x/?a=1&b=2", "open": "1"}


def test_parse_skips_everything_that_is_not_a_key_line():
    assert kvfile.parse(OUT) == WANT
    assert kvfile.parse("") == {} and kvfile.parse(None) == {}
    assert kvfile.parse('Q="в кавычках"', strip_quotes=True) == {"Q": "в кавычках"}


def test_read_is_parse_of_the_file(tmp_path):
    p = tmp_path / "state"
    p.write_text(OUT, encoding="utf-8")
    assert kvfile.read(p) == WANT
    assert kvfile.read(tmp_path / "nope") == {}


# ── читатели: тот же вывод — тот же словарь ─────────────────────────────────

def test_zone_status_and_plan(monkeypatch):
    from awgbot.infra import rsvzone
    monkeypatch.setattr(rsvzone, "run", lambda mode, **kw: OUT)
    assert rsvzone.status() == WANT and rsvzone.plan() == WANT


def test_reserve_link_status_on_the_gateway(monkeypatch):
    from awgbot.infra.gwguard import rsv
    monkeypatch.setattr(rsv, "run", lambda mode, *a, **kw: OUT)
    assert rsv.status() == WANT


def test_quorum_probe_status_ignores_a_warning_with_an_equals_sign(monkeypatch):
    """Строка «ВНИМАНИЕ: … hs_awgq0=…» — не поле: интерфейсы и рукопожатия — только
    из строк-ключей."""
    from awgbot.infra import quoprobe
    out = "ВНИМАНИЕ: старый ns hs_awgq1=5\nns=present\nifaces=awgq0\nhs_awgq0=12\n"
    monkeypatch.setattr(quoprobe, "run", lambda mode, *a, **kw: out)
    assert quoprobe.status() == {"ns": "present", "ifaces": ["awgq0"], "hs": {"awgq0": 12}}


def test_gateway_unit_state(monkeypatch):
    from awgbot.infra import gwguard
    monkeypatch.setattr(gwguard.subprocess, "run",
                        lambda *a, **kw: types.SimpleNamespace(returncode=0, stdout=OUT.encode(), stderr=b""))
    assert gwguard.unit_state() == WANT


def test_gateway_lists_status(tmp_path, monkeypatch):
    from awgbot.infra import gwguard
    p = tmp_path / "lists.status"
    p.write_text(OUT, encoding="utf-8")
    monkeypatch.setattr(gwguard, "LAN_STATUS_FILE", str(p))
    assert gwguard.lists_status() == WANT
    monkeypatch.setattr(gwguard, "LAN_STATUS_FILE", str(tmp_path / "nope"))
    assert gwguard.lists_status() == {}


def test_pair_link_script_output(monkeypatch):
    from awgbot.domain.services.reserve import invite
    monkeypatch.setattr(invite.subprocess, "run",
                        lambda *a, **kw: types.SimpleNamespace(returncode=0, stdout=OUT.encode(), stderr=b""))
    me = types.SimpleNamespace(_pair_script=lambda: "/nonexistent/reserve-pair-setup.sh")
    assert invite.ReserveInviteMixin._run_pair_script(me, "--status") == WANT


def test_the_gateway_kit_lock(tmp_path):
    from tools import gwkit
    (tmp_path / "install").mkdir()
    (tmp_path / "install" / "awg.lock").write_text(OUT, encoding="utf-8")
    assert gwkit._lock(tmp_path) == WANT
    with pytest.raises(gwkit.KitError, match="нет install/awg.lock"):
        gwkit._lock(tmp_path / "nope")
