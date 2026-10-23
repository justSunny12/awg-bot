"""Unit: вёрстка панелей блоками — панель шлюза (texts/gateway/panel.py),
главная админа (texts/admin/panel.py) и строка РФ-доступа в шапке админа
(texts/routing/access.py::routing_admin_status_line).

Блоки отделены ровно одной пустой строкой: шапка; связь (линк, канал, SSH);
железо (📈, диски, 🔌); состояние (🔀, 🩺, 📊 у шлюза; 🇷🇺, 📶, 📊 у админа);
свежесть. Пустой блок не оставляет своей пустой строки. Шлюзы в строке
РФ-доступа — второй строкой с отступом носителей.

Ошибка здесь — панель на телефоне либо слипается в простыню без разделителей,
либо расползается двойными пустыми строками там, где у шлюза нет датчиков
или транзит выключен; строка «работает · NASPi · резерв жив» снова не влезает
в ширину экрана.
"""

import pytest

from awgbot.bot import texts
from awgbot.bot.texts.fmt import DISK_INDENT
from awgbot.domain.gateway import GwStatus

pytestmark = pytest.mark.unit

BOT = "awg_test_bot"


def _gw(**kw) -> GwStatus:
    d = dict(link_up=True, handshake_age=5.0, cpu=0.4, temp=46.2, ram=17.0, ram_free_mb=6436,
             ram_used_mb=1340, ram_total_mb=7812, disk=10.0, disks=[[10.4, 200.0, "/"]],
             throttled={"raw": 0, "now": [], "ever": []},
             ssh={"port": 22, "new_plumbing": True, "filter": False})
    d.update(kw)
    return GwStatus(**d)


_NO_HW = dict(cpu=None, temp=None, ram=None, ram_free_mb=None, ram_used_mb=None, ram_total_mb=None,
              disk=None, disks=[], throttled=None)


def _lines(text: str) -> list[str]:
    return text.splitlines()


def _no_double_blank(lines: list[str]) -> None:
    for a, b in zip(lines, lines[1:]):
        assert not (a == "" and b == ""), f"две пустые строки подряд: {lines}"


def _first(lines: list[str], prefix: str) -> int:
    return next(i for i, ln in enumerate(lines) if ln.startswith(prefix))


# ── панель шлюза ─────────────────────────────────────────────────────────────

def test_gateway_hardware_block_is_separated_from_links_by_a_blank_line():
    """Между «🛡 SSH …» (последняя строка блока связи) и «📈 …» — ровно одна
    пустая строка: железо — свой блок."""
    lines = _lines(texts.gateway_panel(_gw()))
    i = _first(lines, "📈")
    assert lines[i - 1] == "", lines
    assert lines[i - 2] != "" and lines[i - 2].startswith("🛡"), lines


def test_gateway_state_block_is_separated_from_hardware_by_a_blank_line():
    """После последней строки железа («🔌 питание …») — пустая строка, затем
    блок состояния; «🩺 Здоровье» не прилипает к питанию."""
    lines = _lines(texts.gateway_panel(_gw(lan={"iface": "end0", "addr": "192.168.1.10"})))
    i = _first(lines, "🔌")
    assert lines[i + 1] == "", lines
    assert lines[i + 2] == "🔀 VPN-транзит 🟢", lines
    assert lines[i + 3] == "🩺 Здоровье ✅", lines


def test_gateway_state_block_follows_the_disks_when_there_is_no_power_reading():
    """Не Pi — строки «🔌» нет: разделитель встаёт после строки носителей."""
    lines = _lines(texts.gateway_panel(_gw(throttled=None)))
    i = _first(lines, DISK_INDENT + "диск")
    assert lines[i + 1] == "" and lines[i + 2] == "🩺 Здоровье ✅", lines


def test_gateway_without_hardware_has_one_blank_line_between_links_and_state():
    """Снимок без датчиков (cpu/ram/диски/питание — ничего): блок железа пуст,
    его пустая строка не удваивается — между «🛡 SSH» и «🩺 Здоровье» ровно
    одна пустая."""
    lines = _lines(texts.gateway_panel(_gw(**_NO_HW)))
    assert not any(ln.startswith("📈") or ln.startswith("🔌") or "диск" in ln for ln in lines), lines
    i = _first(lines, "🩺")
    assert lines[i - 1] == "" and lines[i - 2].startswith("🛡"), lines
    _no_double_blank(lines)


