"""
awg-lan-domain.sh fill <файл> — адреса новых «в туннель» фоном после sync:
параллельный dig и один вызов nft на весь набор.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from tests.unit import _gw_lan_common as _c
from tests.unit._gw_lan_common import (
    FEED, RU_USER, VPN_USER, _DIG_MAP, _bin, _fake, _fill, _fill_run, _log, _nft, _own, _run, _set, _sets_of,
    _sync, _with_src,
)

script, domain_env, own_env = _c.script, _c.domain_env, _c.own_env


# ── fill <файл>: адреса новых «в туннель» — фоном после sync ────────────────

def test_fill_puts_the_addresses_into_lan_vpn4_and_rewrites_the_snapshot(own_env, tmp_path):
    """Агент зовёт fill после sync: без него новые домены «в туннель» шли бы
    мимо туннеля до первого запроса пользователя к dnsmasq, а после перезагрузки —
    до следующего (слепок не знал бы их адресов)."""
    tool, dns_d, dump, log, env = own_env
    _fill(_sets_of(dump), "lan_vpn4", ["10.3.3.3"])
    r = _fill_run(tool, env, tmp_path, "example.com\nalpha.com\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _set(_sets_of(dump), "lan_vpn4") == {"10.3.3.3", "198.51.100.34", "198.51.100.35", "10.4.4.4"}
    snap = (dump / "lan_vpn4.nft").read_text()
    assert all(ip in snap for ip in ("10.3.3.3", "198.51.100.34", "198.51.100.35", "10.4.4.4")), snap
    nft = _nft(log)
    assert nft[-1] == "nft list set inet awg_lan lan_vpn4", f"слепок снят не после наполнения: {nft}"
    # все адреса — одним вызовом nft, отсортированные: 500 доменов по вызову на адрес шли бы минуты
    assert [ln for ln in nft if "add element" in ln] == \
        ["nft add element inet awg_lan lan_vpn4 { 10.4.4.4,198.51.100.34,198.51.100.35 }"], nft
    assert r.stdout.strip() == "набор lan_vpn4: адреса 2 доменов взяты в обработку", r.stdout
    assert not any("lan_ru4" in ln for ln in nft), "fill тронул «напрямую»"


# родительный падеж после «адреса»: 1, 21, 101 — «домена», остальные (и 11–19) — «доменов»
@pytest.mark.parametrize("n,word", [(1, "домена"), (2, "доменов"), (4, "доменов"), (5, "доменов"),
                                    (11, "доменов"), (12, "доменов"), (21, "домена"), (22, "доменов"),
                                    (25, "доменов")])
def test_fill_names_the_count_in_proper_russian(own_env, tmp_path, n, word):
    tool, dns_d, dump, log, env = own_env
    r = _fill_run(tool, env, tmp_path, "".join(f"site{i}.org\n" for i in range(n)))
    assert r.returncode == 0 and r.stdout.strip() == f"набор lan_vpn4: адреса {n} {word} взяты в обработку", r.stdout


def test_fill_skips_a_foreign_line_and_fills_the_rest(own_env, tmp_path):
    """Файл пишет агент, но скрипт на root не верит ему на слово: строка не
    домен — пропуск (ни dig, ни nft по ней), остальные — в набор."""
    tool, dns_d, dump, log, env = own_env
    bad = ["bad name.com", "-x.com", "nftset=/evil.com/inet#awg_lan#lan_ru4", "a..b.com", "Alpha.com",
           "vpn news.org", "; rm -rf /"]
    dig_log = tmp_path / "dig.log"
    body = f'echo "$*" >> {dig_log}\n' + 'case "$*" in\n' + "".join(
        f'  *" {d} "*) ' + "; ".join(f"echo {ip}" for ip in ips) + " ;;\n" for d, ips in _DIG_MAP.items()
    ) + "esac\n"
    _fake(_bin(env), "dig", body)
    r = _fill_run(tool, env, tmp_path, "\n".join(["news.org", *bad, "zeta.com"]) + "\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _set(_sets_of(dump), "lan_vpn4") == {"10.3.3.3", "10.5.5.5"}
    assert "адреса 2 доменов взяты в обработку" in r.stdout, f"чужие строки посчитаны: {r.stdout}"
    assert [ln for ln in _nft(log) if "add element" in ln] == \
        ["nft add element inet awg_lan lan_vpn4 { 10.3.3.3,10.5.5.5 }"], _nft(log)
    asked = dig_log.read_text().splitlines()
    assert len(asked) == 2 and all(" news.org " in a or " zeta.com " in a for a in asked), \
        f"dig спрошен по чужой строке: {asked}"


def test_fill_does_not_take_the_lists_lock(own_env, tmp_path):
    """fill идёт фоном до получаса; возьми он lists.lock — кнопки «Свои списки»
    и обновление фидов получали бы «обновление списков ещё идёт» (75) всё это
    время. И наоборот: занятая блокировка fill не останавливает."""
    tool, dns_d, dump, log, env = own_env
    _fake(_bin(env), "flock", f'echo "flock $*" >> {log}\nexit 1\n')     # блокировку держит другой
    r = _fill_run(tool, env, tmp_path, "news.org\n")
    assert r.returncode == 0, f"занятая блокировка остановила fill: rc={r.returncode} {r.stderr}"
    assert "flock" not in _log(log), "fill взял блокировку списков"
    assert _set(_sets_of(dump), "lan_vpn4") == {"10.3.3.3"}
    assert not (dump / "lists.lock").exists(), "fill открыл файл блокировки"


def test_fill_leaves_the_dnsmasq_files_alone(own_env, tmp_path):
    """fill — только набор и слепок: ни файлов dnsmasq.d, ни рестарта."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, vpn=["news.org"], ru=["shop.ru"])
    _with_src(dns_d, dump)
    before = {f: (dns_d / f).read_text() for f in (VPN_USER, RU_USER, FEED)}
    assert _fill_run(tool, env, tmp_path, "alpha.com\nshop.ru\n").returncode == 0
    assert {f: (dns_d / f).read_text() for f in before} == before
    assert "systemctl" not in _log(log) and "dnsmasq" not in _log(log)


