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
    "zone_cidr": "169.254.77.0/30",
    "zone_rf_cidr": "169.254.77.4/30",
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
    import ipaddress
    nets = [ipaddress.ip_network(n) for n in (RESERVE["pair_cidr"], RESERVE["zone_cidr"], RESERVE["zone_rf_cidr"],
                                               *RESERVE["rsv_cidrs"])]
    for i, a in enumerate(nets):
        for b in nets[i + 1:]:
            assert not a.overlaps(b), f"{a} и {b} пересекаются"
