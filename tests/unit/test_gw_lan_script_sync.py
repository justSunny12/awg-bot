"""
awg-lan-domain.sh sync <файл> — полный список с сервера: применение целиком
или отказ целиком, итог, наборы, откат и копии для отката вне conf-dir.
"""
from __future__ import annotations

import pytest
from tests.unit import _gw_lan_common as _c
from tests.unit._gw_lan_common import (
    FEED, RU_USER, VPN_USER, _fill, _fill_run, _heredoc, _log, _long, _no_copies, _nft, _own, _ru_line, _run,
    _set, _sets_of, _snapshot_restart, _snapshots, _sync, _vpn_line, _with_src,
)

script, domain_env, own_env = _c.script, _c.domain_env, _c.own_env


# ── sync <файл> ──────────────────────────────────────────────────────────────

def test_the_script_announces_sync_in_its_header(script):
    """По метке агент узнаёт, что скрипт на шлюзе уже понимает sync: без неё
    он не пошлёт полный список в скрипт, который его отвергнет."""
    assert "\n# awg-lan-domain: sync\n" in _heredoc(script, "LAN_DOMAIN", "DOMEOF")


def test_sync_writes_both_lists_one_domain_per_line_in_order(own_env, tmp_path):
    tool, dns_d, dump, log, env = own_env
    _own(dns_d)
    r = _sync(tool, env, tmp_path, "vpn zeta.com\nru shop.ru\nvpn alpha.com\nru bank.ru\n")
    assert r.returncode == 0, r.stderr
    assert (dns_d / VPN_USER).read_text() == _vpn_line("alpha.com") + "\n" + _vpn_line("zeta.com") + "\n"
    assert (dns_d / RU_USER).read_text() == _ru_line("bank.ru") + "\n" + _ru_line("shop.ru") + "\n"
    assert "+ alpha.com (в туннель)" in r.stdout and "+ zeta.com (в туннель)" in r.stdout
    assert "+ bank.ru (напрямую)" in r.stdout and "+ shop.ru (напрямую)" in r.stdout
    assert r.stdout.rstrip().endswith("свои списки: 2 в туннель, 2 напрямую"), r.stdout
    assert "адрес" not in r.stdout and "→" not in r.stdout, f"sync печатает адреса: {r.stdout}"
    nft = _nft(log)
    assert not any("add element inet awg_home lan_vpn4" in ln for ln in nft), \
        f"sync сам резолвит новые «в туннель» — это работа fill: {nft}"
    # слепок sync снимает (после своих удалений), но адресов новых «в туннель»
    # в нём ещё нет — они появятся в наборе и слепке только после fill
    assert "nft list set inet awg_home lan_vpn4" in nft, "sync не переписал слепок"
    assert "10.4.4.4" not in (dump / "lan_vpn4.nft").read_text(), "адрес нового домена в слепке до fill"
    assert _fill_run(tool, env, tmp_path, "alpha.com\nzeta.com\n").returncode == 0
    assert _set(_sets_of(dump), "lan_vpn4") == {"10.4.4.4", "10.5.5.5"}, "fill не наполнил набор"
    snap = (dump / "lan_vpn4.nft").read_text()
    assert "10.4.4.4" in snap and "10.5.5.5" in snap, f"fill не обновил слепок: {snap}"
    assert "nft add element inet awg_home lan_ru4 { 10.1.1.1,10.2.2.2 }" in nft, "«напрямую» наполняется в sync"
    assert _log(log).count("systemctl restart dnsmasq") == 1


