"""
awg-lan-lists.sh — сбой рабочего шага обновления: временные файлы, сборка
фида (перевод, исходник, вычет исключений «напрямую», фильтр, счёт), копия для
отката, запись фида и исходника, сборка набора подсетей, запись итога.

Стережём: такой сбой — rc=1 и строка об этом; до записи фид и набор не тронуты
и dnsmasq не перезапускается, после записи — откат без рестарта. Цена ошибки:
упавший awk вычета ставил фид без исключений «напрямую» (домены человека
уходили в туннель) с кодом 0; упавший счёт директив пропускал проверку длины;
неудавшаяся копия для отката при отказе рестарта удаляла фид совсем; упавшая
запись фида шла дальше к рестарту и объявляла успех; упавший отбор подсетей
давал пустой набор и «подсетей 0» в итоге при нетронутом наборе. Сбой
подставной утилитой: в PATH она стоит раньше системной.
"""
from __future__ import annotations

import subprocess

from tests.conftest import SH
from tests.unit import _gw_lan_common as _c
from tests.unit._gw_lan_common import (
    FEED, RU_USER, _bin, _failing_restart, _flaky, _ru_line, _vpn_line,
)

script, lists_env, channel_env = _c.script, _c.lists_env, _c.channel_env

OLD_FEED = "".join(_vpn_line(f"old{i}.org") + "\n" for i in range(12))
FEED_FAULT = "домены: рабочий шаг сборки фида не прошёл — фид не тронут"
NETS_FAULT = "подсети: рабочий шаг сборки набора не прошёл — набор не тронут"


def _go(tool, env):
    return subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)


def _prepared(lists_env):
    """Прежний фид из 12 доменов, исключение «напрямую» shop.ru, прежний итог
    с 5 подсетями — как после прошлого удачного обновления."""
    tool, dns_d, dump, log, env = lists_env
    (dns_d / FEED).write_text(OLD_FEED, encoding="utf-8")
    (dns_d / RU_USER).write_text(_ru_line("shop.ru") + "\n", encoding="utf-8")
    dump.mkdir(exist_ok=True)
    (dump / "lists.status").write_text("updated_at=x\ndomains=12\nnets=5\nrc=0\nsource=net\n", encoding="utf-8")
    (dump / "vpn-feed.src").write_text(OLD_FEED, encoding="utf-8")


def _log(log) -> str:
    return log.read_text() if log.exists() else ""


def _feed_untouched(lists_env, r, msg=FEED_FAULT) -> None:
    tool, dns_d, dump, log, env = lists_env
    assert r.returncode == 1 and msg in r.stderr, r.stdout + r.stderr
    assert (dns_d / FEED).read_text() == OLD_FEED, "фид тронут при несделанной сборке"
    assert (dump / "vpn-feed.src").read_text() == OLD_FEED, "исходник сменился при несделанной сборке"
    assert not (dump / "vpn-feed.src.tmp").exists()
    assert "systemctl restart dnsmasq" not in _log(log), "рестарт dnsmasq зря"
    assert not list((dump / "rollback").glob("*")) if (dump / "rollback").exists() else True


def test_failed_temp_files_refuse_and_touch_nothing(lists_env):
    tool, dns_d, dump, log, env = lists_env
    _prepared(lists_env)
    _flaky(env, "mktemp", nth=3)
    r = _go(tool, env)
    assert r.returncode == 1 and "рабочий шаг сборки списков не прошёл — списки не тронуты" in r.stderr, r.stderr
    assert (dns_d / FEED).read_text() == OLD_FEED
    assert "nets=5\n" in (dump / "lists.status").read_text(), "итог переписан при несделанном обновлении"
    assert _log(log) == "", f"скрипт пошёл дальше без временных файлов: {_log(log)}"


def test_a_failed_subtraction_does_not_install_a_feed_without_the_exceptions(lists_env):
    """awk вычета «напрямую» упал: прежде в фид вставал скачанный целиком —
    с shop.ru, который человек отправил напрямую, — и код 0."""
    tool, dns_d, dump, log, env = lists_env
    _prepared(lists_env)
    _flaky(env, "awk", when="'-F/ '*")
    r = _go(tool, env)
    _feed_untouched(lists_env, r)
    assert "shop.ru" not in (dns_d / FEED).read_text()


def test_a_failed_conversion_is_named_as_a_fault_not_a_short_feed(lists_env):
    """sed перевода ipset= → nftset= упал: прежде — «подозрительно короткий»."""
    tool, dns_d, dump, log, env = lists_env
    _prepared(lists_env)
    _flaky(env, "sed", when="*vpn_domains*")
    r = _go(tool, env)
    _feed_untouched(lists_env, r)
    assert "подозрительно короткий" not in r.stderr