def test_gateway_without_transit_has_no_double_blank_before_health():
    """Транзит выключен и трафика нет: блок состояния — одна строка здоровья,
    пустых строк вокруг не больше одной с каждой стороны."""
    lines = _lines(texts.gateway_panel(_gw()))
    i = _first(lines, "🩺")
    assert lines[i - 1] == "" and lines[i - 2].startswith("🔌"), lines
    assert lines[i + 1] == "" and lines[i + 2] != "", "за здоровьем — пустая и строка свежести"
    _no_double_blank(lines)


@pytest.mark.parametrize("kw", [
    {},
    _NO_HW,
    dict(throttled=None, disks=[]),
    dict(lan={"iface": "end0", "addr": "192.168.1.10"}, month_rx=5 * 2**30, month_tx=2**30),
    dict(**_NO_HW, lan={"iface": "end0", "addr": "192.168.1.10"}),
])
def test_gateway_panel_never_has_two_blank_lines_in_a_row(kw):
    """В любом наборе блоков разделитель — одна пустая строка, и панель не
    начинается и не кончается пустой."""
    lines = _lines(texts.gateway_panel(_gw(**kw)))
    _no_double_blank(lines)
    assert lines[0] != "" and lines[-1] != "", lines
    assert lines[1] == "", "шапка — отдельно от остального"


# ── главная админа ───────────────────────────────────────────────────────────

def _adm(st: dict | None = None, **kw) -> list[str]:
    d = {"ok": True, "cpu": 0.4, "ram": 17.0, "ram_used_mb": 1340, "ram_total_mb": 7812,
         "disk": 42.0, "disks": [[10.4, 200.0, "/"]], "online_count": 0, "online_profiles": 0}
    d.update(st or {})
    return _lines(texts.admin_panel(d, **kw))


_RT_OK = {"ok": True, "active": "NASPi", "active_slot": 1, "standby": []}


def test_admin_state_block_is_separated_from_disks_by_a_blank_line_before_rf():
    """Между «диск …» и «🇷🇺 РФ-доступ» — одна пустая строка: метрики и
    состояние — разные блоки."""
    lines = _adm(routing_info=_RT_OK)
    i = _first(lines, DISK_INDENT + "диск")
    assert lines[i + 1] == "", lines
    assert lines[i + 2].startswith("🇷🇺 РФ-доступ: 🟢 работает"), lines
    _no_double_blank(lines)


def test_admin_state_block_starts_with_online_when_routing_is_off():
    """Без строки РФ-доступа блок состояния начинается с «📶 Онлайн» — и перед
    ним та же одна пустая строка после «диск …»."""
    lines = _adm()
    i = _first(lines, DISK_INDENT + "диск")
    assert lines[i + 1] == "" and lines[i + 2].startswith("📶 Онлайн"), lines
    _no_double_blank(lines)


def test_admin_without_metrics_has_a_single_blank_line_after_the_header():
    """Замер есть, но без цифр (старый снимок со свежестью): блок метрик пуст,
    после шапки — одна пустая строка и сразу состояние, не две пустых."""
    lines = _lines(texts.admin_panel({"ok": True, "age_seconds": 30, "online_count": 0, "online_profiles": 0},
                                     routing_info=_RT_OK))
    assert not any(ln.startswith("📈") or "диск" in ln for ln in lines), lines
    assert lines[1] == "" and lines[2].startswith("🇷🇺"), lines
    _no_double_blank(lines)


def test_admin_with_nothing_to_say_is_header_blank_and_freshness():
    """Ни метрик, ни состояния: шапка, одна пустая строка, строка свежести —
    пустые блоки не оставляют следов и перед свежестью не две пустых."""
    lines = _lines(texts.admin_panel({"ok": True, "age_seconds": 30}))
    assert len(lines) == 3 and lines[1] == "" and lines[2] != "", lines


def test_admin_with_metrics_but_empty_state_has_one_blank_before_freshness():
    """Есть метрики, но состоянию сказать нечего (счётчиков и трафика в снимке
    нет, РФ-доступ не настроен): между «диск …» и свежестью — одна пустая, не две."""
    lines = _lines(texts.admin_panel({"ok": True, "age_seconds": 30, "cpu": 0.4, "ram_used_mb": 1340,
                                      "ram_total_mb": 7812, "disks": [[10.4, 200.0, "/"]]}))
    _no_double_blank(lines)
    assert lines[-2] == "" and "диск" in lines[-3], lines


