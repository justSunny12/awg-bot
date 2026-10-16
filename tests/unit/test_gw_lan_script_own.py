"""
awg-lan-domain.sh — свои списки против фида и наборов nft: вычитание
исключений из фида, откат, наполнение и чистка lan_ru4/lan_vpn4, слепок.
"""
from __future__ import annotations

from tests.unit import _gw_lan_common as _c
from tests.unit._gw_lan_common import (
    FEED, RU_USER, VPN_USER, _DIG_MAP, _SRC, _bin, _failing_restart, _fake, _feed_from, _fill, _fill_run, _log,
    _nft, _own, _ru_line, _run, _set, _sets_of, _sync, _vpn_line, _with_src,
)

script, domain_env, own_env = _c.script, _c.domain_env, _c.own_env


# ── вычитание исключений из фида ─────────────────────────────────────────────

def test_a_new_exception_leaves_the_feed_at_once_with_one_restart(own_env):
    """Домен из фида перевели «напрямую»: раньше директива фида оставалась до
    смены фидов на сервере, и какая из двух победит, решал порядок чтения
    каталога. Теперь фид пересобирается по исходнику в том же рестарте."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d)
    _with_src(dns_d, dump)
    r = _run(tool, env, "ru", "shop.ru")
    assert r.returncode == 0, r.stderr
    feed = (dns_d / FEED).read_text()
    assert "shop.ru" not in feed, f"исключение не вычтено из фида: {feed}"
    assert _vpn_line("x.com") in feed and _vpn_line("youtube.com/googlevideo.com") in feed, "вычли лишнее"
    assert "<html>" not in feed, "мусор исходника уехал в conf-dir"
    assert feed == _feed_from(_SRC, minus={"shop.ru"}), "фид собран не так, как собрал бы скрипт фидов"
    assert _log(log).count("systemctl restart dnsmasq") == 1, "фид и свой список — один рестарт"
    assert not list(dns_d.glob("*.prev.awg")), "копии отката остались в conf-dir"


def test_an_exception_inside_a_multi_domain_feed_line_cuts_only_itself(own_env):
    tool, dns_d, dump, log, env = own_env
    _own(dns_d)
    _with_src(dns_d, dump)
    assert _run(tool, env, "ru", "googlevideo.com").returncode == 0
    feed = (dns_d / FEED).read_text()
    assert _vpn_line("youtube.com") + "\n" in feed and "googlevideo.com" not in feed, feed


def test_a_removed_exception_returns_to_the_feed(own_env):
    """Убрали «напрямую» — домен снова идёт в туннель по фиду. Раньше исходник
    фида не хранился, и убранное исключение пропадало из фида до смены фидов."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, ru=["shop.ru"])
    _with_src(dns_d, dump, minus={"shop.ru"})
    r = _run(tool, env, "del", "shop.ru")
    assert r.returncode == 0, r.stderr
    assert _vpn_line("shop.ru") + "\n" in (dns_d / FEED).read_text(), "домен не вернулся в фид"
    assert (dns_d / FEED).read_text() == _feed_from(_SRC)


def test_a_removed_exception_returns_to_the_feed_while_others_stay_out(own_env):
    """То же, когда другое исключение остаётся: оно из фида не возвращается."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, ru=["shop.ru", "x.com"])
    _with_src(dns_d, dump, minus={"shop.ru", "x.com"})
    assert _run(tool, env, "del", "shop.ru").returncode == 0
    assert (dns_d / FEED).read_text() == _feed_from(_SRC, minus={"x.com"})


def test_moving_an_exception_to_the_tunnel_returns_it_to_the_feed(own_env):
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, ru=["shop.ru"])
    _with_src(dns_d, dump, minus={"shop.ru"})
    assert _run(tool, env, "add", "shop.ru").returncode == 0
    assert (dns_d / FEED).read_text() == _feed_from(_SRC)
    assert (dns_d / VPN_USER).read_text() == _vpn_line("shop.ru") + "\n"


def test_without_the_feed_source_the_feed_is_left_alone(own_env):
    """Исходника ещё нет (фиды не собирались с этой версии): фид не трогаем —
    его выправит ближайшая сборка; свой список применяется как обычно."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d)
    old = _feed_from(_SRC)
    (dns_d / FEED).write_text(old, encoding="utf-8")
    r = _run(tool, env, "ru", "shop.ru")
    assert r.returncode == 0, r.stderr
    assert (dns_d / FEED).read_text() == old, "фид переписан без исходника"
    assert (dns_d / RU_USER).read_text() == _ru_line("shop.ru") + "\n"


