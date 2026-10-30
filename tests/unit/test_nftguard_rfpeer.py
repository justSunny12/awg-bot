"""Файервол хоста и РФ-доступ через второй сервер (infra/nftguard: collect.rfpeer,
render._rfpeer_input/_rfpeer_forward/_rfpeer_postrouting).

Состояние транзита обвязка берёт из файла согласия бота (`reserve/rfpeer.json`
в каталоге данных) и адресов из `awgpair.conf` — без БД, как `awg-bot firewall
apply` из CLI. Здесь каждый тест собирает таблицу через `build_spec` без БД.

Транзитный («Beta», файл: offer) пропускает с адреса второй стороны в линке
пары только в линки своих слотов и не в частные адреса, обратно — только
ответы, и маскарадит на адрес линка слота. Просящий («Alpha», файл: slot)
пускает своих пользователей из интерфейсов AWG в линк пары (не в частные),
принимает оттуда только ответы своих соединений и на сам хост с линка пары —
только к своему адресу в /30, раньше `ct state established`.

Цена ошибки: правило транзита после запрета линка пары — путь закрыт, и РФ-доступ
пользователей второй стороны не идёт, хотя согласие есть; без запрета частных
адресов — второй сервер ходит через линк пары в локальные сети шлюзов и в
служебные /30; без `ct reply ip daddr` у просящего — пакет второй стороны под
чужое соединение (ушедшее через внешний интерфейс) проходит к пользователям;
вход на хост раньше `established` не закрыт — при расширенных `AllowedIPs`
второй сервер подсовывает пакеты в любые соединения хоста; CLI без файла —
перерисовка из терминала рвёт транзит до ближайшего такта бота.
"""
from __future__ import annotations

import pytest

from awgbot.core import config, settings
from awgbot.infra import nftguard
from awgbot.infra.nftguard import collect
from awgbot.infra.routing import rfpeer as rfp

pytestmark = pytest.mark.unit

# этот хост — 10.99.98.2, вторая сторона — 10.99.98.1 (как у пригласившего)
PAIR = """# Линк пары серверов AWG (резерв). Сгенерировано reserve-pair-setup.sh.
[Interface]
Address = 10.99.98.2/30
ListenPort = 47101
PrivateKey = DUMMY-PRIV=
Jc = 5

[Peer]
PublicKey = DUMMY-PUB=
PresharedKey = DUMMY-PSK=
AllowedIPs = 10.99.98.1/32
PersistentKeepalive = 25-35
"""
LINK = "[Interface]\nAddress = 10.99.99.1/30\nListenPort = 443\nTable = off\n\n[Peer]\nAllowedIPs = 0.0.0.0/0\n"
CLIENTS = "[Interface]\nAddress = 10.9.1.1/24\nListenPort = 42755\n"

PRIV = "{ 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, 169.254.0.0/16 }"
Q = '{ "awgpair" }'
# эталоны правил (порядок внутри групп — как в концепции пути пакета)
BETA_FWD = [f'iifname {Q} ip saddr 10.99.98.1 oifname {{ "awglink" }} ip daddr != {PRIV} accept',
            f"oifname {Q} ip daddr 10.99.98.1 ct direction reply accept"]
ALPHA_FWD = [f'iifname {{ "awg1" }} oifname {Q} ip saddr @tunnel_nets4 ip daddr != {PRIV} accept',
             f"iifname {Q} ct direction reply ct reply ip daddr 10.99.98.2 accept"]
PAIR_DROP = [f"iifname {Q} drop", f"oifname {Q} drop"]
ALPHA_IN = f"iifname {Q} ip daddr != 10.99.98.2 drop"
BETA_MASQ = 'oifname { "awglink" } ip saddr 10.99.98.1 masquerade'
ALPHA_MASQ = f"oifname {Q} ip saddr @tunnel_nets4 masquerade"


@pytest.fixture()
def host(tmp_path, monkeypatch):
    awg = tmp_path / "awg"
    awg.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(config, "AWG_DIR", str(awg))
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "")
    monkeypatch.setattr(config, "SUBNET_PREFIX", "10.9.1")
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "")
    monkeypatch.setattr(config, "SERVER_PORT", 42755)
    monkeypatch.setattr(nftguard, "_tunnel_ifs", lambda: ["awg1"])
    monkeypatch.setattr(nftguard, "wan_ifaces", lambda: [])
    (awg / "awg1.conf").write_text(CLIENTS, encoding="utf-8")
    (awg / "awglink.conf").write_text(LINK, encoding="utf-8")
    (awg / "awgpair.conf").write_text(PAIR, encoding="utf-8")
    return awg


