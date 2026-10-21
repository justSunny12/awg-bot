"""Файервол хоста резерва по состоянию гостевой зоны (infra/nftguard): порт
клона, выход зоны наружу через veth и маскарад, DNAT порта вебхука только с
пула адресов Telegram, пересылка хвоста UDP на основу в relay.

Правила рисует бот по `state.json` зоны; скрипт зоны таблицу хоста не трогает.

Цена ошибки: DNAT порта вебхука без фильтра по пулу — в бота основы (с токеном
и данными клиентов основы) может стучаться кто угодно из интернета; пул без
ядра — после неудачного обновления диапазонов Telegram перестаёт доставлять
вебхуки, бот на резерве молчит; правила зоны в фазе `idle` — порт клона
открыт, клона нет; пересылка хвоста без маскарада — ответ основы не возвращается
клиенту.
"""
from __future__ import annotations

import json

import pytest

from awgbot.core import config
from awgbot.infra import nftguard, rsvzone

pytestmark = pytest.mark.unit


def _spec(**kw) -> nftguard.GuardSpec:
    base = dict(ssh_port=2222, ssh_open=True, tunnel_nets4=["10.8.1.0/24"], tunnel_ifs=["awg0"], udp_ports=[22222])
    base.update(kw)
    return nftguard.GuardSpec(**base)


SERVING = dict(guest_phase="serving", guest_udp_port=45877, guest_veth_if="awggv-h", guest_zone4="169.254.77.2",
               guest_zone_net4="169.254.77.0/30", guest_webhook_port=443,
               tg_pool4=["149.154.160.0/20", "91.108.4.0/22"])
RELAY = dict(guest_phase="relay", relay_to="203.0.113.5:45877", relay_udp_port=45877)


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines()]


@pytest.mark.parametrize("filtering", [True, False], ids=["filter", "nat-only"])
def test_serving_passes_the_webhook_port_to_the_zone_only_from_the_telegram_pool(filtering):
    text = nftguard.render(_spec(filter=filtering, **SERVING))
    lines = _lines(text)
    assert "tcp dport 443 ip saddr @tg_pool4 dnat ip to 169.254.77.2:443" in lines, text
    dnat = [ln for ln in lines if "dnat ip to 169.254.77.2" in ln]
    assert len(dnat) == 1 and "ip saddr @tg_pool4" in dnat[0], f"DNAT вебхука без фильтра по пулу: {dnat}"
    assert "set tg_pool4 {" in text and "149.154.160.0/20" in text and "91.108.4.0/22" in text
    assert 'oifname { "awggv-h" } ip daddr 169.254.77.2 tcp dport 443 ct status dnat accept' in lines, \
        "после DNAT вебхук должен пройти в зону только как DNAT, а не любым пакетом"
    assert 'oifname { "awggv-h" } drop' in lines, "в зону с внешнего интерфейса без DNAT хода нет"


@pytest.mark.parametrize("filtering", [True, False], ids=["filter", "nat-only"])
def test_serving_opens_the_clone_port_lets_the_zone_out_and_masquerades_it(filtering):
    lines = _lines(nftguard.render(_spec(filter=filtering, **SERVING)))
    assert "udp dport 45877 accept" in lines, "порт клона закрыт — клиенты основы не доходят"
    assert 'iifname { "awggv-h" } ip daddr != @tunnel_nets4 accept' in lines, \
        "бот основы в зоне не выйдет в Telegram (без известных внешних интерфейсов — всё, кроме сетей туннеля)"
    assert any(ln.endswith("ip saddr 169.254.77.0/30 masquerade") for ln in lines), "зона без маскарада наружу"


@pytest.mark.parametrize("phase", ["taking", "serving"])
def test_the_zone_goes_only_out_through_the_external_interfaces(phase):
    """Из зоны гостя пересылка — только во внешние интерфейсы хоста, и сразу
    за этим запрет всего остального с veth. Иначе бот основы в зоне (чужой
    код с чужими данными) дотягивается до клиентов резерва в туннеле, до
    линков слотов и до локальных сетей за шлюзами резерва."""
    extra = dict(SERVING) if phase == "serving" else dict(guest_phase="taking", guest_udp_port=45877,
                                                            guest_veth_if="awggv-h", guest_zone4="169.254.77.2",
                                                            guest_zone_net4="169.254.77.0/30")
    lines = _lines(nftguard.render(_spec(wan_ifs=["eth0", "ens3"], **extra)))
    out = [ln for ln in lines if ln.startswith('iifname { "awggv-h" }')]
    assert out[:2] == ['iifname { "awggv-h" } oifname { "eth0", "ens3" } accept', 'iifname { "awggv-h" } drop'], \
        f"из зоны не только наружу: {out}"
    assert 'iifname { "awggv-h" } accept' not in lines, "из зоны — куда угодно, в том числе в туннель резерва"