def test_a_tunnel_only_change_does_not_touch_the_feed(own_env):
    tool, dns_d, dump, log, env = own_env
    _own(dns_d)
    _with_src(dns_d, dump)
    (dns_d / FEED).write_text("nftset=/old.org/inet#awg_lan#lan_vpn4\n", encoding="utf-8")
    assert _run(tool, env, "add", "news.org").returncode == 0
    assert (dns_d / FEED).read_text() == "nftset=/old.org/inet#awg_lan#lan_vpn4\n", \
        "«напрямую» не менялось — фид пересобран вхолостую"


def test_a_rejected_config_rolls_back_the_lists_and_the_feed(own_env):
    """dnsmasq --test отверг: оба своих списка и фид возвращаются как были,
    наборы не трогаются, код 1 — локальная сеть не остаётся без DNS."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, vpn=["shop.ru"])
    _with_src(dns_d, dump)
    before = {f: (dns_d / f).read_text() for f in (VPN_USER, RU_USER, FEED)}
    r = _run(tool, {**env, "DNSMASQ_TEST_RC": "1"}, "ru", "shop.ru")
    assert r.returncode == 1, r.stdout + r.stderr
    # эта строка уходит человеку в итог правки и в монитор — «отверг списки»
    assert "dnsmasq --test отверг списки — откатываю" in r.stderr, r.stderr
    assert "добавлен" not in r.stdout, f"человеку сказано «добавлен» про откаченное: {r.stdout}"
    assert {f: (dns_d / f).read_text() for f in before} == before, "откат вернул не всё"
    assert not list(dns_d.glob("*.prev.awg")), "копии отката остались в conf-dir"
    assert "nft" not in _log(log), "наборы тронуты при откаченных списках"
    tests = [ln for ln in _log(log).splitlines() if ln.startswith("dnsmasq --test")]
    assert tests == [f"dnsmasq --test --conf-dir={dns_d},.dpkg-dist,.dpkg-old,.dpkg-new"], tests


def test_a_failed_restart_rolls_back_and_restarts_with_the_old_lists(own_env):
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, vpn=["shop.ru"])
    _with_src(dns_d, dump)
    before = {f: (dns_d / f).read_text() for f in (VPN_USER, RU_USER, FEED)}
    _failing_restart(_bin(env), log)
    r = _run(tool, env, "ru", "shop.ru")
    assert r.returncode == 1, "отказ рестарта выдан за успех"
    assert "не поднялся" in r.stderr and "откатываю" in r.stderr
    assert "добавлен" not in r.stdout, f"человеку сказано «добавлен» про откаченное: {r.stdout}"
    assert {f: (dns_d / f).read_text() for f in before} == before
    assert _log(log).count("systemctl restart dnsmasq") == 2, "после отката демон надо поднять с прежним"
    assert not list(dns_d.glob("*.prev.awg"))
    assert "nft" not in _log(log)


# ── наборы nft ───────────────────────────────────────────────────────────────

def test_leaving_direct_refills_lan_ru4_from_the_remaining_list(own_env):
    """Домен убрали из «напрямую»: правило lan_ru4 стоит выше меток, и без
    чистки набора домен шёл бы мимо туннеля до перезагрузки. Набор маленький и
    наполняется только отсюда — сбрасываем и наполняем оставшимися."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, ru=["shop.ru", "bank.ru"])
    _fill(_sets_of(dump), "lan_ru4", ["10.1.1.1", "10.2.2.2"])
    r = _run(tool, env, "del", "bank.ru")
    assert r.returncode == 0, r.stderr
    assert _set(_sets_of(dump), "lan_ru4") == {"10.1.1.1"}, "адрес убранного домена остался в lan_ru4"
    assert r.stdout.strip() == "<code>bank.ru</code>: убран", r.stdout
    nft = _nft(log)
    assert "nft flush set inet awg_lan lan_ru4" in nft, nft
    assert "nft add element inet awg_lan lan_ru4 { 10.1.1.1 }" in nft, "оставшийся домен не вернулся в набор"
    assert not any("10.2.2.2" in ln and "add" in ln for ln in nft), "убранный домен снова в наборе"
    assert nft.index("nft flush set inet awg_lan lan_ru4") < nft.index(
        "nft add element inet awg_lan lan_ru4 { 10.1.1.1 }"), "наполнение до сброса стёрто сбросом"
    text = _log(log)
    assert text.index("systemctl restart dnsmasq") < text.index("nft flush"), "dig до рестарта — по старым спискам"


