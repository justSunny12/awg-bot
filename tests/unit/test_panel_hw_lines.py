"""Unit: строки железа на панели шлюза и на главной админа — CPU/RAM со
«свободно», носители своей строкой, питание своей строкой.

Ошибка здесь — админ смотрит на главную и не видит, что на диске шлюза
кончилось место (строка носителей пропала), или видит «питание ОК» в одной
строке с CPU, где оно терялось, или на старом сервере без списка дисков
панель молчит про диск вовсе.
"""

import pytest

from awgbot.bot import texts
from awgbot.bot.texts.fmt import DISK_INDENT
from awgbot.domain.gateway import GwStatus

pytestmark = pytest.mark.unit


def _gw(**kw) -> GwStatus:
    d = dict(link_up=True, handshake_age=5.0, cpu=0.4, temp=46.2, ram=17.0, ram_free_mb=6436,
             disk=10.0, disks=[[10.4, 200.0, "/"]], throttled={"raw": 0, "now": [], "ever": []})
    d.update(kw)
    return GwStatus(**d)


def _lines(text: str) -> list[str]:
    return text.splitlines()


# ── панель шлюза ─────────────────────────────────────────────────────────────

def test_gateway_hw_line_has_cpu_temp_and_ram_with_free_mb():
    lines = _lines(texts.gateway_panel(_gw()))
    assert "📈 CPU 0% | 46 °C · RAM 17% (свободно 6 436 МБ)" in lines, lines
    assert not any("диск" in ln and ln.startswith("📈") for ln in lines), "диск — не в строке CPU/RAM"
    assert not any("питание" in ln and ln.startswith("📈") for ln in lines), "питание — не в строке CPU/RAM"


def test_gateway_ram_without_free_value_has_no_bracket():
    """Снимок от старого агента без ram_free_mb — «RAM 17%» без скобок, а не
    «(свободно 0 МБ)»."""
    lines = _lines(texts.gateway_panel(_gw(ram_free_mb=None)))
    assert "📈 CPU 0% | 46 °C · RAM 17%" in lines, lines
    assert not any("свободно" in ln for ln in lines), lines


def test_gateway_disks_follow_the_hw_line_two_per_line():
    lines = _lines(texts.gateway_panel(_gw(
        disks=[[10.4, 200.0, "/"], [100.0, 1720.3, "/srv/a"], [1228.8, 1720.3, "/srv/b"]])))
    i = lines.index("📈 CPU 0% | 46 °C · RAM 17% (свободно 6 436 МБ)")
    assert lines[i + 1:i + 4] == [f"{DISK_INDENT}диск 1 - 10/200 ГБ · диск 2 - 100 ГБ/1,68 ТБ",
                                  f"{DISK_INDENT}диск 3 - 1,20/1,68 ТБ",
                                  "🔌 питание ОК"], lines


def test_gateway_single_disk_is_one_unnumbered_line():
    lines = _lines(texts.gateway_panel(_gw()))
    assert f"{DISK_INDENT}диск 10/200 ГБ" in lines, lines
    assert not any(ln.lstrip().startswith("диск 1 ") for ln in lines), "один носитель — без номера"


def test_gateway_without_disk_list_says_nothing_about_disks():
    """Снимок агента старше этой версии: списка нет, есть только процент —
    на панели шлюза строки диска нет вовсе (процент один, без объёма,
    ничего не говорит), остальное на месте."""
    lines = _lines(texts.gateway_panel(_gw(disks=[])))
    assert not any(ln.lstrip().startswith("диск") for ln in lines), lines
    assert "📈 CPU 0% | 46 °C · RAM 17% (свободно 6 436 МБ)" in lines and "🔌 питание ОК" in lines, lines


def test_gateway_power_is_its_own_line_ok_and_warning():
    ok = _lines(texts.gateway_panel(_gw()))
    assert "🔌 питание ОК" in ok, ok
    bad = _lines(texts.gateway_panel(_gw(throttled={"raw": 0x50001, "now": ["недонапряжение", "троттлинг"],
                                                    "ever": ["недонапряжение"]})))
    assert "🔌 питание ⚠️ недонапряжение; троттлинг" in bad, bad
    assert "🔌 питание ОК" not in bad


def test_gateway_without_power_reading_has_no_power_line():
    """Не Pi — строки «🔌» нет: непроверенное «питание ОК» — ложное спокойствие."""
    lines = _lines(texts.gateway_panel(_gw(throttled=None)))
    assert not any(ln.startswith("🔌") for ln in lines), lines


def test_gateway_hw_line_without_temperature_has_no_bar():
    lines = _lines(texts.gateway_panel(_gw(temp=None)))
    assert "📈 CPU 0% · RAM 17% (свободно 6 436 МБ)" in lines, lines


def test_gateway_lan_line_has_no_packet_tail():
    """Счётчик пакетов с роутера с главной снят — он живёт на экране
    транзита; на главной строка — только состояние."""
    st = _gw(lan={"iface": "end0", "addr": "192.168.1.10", "resolver": "10.8.0.1", "domains": 10,
                  "nets": 5, "updated_at": "2026-09-20T10:00:00+05:00", "lan_pkts": 123456})
    lines = _lines(texts.gateway_panel(st))
    assert "🔀 VPN-транзит 🟢" in lines, lines
    assert not any("пакет" in ln for ln in lines), lines


