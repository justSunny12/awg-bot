"""Unit: строки железа на панели шлюза и на главной админа — CPU и RAM
«занято/всего», носители своей строкой, питание своей строкой.

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
             ram_used_mb=1340, ram_total_mb=7812, disk=10.0, disks=[[10.4, 200.0, "/"]], throttled={"raw": 0, "now": [], "ever": []})
    d.update(kw)
    return GwStatus(**d)


def _lines(text: str) -> list[str]:
    return text.splitlines()


# ── панель шлюза ─────────────────────────────────────────────────────────────

def test_gateway_hw_line_has_cpu_temp_and_ram_used_of_total():
    """RAM — «занято/всего», а не процент: по «1,31/8 ГБ» видно и сколько
    съедено, и сколько памяти у платы вообще; хвоста «(свободно …)» нет."""
    lines = _lines(texts.gateway_panel(_gw()))
    assert "📈 CPU 0% | 46 °C · RAM 1,31/8 ГБ" in lines, lines
    assert not any("свободно" in ln or "17%" in ln for ln in lines), lines
    assert not any("диск" in ln and ln.startswith("📈") for ln in lines), "диск — не в строке CPU/RAM"
    assert not any("питание" in ln and ln.startswith("📈") for ln in lines), "питание — не в строке CPU/RAM"


@pytest.mark.parametrize("kw", [
    dict(ram_used_mb=None, ram_total_mb=None),   # снимок прежнего выпуска: полей нет вовсе
    dict(ram_used_mb=None),                      # занято не прочиталось
    dict(ram_total_mb=None),                     # всего не прочиталось
    dict(ram_total_mb=0),                        # нулевое «всего» — делить не на что
])
def test_gateway_ram_falls_back_to_percent_without_used_or_total(kw):
    """Снимок от агента прежнего выпуска несёт только процент — показываем
    «RAM 17%», а не «None МБ/None» и не пустую строку; «свободно» не
    возвращается даже при наличии ram_free_mb."""
    lines = _lines(texts.gateway_panel(_gw(**kw)))
    assert "📈 CPU 0% | 46 °C · RAM 17%" in lines, lines
    assert not any("свободно" in ln or "None" in ln for ln in lines), lines


def test_gateway_ram_small_board_shows_megabytes():
    """Плата на 1 ГБ: ядро видит 976 МБ — «478 МБ/1 ГБ», не «478 МБ/976 МБ»."""
    lines = _lines(texts.gateway_panel(_gw(ram_used_mb=478, ram_total_mb=976)))
    assert "📈 CPU 0% | 46 °C · RAM 478 МБ/1 ГБ" in lines, lines


def test_gateway_disks_follow_the_hw_line_two_per_line():
    lines = _lines(texts.gateway_panel(_gw(
        disks=[[10.4, 200.0, "/"], [100.0, 1720.3, "/srv/a"], [1228.8, 1720.3, "/srv/b"]])))
    i = lines.index("📈 CPU 0% | 46 °C · RAM 1,31/8 ГБ")
    assert lines[i + 1:i + 4] == [f"{DISK_INDENT}диск 1 - 10/200 ГБ · диск 2 - 100 ГБ/1,68 ТБ",
                                  f"{DISK_INDENT}диск 3 - 1,2/1,68 ТБ",
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
    assert "📈 CPU 0% | 46 °C · RAM 1,31/8 ГБ" in lines and "🔌 питание ОК" in lines, lines


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
    assert "📈 CPU 0% · RAM 1,31/8 ГБ" in lines, lines


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


@pytest.mark.parametrize("free_gb, want", [
    (8.5, "8,5 ГБ"),        # хвостовой ноль не выводим
    (0.75, "0,75 ГБ"),      # две значащие
    (3.248, "3,25 ГБ"),     # округление до двух значащих
    (9.0, "9 ГБ"),          # целое — без дроби
    (87.4, "87 ГБ"),        # от 10 ГБ — целыми
])
def test_gateway_health_free_disk_uses_two_significant_decimals(free_gb, want):
    """На экране здоровья «диск: свободно 8,5 ГБ» — две значащие цифры дроби
    без хвостовых нулей, как у всех дробей проекта; от 10 ГБ — целыми."""
    text = texts.gateway_health(_gw(disk_free_gb=free_gb))
    assert f"диск: свободно {want}" in text, text


# ── главная админа ───────────────────────────────────────────────────────────

def _adm(**kw) -> str:
    d = {"ok": True, "cpu": 0.4, "ram": 17.0, "ram_free_mb": 6436, "ram_used_mb": 1340,
         "ram_total_mb": 7812, "disk": 42.0, "disks": [[10.4, 200.0, "/"]]}
    d.update(kw)
    return texts.admin_panel(d)


def test_admin_hw_line_and_disk_line_from_the_disk_list():
    lines = _lines(_adm())
    i = lines.index("📈 CPU 0% · RAM 1,31/8 ГБ")
    assert lines[i + 1] == f"{DISK_INDENT}диск 10/200 ГБ", lines
    assert not any("42%" in ln for ln in lines), "есть список носителей — процент не дублируем"


def test_admin_several_disks_two_per_line():
    lines = _lines(_adm(disks=[[10.4, 200.0, "/"], [100.0, 1720.3, "/srv/a"], [1228.8, 1720.3, "/srv/b"]]))
    i = lines.index("📈 CPU 0% · RAM 1,31/8 ГБ")
    assert lines[i + 1:i + 3] == [f"{DISK_INDENT}диск 1 - 10/200 ГБ · диск 2 - 100 ГБ/1,68 ТБ",
                                  f"{DISK_INDENT}диск 3 - 1,2/1,68 ТБ"], lines


def test_admin_old_snapshot_without_disk_list_keeps_the_percent():
    """Снимок в БД снят прошлой версией: списка нет, процент есть — «диск 42%»
    своей строкой, RAM — процентом, без «свободно»."""
    lines = _lines(_adm(disks=None, ram_free_mb=None, ram_used_mb=None, ram_total_mb=None))
    assert "📈 CPU 0% · RAM 17%" in lines, lines
    assert "диск 42%" in lines, lines
    assert not any("свободно" in ln for ln in lines), lines


@pytest.mark.parametrize("kw", [
    dict(ram_used_mb=None, ram_total_mb=None),
    dict(ram_used_mb=None),
    dict(ram_total_mb=None),
    dict(ram_total_mb=0),
])
def test_admin_ram_falls_back_to_percent_without_used_or_total(kw):
    """Замер прошлой версии в БД (или не прочитавшийся meminfo) — «RAM 17%»,
    а не «None МБ/None»; «свободно» не возвращается даже при ram_free_mb."""
    lines = _lines(_adm(**kw))
    assert "📈 CPU 0% · RAM 17%" in lines, lines
    assert not any("свободно" in ln or "None" in ln for ln in lines), lines


def test_admin_ram_in_megabytes_on_a_small_server():
    lines = _lines(_adm(ram_used_mb=300, ram_total_mb=512))
    assert "📈 CPU 0% · RAM 300 МБ/512 МБ" in lines, lines


def test_admin_without_any_snapshot_says_no_measurement_yet():
    lines = _lines(texts.admin_panel({"ok": True}))
    assert "📈 Метрики: ещё нет замера" in lines, lines
    assert not any(ln.lstrip().startswith("диск") for ln in lines), lines


def test_admin_snapshot_without_disk_at_all_has_no_disk_line():
    """Процент не прочитался и списка нет — строки диска нет, «диск ?» не рисуем."""
    lines = _lines(_adm(disk=None, disks=[]))
    assert "📈 CPU 0% · RAM 1,31/8 ГБ" in lines, lines
    assert not any(ln.lstrip().startswith("диск") for ln in lines), lines


def test_server_status_snapshot_carries_ram_used_total_and_disks(services):
    """Снимок статуса сервера (services.server_status_cached) отдаёт панели
    «занято/всего» МБ и список носителей из сохранённого замера; старый замер
    без них — None и пустой список, не KeyError, и на главной — процент."""
    import datetime
    import json
    from awgbot.infra import hostmetrics
    from awgbot.util import timeutil
    ts = timeutil.to_iso(datetime.datetime.now(datetime.timezone.utc))
    services.db.set_state(hostmetrics.STATE_METRICS, json.dumps(
        {"cpu": 1, "ram": 17, "ram_free_mb": 6436, "ram_used_mb": 1340, "ram_total_mb": 7812,
         "disk": 42, "disks": [[10.4, 200.0, "/"]], "ts": ts}))
    st = services.server_status_cached()
    assert (st["ram_free_mb"], st["disks"]) == (6436, [[10.4, 200.0, "/"]]), st
    assert (st["ram_used_mb"], st["ram_total_mb"]) == (1340, 7812), st
    assert "📈 CPU 1% · RAM 1,31/8 ГБ" in texts.admin_panel(st), texts.admin_panel(st)
    services.db.set_state(hostmetrics.STATE_METRICS, json.dumps({"cpu": 1, "ram": 17, "disk": 42, "ts": ts}))
    st = services.server_status_cached()
    assert (st["ram_free_mb"], st["ram_used_mb"], st["ram_total_mb"], st["disks"]) == (None, None, None, []), st
    panel = texts.admin_panel(st)
    assert "диск 42%" in panel and "📈 CPU 1% · RAM 17%" in panel, "старый замер — прежние проценты на главной"