def test_moving_from_direct_to_tunnel_refills_both_sets(own_env):
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, ru=["shop.ru", "bank.ru"])
    _fill(_sets_of(dump), "lan_ru4", ["10.1.1.1", "10.2.2.2"])
    r = _run(tool, env, "add", "bank.ru")
    assert r.returncode == 0, r.stderr
    assert _set(_sets_of(dump), "lan_ru4") == {"10.1.1.1"}, "переведённый в туннель остался напрямую"
    assert _set(_sets_of(dump), "lan_vpn4") == {"10.2.2.2"}
    nft = _nft(log)
    assert "nft flush set inet awg_lan lan_ru4" in nft
    assert "nft add element inet awg_lan lan_ru4 { 10.1.1.1 }" in nft
    assert "nft add element inet awg_lan lan_ru4 { 10.2.2.2 }" not in nft
    assert "nft add element inet awg_lan lan_vpn4 { 10.2.2.2 }" in nft, "домен не лёг в туннельный набор"
    assert "1 адрес(а) в наборе lan_vpn4" in r.stdout


def test_a_new_direct_domain_lands_in_lan_ru4_once(own_env):
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, ru=["shop.ru"])
    r = _run(tool, env, "ru", "bank.ru")
    assert r.returncode == 0, r.stderr
    nft = _nft(log)
    # «напрямую» изменилось — набор сброшен и наполнен заново одним вызовом со всеми адресами
    adds = [ln for ln in nft if "add element inet awg_lan lan_ru4" in ln]
    assert adds == ["nft add element inet awg_lan lan_ru4 { 10.1.1.1,10.2.2.2 }"], nft
    assert _set(_sets_of(dump), "lan_ru4") == {"10.1.1.1", "10.2.2.2"}
    assert "bank.ru → 1 адрес(а) в наборе lan_ru4" in r.stdout


def test_deleting_a_tunnel_domain_takes_its_addresses_out_of_lan_vpn4(own_env):
    """Удаление, донесённое до всех шлюзов, должно действовать: адреса,
    уже попавшие в lan_vpn4, иначе жили бы там и в слепке навсегда."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, vpn=["example.com", "news.org"])
    _fill(_sets_of(dump), "lan_vpn4", ["198.51.100.34", "198.51.100.35", "10.3.3.3"])
    r = _run(tool, env, "del", "example.com")
    assert r.returncode == 0, r.stderr
    assert _set(_sets_of(dump), "lan_vpn4") == {"10.3.3.3"}, "адреса убранного домена остались в lan_vpn4"
    nft = _nft(log)
    for ip in _DIG_MAP["example.com"]:
        assert f"nft delete element inet awg_lan lan_vpn4 {{ {ip} }}" in nft, nft
    assert not any("10.3.3.3" in ln for ln in nft), "тронут адрес оставшегося домена"
    assert not any("lan_ru4" in ln for ln in nft), "«напрямую» не менялось — набор сброшен зря"
    assert "−" not in r.stdout, "в add/ru/del адреса снимаются молча"


def test_a_failing_element_delete_does_not_break_the_run(own_env):
    """Адрес слился в интервал или принадлежит домену фида — nft отказывает.
    Это не повод падать: списки уже применены, остальные адреса вынимаются."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, vpn=["example.com"])
    _fake(_bin(env), "nft", f'echo "nft $*" >> {log}\n[ "$1" = delete ] && {{ echo "Error: interval" >&2; exit 1; }}\nexit 0\n')
    r = _run(tool, env, "del", "example.com")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Error" not in r.stderr, "ошибка nft вылезла человеку"
    nft = _nft(log)
    assert sum("delete element" in ln for ln in nft) == 2, "после первой ошибки второй адрес не вынимался"
    assert (dns_d / VPN_USER).read_text() == ""