def test_gateway_month_traffic_is_its_own_line_right_after_health():
    """Трафик месяца — своей строкой «📊 …» сразу после «🩺 Здоровье ✅», а не
    хвостом строки здоровья: в одной строке значок трафика терялся, и на
    телефоне строка переносилась посреди «↑… ↓…»."""
    lines = _lines(texts.gateway_panel(_gw(month_rx=10 * 2**30, month_tx=2 * 2**30)))
    i = lines.index("🩺 Здоровье ✅")
    assert lines[i + 1].startswith("📊 12 ГБ ") and "↑" in lines[i + 1] and "↓" in lines[i + 1], lines
    assert not any("📊" in ln and ln.startswith("🩺") for ln in lines), "трафик — не в строке здоровья"


def test_gateway_without_month_traffic_has_health_but_no_traffic_line():
    """Нулевой трафик — строки «📊» нет вовсе (нули не выводим), здоровье на месте."""
    lines = _lines(texts.gateway_panel(_gw()))
    assert "🩺 Здоровье ✅" in lines, lines
    assert not any(ln.startswith("📊") for ln in lines), lines


# ── главная админа ───────────────────────────────────────────────────────────

def _adm(**kw) -> str:
    d = {"ok": True, "cpu": 0.4, "ram": 17.0, "ram_free_mb": 6436, "disk": 42.0,
         "disks": [[10.4, 200.0, "/"]]}
    d.update(kw)
    return texts.admin_panel(d)


def test_admin_hw_line_and_disk_line_from_the_disk_list():
    lines = _lines(_adm())
    i = lines.index("📈 CPU 0% · RAM 17% (свободно 6 436 МБ)")
    assert lines[i + 1] == f"{DISK_INDENT}диск 10/200 ГБ", lines
    assert not any("42%" in ln for ln in lines), "есть список носителей — процент не дублируем"


def test_admin_several_disks_two_per_line():
    lines = _lines(_adm(disks=[[10.4, 200.0, "/"], [100.0, 1720.3, "/srv/a"], [1228.8, 1720.3, "/srv/b"]]))
    i = lines.index("📈 CPU 0% · RAM 17% (свободно 6 436 МБ)")
    assert lines[i + 1:i + 3] == [f"{DISK_INDENT}диск 1 - 10/200 ГБ · диск 2 - 100 ГБ/1,68 ТБ",
                                  f"{DISK_INDENT}диск 3 - 1,20/1,68 ТБ"], lines


def test_admin_old_snapshot_without_disk_list_keeps_the_percent():
    """Снимок в БД снят прошлой версией: списка нет, процент есть — «диск 42%»
    своей строкой, RAM без «свободно»."""
    lines = _lines(_adm(disks=None, ram_free_mb=None))
    assert "📈 CPU 0% · RAM 17%" in lines, lines
    assert "диск 42%" in lines, lines
    assert not any("свободно" in ln for ln in lines), lines


def test_admin_without_any_snapshot_says_no_measurement_yet():
    lines = _lines(texts.admin_panel({"ok": True}))
    assert "📈 Метрики: ещё нет замера" in lines, lines
    assert not any(ln.lstrip().startswith("диск") for ln in lines), lines


def test_admin_snapshot_without_disk_at_all_has_no_disk_line():
    """Процент не прочитался и списка нет — строки диска нет, «диск ?» не рисуем."""
    lines = _lines(_adm(disk=None, disks=[]))
    assert "📈 CPU 0% · RAM 17% (свободно 6 436 МБ)" in lines, lines
    assert not any(ln.lstrip().startswith("диск") for ln in lines), lines


def test_server_status_snapshot_carries_free_ram_and_disks(services):
    """Снимок статуса сервера (services.server_status_cached) отдаёт панели
    «свободно МБ» и список носителей из сохранённого замера; старый замер
    без них — None и пустой список, не KeyError."""
    import datetime
    import json
    from awgbot.infra import hostmetrics
    from awgbot.util import timeutil
    ts = timeutil.to_iso(datetime.datetime.now(datetime.timezone.utc))
    services.db.set_state(hostmetrics.STATE_METRICS, json.dumps(
        {"cpu": 1, "ram": 17, "ram_free_mb": 6436, "disk": 42, "disks": [[10.4, 200.0, "/"]], "ts": ts}))
    st = services.server_status_cached()
    assert (st["ram_free_mb"], st["disks"]) == (6436, [[10.4, 200.0, "/"]]), st
    services.db.set_state(hostmetrics.STATE_METRICS, json.dumps({"cpu": 1, "ram": 17, "disk": 42, "ts": ts}))
    st = services.server_status_cached()
    assert (st["ram_free_mb"], st["disks"]) == (None, []), st
    assert "диск 42%" in texts.admin_panel(st), "старый замер — прежний процент на главной"
