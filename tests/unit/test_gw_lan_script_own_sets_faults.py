"""
awg-lan-domain.sh, правка своих списков — сбой рабочего шага наборов nft
после рестарта dnsmasq: адреса добавленных доменов (запуск dig, сбор, сортировка
и склейка адресов) и перестройка «напрямую» (lan_ru4 после смены списка).

Стережём: такой сбой — код 1 и строка о том, какой набор не тронут; списки
к этому моменту применены и верны (dnsmasq с ними поднялся), набор — только
ускорение: адреса наберёт и сам dnsmasq по первым запросам, поэтому списки не
откатываются (откат — второй рестарт, сброс кэша DNS всей сети, и упёрся бы
в те же упавшие шаги). Цена ошибки: прежде упавший шаг давал код 0 и «N
адрес(а) в наборе» про адреса, которых в наборе нет, а lan_ru4 после сброса
оставался пустым — все исключения «напрямую» шли в туннель. Отказ самого nft
при добавлении в lan_vpn4 сбоем не считается — как в fill (адрес внутри
интервала); lan_ru4 перестраивается одной транзакцией, и её отказ — сбой (набор
прежний). Сбой подставной утилитой: в PATH она стоит раньше системной.
"""
from __future__ import annotations

from tests.unit import _gw_lan_common as _c
from tests.unit._gw_lan_common import (
    RU_USER, VPN_USER, _fill, _flaky, _nft, _own, _ru_line, _set, _sets_of, _sync, _vpn_line,
)

script, domain_env, own_env = _c.script, _c.domain_env, _c.own_env


def _fault(name: str) -> str:
    return f"рабочий шаг наполнения набора не прошёл — набор {name} не тронут"