def test_admin_temporary_block_and_freshness_keep_single_blank_lines():
    """Временный блок (доступное обновление) — через одну пустую после
    состояния; свежесть — через одну после него."""
    lines = _adm({"traffic_rx": 2**30, "traffic_tx": 2**30}, routing_info=_RT_OK, update_tag="v9.9.9",
                 bot_username=BOT)
    i = _first(lines, "<b>")
    assert lines[i - 1] == "" and lines[i - 2].startswith("📊"), lines
    assert lines[i + 1] == "" and lines[i + 2] != "", lines
    _no_double_blank(lines)


# ── строка РФ-доступа в шапке админа ─────────────────────────────────────────

def test_rf_line_with_active_and_standby_puts_gateways_on_a_second_line():
    """Активный и резерв — две строки: состояние, под ним с отступом носителей
    «шлюз: NASPi · резерв жив»; в первой строке имён нет."""
    line = texts.routing_admin_status_line(
        {"ok": True, "active": "NASPi", "active_slot": 1,
         "standby": [{"name": "Pi4", "slot": 2, "state": "alive"}]})
    first, second = line.split("\n")
    assert first == "🇷🇺 РФ-доступ: 🟢 работает", line
    assert second == DISK_INDENT + "шлюз: NASPi · резерв жив", line


def test_rf_line_with_dead_standby_is_yellow_and_still_two_lines():
    line = texts.routing_admin_status_line(
        {"ok": True, "active": "NASPi", "active_slot": 1,
         "standby": [{"name": "Pi4", "slot": 2, "state": "dead"}]})
    assert line.split("\n") == ["🇷🇺 РФ-доступ: 🟡 работает",
                                DISK_INDENT + "шлюз: NASPi · резерв не отвечает"], line


def test_rf_line_with_single_gateway_has_the_gateway_on_the_second_line():
    line = texts.routing_admin_status_line({"ok": True, "active": "NASPi", "active_slot": 1, "standby": []})
    assert line.split("\n") == ["🇷🇺 РФ-доступ: 🟢 работает", DISK_INDENT + "шлюз: NASPi"], line


def test_rf_line_second_line_links_the_gateway_name_and_the_word_reserve():
    """С username — ссылки на карточку слота только во второй строке: имя шлюза
    и слово «резерв»; состояние резерва — текстом, первая строка чистая."""
    line = texts.routing_admin_status_line(
        {"ok": True, "active": "NASPi", "active_slot": 1,
         "standby": [{"name": "Pi4", "slot": 2, "state": "alive"}]}, BOT)
    first, second = line.split("\n")
    assert "<a" not in first, line
    assert second == (f'{DISK_INDENT}шлюз: <a href="https://t.me/{BOT}?start=gw-1">NASPi</a> · '
                      f'<a href="https://t.me/{BOT}?start=gw-2">резерв</a> жив'), line


def test_rf_line_without_gateways_is_a_single_line():
    """Работает, а шлюзов в сведениях нет — одна строка, без пустого «шлюз: »
    и без переноса."""
    line = texts.routing_admin_status_line({"ok": True, "active": "", "standby": []})
    assert line == "🇷🇺 РФ-доступ: 🟢 работает", line
    assert "\n" not in line and "шлюз" not in line


def test_rf_line_when_down_is_unchanged_single_line():
    """Ветка «🔴 недоступен …» вёрсткой не тронута: одна строка, имена в ней."""
    line = texts.routing_admin_status_line(
        {"ok": False, "active": "NASPi", "active_slot": 1, "standby": []})
    assert line == "🇷🇺 РФ-доступ: 🔴 недоступен, NASPi не отвечает", line
    assert "\n" not in line


def test_admin_panel_renders_the_gateway_line_right_under_the_rf_line():
    """На главной вторая строка идёт сразу под «🇷🇺 …» тем же отступом, что у
    «диск …», и не ломает разделители блоков."""
    lines = _adm(routing_info={"ok": True, "active": "NASPi", "active_slot": 1,
                               "standby": [{"name": "Pi4", "slot": 2, "state": "alive"}]})
    i = _first(lines, "🇷🇺")
    assert lines[i + 1] == DISK_INDENT + "шлюз: NASPi · резерв жив", lines
    assert lines[i + 2].startswith("📶"), lines
    disk = next(ln for ln in lines if "диск" in ln)
    assert len(disk) - len(disk.lstrip(" ")) == len(lines[i + 1]) - len(lines[i + 1].lstrip(" ")), \
        "отступ строки шлюзов равен отступу носителей"
    _no_double_blank(lines)
