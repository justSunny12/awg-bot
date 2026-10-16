"""
Прежние имена двух полей блока bundle снимка (home_subnets, peer_home_nets):
агент пишет их рядом с новыми для сервера 3.3.0, сервер принимает их от
агента 3.3.0, хранит обе формы и переводит снимок, сохранённый сервером
3.3.0. Снимается со следующим сдвигом минимума.
"""
from __future__ import annotations

import json

import pytest

from awgbot.domain import gwsnapshot
from tests.unit.test_gwsnapshot import BUNDLE, BUNDLE_OLD, _Pi


@pytest.fixture()
def pi(tmp_path, monkeypatch):
    return _Pi(tmp_path, monkeypatch)


def test_the_agent_writes_the_old_field_names_next_to_the_new_ones(pi):
    """Сервер 3.3.0 читает только home_subnets / peer_home_nets: без них агент
    3.3.1 под ним выглядел бы без подсетей, а SMB соседей не раздавались бы."""
    pi.env["LAN_SUBNETS"] = "192.168.1.0/24"
    pi.env["PEER_LAN_NETS"] = "10.242.0.0/16"
    b = pi.snap()["bundle"]
    assert (b["lan_subnets"], b["peer_lan_nets"]) == ("192.168.1.0/24", "10.242.0.0/16")
    assert (b["home_subnets"], b["peer_home_nets"]) == ("192.168.1.0/24", "10.242.0.0/16")


def test_the_server_reads_the_old_names_from_an_agent_330_and_keeps_both():
    """Агент 3.3.0 шлёт только прежние имена: сервер кладёт их под новыми;
    новые, если есть, берутся первыми. Хранятся обе формы — откат сервера на
    3.3.0 прочтёт свою."""
    out = gwsnapshot.sanitize({"bundle": {"home_subnets": "192.168.1.0/24", "peer_home_nets": "10.242.0.0/16"}})
    assert set(out["bundle"]) == set(BUNDLE + BUNDLE_OLD)
    assert (out["bundle"]["lan_subnets"], out["bundle"]["peer_lan_nets"]) == ("192.168.1.0/24", "10.242.0.0/16")
    out = gwsnapshot.sanitize({"bundle": {"lan_subnets": "10.0.0.0/24", "home_subnets": "192.168.1.0/24"}})
    assert out["bundle"]["lan_subnets"] == out["bundle"]["home_subnets"] == "10.0.0.0/24"
    out = gwsnapshot.sanitize({"bundle": {"lan_subnets": "10.0.0.0/24", "peer_lan_nets": "10.242.0.0/16"}})
    assert (out["bundle"]["home_subnets"], out["bundle"]["peer_home_nets"]) == ("10.0.0.0/24", "10.242.0.0/16")


def test_a_snapshot_stored_by_the_server_330_is_read_under_the_new_names(services):
    """Сервер 3.3.0 сохранил снимок с прежними именами; после обновления
    сервера он читается под новыми — иначе до переподключения шлюза подсети
    соседей считались бы не применёнными, и на подключении шлюзу ушёл бы
    пустой список SMB соседей."""
    services.db.set_state("gwlink_snap_1", json.dumps(
        {"bundle": {"lan_mode": "1", "home_subnets": "192.168.1.0/24", "peer_home_nets": "10.242.0.0/16"}}))
    b = services.gwlink_snapshot(1)["bundle"]
    assert (b["lan_subnets"], b["peer_lan_nets"]) == ("192.168.1.0/24", "10.242.0.0/16")


def test_the_snapshot_no_longer_carries_the_plumbing_generation(pi):
    assert "plumbing_gen" not in pi.snap()
    assert "plumbing_gen" not in gwsnapshot.sanitize({"plumbing_gen": "old"})