def test_fill_of_an_empty_file_is_quiet_and_ok(own_env, tmp_path):
    tool, dns_d, dump, log, env = own_env
    r = _fill_run(tool, env, tmp_path, "")
    assert r.returncode == 0, r.stderr
    assert "адреса 0 доменов взяты в обработку" in r.stdout, r.stdout
    assert not any("add element" in ln for ln in _nft(log))


def test_fill_where_dig_finds_nothing_is_still_ok(own_env, tmp_path):
    """DNS не ответил (dig пуст или упал) — не ошибка: dnsmasq наполнит набор
    первым запросом пользователя."""
    tool, dns_d, dump, log, env = own_env
    _fake(_bin(env), "dig", "echo ';; connection timed out' >&2; exit 9\n")
    r = _fill_run(tool, env, tmp_path, "news.org\n")
    assert r.returncode == 0, r.stderr
    assert not any("add element" in ln for ln in _nft(log))


# ── fill_set: параллельный dig и один вызов nft на весь набор ────────────────

def _dig_by(env: dict, table: dict[str, list[str]], extra: str = "") -> None:
    """Подставной dig по своей таблице; extra — строки сценария перед ответом
    (задержка, журнал запросов)."""
    body = extra + 'case "$*" in\n' + "".join(
        f'  *" {d} "*) ' + ("; ".join(f"echo {ip}" for ip in ips) or ":") + " ;;\n" for d, ips in table.items()
    ) + "esac\n"
    _fake(_bin(env), "dig", body)


def _adds(log: Path, name: str) -> list[str]:
    return [ln for ln in _nft(log) if f"add element inet awg_lan {name} " in ln]