def test_the_lan_vpn4_snapshot_forgets_removed_addresses(own_env):
    """Слепок lan_vpn4 грузится при старте: не перепиши его после удаления —
    снятый адрес вернётся в набор с первой же перезагрузкой шлюза."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, vpn=["example.com", "news.org"])
    _fill(_sets_of(dump), "lan_vpn4", ["198.51.100.34", "198.51.100.35", "10.3.3.3"])
    (dump / "lan_vpn4.nft").write_text("elements = { 10.3.3.3, 198.51.100.34, 198.51.100.35 }\n", encoding="utf-8")
    assert _run(tool, env, "del", "example.com").returncode == 0
    snap = (dump / "lan_vpn4.nft").read_text()
    assert "10.3.3.3" in snap and "198.51.100.34" not in snap, f"слепок хранит снятый адрес: {snap}"
    nft = _nft(log)
    assert nft.index("nft list set inet awg_lan lan_vpn4") > max(
        i for i, ln in enumerate(nft) if "delete element" in ln), "слепок снят до удаления"
    assert not (dump / "lan_vpn4.nft.tmp").exists()


def test_a_new_tunnel_domain_gets_into_the_snapshot(own_env, tmp_path):
    tool, dns_d, dump, log, env = own_env
    _own(dns_d)
    assert _run(tool, env, "add", "news.org").returncode == 0
    assert "10.3.3.3" in (dump / "lan_vpn4.nft").read_text()
    # sync адреса нового домена не резолвит — это делает fill, который агент
    # зовёт следом фоном; слепок обновляет тот же fill
    assert _sync(tool, env, tmp_path, "vpn news.org\nvpn alpha.com\n").returncode == 0
    assert "10.4.4.4" not in (dump / "lan_vpn4.nft").read_text(), "sync сам наполнил набор новым доменом"
    assert _fill_run(tool, env, tmp_path, "alpha.com\n").returncode == 0
    assert "10.4.4.4" in (dump / "lan_vpn4.nft").read_text(), "fill не обновил слепок"


def test_a_failed_listing_keeps_the_previous_snapshot(own_env):
    """nft list не ответил — прежний слепок лучше пустого: пустой при старте
    оставил бы набор без адресов до первых запросов."""
    tool, dns_d, dump, log, env = own_env
    _own(dns_d)
    (dump / "lan_vpn4.nft").write_text("elements = { 10.9.9.9 }\n", encoding="utf-8")
    _fake(_bin(env), "nft", f'echo "nft $*" >> {log}\n[ "$1" = list ] && exit 1\nexit 0\n')
    r = _run(tool, env, "add", "news.org")
    assert r.returncode == 0, r.stderr
    assert (dump / "lan_vpn4.nft").read_text() == "elements = { 10.9.9.9 }\n"
    assert not (dump / "lan_vpn4.nft.tmp").exists()


def test_a_repeated_add_changes_nothing(own_env):
    tool, dns_d, dump, log, env = own_env
    _own(dns_d, vpn=["news.org"])
    r = _run(tool, env, "add", "news.org")
    assert r.returncode == 0 and "<code>news.org</code>: уже в списке" in r.stdout
    assert _log(log) == "", f"повтор без изменений — а рестарт или наборы тронуты: {_log(log)}"