def test_sync_of_the_same_lists_in_another_order_does_not_restart(own_env, tmp_path):
    """Кнопка дописывает строку в конец файла, sync пишет по алфавиту, а
    мигрированный ручной слой держит несколько доменов в строке. Сравнение — по
    составу: иначе первая же чужая правка роняла бы кэш DNS всей локальной сети ради
    порядка строк."""
    tool, dns_d, dump, log, env = own_env
    (dns_d / VPN_USER).write_text("nftset=/zeta.com/alpha.com/inet#awg_home#lan_vpn4\n", encoding="utf-8")
    (dns_d / RU_USER).write_text(_ru_line("shop.ru") + "\n" + _ru_line("bank.ru") + "\n", encoding="utf-8")
    before = {f: (dns_d / f).read_text() for f in (VPN_USER, RU_USER)}
    r = _sync(tool, env, tmp_path, "ru bank.ru\nvpn alpha.com\nru shop.ru\nvpn zeta.com\nvpn alpha.com\n")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "свои списки без изменений", r.stdout
    assert _log(log) == "", f"тот же состав — а рестарт или наборы: {_log(log)}"
    assert {f: (dns_d / f).read_text() for f in before} == before, "файлы переписаны без изменений состава"


def test_sync_refuses_the_whole_file_for_one_foreign_line(own_env, tmp_path):
    """Файл собирает агент; строка не того вида — признак поломки или подмены.
    Отказ целиком, rc=2: половина применённого списка хуже прежнего."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, vpn=["news.org"], ru=["shop.ru"])
    before = {f: (dns_d / f).read_text() for f in (VPN_USER, RU_USER)}
    for bad in ("add alpha.com", "vpn alpha.com extra", "vpn Alpha.com", "nftset=/evil.com/inet#awg_home#lan_ru4",
                "vpn -x.com", "ru a..b.com", " vpn alpha.com", "vpn alpha.com ", "del news.org"):
        r = _sync(tool, env, tmp_path, f"vpn alpha.com\n{bad}\nru bank.ru\n")
        assert r.returncode == 2, f"{bad!r}: rc={r.returncode} {r.stdout}{r.stderr}"
        assert {f: (dns_d / f).read_text() for f in before} == before, f"{bad!r}: файлы тронуты"
    assert _log(log) == "", "при отказе что-то применено"


def test_sync_rejects_a_domain_longer_than_253(own_env, tmp_path):
    """Правило канона ограничивает длину 253: sync обязан отвергать то же, что
    кнопка, иначе канон и применённое разойдутся."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d)
    r = _sync(tool, env, tmp_path, f"vpn {_long(254)}\n")
    assert r.returncode == 2, f"домен длиной 254 принят sync: rc={r.returncode} {r.stdout}"
    assert (dns_d / VPN_USER).read_text() == ""


def test_sync_puts_a_domain_listed_both_ways_to_direct(own_env, tmp_path):
    tool, dns_d, dump, log, env = own_env
    _own(dns_d)
    r = _sync(tool, env, tmp_path, "vpn shop.ru\nru shop.ru\nvpn news.org\n")
    assert r.returncode == 0, r.stderr
    assert (dns_d / VPN_USER).read_text() == _vpn_line("news.org") + "\n", "домен остался в обоих списках"
    assert (dns_d / RU_USER).read_text() == _ru_line("shop.ru") + "\n"
    assert "свои списки: 1 в туннель, 1 напрямую" in r.stdout


