"""
Юнит обвязки шлюза: строки Environment=, которые агент пишет по документу
конфигурации (`unit_set_env`, `unit_restore`), и переход с прежних имён юнита
3.3.0 (`unit_old_names`, `unit_rename_old`): HOME_SUBNETS / PEER_HOME_NETS →
LAN_SUBNETS / PEER_LAN_NETS, строка LINK_CHANNEL=1 — вон.

Цена ошибки: правка чужой строки стирает ключ, по которому скрипт узнаёт своё
устройство; юнит, оставшийся под прежними именами после обновления, — скрипт
3.3.1 видит пустые подсети и снимает VPN-транзит локальной сети.
"""
from __future__ import annotations

import pytest

from awgbot.domain.gateway import GatewayServices
from awgbot.infra import gwguard
from awgbot.infra.db import Database
from tests.unit._gw_unit_common import UNIT_TEXT, UNIT_TEXT_330, _cp, _Unit

pytestmark = pytest.mark.unit


@pytest.fixture()
def unit(tmp_path, monkeypatch):
    return _Unit(tmp_path, monkeypatch)


@pytest.fixture()
def svc(tmp_path):
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    yield GatewayServices(d)
    d.close()


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


# ── переход с прежних имён юнита (снимается со следующим сдвигом минимума) ──

def test_the_current_unit_has_no_old_names(unit):
    assert gwguard.unit_old_names() is False
    assert gwguard.unit_rename_old() is False, "юнит нового образца переписан без нужды"
    assert unit.text() == UNIT_TEXT and unit.calls == []


def test_an_old_unit_gets_the_new_names_and_loses_the_channel_line(unit):
    """Юнит 3.3.0 после обновления агента: те же значения под новыми именами,
    строки LINK_CHANNEL=1 нет, остальное — байт в байт; systemd перечитал."""
    unit.path.write_text(UNIT_TEXT_330, encoding="utf-8")
    assert gwguard.unit_old_names() is True
    assert gwguard.unit_rename_old() is True
    assert unit.text() == UNIT_TEXT, "юнит после перехода не совпал с юнитом нового образца"
    assert gwguard.unit_env("LAN_SUBNETS") == "192.168.68.0/24"
    assert gwguard.unit_old_names() is False
    assert ["systemctl", "daemon-reload"] in unit.calls


def test_a_new_name_written_before_the_transition_wins(unit):
    """Документ успел записать новое имя (строка добавлена перед EnvironmentFile),
    а прежняя осталась: прежняя снимается, новое значение не трогается."""
    unit.path.write_text(UNIT_TEXT_330, encoding="utf-8")
    gwguard.unit_set_env({"LAN_SUBNETS": "192.168.70.0/24"})
    gwguard.unit_rename_old()
    text = unit.text()
    assert "HOME_SUBNETS" not in text and text.count("LAN_SUBNETS=") == 1
    assert gwguard.unit_env("LAN_SUBNETS") == "192.168.70.0/24"


def test_the_channel_port_line_is_not_mistaken_for_the_channel_switch(unit):
    unit.path.write_text(UNIT_TEXT_330, encoding="utf-8")
    gwguard.unit_rename_old()
    assert gwguard.unit_env("LINK_CHANNEL_PORT") == "8787"
    assert "LINK_CHANNEL=1" not in unit.text()


def test_a_failed_reload_puts_the_old_unit_back(unit):
    unit.path.write_text(UNIT_TEXT_330, encoding="utf-8")
    unit.reload = [_cp(1, err=b"boom")]
    with pytest.raises(gwguard.GwGuardError):
        gwguard.unit_rename_old()
    assert unit.text() == UNIT_TEXT_330


def test_the_agent_asks_for_a_plumbing_run_after_renaming_an_old_unit(svc, unit, monkeypatch):
    """Старт агента после обновления: юнит под прежними именами — строки
    переписаны сразу, прогон обвязки нужен (он перенесёт таблицу)."""
    unit.path.write_text(UNIT_TEXT_330, encoding="utf-8")
    monkeypatch.setattr(gwguard, "lan_table_old", lambda: False)
    assert svc.plumbing_rename_due() is True
    assert unit.text() == UNIT_TEXT


def test_the_agent_asks_for_a_run_while_the_old_table_is_there(svc, unit, monkeypatch):
    monkeypatch.setattr(gwguard, "lan_table_old", lambda: True)
    assert svc.plumbing_rename_due() is True
    assert unit.text() == UNIT_TEXT


def test_nothing_is_due_on_a_current_gateway(svc, unit, monkeypatch):
    monkeypatch.setattr(gwguard, "lan_table_old", lambda: False)
    assert svc.plumbing_rename_due() is False


def test_nothing_is_due_without_plumbing(svc, tmp_path, monkeypatch):
    _Unit(tmp_path, monkeypatch, text=None)
    monkeypatch.setattr(gwguard, "lan_table_old", lambda: True)
    assert svc.plumbing_rename_due() is False