def test_fill_puts_an_address_shared_by_two_domains_once(own_env, tmp_path):
    """CDN отдаёт один адрес нескольким доменам. В общий вызов nft адрес идёт
    один раз: на сотнях доменов CDN дубли раздули бы строку в разы, а мусор
    из dig (CNAME) в `{ … }` сорвал бы весь вызов — и набор остался бы пустым."""
    tool, dns_d, dump, log, env = own_env
    _dig_by(env, {"alpha.com": ["10.7.7.7", "10.4.4.4"], "beta.com": ["10.7.7.7"],
                  "gamma.com": ["cdn.gamma.net.", "10.7.7.7"]})
    r = _fill_run(tool, env, tmp_path, "alpha.com\nbeta.com\ngamma.com\n")
    assert r.returncode == 0, r.stdout + r.stderr
    adds = _adds(log, "lan_vpn4")
    assert adds == ["nft add element inet awg_lan lan_vpn4 { 10.4.4.4,10.7.7.7 }"], \
        f"общий адрес не схлопнут или CNAME попал в nft: {adds}"
    assert _set(_sets_of(dump), "lan_vpn4") == {"10.4.4.4", "10.7.7.7"}
    assert "адреса 3 доменов взяты в обработку" in r.stdout, r.stdout
    snap = (dump / "lan_vpn4.nft").read_text()
    assert "10.4.4.4" in snap and "10.7.7.7" in snap, f"слепок не снят после наполнения: {snap}"


def test_fill_goes_on_when_one_domain_gets_no_answer(own_env, tmp_path):
    """Один домен не ответил (таймаут dig, код 9) — остальные всё равно в
    наборе: иначе один мёртвый домен из сотни оставлял бы всю сотню мимо
    туннеля до первых запросов пользователей."""
    tool, dns_d, dump, log, env = own_env
    _dig_by(env, {"alpha.com": ["10.4.4.4"], "zeta.com": ["10.5.5.5"]},
            extra='case "$*" in *" silent.com "*) echo ";; connection timed out; no servers could be reached"; '
                  'exit 9 ;; esac\n')
    r = _fill_run(tool, env, tmp_path, "alpha.com\nsilent.com\nzeta.com\n")
    assert r.returncode == 0, f"молчащий домен уронил fill: {r.stdout} {r.stderr}"
    assert _adds(log, "lan_vpn4") == ["nft add element inet awg_lan lan_vpn4 { 10.4.4.4,10.5.5.5 }"], _nft(log)
    assert _set(_sets_of(dump), "lan_vpn4") == {"10.4.4.4", "10.5.5.5"}
    assert "connection timed out" not in r.stdout + r.stderr, "текст dig вылез наружу"
    assert "адреса 3 доменов взяты в обработку" in r.stdout, "счётчик — по отобранным строкам, а не по ответившим"


def test_fill_resolves_domains_in_parallel(own_env, tmp_path):
    """Сотни доменов по одному dig (до 3 с на таймаут) шли бы минутами, всё
    это время новые домены «в туннель» мимо туннеля. 8 доменов по 0,5 с
    подряд — 4 с; параллельно — около половины секунды, с запасом на
    запуск оболочек под нагрузкой — до 3 с."""
    tool, dns_d, dump, log, env = own_env
    names = [f"site{i}.org" for i in range(8)]
    # sleep в bin подменён пустышкой — задержку даём настоящим
    _dig_by(env, {d: [f"10.8.0.{i + 1}"] for i, d in enumerate(names)}, extra="/bin/sleep 0.5\n")
    t0 = time.monotonic()
    r = _fill_run(tool, env, tmp_path, "".join(d + "\n" for d in names))
    took = time.monotonic() - t0
    assert r.returncode == 0, r.stdout + r.stderr
    assert took < 3.0, f"fill занял {took:.2f} с — dig идёт по одному, а не параллельно"
    assert _set(_sets_of(dump), "lan_vpn4") == {f"10.8.0.{i + 1}" for i in range(8)}
    assert len(_adds(log, "lan_vpn4")) == 1, _nft(log)