def _conf(monkeypatch, filtering: bool = True):
    vals = {"app.firewall.enabled": filtering, "app.firewall.ssh_allow": [], "app.firewall.open_tcp": [8080],
            "app.firewall.open_udp": [5353], "app.network.ssh_port": 2222, "app.reserve.pair_channel_port": 8788}
    monkeypatch.setattr(settings, "get", lambda k, d=None: vals.get(k, d))
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(vals.get(k, d)))
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: bool(vals.get(k, d)))


def _chain(text: str, name: str) -> list[str]:
    body = text.split(f"chain {name} {{", 1)[1].split("\n    }", 1)[0]
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


def _text(monkeypatch, *, offer: bool | None, slot: bool = False, filtering: bool = True) -> str:
    """Таблица так, как её нарисует `awg-bot firewall apply`: без БД, по файлу согласия."""
    _conf(monkeypatch, filtering)
    if offer is None:
        rfp.remove()
    else:
        rfp.write(offer, slot, "2026-11-02T14:05:00")
    return nftguard.render(nftguard.build_spec(["10.9.1.2"]))


def _rfp_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines()
            if "ct direction reply" in ln or "100.64.0.0/10, 169.254.0.0/16" in ln
            or "ip daddr != 10.99.98.2 drop" in ln or ln.strip() in (BETA_MASQ, ALPHA_MASQ)]


# ── сбор: файл согласия и конфиг линка пары ─────────────────────────────────

def test_without_the_file_there_are_no_transit_fields_and_no_rules(host, monkeypatch):
    assert collect.rfpeer() == {}
    for filtering in (True, False):
        text = _text(monkeypatch, offer=None, filtering=filtering)
        assert _rfp_lines(text) == [], f"правила транзита без согласия (фильтр={filtering}): {_rfp_lines(text)}"


def test_a_file_with_nothing_on_draws_exactly_the_table_without_the_file(host, monkeypatch):
    """Сторона без согласия и без слота — таблица та же, что без файла: правила
    транзита не появляются от одного факта записи файла."""
    for filtering in (True, False):
        assert _text(monkeypatch, offer=False, slot=False, filtering=filtering) == \
            _text(monkeypatch, offer=None, filtering=filtering), f"фильтр={filtering}"


@pytest.mark.parametrize("offer, slot, want", [
    (True, False, {"rfp_offer4": "10.99.98.1"}),
    (False, True, {"rfp_self4": "10.99.98.2"}),
    (True, True, {"rfp_offer4": "10.99.98.1", "rfp_self4": "10.99.98.2"}),
    (False, False, {}),
])
def test_collect_takes_the_addresses_from_the_pair_config(host, offer, slot, want):
    """Транзитному — адрес второй стороны (AllowedIPs конфига), просящему — свой (Address)."""
    rfp.write(offer, slot)
    assert collect.rfpeer() == want


def test_without_the_pair_config_the_file_alone_gives_nothing(host):
    """Пара снята, файл остался (сбой) — адресов нет, и правила не рисуются вслепую."""
    (host / "awgpair.conf").unlink()
    rfp.write(True, True)
    assert collect.rfpeer() == {}


def test_a_broken_file_is_no_consent(host, monkeypatch):
    rfp.rfpeer_path().parent.mkdir(parents=True, exist_ok=True)
    rfp.rfpeer_path().write_text("{не json", encoding="utf-8")
    assert collect.rfpeer() == {}
    rfp.rfpeer_path().write_text('["offer"]', encoding="utf-8")
    assert collect.rfpeer() == {}, "не словарь в файле — согласие"


# ── «Beta», транзитный ─────────────────────────────────────────────────────

@pytest.mark.parametrize("filtering", [True, False], ids=["filter", "nat-only"])
def test_the_transit_side_forwards_from_the_peer_address_only_into_slot_links_before_the_pair_drop(
        host, monkeypatch, filtering):
    fwd = _chain(_text(monkeypatch, offer=True, filtering=filtering), "forward")
    i = fwd.index(PAIR_DROP[0])
    assert fwd[i - 2:i + 2] == BETA_FWD + PAIR_DROP, \
        f"правила транзита не сразу перед запретом линка пары (фильтр={filtering}):\n" + "\n".join(fwd)
    assert not [ln for ln in fwd if ln in ALPHA_FWD], "у транзитного без слота — правила просящего"


