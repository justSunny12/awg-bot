"""Файервол хоста резерва для РФ-доступа клиентов основы и обучения пула
Telegram (infra/nftguard): veth РФ-трафика зоны `awggr-h` — пересылка
только в линки моих слотов с маскарадом на адрес линка, всё прочее из него
— drop; в serving — динамический набор `tg_seen4` и счёт SYN на порт
вебхука с адресов вне пула.

Сторож снимка таблицы: из пространства хоста резерва к сторонам линков
шлюзов основы (они живут только в зоне) хода нет — в таблице нет ни одного
правила, которое пускало бы трафик с veth зоны куда-то, кроме линков слотов
резерва, и нет правила, которое пускало бы клиентов резерва в veth зоны.

Цена ошибки: пересылка из `awggr-h` без ограничения по выходу — клиенты
основы через хост резерва идут в его локальные интерфейсы и клиентов
резерва; без маскарада — шлюз резерва не узнаёт источник (`tunnel_nets4`
шлюза — подсеть клиентов резерва и /30 линка) и не отвечает; правила в
idle — дверь без зоны; без счёта SYN — пул Telegram никогда не учится, а с
счётом в обход пула — DNAT по-прежнему только с пула.
"""
from __future__ import annotations

import json

import pytest

from awgbot.core import config
from awgbot.infra import nftguard, rsvzone

pytestmark = pytest.mark.unit

LINKS = ["awglink", "awglink2"]


def _spec(**kw) -> nftguard.GuardSpec:
    base = dict(ssh_port=2222, ssh_open=True, tunnel_nets4=["10.8.1.0/24"], tunnel_ifs=["awg0"], udp_ports=[22222],
                link_ifs=list(LINKS))
    base.update(kw)
    return nftguard.GuardSpec(**base)


RF = dict(guest_phase="serving", guest_udp_port=45877, guest_veth_if="awggv-h", guest_zone4="169.254.77.2",
          guest_zone_net4="169.254.77.0/30", guest_webhook_port=443, tg_pool4=list(nftguard.DEFAULT_TG_POOL),
          guest_rf_if="awggr-h", guest_rf_zone4="169.254.77.6")


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines()]


def _chain(text: str, name: str) -> list[str]:
    """Строки цепочки name (первой с таким именем)."""
    out, inside = [], False
    for ln in _lines(text):
        if ln.startswith(f"chain {name} "):
            inside = True
            continue
        if inside and ln == "}":
            break
        if inside:
            out.append(ln)
    return out


RFQ = '{ "awggr-h" }'
LINKQ = '{ "awglink", "awglink2" }'


# ── пересылка из veth РФ-трафика ────────────────────────────────────────────

@pytest.mark.parametrize("filtering", [True, False], ids=["filter", "nat-only"])
@pytest.mark.parametrize("phase", ["taking", "serving"])
def test_rf_traffic_of_the_zone_goes_only_into_my_slot_links(filtering, phase):
    text = nftguard.render(_spec(filter=filtering, **{**RF, "guest_phase": phase}))
    fwd = _chain(text, "forward")
    rf = [ln for ln in fwd if "awggr-h" in ln]
    assert rf == [f"iifname {RFQ} oifname {LINKQ} accept", f"iifname {RFQ} drop", f"oifname {RFQ} drop"], \
        f"из veth РФ-трафика есть ход не только в линки слотов (или в него — с хоста): {rf}"


@pytest.mark.parametrize("filtering", [True, False], ids=["filter", "nat-only"])
def test_rf_traffic_is_masqueraded_onto_the_link_address_only_from_the_zone_side(filtering):
    text = nftguard.render(_spec(filter=filtering, **RF))
    post = _chain(text, "postrouting")
    assert f"oifname {LINKQ} ip saddr 169.254.77.6 masquerade" in post, \
        "без маскарада шлюз резерва не узнает источник и не ответит"
    assert not [ln for ln in post if "169.254.77.6" in ln and LINKQ not in ln], "маскарад адреса зоны не в линки"


def test_without_slot_links_rf_traffic_is_dropped_and_not_masqueraded():
    text = nftguard.render(_spec(link_ifs=[], **RF))
    fwd = _chain(text, "forward")
    assert [ln for ln in fwd if "awggr-h" in ln] == [f"iifname {RFQ} drop", f"oifname {RFQ} drop"]
    assert "169.254.77.6" not in text, "маскарад в линки, которых нет"


@pytest.mark.parametrize("filtering", [True, False], ids=["filter", "nat-only"])
def test_no_rf_rules_in_idle_relay_or_without_the_veth(filtering):
    for over in (dict(guest_phase="idle"), dict(guest_phase="relay", relay_to="203.0.113.5:45877",
                                                relay_udp_port=45877), dict(guest_rf_if="")):
        text = nftguard.render(_spec(filter=filtering, **{**RF, **over}))
        assert "awggr-h" not in text and "169.254.77.6" not in text, f"{over}: правила РФ-трафика без зоны"