def test_taking_has_the_port_and_the_exit_but_no_webhook_door_and_no_pool():
    """Пока факт записи DNS не подтверждён (taking), Telegram в зону не пускаем."""
    text = nftguard.render(_spec(guest_phase="taking", guest_udp_port=45877, guest_veth_if="awggv-h",
                                 guest_zone4="169.254.77.2", guest_zone_net4="169.254.77.0/30"))
    assert "udp dport 45877 accept" in _lines(text)
    assert 'iifname { "awggv-h" } ip daddr != @tunnel_nets4 accept' in _lines(text), "зона без выхода наружу"
    assert "dnat" not in text and "tg_pool4" not in text and "prerouting" not in text


@pytest.mark.parametrize("filtering", [True, False], ids=["filter", "nat-only"])
def test_a_zone_that_is_not_running_adds_nothing_to_the_table(filtering):
    plain = nftguard.render(_spec(filter=filtering))
    for extra in (dict(guest_phase=""), dict(guest_phase="idle", guest_udp_port=45877, guest_veth_if="awggv-h")):
        assert nftguard.render(_spec(filter=filtering, **extra)) == plain, f"{extra}: правила зоны при остановленной зоне"
    assert "awggv-h" not in plain and "45877" not in plain


@pytest.mark.parametrize("filtering", [True, False], ids=["filter", "nat-only"])
def test_relay_forwards_the_clone_port_to_the_main_and_masquerades_the_answer(filtering):
    text = nftguard.render(_spec(filter=filtering, **RELAY))
    lines = _lines(text)
    assert "udp dport 45877 dnat ip to 203.0.113.5:45877" in lines, text
    assert "ct status dnat meta l4proto udp masquerade" in lines, "ответ основы не вернётся клиенту"
    assert "ct status dnat meta l4proto udp accept" in lines, "пересылка хвоста не пропущена фильтром хоста"
    assert "udp dport 45877 accept" not in lines, "клона уже нет, а порт для хоста открыт"
    assert "tg_pool4" not in text and "169.254.77" not in text


@pytest.mark.parametrize("over", [dict(relay_to=""), dict(relay_udp_port=0), dict(guest_phase="serving")])
def test_a_relay_without_a_target_or_a_port_forwards_nothing(over):
    spec = dict(RELAY)
    spec.update(over)
    text = nftguard.render(_spec(**spec))
    assert "dnat ip to 203.0.113.5" not in text and "masquerade" in text, text  # маскарад клиентов остаётся


@pytest.mark.parametrize("miss", ["guest_zone4", "guest_webhook_port"])
def test_a_serving_zone_without_an_address_or_a_port_opens_no_webhook_door(miss):
    spec = dict(SERVING)
    spec[miss] = "" if miss == "guest_zone4" else 0
    text = nftguard.render(_spec(**spec))
    assert "dnat" not in text and "tg_pool4" not in text


def test_the_table_is_deterministic_for_the_cheap_comparison_by_text():
    assert nftguard.render(_spec(**SERVING)) == nftguard.render(_spec(**SERVING))
    assert nftguard.render(_spec(**SERVING)) != nftguard.render(_spec(**RELAY))



# ── два клона: клиентский интерфейс и окно переезда профилей ────────────────

def test_serving_with_two_clones_opens_both_ports_in_one_rule():
    """Основа в окне переезда профилей: часть устройств ещё на старом
    интерфейсе, часть — на новом. Закрытый второй порт — эти устройства без
    VPN всё время переезда."""
    spec = dict(SERVING, guest_udp_ports=[45877, 45878])
    lines = _lines(nftguard.render(_spec(**spec)))
    assert "udp dport { 45877, 45878 } accept" in lines, "второй порт клона закрыт"
    assert "udp dport 45877 accept" not in lines, "порт первого клона продублирован отдельным правилом"


@pytest.mark.parametrize("filtering", [True, False], ids=["filter", "nat-only"])
def test_relay_with_two_ports_forwards_each_to_the_same_port_of_the_main(filtering):
    """DNAT набора портов на «адрес:порт» отправил бы оба интерфейса в один
    порт основы — устройства второго интерфейса стучались бы не в свой
    интерфейс. При нескольких портах DNAT — на адрес без порта (порт тот же)."""
    spec = dict(RELAY, relay_udp_ports=[45877, 45878])
    lines = _lines(nftguard.render(_spec(filter=filtering, **spec)))
    assert "udp dport { 45877, 45878 } dnat ip to 203.0.113.5" in lines, "\n".join(lines)
    assert not [ln for ln in lines if "dnat ip to 203.0.113.5:" in ln], "при двух портах DNAT с портом назначения"
    assert "ct status dnat meta l4proto udp masquerade" in lines, "ответ основы не вернётся клиенту"
    if filtering:
        assert "ct status dnat meta l4proto udp accept" in lines, "пересылка хвоста не пропущена фильтром хоста"


def test_relay_with_one_port_in_the_list_keeps_the_old_rule_with_the_port():
    lines = _lines(nftguard.render(_spec(**dict(RELAY, relay_udp_ports=[45877]))))
    assert "udp dport 45877 dnat ip to 203.0.113.5:45877" in lines, "один клон — правило поменяло вид"