def test_fill_with_nothing_selected_does_not_call_nft_add_but_takes_the_snapshot(own_env, tmp_path):
    """Все строки чужие — ни dig, ни `nft add element` с пустыми `{ }` (это
    синтаксическая ошибка nft); слепок всё равно снимается — как и после
    любого fill."""
    tool, dns_d, dump, log, env = own_env
    dig_log = tmp_path / "dig.log"
    _dig_by(env, {"news.org": ["10.3.3.3"]}, extra=f'echo "$*" >> {dig_log}\n')
    _fill(_sets_of(dump), "lan_vpn4", ["10.3.3.3"])
    r = _fill_run(tool, env, tmp_path, "bad name.com\n-x.com\n; rm -rf /\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == "набор lan_vpn4: адреса 0 доменов взяты в обработку", r.stdout
    assert not any("add element" in ln for ln in _nft(log)), f"nft add вызван без адресов: {_nft(log)}"
    assert not dig_log.exists(), "dig спрошен по чужой строке"
    assert "nft list set inet awg_lan lan_vpn4" in _nft(log), "слепок не снят"
    assert "10.3.3.3" in (dump / "lan_vpn4.nft").read_text()


def test_fill_survives_a_failing_nft_add_and_still_takes_the_snapshot(own_env, tmp_path):
    """nft отверг пакет (адрес внутри интервала, набор не создан) — fill не
    падает и ошибку человеку не показывает; слепок снимается как обычно."""
    tool, dns_d, dump, log, env = own_env
    _fake(_bin(env), "nft", f'echo "nft $*" >> {log}\n[ "$1" = add ] && {{ echo "Error: conflicting intervals" >&2; exit 1; }}\n'
                            'echo "elements = { 10.3.3.3 }"\nexit 0\n')
    r = _fill_run(tool, env, tmp_path, "news.org\nalpha.com\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Error" not in r.stderr, "ошибка nft вылезла человеку"
    assert _nft(log)[-1] == "nft list set inet awg_lan lan_vpn4", _nft(log)
    assert "адреса 2 доменов взяты в обработку" in r.stdout


def test_sync_refills_lan_ru4_with_one_nft_call_after_the_flush(own_env, tmp_path):
    """«Напрямую» после sync наполняется заново: сброс, затем ВСЕ адреса одним
    вызовом. Вызов до сброса был бы им стёрт; вызов на адрес — минуты на
    большом списке, пока исключения идут в туннель."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, ru=["shop.ru"])
    _fill(_sets_of(dump), "lan_ru4", ["10.1.1.1", "10.9.9.9"])
    r = _sync(tool, env, tmp_path, "ru shop.ru\nru bank.ru\nru example.com\n")
    assert r.returncode == 0, r.stdout + r.stderr
    nft = _nft(log)
    adds = _adds(log, "lan_ru4")
    assert adds == ["nft add element inet awg_lan lan_ru4 { 10.1.1.1,10.2.2.2,198.51.100.34,198.51.100.35 }"], nft
    assert nft.index("nft flush set inet awg_lan lan_ru4") < nft.index(adds[0]), "наполнение до сброса стёрто сбросом"
    assert _set(_sets_of(dump), "lan_ru4") == {"10.1.1.1", "10.2.2.2", "198.51.100.34", "198.51.100.35"}, \
        "адрес не из списка пережил перезаполнение"
    assert not _adds(log, "lan_vpn4"), "sync наполнил «в туннель» — это работа fill"

def test_fill_without_a_readable_file_is_a_usage_error_naming_fill(own_env, tmp_path):
    tool, dns_d, dump, log, env = own_env
    for args in (("fill",), ("fill", str(tmp_path / "missing.txt")), ("fill", "a", "b")):
        r = _run(tool, env, *args)
        assert r.returncode == 1 and "usage" in r.stdout and "fill" in r.stdout, (args, r.stdout)
    r = _run(tool, env, "nope")
    assert r.returncode == 1 and "fill <файл>" in r.stdout, f"общий usage не называет fill: {r.stdout}"
    assert _log(log) == ""