def _prepared(own_env) -> None:
    """В «в туннель» — news.org, в «напрямую» — shop.ru; наборы наполнены ими."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, vpn=["news.org"], ru=["shop.ru"])
    _fill(_sets_of(dump), "lan_vpn4", ["10.3.3.3"])
    _fill(_sets_of(dump), "lan_ru4", ["10.1.1.1"])


def _applied_vpn(dns_d, *doms) -> None:
    text = (dns_d / VPN_USER).read_text()
    assert all(_vpn_line(d) in text for d in doms), f"списки не применены: {text}"


def _add_failed(own_env, r) -> None:
    tool, dns_d, dump, log, env = own_env
    assert r.returncode == 1 and _fault("lan_vpn4") in r.stderr, r.stdout + r.stderr
    assert "в наборе" not in r.stdout, f"про адреса сказано «в наборе», а их там нет: {r.stdout}"
    _applied_vpn(dns_d, "alpha.com")
    assert _set(_sets_of(dump), "lan_vpn4") == {"10.3.3.3"}
    assert "nft list set inet awg_lan lan_vpn4" in _nft(log), "слепок не снят"


def test_add_with_dig_not_started_is_not_reported_in_the_set(own_env):
    """xargs не запустил запросы: прежде — «0 адрес(а) в наборе» и код 0."""
    tool, dns_d, dump, log, env = own_env
    _prepared(own_env)
    _flaky(env, "xargs")
    _add_failed(own_env, _c._run(tool, env, "add", "alpha.com"))


def test_add_with_failed_address_collection_is_not_reported_in_the_set(own_env):
    """cat адресов упал: прежде пустой список — nft не звался, а в выводе «1
    адрес(а) в наборе» и код 0."""
    tool, dns_d, dump, log, env = own_env
    _prepared(own_env)
    _flaky(env, "cat", when="*/ips_*")
    _add_failed(own_env, _c._run(tool, env, "add", "alpha.com"))


def test_add_with_failed_address_join_is_not_reported_in_the_set(own_env):
    tool, dns_d, dump, log, env = own_env
    _prepared(own_env)
    _flaky(env, "paste")
    _add_failed(own_env, _c._run(tool, env, "add", "alpha.com"))


def test_a_refusing_nft_on_add_is_not_a_step_failure(own_env):
    """Отказ nft (адрес внутри интервала) — не сбой рабочего шага, как в fill."""
    tool, dns_d, dump, log, env = own_env
    _prepared(own_env)
    _c._fake(_c._bin(env), "nft", f'echo "nft $*" >> {log}\n[ "$1" = add ] && exit 1\nexit 0\n')
    r = _c._run(tool, env, "add", "alpha.com")
    assert r.returncode == 0, r.stdout + r.stderr
    _applied_vpn(dns_d, "alpha.com")


def _ru_failed(own_env, r) -> None:
    tool, dns_d, dump, log, env = own_env
    assert r.returncode == 1 and _fault("lan_ru4") in r.stderr, r.stdout + r.stderr
    assert "в наборе" not in r.stdout, r.stdout
    assert _set(_sets_of(dump), "lan_ru4") == {"10.1.1.1"}, "набор «напрямую» сброшен и не наполнен"
    assert "nft flush set inet awg_lan lan_ru4" not in _nft(log), "сброс без готовых адресов"


def test_ru_with_failed_rebuild_keeps_lan_ru4_instead_of_emptying_it(own_env):
    """Сбор адресов «напрямую» упал: прежде набор сбрасывался и оставался
    пустым — исключения шли в туннель — при коде 0 и «добавлен»."""
    tool, dns_d, dump, log, env = own_env
    _prepared(own_env)
    _flaky(env, "paste")
    r = _c._run(tool, env, "ru", "bank.ru")
    _ru_failed(own_env, r)
    assert _ru_line("bank.ru") in (dns_d / RU_USER).read_text(), "списки не применены"


def test_sync_with_failed_rebuild_keeps_lan_ru4(own_env, tmp_path):
    tool, dns_d, dump, log, env = own_env
    _prepared(own_env)
    _flaky(env, "paste")
    r = _sync(tool, env, tmp_path, "vpn news.org\nru shop.ru\nru bank.ru\n")
    _ru_failed(own_env, r)
    assert "свои списки:" not in r.stdout, f"итог sync при несделанном наборе: {r.stdout}"


def test_add_of_a_longest_domain_puts_its_addresses_into_the_set(own_env):
    """Домен в 253 знака: файл его адресов звался «ips_<домен>» — 257 знаков,
    больше предела имени файла (255); файл не создавался, и адреса терялись
    молча — «0 адрес(а) в наборе» и код 0. Теперь файл — по номеру домена."""
    tool, dns_d, dump, log, env = own_env
    _prepared(own_env)
    d = _c._long(253)
    _c._fake(_c._bin(env), "dig", f'case "$*" in *" {d} "*) echo 10.6.6.6 ;; esac\n')
    r = _c._run(tool, env, "add", d)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _set(_sets_of(dump), "lan_vpn4") == {"10.3.3.3", "10.6.6.6"}
    assert f"{d} → 1 адрес(а) в наборе lan_vpn4" in r.stdout, r.stdout


def test_without_faults_add_and_ru_fill_the_sets(own_env):
    """Сторож самих подмен: те же шаги без сбоя."""
    tool, dns_d, dump, log, env = own_env
    _prepared(own_env)
    r = _c._run(tool, env, "add", "alpha.com")
    assert r.returncode == 0 and "alpha.com → 1 адрес(а) в наборе lan_vpn4" in r.stdout, r.stdout + r.stderr
    assert _set(_sets_of(dump), "lan_vpn4") == {"10.3.3.3", "10.4.4.4"}
    r = _c._run(tool, env, "ru", "bank.ru")
    assert r.returncode == 0 and "bank.ru → 1 адрес(а) в наборе lan_ru4" in r.stdout, r.stdout + r.stderr
    assert _set(_sets_of(dump), "lan_ru4") == {"10.1.1.1", "10.2.2.2"}


# ── отказ nft на «напрямую»: сброс и наполнение одной транзакцией ───────────

def test_a_refused_direct_set_keeps_lan_ru4_and_says_so(own_env):
    """lan_ru4 наполняет только скрипт. Прежде сброс и наполнение шли двумя
    вызовами nft: отказ add оставлял набор пустым — все исключения «напрямую»
    уходили в туннель до первых запросов — при коде 0 и «→ N адрес(а) в
    наборе». Теперь одна транзакция: её отказ — набор прежний, код 1."""
    tool, dns_d, dump, log, env = own_env
    _prepared(own_env)
    r = _c._run(tool, {**env, "NFT_FAIL_RU": "1"}, "ru", "bank.ru")
    assert r.returncode == 1 and _fault("lan_ru4") in r.stderr, r.stdout + r.stderr
    assert "в наборе" not in r.stdout, r.stdout
    assert _set(_sets_of(dump), "lan_ru4") == {"10.1.1.1"}, "набор «напрямую» сброшен и не наполнен"
    assert _ru_line("bank.ru") in (dns_d / RU_USER).read_text(), "списки не применены"


def test_sync_with_a_refused_direct_set_keeps_lan_ru4(own_env, tmp_path):
    tool, dns_d, dump, log, env = own_env
    _prepared(own_env)
    r = _sync(tool, {**env, "NFT_FAIL_RU": "1"}, tmp_path, "vpn news.org\nru shop.ru\nru bank.ru\n")
    assert r.returncode == 1 and _fault("lan_ru4") in r.stderr, r.stdout + r.stderr
    assert "свои списки:" not in r.stdout, f"итог sync при несделанном наборе: {r.stdout}"
    assert _set(_sets_of(dump), "lan_ru4") == {"10.1.1.1"}
