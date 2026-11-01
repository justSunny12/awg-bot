"""
awg-lan-domain.sh fill — сбой рабочего шага наполнения: временный каталог,
чтение файла доменов, отбор строк, счёт, сбор адресов (запуск dig, отбор,
сортировка и склейка адресов).

Стережём: такой сбой — код 1 и строка об этом, набор не тронут. Цена ошибки:
прежде любой упавший шаг давал код 0 и «адреса N доменов взяты в обработку»
(N пустое или 0) при нетронутом наборе — агент писал в журнал «набор
пополнен», а новые домены «в туннель» шли мимо туннеля до первого запроса
пользователя к dnsmasq. Молчание DNS (dig не ответил) и отказ nft сбоем
рабочего шага не считаются — набор пополнит сам dnsmasq (их стерегут тесты
test_gw_lan_script_fill.py). Сбой подставной утилитой: в PATH она стоит
раньше системной.
"""
from __future__ import annotations

from tests.unit import _gw_lan_common as _c
from tests.unit._gw_lan_common import _bin, _fake, _fill, _fill_run, _flaky, _nft, _set, _sets_of

script, domain_env, own_env = _c.script, _c.domain_env, _c.own_env

UNFILLED = "рабочий шаг наполнения набора не прошёл — набор lan_vpn4 не тронут"


def _prepared(own_env) -> None:
    """В наборе и слепке — адрес прошлого наполнения."""
    tool, dns_d, dump, log, env = own_env
    _fill(_sets_of(dump), "lan_vpn4", ["10.3.3.3"])
    (dump / "lan_vpn4.nft").write_text("elements = { 10.3.3.3 }\n", encoding="utf-8")


def _untouched(own_env, r) -> None:
    tool, dns_d, dump, log, env = own_env
    assert r.returncode == 1 and UNFILLED in r.stderr, r.stdout + r.stderr
    assert "взяты в обработку" not in r.stdout, f"сказано «взяты в обработку» про несделанное: {r.stdout}"
    assert not any("add element" in ln for ln in _nft(log)), _nft(log)
    assert _set(_sets_of(dump), "lan_vpn4") == {"10.3.3.3"}
    assert (dump / "lan_vpn4.nft").read_text() == "elements = { 10.3.3.3 }\n", "слепок переписан"


def test_a_failed_temp_dir_refuses(own_env, tmp_path):
    tool, dns_d, dump, log, env = own_env
    _prepared(own_env)
    _fake(_bin(env), "mktemp", "exit 1\n")
    _untouched(own_env, _fill_run(tool, env, tmp_path, "alpha.com\n"))


def test_an_unreadable_domain_file_is_not_zero_domains(own_env, tmp_path):
    """Файл доменов есть, но не читается: прежде — «адреса 0 доменов» и код 0."""
    tool, dns_d, dump, log, env = own_env
    _prepared(own_env)
    f = tmp_path / "fill.txt"; f.write_text("alpha.com\n", encoding="utf-8"); f.chmod(0)
    try:
        r = _c._run(tool, env, "fill", str(f))
    finally:
        f.chmod(0o644)
    _untouched(own_env, r)


def test_a_failed_domain_check_is_not_a_foreign_line(own_env, tmp_path):
    """grep проверки домена упал (код 2): прежде каждая строка сходила за
    чужую — «адреса 0 доменов» и код 0."""
    tool, dns_d, dump, log, env = own_env
    _prepared(own_env)
    _flaky(env, "grep", when="-Eq*", how="exit 2")
    _untouched(own_env, _fill_run(tool, env, tmp_path, "alpha.com\n"))


def test_a_failed_count_refuses(own_env, tmp_path):
    """grep счёта отобранных упал: прежде — «адреса  доменов» с пустым числом и код 0."""
    tool, dns_d, dump, log, env = own_env
    _prepared(own_env)
    _flaky(env, "grep", when='"-c ."*', how="exit 2")
    _untouched(own_env, _fill_run(tool, env, tmp_path, "alpha.com\n"))


def test_dig_not_started_refuses(own_env, tmp_path):
    """xargs не запустил запросы: прежде пустой список адресов — набор не
    тронут, а код 0."""
    tool, dns_d, dump, log, env = own_env
    _prepared(own_env)
    _flaky(env, "xargs")
    _untouched(own_env, _fill_run(tool, env, tmp_path, "alpha.com\n"))


def test_a_failed_address_filter_refuses(own_env, tmp_path):
    tool, dns_d, dump, log, env = own_env
    _prepared(own_env)
    _flaky(env, "grep", when='"-E ^[0-9]"*', how="exit 2")
    _untouched(own_env, _fill_run(tool, env, tmp_path, "alpha.com\n"))


def test_a_failed_address_sort_refuses(own_env, tmp_path):
    tool, dns_d, dump, log, env = own_env
    _prepared(own_env)
    _flaky(env, "sort")
    _untouched(own_env, _fill_run(tool, env, tmp_path, "alpha.com\n"))


def test_a_failed_address_join_refuses(own_env, tmp_path):
    tool, dns_d, dump, log, env = own_env
    _prepared(own_env)
    _flaky(env, "paste")
    _untouched(own_env, _fill_run(tool, env, tmp_path, "alpha.com\n"))


def test_without_faults_the_same_fill_goes_through(own_env, tmp_path):
    """Сторож самих подмен: тот же сценарий без сбоя — адрес в наборе."""
    tool, dns_d, dump, log, env = own_env
    _prepared(own_env)
    r = _fill_run(tool, env, tmp_path, "alpha.com\n")
    assert r.returncode == 0 and "адреса 1 домена взяты в обработку" in r.stdout, r.stdout + r.stderr
    assert _set(_sets_of(dump), "lan_vpn4") == {"10.3.3.3", "10.4.4.4"}