def test_sync_skips_the_server_host_with_a_note(own_env, tmp_path):
    """Хост сервера AWG в туннель — запереть себя. Остальной список
    применяется, пропуск назван."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d)
    for kind in ("vpn", "ru"):
        r = _sync(tool, env, tmp_path, f"{kind} vps.example.net\nvpn news.org\n", name=f"{kind}.txt")
        assert r.returncode == 0, r.stderr
        assert "vps.example.net: это адрес самого сервера AWG — пропущен" in r.stdout, r.stdout
        assert "vps.example.net" not in (dns_d / VPN_USER).read_text() + (dns_d / RU_USER).read_text()
        assert (dns_d / VPN_USER).read_text() == _vpn_line("news.org") + "\n"


def test_sync_reports_moves_and_removals_and_cleans_the_sets(own_env, tmp_path):
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, vpn=["example.com", "news.org"], ru=["shop.ru", "bank.ru"])
    _fill(_sets_of(dump), "lan_vpn4", ["198.51.100.34", "198.51.100.35", "10.3.3.3"])
    _fill(_sets_of(dump), "lan_ru4", ["10.1.1.1", "10.2.2.2"])
    # example.com ушёл совсем, bank.ru переехал в туннель, news.org и shop.ru — на месте
    r = _sync(tool, env, tmp_path, "vpn news.org\nvpn bank.ru\nru shop.ru\n")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert "− example.com" in lines and "+ bank.ru (в туннель)" in lines, lines
    assert "− bank.ru" not in lines, f"переезд из вида в вид назван удалением: {lines}"
    assert not any("news.org" in ln or "shop.ru" in ln for ln in lines), f"неизменное названо изменением: {lines}"
    assert lines[-1] == "свои списки: 2 в туннель, 1 напрямую"
    nft = _nft(log)
    assert "nft delete element inet awg_home lan_vpn4 { 198.51.100.34 }" in nft
    assert "nft flush set inet awg_home lan_ru4" in nft
    assert "nft add element inet awg_home lan_ru4 { 10.1.1.1 }" in nft
    assert "nft add element inet awg_home lan_ru4 { 10.2.2.2 }" not in nft, "переехавший остался напрямую"
    assert "nft add element inet awg_home lan_vpn4 { 10.2.2.2 }" not in nft, "переехавшего в туннель резолвит sync, а не fill"
    assert _set(_sets_of(dump), "lan_vpn4") == {"10.3.3.3"}, "адреса ушедшего не сняты синхронно"
    assert _set(_sets_of(dump), "lan_ru4") == {"10.1.1.1"}
    # переехавший в туннель — через fill, как его позовёт агент
    assert _fill_run(tool, env, tmp_path, "bank.ru\n").returncode == 0
    assert _set(_sets_of(dump), "lan_vpn4") == {"10.3.3.3", "10.2.2.2"}


def test_sync_that_only_removes_rewrites_the_snapshot(own_env, tmp_path):
    """Канон только убрал домен «в туннель»: новых доменов нет, fill агент не
    зовёт. Слепок lan_vpn4 грузится при старте — не перепиши его sync, снятый
    адрес вернётся в набор с первой же перезагрузкой шлюза (как у кнопки del)."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, vpn=["example.com", "news.org"])
    _fill(_sets_of(dump), "lan_vpn4", ["198.51.100.34", "198.51.100.35", "10.3.3.3"])
    (dump / "lan_vpn4.nft").write_text("elements = { 10.3.3.3, 198.51.100.34, 198.51.100.35 }\n", encoding="utf-8")
    r = _sync(tool, env, tmp_path, "vpn news.org\n")
    assert r.returncode == 0, r.stderr
    assert _set(_sets_of(dump), "lan_vpn4") == {"10.3.3.3"}
    snap = (dump / "lan_vpn4.nft").read_text()
    assert "198.51.100.34" not in snap, f"sync снял адреса из набора, а слепок хранит их: {snap}"


def test_sync_of_an_empty_list_clears_both(own_env, tmp_path):
    """Канон пуст (всё удалили на других шлюзах) — пустой файл применяется,
    а не считается ошибкой."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, vpn=["news.org"], ru=["shop.ru"])
    r = _sync(tool, env, tmp_path, "")
    assert r.returncode == 0, r.stderr
    assert (dns_d / VPN_USER).read_text() == "" and (dns_d / RU_USER).read_text() == ""
    assert "свои списки: 0 в туннель, 0 напрямую" in r.stdout


def test_sync_of_fifty_domains_restarts_dnsmasq_once(own_env, tmp_path):
    """Пакет от сервера — один рестарт dnsmasq, а не рестарт на домен: каждый
    рестарт роняет кэш DNS всей локальной сети."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d)
    _with_src(dns_d, dump)
    text = "".join(f"vpn site{i}.org\n" for i in range(25)) + "".join(f"ru shop{i}.ru\n" for i in range(24)) \
        + "ru shop.ru\n"
    r = _sync(tool, env, tmp_path, text)
    assert r.returncode == 0, r.stderr
    t = _log(log)
    assert t.count("systemctl restart dnsmasq") == 1, t.count("systemctl restart dnsmasq")
    assert t.count("dnsmasq --test") == 1
    assert "свои списки: 25 в туннель, 25 напрямую" in r.stdout
    assert "shop.ru" not in (dns_d / FEED).read_text(), "sync не вычел исключение из фида"