def test_a_failed_source_copy_refuses_the_feed(lists_env):
    tool, dns_d, dump, log, env = lists_env
    _prepared(lists_env)
    _flaky(env, "cp", when="*vpn-feed.src.tmp*")
    _feed_untouched(lists_env, _go(tool, env))


def test_a_failed_filter_refuses_the_feed(lists_env):
    tool, dns_d, dump, log, env = lists_env
    _prepared(lists_env)
    _flaky(env, "grep", when="'-Ei '*", how="exit 2")
    _feed_untouched(lists_env, _go(tool, env))


def test_a_failed_count_does_not_skip_the_length_check(lists_env):
    """grep -c счёта директив упал: прежде «[ "" -lt 10 ]» был ложью, и фид
    ставился без проверки длины."""
    tool, dns_d, dump, log, env = lists_env
    _prepared(lists_env)
    _flaky(env, "grep", when="'-c ^nftset= '*", how="exit 2")
    _feed_untouched(lists_env, _go(tool, env))


def test_a_failed_rollback_copy_leaves_the_feed_alone(lists_env):
    """Копия прежнего фида для отката не легла: прежде скрипт шёл дальше, и
    при отказе рестарта «откат» удалял фид совсем — без фида до следующего похода."""
    tool, dns_d, dump, log, env = lists_env
    _prepared(lists_env)
    _failing_restart(_bin(env), log)
    _flaky(env, "cp", when="*vpn-feed.conf.prev*")
    r = _go(tool, env)
    _feed_untouched(lists_env, r, "домены: копия фида для отката не легла — фид не тронут")


def test_a_failed_feed_write_rolls_back_without_a_restart(lists_env):
    """install фида упал, оставив обрывок: прежде скрипт шёл к --test и
    рестарту и объявлял успех. Теперь — прежний фид из копии, рестарта нет."""
    tool, dns_d, dump, log, env = lists_env
    _prepared(lists_env)
    _flaky(env, "install", when=f"*{FEED}*", how='eval "last=\\${$#}"; echo обрывок > "$last"; exit 1')
    r = _go(tool, env)
    _feed_untouched(lists_env, r, "домены: запись фида не прошла — откатываю")
    assert "dnsmasq --test" not in _log(log)


def test_a_failed_source_save_is_reported(lists_env):
    """Фид встал, а исходник не лёг: rc=1 — следующий запуск (фид тот же,
    без рестарта) положит его снова; иначе правка «напрямую» пересобирала бы
    фид по прежнему исходнику."""
    tool, dns_d, dump, log, env = lists_env
    _prepared(lists_env)
    _flaky(env, "mv", when="*vpn-feed.src.tmp*")
    r = _go(tool, env)
    assert r.returncode == 1 and "домены: исходник фида не сохранён" in r.stderr, r.stderr
    assert "nftset=/x.com/inet#awg_lan#lan_vpn4" in (dns_d / FEED).read_text()
    assert not (dump / "vpn-feed.src.tmp").exists()
    r = _go(tool, env)
    assert r.returncode == 0, r.stderr
    assert _vpn_line("shop.ru") in (dump / "vpn-feed.src").read_text(), "повтор не положил исходник"


def test_a_failed_subnet_selection_keeps_the_set_and_its_count(lists_env):
    """grep отбора подсетей упал: прежде пустой набор молча не ставился, а в
    итог уходило «подсетей 0» с кодом 0."""
    tool, dns_d, dump, log, env = lists_env
    _prepared(lists_env)
    _flaky(env, "grep", when="'-E ^[0-9]'*", how="exit 2")
    r = _go(tool, env)
    assert r.returncode == 1 and NETS_FAULT in r.stderr, r.stderr
    assert "nft -f" not in _log(log), "набор тронут при несделанной сборке"
    status = (dump / "lists.status").read_text()
    assert "nets=5\n" in status and "rc=1\n" in status, status
    assert "systemctl restart dnsmasq" in _log(log), "фид доменов от сбоя подсетей не зависит"


def test_a_failed_subnet_sort_keeps_the_set(lists_env):
    tool, dns_d, dump, log, env = lists_env
    _prepared(lists_env)
    _flaky(env, "sort", when="'-u -o '*")
    r = _go(tool, env)
    assert r.returncode == 1 and NETS_FAULT in r.stderr, r.stderr
    assert "nft -f" not in _log(log)
    assert "nets=5\n" in (dump / "lists.status").read_text()


# ── число подсетей в итоге — число в наборе, а не число пришедших ───────────
# Панель шлюза показывает его как есть: «0 подсетей» при полном наборе — ложь,
# красной проверки нет (domains > 0), и её никто не заметит.

def _nets_in_status(dump) -> str:
    return next(ln for ln in (dump / "lists.status").read_text().splitlines() if ln.startswith("nets="))


