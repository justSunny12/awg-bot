"""
Юнит обвязки шлюза: строки Environment=, которые агент пишет по документу
конфигурации (`unit_set_env`, `unit_restore`).

Цена ошибки: правка чужой строки стирает ключ, по которому скрипт узнаёт своё
устройство.
"""
from __future__ import annotations

import pytest

from awgbot.infra import gwguard
from tests.unit._gw_unit_common import UNIT_TEXT, _Unit

pytestmark = pytest.mark.unit


@pytest.fixture()
def unit(tmp_path, monkeypatch):
    return _Unit(tmp_path, monkeypatch)


# ── правка юнита ─────────────────────────────────────────────────────────────

def test_rewriting_the_unit_touches_only_the_named_lines(unit):
    """Юнит несёт и ключи шлюза, и чужие строки. Правка настроек обязана
    поменять ровно свои строки, а прочее оставить байт в байт — иначе доставка
    подсетей однажды сотрёт ключ, по которому скрипт узнаёт свою машину."""
    before = gwguard.unit_set_env({"LAN_SUBNETS": "192.168.70.0/24 10.20.0.0/16",
                                   "RESOLVER": ""})
    assert before == UNIT_TEXT, "для отката возвращён не прежний текст"
    after = unit.text()
    assert 'Environment="LAN_SUBNETS=192.168.70.0/24 10.20.0.0/16"' in after
    assert 'Environment="RESOLVER="' in after
    changed = [(a, b) for a, b in zip(UNIT_TEXT.splitlines(), after.splitlines()) if a != b]
    assert len(changed) == 2 and len(after.splitlines()) == len(UNIT_TEXT.splitlines()), changed
    # тот же юнит читает скрипт обвязки и снимок агента — форматы обязаны сойтись
    assert gwguard.unit_env("LAN_SUBNETS") == "192.168.70.0/24 10.20.0.0/16"
    assert gwguard.unit_env("RESOLVER") == ""
    assert gwguard.unit_env("GATEWAY_PUBKEY") == "AAAAPUBKEY="
    assert ["systemctl", "daemon-reload"] in unit.calls, "systemd не перечитал изменённый юнит"


def test_a_unit_rewritten_by_the_channel_stays_readable_only_by_root(unit):
    """Скрипт обвязки ставит юниту 0600, канал переписывает его через
    временный файл. Вернись права к 0644 после первой доставки подсетей —
    любой локальный пользователь шлюза (OMV, шары) читал бы юнит, в том числе
    юнит прежнего выпуска, где ещё лежит конфиг аплинка с приватным ключом."""
    unit.path.chmod(0o644)                        # юнит прежнего выпуска
    gwguard.unit_set_env({"LAN_SUBNETS": "192.168.70.0/24"})
    assert unit.path.stat().st_mode & 0o777 == 0o600, oct(unit.path.stat().st_mode & 0o777)
    assert not list(unit.path.parent.glob("*.tmp")), "временный файл юнита остался рядом"


def test_admin_addresses_written_by_the_channel_read_back_as_a_list(unit):
    """Файервол шлюза берёт устройства админа из юнита; список из трёх адресов
    обязан прочитаться тремя адресами, а не одним со скобкой."""
    gwguard.unit_set_env({"ADMIN_IPS": "10.8.1.2 10.8.1.3 10.8.1.4"})
    assert gwguard.unit_admin_ips() == ["10.8.1.2", "10.8.1.3", "10.8.1.4"]


def test_an_unquoted_line_rewritten_in_quotes_reads_back_the_same(unit):
    """Скрипт пишет часть строк без кавычек (LAN_MODE, RESOLVER), канал — всегда
    в кавычках. Читатель юнита обязан видеть одно и то же значение в обоих
    видах: иначе снимок после доставки показал бы расхождение, которого нет."""
    gwguard.unit_set_env({"LAN_MODE": "1", "RESOLVER": "10.9.1.2"})
    assert 'Environment="LAN_MODE=1"' in unit.text()
    assert gwguard.unit_env("LAN_MODE") == "1" and gwguard.lan_mode() is True
    assert gwguard.unit_env("RESOLVER") == "10.9.1.2"


def test_a_key_missing_from_an_old_unit_is_added_before_the_start_line(unit):
    """Обвязка старого образца — без строки PEER_LAN_NETS. Добавленная строка
    обязана стоять до ExecStart, иначе скрипт её не увидит."""
    unit.path.write_text(UNIT_TEXT.replace('Environment="PEER_LAN_NETS="\n', ""), encoding="utf-8")
    gwguard.unit_set_env({"PEER_LAN_NETS": "10.30.0.0/24"})
    text = unit.text()
    assert text.index('Environment="PEER_LAN_NETS=10.30.0.0/24"') < text.index("EnvironmentFile=")
    assert gwguard.unit_env("PEER_LAN_NETS") == "10.30.0.0/24"


def test_a_unit_without_a_start_line_is_left_alone(unit):
    broken = "[Service]\nEnvironment=LAN_MODE=1\n"
    unit.path.write_text(broken, encoding="utf-8")
    with pytest.raises(gwguard.GwGuardError):
        gwguard.unit_set_env({"LAN_SUBNETS": "192.168.70.0/24"})
    assert unit.text() == broken and unit.calls == []


def test_the_unit_writer_refuses_quotes_and_newlines_on_its_own(unit):
    """Вторая линия обороны: даже если проверку сообщения когда-нибудь ослабят,
    запись в юнит сама не пропустит кавычку и перевод строки."""
    for bad in ('192.168.1.0/24"\nExecStart=/bin/sh', "1;reboot", "a"):
        with pytest.raises(gwguard.GwGuardError):
            gwguard.unit_set_env({"LAN_SUBNETS": bad})
    assert unit.text() == UNIT_TEXT and unit.calls == []


def test_restoring_the_unit_brings_back_the_text_byte_for_byte(unit):
    before = gwguard.unit_set_env({"LAN_SUBNETS": "192.168.70.0/24"})
    gwguard.unit_restore(before)
    assert unit.text() == UNIT_TEXT
    assert unit.calls.count(["systemctl", "daemon-reload"]) == 2
