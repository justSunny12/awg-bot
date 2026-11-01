"""
awg-lan-domain.sh — сбой рабочего шага самой правки: временный каталог, чтение
и сборка своих списков, запись файлов, пересборка фида.

Стережём: такой сбой — код 1 и строка об этом; до записи списки не тронуты,
после — откат (без рестарта dnsmasq: с новыми списками он не перезапускался).
Цена ошибки: прежде любой упавший шаг давал код 0 — «в списках нет» про домен,
который в списке, или «убран» при нетронутом фиде (домен не вернулся в туннель),
а без временного каталога рабочие файлы под root ложились в корень. Сбой
подставной утилитой: в PATH она стоит раньше системной.
"""
from __future__ import annotations

import pytest
from tests.unit import _gw_lan_common as _c
from tests.unit._gw_lan_common import (
    FEED, RU_USER, VPN_USER, _SRC, _bin, _fake, _feed_from, _flaky, _log, _own, _run, _with_src,
)

script, domain_env, own_env = _c.script, _c.domain_env, _c.own_env

UNTOUCHED = "рабочий шаг правки не прошёл — списки не тронуты"
UNWRITTEN = "запись списков не прошла — откатываю"


def _state(dns_d) -> dict:
    return {f: (dns_d / f).read_text() for f in (VPN_USER, RU_USER, FEED)}


