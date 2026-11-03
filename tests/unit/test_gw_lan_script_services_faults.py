"""
awg-lan-services.sh — сбой рабочего шага: проверка строк по белому списку,
копия для отката, запись файла, снятие файла, отказ dnsmasq после снятия.

Стережём: такой сбой — rc=1 и строка об этом; до записи файл не тронут и
dnsmasq не перезапускается, после — откат прежнего файла (без рестарта, если
демон с новым ещё не перезапускался). Цена ошибки: упавший grep проверки
(код 2) сходил за «чужих строк нет», и файл ставился непроверенным — с
server= от чужого ВПС — и кодом 0; неудавшаяся копия для отката при отказе
dnsmasq удаляла прежние записи совсем, а оставшаяся от оборванного запуска
старая копия возвращалась вместо прежнего файла; упавшая запись файла шла к
рестарту и объявляла «применены» с прежними записями; файл, который не
удалось снять, объявлялся снятым; отказ dnsmasq после снятия оставлял файл
снятым — следующий заход агента видел совпадение («снимать нечего») и
объявлял успех, хотя демон держал прежние записи. Сбой подставной утилитой:
в PATH она стоит раньше системной.
"""
from __future__ import annotations

from tests.unit import _gw_lan_common as _c
from tests.unit._gw_lan_common import OLD_SVC, _fake, _failing_restart, _flaky, _svc_file

script, svc_env = _c.script, _c.svc_env

UNTOUCHED = "рабочий шаг записей SMB не прошёл — записи не тронуты"
UNWRITTEN = "установка записей SMB не прошла — откатываю"


def _prev(conf):
    return conf.parent.parent / "dump" / "rollback" / "peer-services.conf.prev"


def _restarts(log) -> int:
    return log.read_text().count("systemctl restart dnsmasq")


def test_a_failed_whitelist_check_does_not_install_the_file(svc_env, tmp_path):
    """grep белого списка упал (код 2) — это не «чужих строк нет»: прежде
    файл с server= ставился, и dnsmasq перезапускался с ним."""
    go, conf, log = svc_env
    conf.write_text(OLD_SVC, encoding="utf-8")
    _fake(tmp_path / "bin", "grep", 'case "$1" in -Ev) exit 2 ;; esac\nexec /usr/bin/grep "$@"\n')
    r = go(_svc_file() + "server=/awg.internal/203.0.113.5\n")
    assert r.returncode == 1 and UNTOUCHED in r.stderr, r.stdout + r.stderr
    assert conf.read_text(encoding="utf-8") == OLD_SVC, "непроверенный файл встал"
    assert _restarts(log) == 0 and "dnsmasq --test" not in log.read_text()


def test_a_failed_rollback_copy_does_not_touch_the_file(svc_env, tmp_path):
    """Копия для отката не легла: прежде при отказе рестарта откат удалял
    прежние записи совсем — откатывать было нечем."""
    go, conf, log = svc_env
    conf.write_text(OLD_SVC, encoding="utf-8")
    _flaky({"PATH": str(tmp_path / "bin")}, "cp")
    _failing_restart(tmp_path / "bin", log)
    r = go(_svc_file())
    assert conf.exists() and conf.read_text(encoding="utf-8") == OLD_SVC, "прежние записи потеряны"
    assert r.returncode == 1 and UNTOUCHED in r.stderr, r.stdout + r.stderr
    assert _restarts(log) == 0


def test_a_stale_rollback_copy_is_not_restored_instead_of_no_file(svc_env):
    """Копия от оборванного запуска лежит в rollback/, файла записей нет:
    при отказе --test прежде возвращалась она — записи, которых уже не было."""
    go, conf, log = svc_env
    prev = _prev(conf); prev.parent.mkdir(parents=True)
    prev.write_text(OLD_SVC, encoding="utf-8")
    r = go(_svc_file(), DNSMASQ_TEST_RC="1")
    assert r.returncode == 1 and "dnsmasq --test отверг записи SMB — откатываю" in r.stderr, r.stderr
    assert not conf.exists(), f"откат вернул старую копию: {conf.read_text() if conf.exists() else ''}"
    assert not prev.exists()


