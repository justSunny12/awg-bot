"""
awg-lan-domain.sh sync <файл> — сбой рабочего шага: проверка строк файла,
выборка и сортировка обоих списков, вычет «напрямую» из «в туннель», поиск
адреса сервера AWG.

Стережём: такой сбой — код 1 и «рабочий шаг правки не прошёл — списки не
тронуты», файлы и фид прежние, рестарта dnsmasq нет. Цена ошибки: шаги шли
конвейерами с «|| true», и упавший sed или sort давал пустой список — sync
стирал свои списки шлюза целиком с кодом 0 (агент считал канон применённым), упавший grep
проверки пропускал чужую строку, а упавший поиск — адрес самого сервера AWG в
туннель. Сбой подставной утилитой: в PATH она стоит раньше системной.
"""
from __future__ import annotations

from tests.unit import _gw_lan_common as _c
from tests.unit._gw_lan_common import (
    FEED, RU_USER, VPN_USER, _flaky, _log, _own, _ru_line, _sync, _vpn_line, _with_src,
)

script, domain_env, own_env = _c.script, _c.domain_env, _c.own_env

UNTOUCHED = "рабочий шаг правки не прошёл — списки не тронуты"
CANON = "vpn alpha.com\nvpn zeta.com\nru shop.ru\nru bank.ru\n"


def _state(dns_d) -> dict:
    return {f: (dns_d / f).read_text() for f in (VPN_USER, RU_USER, FEED)}


def _before(own_env) -> dict:
    """Свои списки и фид до sync: «в туннель» alpha.com, «напрямую» shop.ru,
    исходник фида есть — sync с новым «напрямую» пересобирал бы и фид."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, vpn=["alpha.com"], ru=["shop.ru"])
    _with_src(dns_d, dump, minus={"shop.ru"})
    return _state(dns_d)


def _refused(own_env, r, before) -> None:
    tool, dns_d, dump, log, env = own_env
    assert r.returncode == 1 and UNTOUCHED in r.stderr, r.stdout + r.stderr
    assert _state(dns_d) == before, "списки или фид тронуты при несделанной правке"
    assert "systemctl" not in _log(log), "рестарт dnsmasq при несделанной правке"
    assert "свои списки:" not in r.stdout, f"итог успеха при отказе: {r.stdout}"


def test_a_failed_ru_selection_does_not_wipe_the_lists(own_env, tmp_path):
    """sed выборки «напрямую» упал: прежде — пустой «напрямую» и код 0."""
    tool, dns_d, dump, log, env = own_env
    before = _before(own_env)
    _flaky(env, "sed", when="*'s/^ru //p'*")
    _refused(own_env, _sync(tool, env, tmp_path, CANON), before)


def test_a_failed_vpn_sort_does_not_wipe_the_tunnel_list(own_env, tmp_path):
    """sort «в туннель» упал: прежде «… | sort -u | grep … || true» давал
    пустой «в туннель» — alpha.com уходил из списка с кодом 0."""
    tool, dns_d, dump, log, env = own_env
    before = _before(own_env)
    _flaky(env, "sort", when="*vpn.in*")
    _refused(own_env, _sync(tool, env, tmp_path, CANON), before)


def test_a_failed_ru_sort_does_not_wipe_the_direct_list(own_env, tmp_path):
    tool, dns_d, dump, log, env = own_env
    before = _before(own_env)
    _flaky(env, "sort", when="*ru.in*")
    _refused(own_env, _sync(tool, env, tmp_path, CANON), before)


def test_a_failed_subtraction_of_direct_from_tunnel_refuses(own_env, tmp_path):
    """grep вычета «напрямую» из «в туннель» упал (код 2) — не пустой «в туннель»."""
    tool, dns_d, dump, log, env = own_env
    before = _before(own_env)
    _flaky(env, "grep", when="'-vxF -f '*", how="exit 2")
    _refused(own_env, _sync(tool, env, tmp_path, CANON), before)


def test_a_failed_line_check_does_not_let_a_foreign_line_through(own_env, tmp_path):
    """grep проверки строк упал (код 2): прежде в конвейере это было «чужих
    строк нет», и строка с негодным доменом уходила в список."""
    tool, dns_d, dump, log, env = own_env
    before = _before(own_env)
    _flaky(env, "grep", when="'-Ev '*", how="exit 2")
    _refused(own_env, _sync(tool, env, tmp_path, CANON + "vpn bad_domain.com\n"), before)
    assert "bad_domain" not in (dns_d / VPN_USER).read_text()


def test_a_failed_length_check_is_not_taken_for_a_foreign_line(own_env, tmp_path):
    """awk проверки длины упал — это сбой шага (код 1), а не «чужая строка» (код 2)."""
    tool, dns_d, dump, log, env = own_env
    before = _before(own_env)
    _flaky(env, "awk", when="*'length($2) > 253'*")
    _refused(own_env, _sync(tool, env, tmp_path, CANON), before)


def test_a_failed_server_lookup_does_not_let_the_server_into_the_tunnel(own_env, tmp_path):
    """Поиск адреса сервера AWG в списке упал (grep код 2): прежде — «нет в
    списке», и хост ВПС уходил «в туннель» — туннель в туннель."""
    tool, dns_d, dump, log, env = own_env
    before = _before(own_env)
    _flaky(env, "grep", when="'-qxF vps.example.net '*", how="exit 2")
    r = _sync(tool, env, tmp_path, CANON + "vpn vps.example.net\n")
    _refused(own_env, r, before)
    assert "vps.example.net" not in (dns_d / VPN_USER).read_text()


def test_without_faults_the_same_sync_goes_through(own_env, tmp_path):
    """Сторож самих подмен: тот же канон без сбоя ставится, сервер пропущен."""
    tool, dns_d, dump, log, env = own_env
    _before(own_env)
    _flaky(env, "sed", when="*'s/^ru //p'*", nth=99)
    _flaky(env, "grep", when="'-qxF vps.example.net '*", nth=99, how="exit 2")
    r = _sync(tool, env, tmp_path, CANON + "vpn vps.example.net\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (dns_d / VPN_USER).read_text() == _vpn_line("alpha.com") + "\n" + _vpn_line("zeta.com") + "\n"
    assert (dns_d / RU_USER).read_text() == _ru_line("bank.ru") + "\n" + _ru_line("shop.ru") + "\n"
    assert "vps.example.net: это адрес самого сервера AWG — пропущен" in r.stdout
    assert (tmp_path / "sed.calls").read_text().strip() == "1", "подмена sed не встала на путь sync"
