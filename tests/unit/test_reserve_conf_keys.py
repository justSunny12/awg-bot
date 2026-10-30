"""Ключи резерва в шаблоне conf/app.yaml: `network.self_host` и блок
`reserve:` с умолчаниями.

Шаблон досевается в живые установки при обновлении, а бот читает эти ключи
по месту (горячие): ключ с опечаткой в имени или с другим умолчанием
молча меняет поведение у всех — например, `ready: true` по умолчанию
сделал бы каждый сервер резервом без вопроса, а пустой `pair_cidr` —
линк пары без подсети.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

APP = Path(__file__).resolve().parents[2] / "conf" / "app.yaml"

RESERVE = {
    "ready": False,
    "return": "immediate",
    "rf_via_mine": True,
    "pair_cidr": "10.99.98.0/30",
    "pair_port": 47101,
    "pair_channel_port": 8788,
    "zone_cidr": "169.254.77.0/30",
    "zone_rf_cidr": "169.254.77.4/30",
    "zone_tg_cidr": "169.254.77.8/30",
    "zone_pool": "169.254.96.0/19",
    "rsv_cidrs": ["10.99.97.0/30", "10.99.97.4/30"],
    "suspect_seconds": 40,
    "probe_seconds": 15,
    "rf_block_minutes": 15,
    "ask_seconds": 60,
    "notify_online_minutes": 15,
    "pkg_minutes": 30,
    "min_interval_minutes": 30,
    "relay_idle_hours": 2,
    "relay_max_days": 7,
    "tg_pool_asns": [62041, 62014, 59930, 44907, 211157],
    "tg_pool_url": "https://stat.ripe.net/data/announced-prefixes/data.json?resource=AS{asn}",
    "tg_pool_hours": 24,
    "tg_pool_max": 256,
    "tg_pool_learn_syn": 3,
    "tg_pool_stale_days": 3,
    "quorum": {"port": 8789, "probe_seconds": 15, "no_seconds": 45, "yes_window_seconds": 40, "down_seconds": 20,
               "up_seconds": 100, "pledge_minutes": 10, "tie_minutes": 3},
}


@pytest.fixture(scope="module")
def app() -> dict:
    return yaml.safe_load(APP.read_text(encoding="utf-8"))


def test_the_reserve_block_has_exactly_the_agreed_keys_and_defaults(app):
    got = app.get("reserve")
    assert isinstance(got, dict), "в шаблоне нет блока reserve"
    assert sorted(got) == sorted(RESERVE), \
        f"лишние: {sorted(set(got) - set(RESERVE))}, нет: {sorted(set(RESERVE) - set(got))}"
    wrong = {k: (got[k], v) for k, v in RESERVE.items() if got[k] != v or type(got[k]) is not type(v)}
    assert not wrong, f"умолчания разошлись (есть, ждали): {wrong}"


def test_self_host_is_empty_by_default_next_to_server_host(app):
    assert app["network"].get("self_host") == "", "пустой self_host — поведение прошлой версии"
    assert "server_host" in app["network"]


def test_the_reserve_subnets_do_not_overlap_each_other():
    """Подсети резерва и блок пиров-зондов кворума (util/probenet, не настройка):
    пул NETMAP, задевший блок, отобразил бы устройства основы на адреса зондов."""
    import ipaddress
    from awgbot.util import probenet
    nets = [ipaddress.ip_network(n) for n in (RESERVE["pair_cidr"], RESERVE["zone_cidr"], RESERVE["zone_rf_cidr"],
                                               RESERVE["zone_pool"], *RESERVE["rsv_cidrs"])] + [probenet.PROBE_NET]
    for i, a in enumerate(nets):
        for b in nets[i + 1:]:
            assert not a.overlaps(b), f"{a} и {b} пересекаются"


def test_the_code_defaults_match_the_template():
    """Порт линка пары и порт канала пары читаются с умолчанием в коде: оно
    обязано совпадать с шаблоном, иначе сервер без ключа в своём app.yaml
    (досев не дошёл) поднимет линк или слушатель не на том порту, а
    файервол откроет другой."""
    from awgbot.domain.services.reserve import invite
    from awgbot.runtime.pairlink import common
    assert invite.DEFAULT_PAIR_PORT == RESERVE["pair_port"]
    assert common.DEFAULT_PORT == RESERVE["pair_channel_port"]


@pytest.mark.parametrize("stored", ["absent", ""], ids=["no-key", "empty"])
def test_the_zone_wrapper_defaults_match_the_template(monkeypatch, stored):
    """Подсети veth и пул NETMAP обёртка зоны передаёт скрипту окружением при
    каждом вызове; без ключа в app.yaml сервера (досев не дошёл) или с пустым
    значением — умолчание кода. Разойдись оно с шаблоном — зона отобразит
    подсети клонов на один пул, а файервол хоста по документации и ожиданиям
    админа будет считать своим другой."""
    from awgbot.core import settings
    from awgbot.infra import rsvzone
    monkeypatch.setattr(settings, "get", lambda key, default=None: default if stored == "absent" else "")
    env = rsvzone._zone_env()
    assert (env["ZONE_CIDR"], env["ZONE_RF_CIDR"], env["ZONE_POOL"]) == \
        (RESERVE["zone_cidr"], RESERVE["zone_rf_cidr"], RESERVE["zone_pool"]), env


@pytest.mark.parametrize("bad", ["169.254.96.0/33", "мусор", "fd00:96::/64", "169.254.96.0/19; rm"])
def test_a_broken_zone_pool_is_not_passed_to_the_script_and_is_logged_once(monkeypatch, caplog, bad):
    """Опечатка в reserve.zone_pool: скрипту уходит умолчание, в журнале одна
    строка на значение (обёртку зовут на каждом режиме зоны) — иначе план NETMAP
    молча пуст, а причина в журнале резерва ложная."""
    import logging
    from awgbot.core import settings
    from awgbot.infra import rsvzone
    monkeypatch.setattr(settings, "get", lambda key, default=None: bad if key.endswith("zone_pool") else default)
    monkeypatch.setattr(rsvzone, "_pool_told", set())
    caplog.set_level(logging.WARNING)
    assert rsvzone._zone_env()["ZONE_POOL"] == RESERVE["zone_pool"]
    assert rsvzone._zone_env()["ZONE_POOL"] == RESERVE["zone_pool"]
    told = [r for r in caplog.records if "zone_pool" in r.getMessage()]
    assert len(told) == 1 and told[0].levelno == logging.WARNING and bad in told[0].getMessage(), \
        [r.getMessage() for r in caplog.records]


def test_a_good_zone_pool_goes_through_normalized_and_without_a_line(monkeypatch, caplog):
    import logging
    from awgbot.core import settings
    from awgbot.infra import rsvzone
    monkeypatch.setattr(settings, "get", lambda key, default=None: " 100.127.0.5/20 " if key.endswith("zone_pool") else default)
    caplog.set_level(logging.WARNING)
    assert rsvzone._zone_env()["ZONE_POOL"] == "100.127.0.0/20"
    assert not [r for r in caplog.records if "zone_pool" in r.getMessage()]


def test_each_broken_zone_pool_value_gets_its_own_line_once(monkeypatch, caplog):
    """Админ поправил одну опечатку на другую: новое битое значение — своя
    строка (иначе журнал молчит о свежей ошибке, потому что «уже говорил»);
    возврат к прежнему битому и повторы — без новых строк."""
    import logging
    from awgbot.core import settings
    from awgbot.infra import rsvzone
    current = {"v": "169.254.96.0/33"}
    monkeypatch.setattr(settings, "get", lambda key, default=None: current["v"] if key.endswith("zone_pool") else default)
    monkeypatch.setattr(rsvzone, "_pool_told", set())
    caplog.set_level(logging.WARNING)
    for value in ("169.254.96.0/33", "169.254.96.0/33", "fd00:96::/64", "fd00:96::/64", "169.254.96.0/33"):
        current["v"] = value
        assert rsvzone._zone_env()["ZONE_POOL"] == RESERVE["zone_pool"], f"{value!r} ушло скрипту вместо умолчания"
    told = [r.getMessage() for r in caplog.records if "zone_pool" in r.getMessage()]
    assert len(told) == 2, f"ждали по строке на каждое из двух битых значений: {told}"
    assert "169.254.96.0/33" in told[0] and "fd00:96::/64" in told[1], told