def _two_exceptions(own_env):
    """Как в test_a_removed_exception_returns_to_the_feed_while_others_stay_out:
    два исключения «напрямую», фид без них, исходник фида есть."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, ru=["shop.ru", "x.com"])
    _with_src(dns_d, dump, minus={"shop.ru", "x.com"})
    return _state(dns_d)


def _no_copies_left(dump) -> bool:
    return not list((dump / "rollback").glob("*.prev")) if (dump / "rollback").exists() else True


def test_a_failed_temp_dir_refuses_and_touches_nothing(own_env):
    tool, dns_d, dump, log, env = own_env
    before = _two_exceptions(own_env)
    _fake(_bin(env), "mktemp", "exit 1\n")
    r = _run(tool, env, "del", "shop.ru")
    assert r.returncode == 1 and UNTOUCHED in r.stderr, r.stdout + r.stderr
    assert _state(dns_d) == before
    assert "systemctl" not in _log(log), "рестарт при несделанной правке"


def test_failed_working_copies_refuse_instead_of_not_in_the_lists(own_env):
    """Рабочие копии списков не легли: прежде — «в списках нет» про домен из
    списка и код 0, исключение так и оставалось."""
    tool, dns_d, dump, log, env = own_env
    before = _two_exceptions(own_env)
    _fake(_bin(env), "cp", "exit 1\n")
    r = _run(tool, env, "del", "shop.ru")
    assert r.returncode == 1 and UNTOUCHED in r.stderr, r.stdout + r.stderr
    assert "в списках нет" not in r.stdout, r.stdout
    assert _state(dns_d) == before
    assert "systemctl" not in _log(log)


def test_a_failed_list_lookup_is_not_taken_for_a_missing_domain(own_env):
    """grep поиска домена упал (код 2) — это не «нет в списке»."""
    tool, dns_d, dump, log, env = own_env
    before = _two_exceptions(own_env)
    _fake(_bin(env), "grep", 'case "$1" in -qxF) exit 2 ;; esac\nexec /usr/bin/grep "$@"\n')
    r = _run(tool, env, "del", "shop.ru")
    assert r.returncode == 1 and UNTOUCHED in r.stderr, r.stdout + r.stderr
    assert "в списках нет" not in r.stdout, r.stdout
    assert _state(dns_d) == before


def test_a_failed_domain_check_is_not_taken_for_a_bad_name(own_env):
    """grep проверки домена упал (код 2) — это не «не похоже на домен»:
    прежде домен пропускался с этой строкой и кодом 0, хотя он верный."""
    tool, dns_d, dump, log, env = own_env
    before = _two_exceptions(own_env)
    _flaky(env, "grep", when="-Eq*", how="exit 2")
    r = _run(tool, env, "add", "alpha.com")
    assert r.returncode == 1 and UNTOUCHED in r.stderr, r.stdout + r.stderr
    assert "не похоже на домен" not in r.stdout, r.stdout
    assert _state(dns_d) == before
    assert "systemctl" not in _log(log)


def test_a_failed_name_cleanup_is_not_taken_for_a_bad_name(own_env):
    """sed разбора введённого (схема, путь, www.) упал: прежде имя выходило
    пустым — «: не похоже на домен, пропущен» и код 0, хотя домен верный."""
    tool, dns_d, dump, log, env = own_env
    before = _two_exceptions(own_env)
    _flaky(env, "sed", when="-E*")
    r = _run(tool, env, "add", "https://www.alpha.com/path")
    assert r.returncode == 1 and UNTOUCHED in r.stderr, r.stdout + r.stderr
    assert "не похоже на домен" not in r.stdout, r.stdout
    assert _state(dns_d) == before
    assert "systemctl" not in _log(log)


def test_a_failed_case_fold_is_not_taken_for_a_bad_name(own_env):
    """tr перевода в нижний регистр упал — то же: пустое имя не «не домен».
    Первый такой tr — хост сервера AWG из аплинка, второй — введённое имя."""
    tool, dns_d, dump, log, env = own_env
    before = _two_exceptions(own_env)
    _flaky(env, "tr", when='"A-Z a-z"', nth=2)
    r = _run(tool, env, "add", "Alpha.com")
    assert r.returncode == 1 and UNTOUCHED in r.stderr, r.stdout + r.stderr
    assert "не похоже на домен" not in r.stdout, r.stdout
    assert _state(dns_d) == before


# первый head -n1 скрипта — имя аплинка из статуса обвязки, второй — хост сервера AWG
@pytest.mark.parametrize("tool_name,when,nth", [("sed", "*Endpoint*", 1), ("head", "-n1*", 2),
                                             ("tr", '"A-Z a-z"', 1)])
def test_a_failed_server_host_lookup_does_not_let_the_vps_host_in(own_env, tool_name, when, nth):
    """Хост сервера AWG из Endpoint аплинка не вычислился (упал sed, head или
    tr): прежде запрет выходил пустым, и хост ВПС ложился «в туннель» —
    туннель уводился в туннель, шлюз запирал себя — с кодом 0 и «добавлен»."""
    tool, dns_d, dump, log, env = own_env
    before = _two_exceptions(own_env)
    _flaky(env, tool_name, when=when, nth=nth)
    r = _run(tool, env, "add", "vps.example.net")
    assert r.returncode == 1 and UNTOUCHED in r.stderr, r.stdout + r.stderr
    assert "добавлен" not in r.stdout, r.stdout
    assert _state(dns_d) == before, "хост ВПС лёг в списки"
    assert "systemctl" not in _log(log)


def test_without_faults_the_vps_host_is_still_refused(own_env):
    """Сторож исправного пути: хост ВПС «в туннель» не направить, остальное — как было."""
    tool, dns_d, dump, log, env = own_env
    before = _two_exceptions(own_env)
    r = _run(tool, env, "add", "VPS.example.net")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "это адрес самого сервера AWG — в туннель его не направить" in r.stdout, r.stdout
    assert _state(dns_d) == before


def test_without_an_uplink_conf_there_is_no_ban_as_before(own_env, tmp_path):
    """Конфига аплинка нет — запрета нет и сбоя нет: так было и прежде."""
    tool, dns_d, dump, log, env = own_env
    _two_exceptions(own_env)
    r = _run(tool, {**env, "AWG_UPLINK_CONF": str(tmp_path / "missing.conf")}, "add", "vps.example.net")
    assert r.returncode == 0 and "добавлен" in r.stdout, r.stdout + r.stderr
    assert "vps.example.net" in (dns_d / VPN_USER).read_text()


def test_a_failed_feed_rebuild_rolls_the_lists_back(own_env):
    """awk пересборки фида упал: прежде пустой результат молча не ставился —
    «убран» с кодом 0, а домен в фид не вернулся (идёт напрямую, хотя исключение снято)."""
    tool, dns_d, dump, log, env = own_env
    before = _two_exceptions(own_env)
    _fake(_bin(env), "awk", "exit 1\n")
    r = _run(tool, env, "del", "shop.ru")
    assert r.returncode == 1 and UNWRITTEN in r.stderr, r.stdout + r.stderr
    assert "убран" not in r.stdout, f"человеку сказано «убран» про откаченное: {r.stdout}"
    assert _state(dns_d) == before, "откат вернул не всё"
    assert _no_copies_left(dump), "копии отката остались"
    assert "systemctl" not in _log(log), "откат до рестарта — рестарт не нужен"


def test_a_failed_feed_write_rolls_the_lists_and_the_feed_back(own_env):
    tool, dns_d, dump, log, env = own_env
    before = _two_exceptions(own_env)
    _fake(_bin(env), "install", f'case "$*" in *{FEED}) exit 1 ;; esac\nexec /usr/bin/install "$@"\n')
    r = _run(tool, env, "del", "shop.ru")
    assert r.returncode == 1 and UNWRITTEN in r.stderr, r.stdout + r.stderr
    assert _state(dns_d) == before
    assert _no_copies_left(dump)
    assert "systemctl" not in _log(log)


def test_a_failed_list_write_rolls_back_before_the_feed(own_env):
    tool, dns_d, dump, log, env = own_env
    before = _two_exceptions(own_env)
    _fake(_bin(env), "install", f'case "$*" in *{RU_USER}) exit 1 ;; esac\nexec /usr/bin/install "$@"\n')
    r = _run(tool, env, "del", "shop.ru")
    assert r.returncode == 1 and UNWRITTEN in r.stderr, r.stdout + r.stderr
    assert _state(dns_d) == before
    assert _no_copies_left(dump)


def test_without_faults_the_same_change_goes_through(own_env):
    """Сторож самих подмен: тот же сценарий без сбоя — домен вернулся в фид."""
    tool, dns_d, dump, log, env = own_env
    _two_exceptions(own_env)
    r = _run(tool, env, "del", "shop.ru")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (dns_d / FEED).read_text() == _feed_from(_SRC, minus={"x.com"})