def test_a_failed_write_rolls_back_without_a_restart(svc_env, tmp_path):
    """install не прошёл: прежде — рестарт с прежним файлом и «применены» с кодом 0."""
    go, conf, log = svc_env
    conf.write_text(OLD_SVC, encoding="utf-8")
    _flaky({"PATH": str(tmp_path / "bin")}, "install")
    r = go(_svc_file())
    assert r.returncode == 1 and UNWRITTEN in r.stderr, r.stdout + r.stderr
    assert "применены" not in r.stdout, r.stdout
    assert conf.read_text(encoding="utf-8") == OLD_SVC
    assert not _prev(conf).exists(), "копия отката осталась"
    assert _restarts(log) == 0, "откат до рестарта — рестарт не нужен"


def test_a_file_that_cannot_be_removed_is_not_reported_removed(svc_env, tmp_path):
    go, conf, log = svc_env
    conf.write_text(OLD_SVC, encoding="utf-8")
    for tool in ("rm", "mv"):
        _flaky({"PATH": str(tmp_path / "bin")}, tool, when="*awg-gw-peer-services.conf*")
    r = go(None)
    assert r.returncode == 1 and UNTOUCHED in r.stderr, r.stdout + r.stderr
    assert "сняты" not in r.stdout, r.stdout
    assert conf.read_text(encoding="utf-8") == OLD_SVC
    assert _restarts(log) == 0


def test_dnsmasq_test_refusing_after_removal_brings_the_file_back(svc_env):
    """--test отверг conf-dir без файла: демон не перезапускался и держит
    прежние записи — файл возвращается, иначе следующий заход агента увидел
    бы «снимать нечего» и объявил успех."""
    go, conf, log = svc_env
    conf.write_text(OLD_SVC, encoding="utf-8")
    r = go(None, DNSMASQ_TEST_RC="1")
    assert conf.exists() and conf.read_text(encoding="utf-8") == OLD_SVC, "файл снят, а демон с прежними записями"
    assert r.returncode == 1 and "откатываю" in r.stderr, r.stderr
    assert not _prev(conf).exists()
    assert _restarts(log) == 0


def test_a_failed_restart_after_removal_brings_the_file_back_and_restarts(svc_env, tmp_path):
    go, conf, log = svc_env
    conf.write_text(OLD_SVC, encoding="utf-8")
    _failing_restart(tmp_path / "bin", log)
    r = go(None)
    assert conf.exists() and conf.read_text(encoding="utf-8") == OLD_SVC, "файл снят, а dnsmasq не поднялся"
    assert r.returncode == 1 and "откатываю" in r.stderr, r.stderr
    assert not _prev(conf).exists()
    assert _restarts(log) == 2, "после отката dnsmasq не поднимали с прежним файлом"


def test_without_faults_install_and_removal_go_through(svc_env):
    """Сторож самих подмен: те же шаги без сбоя."""
    go, conf, log = svc_env
    conf.write_text(OLD_SVC, encoding="utf-8")
    r = go(_svc_file())
    assert r.returncode == 0 and conf.read_text(encoding="utf-8") == _svc_file(), r.stderr
    r = go(None)
    assert r.returncode == 0 and not conf.exists(), r.stderr
    assert not _prev(conf).exists()


def test_an_unopenable_lock_is_a_step_failure_not_a_bad_file(svc_env):
    """Файл блокировки не открылся (каталог только для чтения): у dash отказ
    перенаправления exec завершал скрипт кодом 2 — «файл не прошёл проверку»."""
    go, conf, log = svc_env
    conf.write_text(OLD_SVC, encoding="utf-8")
    (conf.parent.parent / "dump" / "lists.lock").mkdir(parents=True)
    r = go(_svc_file())
    assert r.returncode == 1 and UNTOUCHED in r.stderr, (r.returncode, r.stderr)
    assert conf.read_text(encoding="utf-8") == OLD_SVC
    assert _restarts(log) == 0
