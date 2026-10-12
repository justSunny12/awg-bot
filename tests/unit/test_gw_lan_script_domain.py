"""
awg-lan-domain.sh — кнопки своих списков: add/ru/del/list, блокировка общая
со скриптом фидов, правило домена — одно с сервером AWG.
"""
from __future__ import annotations

import re

import pytest
from tests.unit import _gw_lan_common as _c
from tests.unit._gw_lan_common import (
    RU_USER, VPN_USER, _CANON_DOMAIN_RE, _DOMAIN_EXAMPLES, _bin, _fake, _heredoc, _log, _own, _run, _vpn_line,
)

script, domain_env, own_env = _c.script, _c.domain_env, _c.own_env


# ── персональные списки: awg-lan-domain.sh ───────────────────────────────────

def test_domain_tool_adds_resolves_and_fills_the_set(domain_env):
    tool, dns_d, log, env = domain_env
    r = _run(tool, env, "add", "https://www.Example.com/path", "not a domain")
    assert r.returncode == 0, r.stderr
    assert "<code>example.com</code>: добавлен" in r.stdout and "не похоже на домен" in r.stdout
    assert (dns_d / "awg-gw-vpn-user.conf").read_text() == "nftset=/example.com/inet#awg_home#lan_vpn4\n"
    text = log.read_text()
    assert "systemctl restart dnsmasq" in text, "именно restart: SIGHUP конфиги не перечитывает"
    assert "nft add element inet awg_home lan_vpn4 { 198.51.100.34 }" in text
    assert "2 адрес(а) в наборе lan_vpn4" in r.stdout
    assert "<code>example.com</code>: уже в списке" in _run(tool, env, "add", "example.com").stdout


def test_domain_tool_moves_between_lists_and_deletes(domain_env):
    tool, dns_d, log, env = domain_env
    _run(tool, env, "add", "shop.ru")
    r = _run(tool, env, "ru", "shop.ru")                      # напрямую → уходит из туннельного
    assert "<code>shop.ru</code>: добавлен" in r.stdout
    assert "shop.ru" not in (dns_d / "awg-gw-vpn-user.conf").read_text()
    assert (dns_d / "awg-gw-ru-user.conf").read_text().strip() == "nftset=/shop.ru/inet#awg_home#lan_ru4"
    assert _run(tool, env, "list").stdout.strip() == "ru shop.ru"
    assert "<code>shop.ru</code>: убран" in _run(tool, env, "del", "shop.ru").stdout
    assert _run(tool, env, "list").stdout.strip() == ""
    assert "в списках нет" in _run(tool, env, "del", "shop.ru").stdout


def test_domain_tool_refuses_the_vps_host(domain_env):
    """Увести хост ВПС в туннель — запереть себя: Endpoint аплинка под запретом."""
    tool, dns_d, log, env = domain_env
    r = _run(tool, env, "add", "VPS.example.net")
    assert "это хост сервера" in r.stdout
    assert "vps.example.net" not in (dns_d / "awg-gw-vpn-user.conf").read_text()


# ── блокировка ───────────────────────────────────────────────────────────────

def test_own_lists_wait_for_the_feeds_lock_and_give_up_with_75(own_env):
    """Кнопка «напрямую» совпала с обновлением фидов: оба пишут dnsmasq.d и
    перезапускают dnsmasq, скрипт фидов при этом читает ru-user.conf для
    вычитания. Без общей блокировки фид вычитался бы по полузаписанному файлу.
    Не дождались двух минут — код 75 и ничего не тронуто."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, vpn=["example.com"])
    before = (dns_d / VPN_USER).read_text()
    _fake(_bin(env), "flock", f'echo "flock $*" >> {log}\nexit 1\n')
    for args in (("add", "news.org"), ("ru", "shop.ru"), ("del", "example.com")):
        r = _run(tool, env, *args)
        assert r.returncode == 75, f"{args}: занятая блокировка выдана за {r.returncode}: {r.stdout}{r.stderr}"
        assert "обновление списков ещё идёт" in r.stderr
    text = _log(log)
    assert "flock -w 120 9" in text, f"ждать надо до двух минут: {text}"
    assert "systemctl" not in text and "nft" not in text and "dnsmasq" not in text, \
        f"без блокировки что-то применено: {text}"
    assert (dns_d / VPN_USER).read_text() == before
    assert not (dns_d / RU_USER).read_text()
    assert (dump / "lists.lock").exists(), "блокировка не в каталоге скрипта фидов — это не общая блокировка"


def test_own_lists_sync_also_takes_the_lock(own_env, tmp_path):
    tool, dns_d, dump, log, env = own_env
    _fake(_bin(env), "flock", f'echo "flock $*" >> {log}\nexit 1\n')
    f = tmp_path / "own.txt"; f.write_text("vpn news.org\n", encoding="utf-8")
    r = _run(tool, env, "sync", str(f))
    assert r.returncode == 75, r.stdout + r.stderr
    assert "news.org" not in (dns_d / VPN_USER).read_text()


def test_listing_own_lists_does_not_wait_for_the_lock(own_env):
    """Экран «Свои списки» читает list: пока идёт обновление фидов (до пары
    минут), экран не должен висеть и не должен падать с «занято»."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, vpn=["example.com"], ru=["shop.ru"])
    _fake(_bin(env), "flock", f'echo "flock $*" >> {log}\nexit 1\n')
    r = _run(tool, env, "list")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "vpn example.com\nru shop.ru\n"
    assert "flock" not in _log(log), "list взял блокировку — экран ждал бы обновления фидов"