def test_the_rf_drop_stands_before_the_general_accepts_of_the_forward():
    """Порядок: правила veth стоят раньше общих «из туннеля — пропустить»,
    иначе пакет из зоны с адресом из подсети клиентов прошёл бы ими."""
    fwd = _chain(nftguard.render(_spec(**RF)), "forward")
    i_drop = fwd.index(f"iifname {RFQ} drop")
    general = [i for i, ln in enumerate(fwd) if ln.endswith("accept") and "awggr-h" not in ln
               and "ct state" not in ln and "awggv-h" not in ln]
    assert general and all(i_drop < i for i in general), fwd


def test_reserve_clients_have_no_way_into_the_rf_veth_of_the_zone():
    """Сторож снимка: свои клиенты резерва в зону не ходят (стороны линков
    шлюзов основы — в зоне). Ответы зоне — по conntrack (ct state выше),
    новое соединение из туннеля в `awggr-h` — drop, как у `awggv-h`."""
    fwd = _chain(nftguard.render(_spec(**RF)), "forward")
    i_tunnel = fwd.index("ip saddr @tunnel_nets4 accept")
    oif_drop = [i for i, ln in enumerate(fwd) if ln == f"oifname {RFQ} drop"]
    assert oif_drop and oif_drop[0] < i_tunnel, \
        f"клиенты резерва (tunnel_nets4) проходят в veth РФ-трафика зоны: {fwd}"


def test_no_rule_sends_anything_from_the_host_to_the_zone_link_sides():
    """Стороны линков шлюзов основы (`awglink*` зоны) хосту не видны: имён
    из reserve/links/ в таблице хоста нет, и ни одно правило не ведёт в них."""
    text = nftguard.render(_spec(**RF))
    assert "awglink*" not in text, "в таблице хоста правило на стороны линков зоны"


# ── обучение пула: SYN вне пула ─────────────────────────────────────────────

SYN = ("tcp dport 443 ip saddr != @tg_pool4 tcp flags & (syn|ack) == syn limit rate 20/second "
       "update @tg_seen4 { ip saddr counter } drop")


@pytest.mark.parametrize("filtering", [True, False], ids=["filter", "nat-only"])
def test_serving_counts_syn_to_the_webhook_port_from_outside_the_pool(filtering):
    text = nftguard.render(_spec(filter=filtering, **RF))
    assert SYN in _chain(text, "input"), text
    assert "set tg_seen4 {" in text and "flags dynamic,timeout" in text and "timeout 10m" in text
    dnat = [ln for ln in _lines(text) if "dnat ip to 169.254.77.2:443" in ln]
    assert dnat == ["tcp dport 443 ip saddr @tg_pool4 dnat ip to 169.254.77.2:443"], \
        f"счёт SYN открыл DNAT вебхука в обход пула: {dnat}"


# taking — как его собирает guest_zone: порта вебхука ещё нет
@pytest.mark.parametrize("over", [dict(guest_phase="taking", guest_webhook_port=0), dict(guest_webhook_port=0),
                                  dict(guest_zone4="")])
def test_no_syn_counting_and_no_seen_set_outside_serving(over):
    text = nftguard.render(_spec(**{**RF, **over}))
    assert "tg_seen4" not in text, f"{over}: набор обучения без двери вебхука"


def test_the_syn_counter_stands_before_the_ssh_and_open_port_accepts():
    """Если порт вебхука совпал с открытым портом хоста, accept выше съел бы
    SYN до счёта — правило стоит раньше открытых портов."""
    inp = _chain(nftguard.render(_spec(open_tcp=[443], **RF)), "input")
    assert inp.index(SYN) < inp.index("tcp dport 443 accept"), inp


# ── сбор из состояния зоны ───────────────────────────────────────────────────

@pytest.fixture()
def zone_state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    orig = rsvzone.state
    monkeypatch.setattr(rsvzone, "state", lambda p=path: orig(p))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    def put(**data):
        path.write_text(json.dumps(data))
    return put


@pytest.mark.parametrize("phase", ["taking", "serving"])
def test_the_rf_veth_and_the_zone_address_come_from_the_zone_state(zone_state, phase):
    zone_state(phase=phase, port="45877", zone_rf_cidr="169.254.77.8/30")
    z = nftguard.guest_zone()
    assert (z["guest_rf_if"], z["guest_rf_zone4"]) == ("awggr-h", "169.254.77.10")


def test_the_rf_cidr_has_a_default_and_a_broken_one_gives_no_rf_fields(zone_state):
    zone_state(phase="serving", port="45877")
    assert nftguard.guest_zone()["guest_rf_zone4"] == "169.254.77.6"
    zone_state(phase="serving", port="45877", zone_rf_cidr="не сеть")
    assert "guest_rf_if" not in nftguard.guest_zone()
    zone_state(phase="serving", port="45877", zone_rf_cidr="169.254.77.4/32")
    assert "guest_rf_if" not in nftguard.guest_zone(), "в /32 нет адреса зоны — правил быть не должно"


@pytest.mark.parametrize("phase", ["idle", "relay"])
def test_no_rf_fields_outside_taking_and_serving(zone_state, phase):
    zone_state(phase=phase, port="45877", relay_to="203.0.113.5:45877", zone_rf_cidr="169.254.77.4/30")
    assert "guest_rf_if" not in nftguard.guest_zone()