@pytest.mark.parametrize("filtering", [True, False], ids=["filter", "nat-only"])
def test_the_transit_side_masquerades_the_peer_address_onto_the_slot_links(host, monkeypatch, filtering):
    post = _chain(_text(monkeypatch, offer=True, filtering=filtering), "postrouting")
    assert BETA_MASQ in post, "без маскарада шлюз не знает адреса линка пары и не вернёт ответ:\n" + "\n".join(post)
    assert ALPHA_MASQ not in post


@pytest.mark.parametrize("filtering", [True, False], ids=["filter", "nat-only"])
def test_the_transit_side_keeps_the_host_input_from_the_pair_link_as_before(host, monkeypatch, filtering):
    """Вход на хост с линка пары у транзитного — прежний (только канал пары):
    согласие не открывает второму серверу ничего на самом хосте."""
    with_offer = _chain(_text(monkeypatch, offer=True, filtering=filtering), "input")
    without = _chain(_text(monkeypatch, offer=None, filtering=filtering), "input")
    assert with_offer == without


def test_the_transit_side_without_slot_links_draws_no_transit_rules(host, monkeypatch):
    """Согласие в файле, а линков слотов нет (слот сняли раньше такта) — пересылать
    некуда: ни «accept» в никуда, ни маскарада."""
    (host / "awglink.conf").unlink()
    text = _text(monkeypatch, offer=True)
    assert _rfp_lines(text) == [], _rfp_lines(text)


# ── «Alpha», просящий ──────────────────────────────────────────────────────

@pytest.mark.parametrize("filtering", [True, False], ids=["filter", "nat-only"])
def test_the_asking_side_drops_input_from_the_pair_link_to_foreign_addresses_before_established(
        host, monkeypatch, filtering):
    inp = _chain(_text(monkeypatch, offer=False, slot=True, filtering=filtering), "input")
    assert ALPHA_IN in inp, f"нет запрета входа с линка пары (фильтр={filtering})"
    assert inp.index(ALPHA_IN) < inp.index("ct state established,related accept"), \
        "запрет — после established: пакет под чужое соединение хоста пройдёт\n" + "\n".join(inp)
    pair_accept = next(ln for ln in inp if "tcp dport 8788 accept" in ln)
    assert inp.index(ALPHA_IN) < inp.index(pair_accept), "канал пары ниже запрета — он к своему адресу, не задет"


@pytest.mark.parametrize("filtering", [True, False], ids=["filter", "nat-only"])
def test_the_asking_side_lets_users_into_the_pair_link_and_only_replies_back(host, monkeypatch, filtering):
    fwd = _chain(_text(monkeypatch, offer=False, slot=True, filtering=filtering), "forward")
    i = fwd.index(PAIR_DROP[0])
    assert fwd[i - 2:i + 2] == ALPHA_FWD + PAIR_DROP, "\n".join(fwd)
    assert not [ln for ln in fwd if ln in BETA_FWD], "у просящего без согласия — правила транзитного"


@pytest.mark.parametrize("filtering", [True, False], ids=["filter", "nat-only"])
def test_the_asking_side_masquerades_users_into_the_pair_link(host, monkeypatch, filtering):
    post = _chain(_text(monkeypatch, offer=False, slot=True, filtering=filtering), "postrouting")
    assert ALPHA_MASQ in post and BETA_MASQ not in post, "\n".join(post)


# ── обе роли сразу ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("filtering", [True, False], ids=["filter", "nat-only"])
def test_both_roles_at_once_draw_all_rules_without_conflict(host, monkeypatch, filtering):
    """Оба сервера дают друг другу шлюзы и у обоих стоит слот: четыре правила
    пересылки — все раньше запрета линка пары, оба маскарада, запрет входа."""
    text = _text(monkeypatch, offer=True, slot=True, filtering=filtering)
    fwd = _chain(text, "forward")
    i = fwd.index(PAIR_DROP[0])
    assert fwd[i - 4:i] == BETA_FWD + ALPHA_FWD, "\n".join(fwd)
    post = _chain(text, "postrouting")
    assert BETA_MASQ in post and ALPHA_MASQ in post
    inp = _chain(text, "input")
    assert inp.index(ALPHA_IN) < inp.index("ct state established,related accept")


def test_the_transit_rules_never_allow_private_or_service_destinations(host, monkeypatch):
    """Личный домен, разрешившийся в адрес линка пары, сеть шлюза, CGNAT и
    link-local — мимо транзита в обе стороны."""
    for ln in _rfp_lines(_text(monkeypatch, offer=True, slot=True)):
        if ln.endswith(" accept") and "ct direction reply" not in ln:
            assert f"ip daddr != {PRIV}" in ln, ln
