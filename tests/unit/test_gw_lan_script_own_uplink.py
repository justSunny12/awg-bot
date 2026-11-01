"""
awg-lan-domain.sh — имя аплинка из статуса обвязки (UPLINK_IF в
gateway.status) для запрета хоста сервера AWG.

Стережём: аплинк не awg0 — запрет берётся из его конфига; сбой чтения или
разбора статуса (упал sed, head или tr) при существующем файле — код 1
«рабочий шаг правки не прошёл — списки не тронуты», до записи. Цена ошибки:
прежде упавший шаг конвейера молча давал awg0, конфига awg0 на такой машине
нет, запрет выходил пустым — хост ВПС ложился «в туннель» (туннель уводился в
туннель, шлюз запирал себя) с «добавлен» и кодом 0. Файла статуса нет — awg0,
как прежде. Пути — AWG_GW_STATUS и AWG_UPLINK_DIR (по умолчанию прежние);
сценарии сбоя подставляют в текст скрипта сами пути по умолчанию — так тот же
тест гоняется и на выпуске без переменных.
"""
from __future__ import annotations

import pytest
from tests.unit import _gw_lan_common as _c
from tests.unit._gw_lan_common import RU_USER, VPN_USER, _flaky, _heredoc, _log, _own, _run

script, domain_env, own_env = _c.script, _c.domain_env, _c.own_env

UNTOUCHED = "рабочий шаг правки не прошёл — списки не тронуты"
REFUSED = "это адрес самого сервера AWG — в туннель его не направить"


def _uplink(own_env, tmp_path, script, *, by_env: bool):
    """Аплинк обвязки — wg1, его конфиг с Endpoint ВПС; awg0.conf нет. by_env —
    пути переменными окружения, иначе — подставлены в текст скрипта."""
    tool, dns_d, dump, log, env = own_env
    status = tmp_path / "gateway.status"
    status.write_text("LAN_MODE=1\nUPLINK_IF=wg1\n", encoding="utf-8")
    amn = tmp_path / "amneziawg"; amn.mkdir()
    (amn / "wg1.conf").write_text("[Peer]\nEndpoint = vps.example.net:51820\n", encoding="utf-8")
    env = {k: v for k, v in env.items() if k != "AWG_UPLINK_CONF"}
    if by_env:
        env.update(AWG_GW_STATUS=str(status), AWG_UPLINK_DIR=str(amn))
    else:
        tool = tmp_path / "awg-lan-domain-paths.sh"
        tool.write_text(_heredoc(script, "LAN_DOMAIN", "DOMEOF")
                        .replace("/etc/awg-gw/gateway.status", str(status))
                        .replace("/etc/amnezia/amneziawg", str(amn)), encoding="utf-8")
    _own(dns_d, vpn=["news.org"])
    return tool, dns_d, log, env, status


@pytest.fixture()
def uplink_env(own_env, tmp_path, script):
    return _uplink(own_env, tmp_path, script, by_env=False)


@pytest.fixture()
def uplink_env_vars(own_env, tmp_path, script):
    return _uplink(own_env, tmp_path, script, by_env=True)


def _state(dns_d) -> dict:
    return {f: (dns_d / f).read_text() for f in (VPN_USER, RU_USER)}


def test_an_uplink_other_than_awg0_bans_its_server_host(uplink_env):
    tool, dns_d, log, env, status = uplink_env
    before = _state(dns_d)
    r = _run(tool, env, "add", "vps.example.net")
    assert r.returncode == 0 and REFUSED in r.stdout, r.stdout + r.stderr
    assert _state(dns_d) == before
    r = _run(tool, env, "add", "alpha.com")
    assert r.returncode == 0 and "добавлен" in r.stdout, r.stdout + r.stderr


@pytest.mark.parametrize("tool_name,when", [("sed", "*UPLINK_IF*"), ("head", "-n1"), ("tr", "-cd*")])
def test_a_failed_uplink_name_does_not_lift_the_ban(uplink_env, tool_name, when):
    tool, dns_d, log, env, status = uplink_env
    before = _state(dns_d)
    _flaky(env, tool_name, when=when)
    r = _run(tool, env, "add", "vps.example.net")
    assert r.returncode == 1 and UNTOUCHED in r.stderr, r.stdout + r.stderr
    assert "добавлен" not in r.stdout, r.stdout
    assert _state(dns_d) == before, "хост ВПС лёг в списки"
    assert "systemctl" not in _log(log)


def test_list_still_works_when_the_status_cannot_be_read(uplink_env):
    """Хост сервера нужен только правке: list по-прежнему отвечает."""
    tool, dns_d, log, env, status = uplink_env
    _flaky(env, "sed", when="*UPLINK_IF*")
    r = _run(tool, env, "list")
    assert r.returncode == 0 and "vpn news.org" in r.stdout, r.stdout + r.stderr


def test_the_paths_come_from_the_environment(uplink_env_vars):
    """AWG_GW_STATUS и AWG_UPLINK_DIR — как AWG_LAN_DUMP и AWG_DNSMASQ_D у соседей."""
    tool, dns_d, log, env, status = uplink_env_vars
    before = _state(dns_d)
    r = _run(tool, env, "add", "vps.example.net")
    assert r.returncode == 0 and REFUSED in r.stdout, r.stdout + r.stderr
    assert _state(dns_d) == before
    _flaky(env, "tr", when="-cd*")
    r = _run(tool, env, "add", "vps.example.net")
    assert r.returncode == 1 and UNTOUCHED in r.stderr, r.stdout + r.stderr


def test_without_a_status_file_awg0_is_used_as_before(uplink_env, tmp_path):
    tool, dns_d, log, env, status = uplink_env
    status.unlink()
    (tmp_path / "amneziawg" / "awg0.conf").write_text("[Peer]\nEndpoint = old.example.net:51820\n",
                                                      encoding="utf-8")
    r = _run(tool, env, "add", "old.example.net")
    assert r.returncode == 0 and REFUSED in r.stdout, r.stdout + r.stderr
    r = _run(tool, env, "add", "vps.example.net")
    assert r.returncode == 0 and "добавлен" in r.stdout, r.stdout + r.stderr