def test_no_subnets_at_all_keep_the_previous_count(channel_env):
    """Канал привёз домены без nets.lst: набор не тронут — прежде в итоге nets=0."""
    tool, dns_d, dump, log, env, feed = channel_env
    dump.mkdir(exist_ok=True)
    (dump / "lists.status").write_text("updated_at=x\ndomains=12\nnets=50\nrc=0\nsource=channel\n", encoding="utf-8")
    (feed / "nets.lst").unlink()
    r = _go(tool, env)
    assert r.returncode == 1 and "подсети: нет" in r.stderr, r.stderr
    assert "nft -f" not in _log(log), "набор тронут без подсетей"
    assert _nets_in_status(dump) == "nets=50"


def test_a_refused_subnet_set_keeps_the_previous_count(lists_env):
    """nft отклонил транзакцию — набор прежний: прежде в итог шло число пришедших."""
    tool, dns_d, dump, log, env = lists_env
    _prepared(lists_env)
    _c._fake(_bin(env), "nft", f'if [ "$1" = -f ]; then cat > /dev/null; exit 1; fi\necho "nft $*" >> {log}\n')
    r = _go(tool, env)
    assert r.returncode == 1 and "подсети: nft отклонил набор" in r.stderr, r.stderr
    assert _nets_in_status(dump) == "nets=5"


def test_a_topped_up_subnet_set_is_counted_by_the_set_itself(lists_env):
    """Один фид не скачался — набор только пополняется, в нём и подсети прежних
    прогонов: в итоге число по самому набору (интервал из слияния — один), а не
    число скачанных сейчас."""
    tool, dns_d, dump, log, env = lists_env
    _prepared(lists_env)
    listing = ("table inet awg_lan {\\n\\tset lan_vpn_nets4 {\\n\\t\\ttype ipv4_addr\\n\\t\\tflags interval\\n"
               "\\t\\tauto-merge\\n\\t\\telements = { 1.2.3.0/24, 5.6.0.0/16,\\n"
               "\\t\\t\\t     8.8.8.0/24, 91.108.4.0-91.108.7.255,\\n\\t\\t\\t     149.154.160.0/20, 157.240.0.0/16 }\\n"
               "\\t}\\n}\\n")
    _c._fake(_bin(env), "nft", f'if [ "$1" = -f ]; then echo "nft -f: $(cat)" >> {log}; exit 0; fi\n'
                               f'echo "nft $*" >> {log}; printf "{listing}"\n')
    r = _go(tool, {**env, "AWG_LAN_SUBNET_SERVICES": "telegram missing"})
    assert r.returncode == 1 and "подсети: missing не скачался" in r.stderr, r.stderr
    assert "flush set" not in _log(log), "при недошедшем фиде набор сброшен"
    # прежний итог — 5, скачано сейчас — 3, в наборе — 6
    assert _nets_in_status(dump) == "nets=6"


def test_a_failed_status_write_is_reported(lists_env):
    tool, dns_d, dump, log, env = lists_env
    _prepared(lists_env)
    _flaky(env, "mv", when="*lists.status*")
    r = _go(tool, env)
    assert r.returncode == 1 and "итог списков не записан" in r.stderr, r.stderr
    assert "nets=5\n" in (dump / "lists.status").read_text()
    assert not (dump / "lists.status.tmp").exists()


def test_without_faults_the_same_update_goes_through(lists_env):
    """Сторож самих подмен: тот же прогон без сбоя — фид с вычетом, набор, итог."""
    tool, dns_d, dump, log, env = lists_env
    _prepared(lists_env)
    cnt = _flaky(env, "awk", when="'-F/ '*", nth=99)
    r = _go(tool, env)
    assert r.returncode == 0, r.stderr
    feed = (dns_d / FEED).read_text()
    assert "nftset=/x.com/inet#awg_lan#lan_vpn4" in feed and "shop.ru" not in feed
    assert _vpn_line("shop.ru") in (dump / "vpn-feed.src").read_text()
    assert "nets=3\n" in (dump / "lists.status").read_text()
    assert "flush set inet awg_lan lan_vpn_nets4" in _log(log)
    assert cnt.read_text().strip() == "1", "подмена awk не встала на путь вычета"
    assert not list((dump / "rollback").glob("*"))


def test_an_unopenable_lock_is_a_step_failure(lists_env):
    """Файл блокировки не открылся: у dash отказ перенаправления exec завершал
    скрипт кодом 2 мимо всех строк — теперь код 1 и «списки не тронуты»."""
    tool, dns_d, dump, log, env = lists_env
    _prepared(lists_env)
    (dump / "lists.lock").mkdir()
    r = _go(tool, env)
    assert r.returncode == 1 and "списки не тронуты" in r.stderr, (r.returncode, r.stderr)
    assert (dns_d / FEED).read_text() == OLD_FEED
    assert "nets=5\n" in (dump / "lists.status").read_text()
    assert _log(log) == ""
