"""install/reserve-gw-setup.sh после ревью сети: адрес стороны зоны (первый
AllowedIPs пира) маскарадится наружу цепочкой `rsv_post`; ответы самого шлюза
на соединения с меткой линка получают марку маршрута в `rsv_output` (хук
route, output); при чужой политике DROP в iptables FORWARD линку резерва
открывается транзит (`--up`/`--reassert`) правилами с меткой
`-m comment --comment awg-bot` (прежние без метки на awgrsv заменяются), а
`--down` снимает помеченные и прежние на awgrsv, не трогая чужого.

Обвязка — `_Gw` из test_reserve_gw_script (sh, подставные nft/ip/awg/systemctl
с журналом, программа `nft -f -` целиком); `iptables` — подставной, правила
FORWARD — файл-модель. Таблицу с новыми цепочками на стенде проверяет `nft -c`;
здесь — её текст.

Цена ошибки: без маскарада адреса зоны — зонды живости бота основы на резерве
через шлюз наружу уходят с адресом /30 линка и не возвращаются, бот основы
считает шлюз мёртвым; без марки ответов самого шлюза — ответы на соединения
с линка резерва (DNS, канал) уходят маршрутом по умолчанию и теряются; без
транзита в FORWARD с политикой DROP (Docker, OMV-плагины) — РФ-доступ
пользователей основы у резерва мёртв, хотя набор открыт; транзит, оставшийся после
`--down`, — дыра в чужом файерволе навсегда; повторный `--reassert`,
дописывающий правила, — FORWARD пухнет каждые несколько минут; транзит без
метки — откат и проверки обвязки не отличают его от правила чужого файервола.
"""
from __future__ import annotations

import pytest

from tests.conftest import fake_cmd
from tests.unit.test_reserve_gw_script import CONF, _chain, _Gw

pytestmark = pytest.mark.unit

TAGGED = ["-i awgrsv -m comment --comment awg-bot -j ACCEPT", "-o awgrsv -m comment --comment awg-bot -j ACCEPT"]
ZONE_CONF = CONF.replace("AllowedIPs = 10.99.97.1/32\n", "AllowedIPs = 10.99.97.1/32, 198.51.100.0/24\n")


@pytest.fixture()
def gw(tmp_path):
    return _Gw(tmp_path)


def test_replies_of_the_gateway_itself_get_the_route_mark(gw):
    r = gw.up_with(ZONE_CONF)
    assert r.returncode == 0, r.stdout + r.stderr
    out = _chain(gw.prog(), "rsv_output")
    assert out == ["type route hook output priority mangle; policy accept;",
                   "ct mark 0x52 ct direction reply meta mark set 0x52"], out


def test_the_zone_address_is_masqueraded_out_of_every_other_interface(gw):
    gw.up_with(ZONE_CONF)
    post = _chain(gw.prog(), "rsv_post")
    assert post == ["type nat hook postrouting priority srcnat; policy accept;",
                    'oifname != "awgrsv" ip saddr 10.99.97.1 masquerade'], post
    assert "198.51.100.0" not in "\n".join(post), "маскарад подсети пользователей основы — у шлюза он свой, не этот"


def test_the_zone_address_is_the_first_allowed_ip_even_with_one_entry(gw):
    gw.up_with(CONF)
    assert 'oifname != "awgrsv" ip saddr 10.99.97.1 masquerade' in _chain(gw.prog(), "rsv_post")


def test_without_allowed_ips_there_is_no_masquerade_chain(gw):
    r = gw.up_with(CONF.replace("AllowedIPs = 10.99.97.1/32\n", ""))
    assert r.returncode == 0, r.stdout + r.stderr
    prog = gw.prog()
    assert "chain rsv_output" in prog, "марка ответов не зависит от адреса зоны"
    assert "chain rsv_post" not in prog and "ip saddr  masquerade" not in prog, \
        "маскарад пустого адреса — nft отвергнет таблицу"


# ── iptables FORWARD с чужой политикой DROP ─────────────────────────────────