def test_the_collection_reads_all_clone_ports_for_serving_and_relay(zone_state):
    zone_state(phase="serving", port="45877", ports="45877 45878", zone_cidr="169.254.77.0/30")
    z = nftguard.guest_zone()
    assert (z["guest_udp_port"], z["guest_udp_ports"]) == (45877, [45877, 45878]), z
    zone_state(phase="relay", port="45877", ports="45877 45878", relay_to="203.0.113.5:45877")
    z = nftguard.guest_zone()
    assert z["relay_udp_ports"] == [45877, 45878], "в relay второй порт не пересылается — хвост его устройств пропал"


@pytest.mark.parametrize("ports, want", [("", [45877]), ("45877 мусор 70000 0", [45877])])
def test_garbage_or_missing_ports_fall_back_to_the_first_clone(zone_state, ports, want):
    zone_state(phase="taking", port="45877", ports=ports)
    assert nftguard.guest_zone()["guest_udp_ports"] == want

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


def test_an_absent_or_idle_zone_gives_no_guest_fields(zone_state):
    assert nftguard.guest_zone() == {}
    zone_state(phase="idle", port="45877")
    assert nftguard.guest_zone() == {}
    zone_state(phase="dancing", port="45877")
    assert nftguard.guest_zone() == {}


def test_a_serving_zone_yields_port_veth_address_webhook_and_pool(zone_state):
    zone_state(phase="serving", port="45877", zone_cidr="169.254.77.0/30", webhook_port="8443")
    z = nftguard.guest_zone()
    assert (z["guest_phase"], z["guest_udp_port"], z["guest_veth_if"], z["guest_zone4"], z["guest_zone_net4"],
            z["guest_webhook_port"]) == ("serving", 45877, "awggv-h", "169.254.77.2", "169.254.77.0/30", 8443)
    assert z["tg_pool4"][:2] == ["149.154.160.0/20", "91.108.4.0/22"]


def test_a_taking_zone_has_no_webhook_port_and_no_pool(zone_state):
    zone_state(phase="taking", port="45877", webhook_port="443")
    z = nftguard.guest_zone()
    assert z["guest_phase"] == "taking" and "guest_webhook_port" not in z and "tg_pool4" not in z


def test_a_relay_zone_yields_only_the_forward_target(zone_state):
    zone_state(phase="relay", port="45877", relay_to="203.0.113.5:45877", zone_cidr="169.254.77.0/30")
    z = nftguard.guest_zone()
    assert z == {"guest_phase": "relay", "relay_to": "203.0.113.5:45877", "relay_udp_port": 45877,
                 "relay_udp_ports": [45877]}, "старое состояние без ports: пересылается один порт клона"


@pytest.mark.parametrize("state", [dict(relay_to="203.0.113.5", port="45877"), dict(relay_to="203.0.113.5:1", port="0"),
                                   dict(relay_to="", port="45877")])
def test_a_relay_without_a_full_target_or_a_port_gives_no_forward(zone_state, state):
    zone_state(phase="relay", **state)
    assert nftguard.guest_zone() == {"guest_phase": "relay"}


def test_a_garbage_port_or_subnet_in_the_state_does_not_crash_the_collection(zone_state):
    zone_state(phase="serving", port="не число", zone_cidr="мусор", webhook_port="тоже")
    z = nftguard.guest_zone()
    assert z["guest_udp_port"] == 0 and z["guest_zone4"] == "" and z["guest_webhook_port"] == 0


# ── пул адресов Telegram ─────────────────────────────────────────────────────

def _pool_file(tmp_path, data):
    (tmp_path / "reserve").mkdir(exist_ok=True)
    (tmp_path / "reserve" / "tg_pool.json").write_text(data if isinstance(data, str) else json.dumps(data))


def test_the_pool_always_has_the_core_and_never_less(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert nftguard.tg_pool() == ["149.154.160.0/20", "91.108.4.0/22"]
    _pool_file(tmp_path, {"pool": ["198.51.100.0/24"]})
    assert nftguard.tg_pool() == ["149.154.160.0/20", "91.108.4.0/22", "198.51.100.0/24"]


@pytest.mark.parametrize("data", ["{oops", "[]", {"pool": None}, {"pool": ["мусор", "999.1.1.1/8"]}, {"nope": 1}])
def test_a_broken_pool_file_leaves_the_core(tmp_path, monkeypatch, data):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _pool_file(tmp_path, data)
    assert nftguard.tg_pool() == ["149.154.160.0/20", "91.108.4.0/22"]


def test_the_pool_has_no_duplicates_and_normalizes_hosts(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _pool_file(tmp_path, {"pool": ["149.154.160.0/20", "198.51.100.7", "198.51.100.7/32"]})
    assert nftguard.tg_pool() == ["149.154.160.0/20", "91.108.4.0/22", "198.51.100.7/32"]
