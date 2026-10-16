"""
awg-lan-lists.sh — фиды списков: скачанные и из канала, проверка dnsmasq,
блокировка, откат по отказу рестарта, исходник фида до вычитания, копии для
отката вне conf-dir.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from tests.conftest import SH
from tests.unit import _gw_lan_common as _c
from tests.unit._gw_lan_common import (
    FEED, RU_USER, _bin, _failing_restart, _fake, _heredoc, _no_copies, _ru_line, _run, _snapshot_restart,
    _snapshots, _vpn_line,
)

script, lists_env, channel_env = _c.script, _c.lists_env, _c.channel_env


# ── списки: awg-lan-lists.sh ─────────────────────────────────────────────────

def test_lists_convert_feed_subtract_exceptions_and_load_subnets(lists_env):
    tool, dns_d, dump, log, env = lists_env
    (dns_d / "awg-gw-ru-user.conf").write_text("nftset=/shop.ru/inet#awg_lan#lan_ru4\n", encoding="utf-8")
    r = subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    feed = (dns_d / "awg-gw-vpn-feed.conf").read_text()
    assert "nftset=/youtube.com/googlevideo.com/inet#awg_lan#lan_vpn4" in feed, "ipset= → nftset= в наш набор"
    assert "shop.ru" not in feed, "исключение вычтено: dnsmasq применяет одну директиву на домен"
    assert "nftset=/x.com/inet#awg_lan#lan_vpn4" in feed
    assert "<html>" not in feed, "мусор HTTP 200 в conf-dir не попадает"
    text = log.read_text()
    assert "dnsmasq --test" in text and text.index("dnsmasq --test") < text.index("systemctl restart dnsmasq"), \
        "конфиг проверен до рестарта"
    assert "systemctl restart dnsmasq" in text
    assert "flush set inet awg_lan lan_vpn_nets4" in text and "8.8.8.0/24" in text and "91.108.4.0/22" in text
    assert text.index("flush set") < text.index("add element"), "одной транзакцией: flush, затем add"
    assert (dump / "lan_vpn4.nft").exists() and (dump / "lan_vpn_nets4.nft").exists(), "слепки для старта"
    status = (dump / "lists.status").read_text()
    assert "domains=14\n" in status and "nets=3\n" in status and "rc=0" in status


def test_lists_roll_back_a_feed_that_dnsmasq_rejects(lists_env):
    """dnsmasq --test отверг новый фид — прежний файл на месте, рестарта нет,
    локальная сеть не остаётся без DNS."""
    tool, dns_d, dump, log, env = lists_env
    (dns_d / "awg-gw-vpn-feed.conf").write_text("nftset=/old.org/inet#awg_lan#lan_vpn4\n", encoding="utf-8")
    r = subprocess.run([SH, str(tool)], capture_output=True, text=True, env={**env, "DNSMASQ_TEST_RC": "1"})
    assert r.returncode == 1 and "откатываю" in r.stderr
    assert (dns_d / "awg-gw-vpn-feed.conf").read_text() == "nftset=/old.org/inet#awg_lan#lan_vpn4\n"
    assert "systemctl restart dnsmasq" not in log.read_text()


def test_lists_refuse_a_suspiciously_short_feed(lists_env, tmp_path):
    tool, dns_d, dump, log, env = lists_env
    (tmp_path / "feeds" / "domains").write_text("<html>blocked</html>\n", encoding="utf-8")
    r = subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "подозрительно короткий" in r.stderr
    assert not (dns_d / "awg-gw-vpn-feed.conf").exists()


def test_lists_restart_dnsmasq_only_when_the_feed_changed(lists_env):
    """Рестарт роняет кэш всей сети — вхолостую его не делаем."""
    tool, dns_d, dump, log, env = lists_env
    subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)
    n = log.read_text().count("systemctl restart dnsmasq")
    subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)
    assert log.read_text().count("systemctl restart dnsmasq") == n, "фид тот же — рестарта нет"


def test_lists_report_a_failed_feed_but_keep_going(lists_env):
    tool, dns_d, dump, log, env = lists_env
    env = {**env, "AWG_LAN_DOMAINS_URL": "https://nowhere.invalid/x.lst"}
    r = subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "фид не скачался" in r.stderr
    assert "flush set" in log.read_text(), "подсети залиты, несмотря на отказ доменов"
    assert "rc=1" in (dump / "lists.status").read_text()


# ── списки из канала линка ──────────────────

def test_channel_feeds_are_used_without_a_single_download(channel_env):
    """Смысл этапа 3: фиды привёз сервер, и адрес локальной сети за ними не ходит ни
    на GitHub, ни в Google. Проверки те же, что для скачанного."""
    tool, dns_d, dump, log, env, _feed = channel_env
    (dns_d / "awg-gw-ru-user.conf").write_text("nftset=/shop.ru/inet#awg_lan#lan_ru4\n", encoding="utf-8")
    r = subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    text = log.read_text()
    assert "curl" not in text, f"при фидах из канала скрипт ходил в сеть: {text}"
    feed = (dns_d / "awg-gw-vpn-feed.conf").read_text()
    assert "nftset=/chan0.org/inet#awg_lan#lan_vpn4" in feed
    assert "shop.ru" not in feed and "<html>" not in feed, "исключения и мусор — как у скачанного"
    assert "91.108.4.0/22" in text and "8.8.8.0/24" in text and "не подсеть" not in text
    status = (dump / "lists.status").read_text()
    assert "source=channel\n" in status and "rc=0" in status


def test_a_broken_channel_feed_is_refused_like_a_downloaded_one(channel_env):
    """Сервер прислал заглушку вместо фида (или его подменили). dnsmasq локальной сети
    получает её не больше, чем получил бы скачанную: короткий фид отвергнут,
    прежний файл на месте."""
    tool, dns_d, dump, log, env, feed = channel_env
    (dns_d / "awg-gw-vpn-feed.conf").write_text("nftset=/old.org/inet#awg_lan#lan_vpn4\n", encoding="utf-8")
    (feed / "domains.lst").write_text("<html>blocked</html>\n", encoding="utf-8")
    r = subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "подозрительно короткий" in r.stderr
    assert (dns_d / "awg-gw-vpn-feed.conf").read_text() == "nftset=/old.org/inet#awg_lan#lan_vpn4\n"
    assert "curl" not in log.read_text(), "отказ фида из канала не повод идти за ним в сеть"


def test_a_channel_feed_that_dnsmasq_rejects_is_rolled_back(channel_env):
    tool, dns_d, dump, log, env, _feed = channel_env
    (dns_d / "awg-gw-vpn-feed.conf").write_text("nftset=/old.org/inet#awg_lan#lan_vpn4\n", encoding="utf-8")
    r = subprocess.run([SH, str(tool)], capture_output=True, text=True, env={**env, "DNSMASQ_TEST_RC": "1"})
    assert r.returncode == 1 and "откатываю" in r.stderr
    assert (dns_d / "awg-gw-vpn-feed.conf").read_text() == "nftset=/old.org/inet#awg_lan#lan_vpn4\n"
    assert "systemctl restart dnsmasq" not in log.read_text()


def test_a_missing_channel_feed_is_named_as_such(channel_env):
    """Каталог пуст — сообщение говорит про канал, а не «фид не скачался»:
    иначе человек пошёл бы чинить сеть шлюза, которая тут ни при чём."""
    tool, dns_d, dump, log, env, feed = channel_env
    (feed / "domains.lst").unlink()
    (feed / "nets.lst").unlink()
    r = subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 1
    assert "фида нет в" in r.stderr and "привозит канал" in r.stderr
    assert "не скачался" not in r.stderr
    assert "curl" not in log.read_text()


def test_without_the_channel_the_script_downloads_and_says_so(lists_env):
    tool, dns_d, dump, log, env = lists_env
    r = subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "source=net\n" in (dump / "lists.status").read_text()


# ── списки: блокировка, проверка конфигурации, отказ рестарта ───────────────

def test_lists_wait_for_the_lock_and_say_busy_with_code_75(lists_env):
    """Ручное «Обновить» и фиды из канала совпали по времени. Раньше второй
    запуск молча выходил с нулём — агент считал фиды из канала применёнными,
    хотя они не легли. Теперь ждём блокировку до двух минут, а не дождались —
    код 75 («занято»), и ничего не трогаем."""
    tool, dns_d, dump, log, env = lists_env
    bin_dir = Path(env["PATH"].split(":", 1)[0])
    _fake(bin_dir, "flock", f'echo "flock $*" >> {log}\nexit 1\n')       # блокировку держит другой
    (dns_d / "awg-gw-vpn-feed.conf").write_text("nftset=/old.org/inet#awg_lan#lan_vpn4\n", encoding="utf-8")
    r = subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 75, f"занятая блокировка выдана за {r.returncode}: {r.stderr}"
    assert "обновление уже идёт" in r.stderr
    text = log.read_text()
    assert "flock -w 120 9" in text, f"ждать блокировку надо до двух минут, а не сдаваться сразу: {text}"
    assert "systemctl" not in text and "nft" not in text, "без блокировки скрипт что-то применил"
    assert (dns_d / "awg-gw-vpn-feed.conf").read_text() == "nftset=/old.org/inet#awg_lan#lan_vpn4\n"
    assert not (dump / "lists.status").exists(), "статус переписан запуском, который ничего не сделал"


def test_lists_run_normally_once_the_lock_is_taken(lists_env):
    tool, dns_d, dump, log, env = lists_env
    bin_dir = Path(env["PATH"].split(":", 1)[0])
    _fake(bin_dir, "flock", f'echo "flock $*" >> {log}\nexit 0\n')
    r = subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "systemctl restart dnsmasq" in log.read_text()


def test_lists_check_the_feed_with_the_same_conf_dir_as_debian(lists_env):
    """init-скрипт Debian подключает conf-dir ключом, а не строкой в
    dnsmasq.conf: голый `dnsmasq --test` наш фид не видел вовсе и одобрял
    любой мусор, после которого рестарт оставлял локальную сеть без DNS."""
    tool, dns_d, dump, log, env = lists_env
    r = subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    tests = [ln for ln in log.read_text().splitlines() if ln.startswith("dnsmasq --test")]
    assert tests == [f"dnsmasq --test --conf-dir={dns_d},.dpkg-dist,.dpkg-old,.dpkg-new"], (
        f"проверка конфигурации не видит каталог с фидом: {tests}")


def test_a_feed_dnsmasq_cannot_start_with_is_rolled_back(lists_env):
    """--test пропустил, а демон с новым фидом не поднялся (память, дубли с
    чужим файлом). Раньше код был 0 и файл оставался — dnsmasq лежал, локальная сеть
    без DNS, агент думал, что всё применено. Теперь прежний фид возвращается,
    демон поднимается с ним, код 1."""
    tool, dns_d, dump, log, env = lists_env
    feed = dns_d / "awg-gw-vpn-feed.conf"
    feed.write_text("nftset=/old.org/inet#awg_lan#lan_vpn4\n", encoding="utf-8")
    _failing_restart(Path(env["PATH"].split(":", 1)[0]), log)
    r = subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 1, f"отказ рестарта выдан за успех: rc={r.returncode}"
    assert "не поднялся с новым фидом" in r.stderr and "откатываю" in r.stderr
    assert feed.read_text() == "nftset=/old.org/inet#awg_lan#lan_vpn4\n", "прежний фид не вернулся"
    assert not (dns_d / "awg-gw-vpn-feed.conf.prev.awg").exists(), "копия отката осталась в conf-dir"
    assert log.read_text().count("systemctl restart dnsmasq") == 2, (
        "после отката демон надо поднять с прежним фидом")
    assert "rc=1" in (dump / "lists.status").read_text()


def test_a_first_feed_dnsmasq_cannot_start_with_is_removed(lists_env):
    """Прежнего фида не было — откатывать не к чему, новый убирается целиком:
    dnsmasq без фида лучше, чем лежащий dnsmasq с фидом."""
    tool, dns_d, dump, log, env = lists_env
    _failing_restart(Path(env["PATH"].split(":", 1)[0]), log)
    r = subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 1
    assert not (dns_d / "awg-gw-vpn-feed.conf").exists(), "фид, с которым демон не встал, остался"


def test_the_rollback_copy_is_removed_only_after_a_successful_restart(lists_env):
    tool, dns_d, dump, log, env = lists_env
    feed = dns_d / "awg-gw-vpn-feed.conf"
    feed.write_text("nftset=/old.org/inet#awg_lan#lan_vpn4\n", encoding="utf-8")
    r = subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "nftset=/x.com/inet#awg_lan#lan_vpn4" in feed.read_text()
    assert not (dns_d / "awg-gw-vpn-feed.conf.prev.awg").exists(), (
        "копия отката осталась в conf-dir: dnsmasq читает там всё, кроме .dpkg-*")


# ── исходник фида: копия до вычитания ────────────────────────────────────────

def test_downloaded_feed_source_is_kept_before_subtraction(lists_env):
    """По исходнику скрипт своих списков вычитает исключения заново. Храни мы
    фид после вычитания — убранное «напрямую» в фид бы не вернулось."""
    tool, dns_d, dump, log, env = lists_env
    (dns_d / RU_USER).write_text(_ru_line("shop.ru") + "\n", encoding="utf-8")
    r = subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    src = (dump / "vpn-feed.src").read_text()
    assert _vpn_line("shop.ru") + "\n" in src, "исходник сохранён уже после вычитания"
    assert _vpn_line("youtube.com/googlevideo.com") in src and "vpn_domains" not in src, \
        "исходник не в формате nftset — вычитание по нему не сработает"
    assert "shop.ru" not in (dns_d / FEED).read_text()
    assert not (dump / "vpn-feed.src.tmp").exists()


def test_channel_feed_source_is_kept_before_subtraction(channel_env):
    tool, dns_d, dump, log, env, _feed = channel_env
    (dns_d / RU_USER).write_text(_ru_line("shop.ru") + "\n", encoding="utf-8")
    r = subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    src = (dump / "vpn-feed.src").read_text()
    assert _vpn_line("shop.ru") + "\n" in src and _vpn_line("chan0.org") + "\n" in src, src


def _domain_tool_beside(script, lists_env_tuple, tmp_path) -> tuple[Path, dict]:
    """Скрипт своих списков рядом со скриптом фидов: тот же dnsmasq.d, тот же
    каталог исходника и блокировки — как на шлюзе."""
    tool, dns_d, dump, log, env = lists_env_tuple[:5]
    bin_dir = _bin(env)
    _fake(bin_dir, "id", "echo 0\n")
    _fake(bin_dir, "sleep", "")
    _fake(bin_dir, "dig", "")
    dtool = tmp_path / "awg-lan-domain.sh"
    dtool.write_text(_heredoc(script, "LAN_DOMAIN", "DOMEOF"), encoding="utf-8")
    conf = tmp_path / "awg0.conf"; conf.write_text("[Peer]\nEndpoint = vps.example.net:51820\n", encoding="utf-8")
    denv = {k: v for k, v in env.items() if k != "AWG_LAN_FROM"}
    return dtool, {**denv, "AWG_UPLINK_CONF": str(conf)}


def test_an_exception_removed_after_a_feed_run_returns_to_that_feed(script, lists_env, tmp_path):
    """Сквозной сценарий: фиды применены с исключением shop.ru, человек
    убрал исключение — shop.ru снова в фиде, не дожидаясь новых фидов."""
    tool, dns_d, dump, log, env = lists_env
    (dns_d / RU_USER).write_text(_ru_line("shop.ru") + "\n", encoding="utf-8")
    assert subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env).returncode == 0
    assert "shop.ru" not in (dns_d / FEED).read_text()
    dtool, denv = _domain_tool_beside(script, lists_env, tmp_path)
    r = _run(dtool, denv, "del", "shop.ru")
    assert r.returncode == 0, r.stderr
    assert _vpn_line("shop.ru") + "\n" in (dns_d / FEED).read_text(), "исключение убрано, а в фид домен не вернулся"


def test_a_refused_short_feed_is_not_applied_later_by_an_exception_change(script, channel_env, tmp_path):
    """Сервер прислал обрезанный фид — скрипт фидов его отверг как
    подозрительно короткий, в dnsmasq.d остался прежний полный. Следующее
    изменение «напрямую» не должно поставить отвергнутый фид в обход этой
    проверки: исходник обязан соответствовать применённому фиду."""
    tool, dns_d, dump, log, env, feed = channel_env
    assert subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env).returncode == 0
    good = (dns_d / FEED).read_text()
    assert good.count("nftset=") >= 10
    (feed / "domains.lst").write_text("ipset=/cut1.org/vpn_domains\nipset=/cut2.org/vpn_domains\n", encoding="utf-8")
    r = subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "подозрительно короткий" in r.stderr
    assert (dns_d / FEED).read_text() == good
    dtool, denv = _domain_tool_beside(script, channel_env, tmp_path)
    r = _run(dtool, denv, "ru", "chan0.org")
    assert r.returncode == 0, r.stderr
    now = (dns_d / FEED).read_text()
    assert "cut1.org" not in now and now.count("nftset=") >= 10, \
        f"отвергнутый короткий фид встал через кнопку «напрямую»: {now}"


# ── копии для отката — вне conf-dir ──────────────────────────────────────────

def test_the_feed_rollback_copy_is_outside_the_conf_dir_during_the_restart(lists_env):
    tool, dns_d, dump, log, env = lists_env
    (dns_d / FEED).write_text("nftset=/old.org/inet#awg_lan#lan_vpn4\n", encoding="utf-8")
    snap = _snapshot_restart(env, log)
    r = subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    (confdir, rb), = _snapshots(snap)
    assert _no_copies(confdir), f"при рестарте dnsmasq в conf-dir лежала копия: {confdir}"
    assert FEED in confdir
    assert rb == {"vpn-feed.conf.prev"}, f"копии для отката на время рестарта нет в rollback/: {rb}"
    assert not (dump / "rollback" / "vpn-feed.conf.prev").exists(), "копия пережила удачный рестарт"


def test_a_failed_feed_restart_restores_the_feed_from_rollback(lists_env):
    tool, dns_d, dump, log, env = lists_env
    old = "nftset=/old.org/inet#awg_lan#lan_vpn4\n"
    (dns_d / FEED).write_text(old, encoding="utf-8")
    snap = _snapshot_restart(env, log, fails=1)
    r = subprocess.run([SH, str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "откатываю" in r.stderr, (r.returncode, r.stderr)
    assert (dns_d / FEED).read_text() == old, "прежний фид не вернулся из rollback/"
    shots = _snapshots(snap)
    assert len(shots) == 2 and all(_no_copies(c) for c, _ in shots), shots
    assert not (dump / "rollback" / "vpn-feed.conf.prev").exists(), "копия осталась после отката"