def _iptables(gw: _Gw, policy: str) -> None:
    """iptables-модель: -S печатает политику и правила из файла; -C ищет правило;
    -I/-D добавляют/снимают строку."""
    rules = gw.tmp / "fwd.rules"
    rules.write_text("", encoding="utf-8")
    fake_cmd(gw.bin / "iptables",
             f'echo "iptables $*" >> {gw.log}\n'
             '[ "$1" = "-w" ] && shift\n'
             f'case "$1" in\n'
             f'  -S) echo "-P FORWARD {policy}"; sed "s/^/-A FORWARD /" {rules}; exit 0 ;;\n'
             f'  -C) shift 2; grep -qxF -- "$*" {rules}; exit $? ;;\n'
             f'  -I) shift 3; echo "$*" >> {rules}; exit 0 ;;\n'
             f'  -D) shift 2; grep -vxF -- "$*" {rules} > {rules}.n; mv {rules}.n {rules}; exit 0 ;;\n'
             'esac\nexit 0\n')


def _fwd(gw: _Gw) -> list[str]:
    return (gw.tmp / "fwd.rules").read_text(encoding="utf-8").splitlines()


def test_a_foreign_drop_policy_gets_transit_for_the_reserve_link_both_ways(gw):
    _iptables(gw, "DROP")
    r = gw.up_with(ZONE_CONF)
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(_fwd(gw)) == TAGGED, f"транзит без метки awg-bot: {_fwd(gw)}"
    assert any(c.startswith("iptables -w -I FORWARD 1 ") for c in gw.calls()), "транзит не в начало цепочки"


def test_a_repeated_reassert_adds_no_second_copy(gw):
    _iptables(gw, "DROP")
    gw.up_with(ZONE_CONF)
    r = gw.run("--reassert")
    assert r.returncode == 0, r.stdout + r.stderr
    r = gw.run("--reassert")
    assert sorted(_fwd(gw)) == TAGGED, f"FORWARD пухнет: {_fwd(gw)}"


def test_an_accept_policy_is_left_alone(gw):
    _iptables(gw, "ACCEPT")
    gw.up_with(ZONE_CONF)
    assert _fwd(gw) == [] and not [c for c in gw.calls() if " -I " in c], "в чужой FORWARD без нужды дописано"


def test_down_takes_the_transit_away_however_many_copies_there_are(gw):
    _iptables(gw, "DROP")
    gw.up_with(ZONE_CONF)
    (gw.tmp / "fwd.rules").write_text("\n".join(TAGGED + [TAGGED[0], "-i awgrsv -j ACCEPT", "-o awgrsv -j ACCEPT",
                                                          "-i eth0 -j ACCEPT",
                                                          "-i awglink -m comment --comment awg-bot -j ACCEPT"]) + "\n",
                                     encoding="utf-8")
    r = gw.run("--down")
    assert r.returncode == 0 and "down: ok" in r.stdout, r.stdout + r.stderr
    assert _fwd(gw) == ["-i eth0 -j ACCEPT", "-i awglink -m comment --comment awg-bot -j ACCEPT"], \
        f"после --down в чужом FORWARD осталось наше (или снято правило обвязки шлюза): {_fwd(gw)}"


def test_a_transit_of_the_previous_release_without_the_tag_is_replaced_by_the_tagged_one(gw):
    _iptables(gw, "DROP")
    (gw.tmp / "fwd.rules").write_text("-i awgrsv -j ACCEPT\n-o awgrsv -j ACCEPT\n-i eth0 -j ACCEPT\n", encoding="utf-8")
    r = gw.up_with(ZONE_CONF)
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(_fwd(gw)) == sorted(TAGGED + ["-i eth0 -j ACCEPT"]), f"правило без метки не заменено: {_fwd(gw)}"


def test_without_iptables_up_and_down_go_on(gw):
    r = gw.up_with(ZONE_CONF)
    assert r.returncode == 0, r.stdout + r.stderr
    assert gw.run("--down").returncode == 0
    assert not [c for c in gw.calls() if c.startswith("iptables")]