def test_own_lists_run_normally_once_the_lock_is_taken(own_env):
    tool, dns_d, dump, log, env = own_env
    _fake(_bin(env), "flock", f'echo "flock $*" >> {log}\nexit 0\n')
    r = _run(tool, env, "add", "news.org")
    assert r.returncode == 0, r.stderr
    assert "flock -w 120 9" in _log(log) and "systemctl restart dnsmasq" in _log(log)
    assert (dns_d / VPN_USER).read_text() == _vpn_line("news.org") + "\n"


# ── правило домена — одно с сервером AWG ─────────────────────────────────────

def test_the_example_verdicts_are_the_canon_rule():
    """Сами примеры сверены с правилом канона: иначе ниже сравнивали бы скрипт
    с опечаткой в таблице."""
    wrong = [d for d, ok in _DOMAIN_EXAMPLES.items() if bool(_CANON_DOMAIN_RE.fullmatch(d)) != ok]
    assert wrong == [], f"пример расходится с правилом канона: {wrong}"


def test_the_script_domain_rule_is_the_canon_rule(script):
    """Строка DOMAIN_RE в скрипте — то же выражение, что у канона (без
    опережающей проверки длины: её делает `valid`)."""
    m = re.search(r"^DOMAIN_RE='([^']*)'$", _heredoc(script, "LAN_DOMAIN", "DOMEOF"), re.M)
    assert m, "в скрипте своих списков нет DOMAIN_RE"
    assert m.group(1) == _CANON_DOMAIN_RE.pattern.split("$)", 1)[1], m.group(1)


@pytest.mark.parametrize("locale", ["C", "C.UTF-8", "en_US.UTF-8"])
def test_buttons_accept_exactly_what_the_canon_accepts(own_env, locale):
    """Разойдись правило у агента и на сервере — вечное «применено ≠ канон»:
    домен, который сервер разослал, скрипт отвергает на каждом шлюзе. Прогоняем
    общий список через кнопку add и через правило канона. Локаль — как у юнита:
    [a-z] под UTF-8 не должен пускать кириллицу и умляуты."""
    tool, dns_d, dump, log, env = own_env
    r = _run(tool, {**env, "LC_ALL": locale}, "add", *_DOMAIN_EXAMPLES)
    assert r.returncode == 0, r.stderr
    lines = set(r.stdout.splitlines())
    disagree = []
    for d, ok in _DOMAIN_EXAMPLES.items():
        took = f"<code>{d}</code>: добавлен" in lines
        refused = f"{d}: не похоже на домен, пропущен" in lines
        assert took != refused, f"{d}: ни принят, ни отвергнут: {r.stdout}"
        if took != ok:
            disagree.append((d[:40], "скрипт принял" if took else "скрипт отверг"))
    assert disagree == [], f"кнопка и канон разошлись: {disagree}"
    written = set(_run(tool, env, "list").stdout.split())
    assert "-x.com" not in written and "a..b.com" not in written


@pytest.mark.parametrize("locale", ["C", "C.UTF-8", "en_US.UTF-8"])
def test_sync_accepts_exactly_what_the_canon_accepts(own_env, tmp_path, locale):
    """То же правило в режиме sync: файл с одним отвергнутым доменом — отказ
    целиком (rc=2), с принятым — применён."""
    tool, dns_d, dump, log, env = own_env
    disagree = []
    for i, (d, ok) in enumerate(_DOMAIN_EXAMPLES.items()):
        f = tmp_path / f"own{i}.txt"; f.write_text(f"vpn {d}\n", encoding="utf-8")
        r = _run(tool, {**env, "LC_ALL": locale}, "sync", str(f))
        assert r.returncode in (0, 2), (d, r.returncode, r.stderr)
        if (r.returncode == 0) != ok:
            disagree.append((d[:40], len(d), "sync принял" if r.returncode == 0 else "sync отверг"))
    assert disagree == [], f"sync и канон разошлись: {disagree}"


def test_a_multiline_argument_is_not_a_domain(own_env):
    """Аргумент кнопки с переводом строки: правило проверяет строку целиком, а
    не «хоть одну строку внутри» — иначе в список пишутся две директивы из
    одного значения, одно из которых правило не видело."""
    tool, dns_d, dump, log, env = own_env
    r = _run(tool, env, "add", "news.org\nbad name.com")
    assert "добавлен" not in r.stdout, r.stdout
    assert (dns_d / VPN_USER).read_text() == "", "значение с переводом строки записано в список"


def test_resolving_asks_once_with_a_short_timeout(own_env):
    """dig по умолчанию делает три попытки: недоступный резолвер держал бы
    кнопку по 9 с на домен и выходил за таймаут агента."""
    tool, dns_d, dump, log, env = own_env
    calls = dump.parent / "dig-calls"
    _fake(_bin(env), "dig", f'echo "$*" >> {calls}\necho 10.4.4.4\n')
    _own(dns_d)
    assert _run(tool, env, "add", "alpha.com").returncode == 0
    lines = calls.read_text().splitlines()
    assert lines and all("+time=3" in ln and "+tries=1" in ln for ln in lines), lines


def test_domain_tool_prints_plain_domains_without_the_html_flag(domain_env):
    """В терминале (awg-bot lan …) и у агента прежних выпусков теги <code> не
    нужны: без AWG_HTML скрипт печатает домен голым текстом."""
    tool, dns_d, log, env = domain_env
    env = {k: v for k, v in env.items() if k != "AWG_HTML"}
    r = _run(tool, env, "add", "example.com")
    assert r.returncode == 0, r.stderr
    assert "example.com: добавлен" in r.stdout and "<code>" not in r.stdout, r.stdout