def test_sync_rolls_back_all_three_files_when_dnsmasq_rejects(own_env, tmp_path):
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, vpn=["news.org"])
    _with_src(dns_d, dump)
    before = {f: (dns_d / f).read_text() for f in (VPN_USER, RU_USER, FEED)}
    f = tmp_path / "own.txt"; f.write_text("vpn alpha.com\nru shop.ru\n", encoding="utf-8")
    r = _run(tool, {**env, "DNSMASQ_TEST_RC": "1"}, "sync", str(f))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "+ " not in r.stdout and "свои списки:" not in r.stdout, f"итог про откаченное: {r.stdout}"
    assert {f: (dns_d / f).read_text() for f in before} == before, "откат вернул не все три файла"
    assert not list(dns_d.glob("*.prev.awg"))
    assert "nft" not in _log(log)


def test_sync_without_a_readable_file_is_a_usage_error(own_env, tmp_path):
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, vpn=["news.org"])
    for args in (("sync",), ("sync", str(tmp_path / "missing.txt"))):
        r = _run(tool, env, *args)
        assert r.returncode == 1 and "usage" in r.stdout, (args, r.stdout)
    assert (dns_d / VPN_USER).read_text() == _vpn_line("news.org") + "\n"
    assert _log(log) == ""


# ── копии для отката — вне conf-dir ──────────────────────────────────────────

def test_own_list_copies_are_outside_the_conf_dir_during_the_restart(own_env, tmp_path):
    """И кнопочная правка (del), и sync: при рестарте в conf-dir только
    настоящие файлы, копии — в rollback/, после удачи их нет."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, vpn=["news.org"], ru=["shop.ru"])
    _with_src(dns_d, dump, minus={"shop.ru"})
    snap = _snapshot_restart(env, log)
    assert _run(tool, env, "del", "shop.ru").returncode == 0
    assert _sync(tool, env, tmp_path, "vpn alpha.com\nru bank.ru\n").returncode == 0
    shots = _snapshots(snap)
    assert len(shots) == 2, shots
    for confdir, rb in shots:
        assert _no_copies(confdir), f"при рестарте dnsmasq в conf-dir лежала копия: {confdir}"
        assert {VPN_USER, RU_USER} <= confdir
        assert {f"{VPN_USER}.prev", f"{RU_USER}.prev"} <= rb, f"копий для отката нет в rollback/: {rb}"
    assert f"{FEED}.prev" in shots[0][1], "фид пересобран (исключение ушло), а его копии для отката нет"
    assert not list((dump / "rollback").iterdir()), "копии пережили удачный рестарт"


@pytest.mark.parametrize("fail", ["test", "restart"])
def test_own_lists_are_restored_from_rollback(own_env, tmp_path, fail):
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, vpn=["news.org"], ru=["shop.ru"])
    _with_src(dns_d, dump, minus={"shop.ru"})
    before = {f: (dns_d / f).read_text() for f in (VPN_USER, RU_USER, FEED)}
    snap = _snapshot_restart(env, log, fails=1 if fail == "restart" else 0)
    extra = {"DNSMASQ_TEST_RC": "1"} if fail == "test" else {}
    r = _sync(tool, {**env, **extra}, tmp_path, "vpn alpha.com\n")
    assert r.returncode == 1 and "откатываю" in r.stderr, (r.returncode, r.stderr)
    assert {f: (dns_d / f).read_text() for f in before} == before, "откат из rollback/ вернул не всё"
    assert not list((dump / "rollback").iterdir()), "копии остались после отката"
    if fail == "restart":
        assert all(_no_copies(c) for c, _ in _snapshots(snap)), _snapshots(snap)
