"""Слияние бэкапа с текущим после перепроверки выпуска: выпуск набора и
заявки сопряжения — этого сервера; пространство адресов в туннеле и
документ конфигурации шлюза на той же линии.

- fleet_release (выпуск набора) — версия ЭТОГО сервера на любой линии:
  выпуск из чужой копии повёл бы текущие шлюзы за чужим сервером;
- gw_join_<n> — заявки сопряжения всех номеров слотов текущие: линк на
  ключах токена стоит на этом хосте, заявка из копии без него (или без
  заявки при штампе) — карантин без возврата;
- routing.lan_space на той же линии — текущее (роздано в адреса в туннеле
  шлюзов), на другой — из бэкапа;
- документ конфигурации по восстановленной базе и файлам той же линии
  совпадает с тем, что стоит на шлюзе: раздел link есть и не расходится —
  восстановление не шлёт шлюзу ключи линка, которых у ВПС уже нет.

Цена ошибки: шлюзы, ушедшие на чужой выпуск после восстановления; вечный
карантин линка; адреса в туннеле, разошедшиеся с уже розданными; линк,
порванный документом с ключами из бэкапа.
"""
from __future__ import annotations

import json

import pytest
import yaml

from awgbot.core import config
from awgbot.domain.services.gwchannel import cfg as cfgmod
from tests.integration._restore_merge_common import _one, make_fresh, make_world
from tests.unit.test_wgconf_allowed import LINK

pytestmark = pytest.mark.integration

JOIN = {"id": "0123456789abcdef", "slot": 2, "kind": "new", "state": "waiting", "link_if": "awglink2",
        "port": 444, "cidr": "10.99.99.4/30"}


@pytest.fixture()
def world(services, make_active_client, monkeypatch, tmp_path, fake_awg):
    return make_world(services, make_active_client, monkeypatch, tmp_path)


@pytest.fixture()
def fresh(tmp_path):
    w, db = make_fresh(tmp_path)
    yield w
    db.close()


def _state(path: str, key: str):
    return _one(path, "SELECT value FROM server_state WHERE key = ?", key)


# ── cross-6: выпуск набора и заявки сопряжения — этого сервера ──────────────

@pytest.mark.parametrize("line", ["same", "other"])
def test_the_fleet_release_and_join_requests_stay_those_of_this_server(world, fresh, tmp_path, line):
    world.db.set_state("fleet_release", "v3.3.0")
    world.db.set_state("gw_join_2", json.dumps(JOIN))
    copy = world.take_copy(tmp_path / "copy")
    here = world if line == "same" else fresh
    here.db.set_state("fleet_release", "v3.4.0")
    here.db.set_state("gw_join_2", "")
    here.db.set_state("gw_join_3", json.dumps({**JOIN, "slot": 3, "link_if": "awglink3"}))
    report = here.run(copy)
    assert report["line"] == ("same" if line == "same" else "clean")
    res = str(copy / "state" / "bot.db")
    assert _state(res, "fleet_release") == "v3.4.0", "выпуск набора из копии чужого (прежнего) сервера"
    assert not _state(res, "gw_join_2"), "заявка сопряжения из копии без линка на этом хосте"
    assert json.loads(_state(res, "gw_join_3"))["slot"] == 3, "заявка сопряжения этого хоста потеряна"


# ── honest-5: lan_space ─────────────────────────────────────────────────────

def _lan_space(copy) -> str:
    app = yaml.safe_load((copy / "state" / "conf" / "app.yaml").read_text(encoding="utf-8"))
    return app["routing"]["lan_space"]


def test_the_tunnel_address_space_stays_current_on_the_same_line(world, tmp_path):
    copy = world.take_copy(tmp_path / "copy")
    app = world.conf / "app.yaml"
    app.write_text(app.read_text(encoding="utf-8").replace("10.240.0.0/12", "10.224.0.0/12"), encoding="utf-8")
    assert world.run(copy)["line"] == "same"
    assert _lan_space(copy) == "10.224.0.0/12", "пространство адресов в туннеле — из бэкапа на той же линии"


def test_the_tunnel_address_space_comes_from_the_copy_on_another_line(world, fresh, tmp_path):
    app = world.conf / "app.yaml"
    app.write_text(app.read_text(encoding="utf-8").replace("10.240.0.0/12", "10.224.0.0/12"), encoding="utf-8")
    copy = world.take_copy(tmp_path / "copy")
    fresh.run(copy)
    assert _lan_space(copy) == "10.224.0.0/12", "новый сервер не взял пространство адресов из бэкапа"


# ── honest-5: документ конфигурации по восстановленной базе ─────────────────

def _root_reads(monkeypatch, root) -> None:
    real_open = open

    def _open(path, *a, **kw):
        p = str(path)
        if p.startswith("/root/"):
            p = str(root / p[len("/root/"):])
        return real_open(p, *a, **kw)
    monkeypatch.setattr(cfgmod, "open", _open, raising=False)


def test_the_restored_document_matches_what_the_gateway_has_including_the_link(world, tmp_path, monkeypatch):
    """Ключи линка сменили после бэкапа (шлюз на новых): документ, собранный
    по восстановленной базе и файлам, — тот же, что у шлюза стоит; раздел link
    в нём есть, расхождения нет."""
    gw_src = world.gwroot / "gw-awglink.conf"
    gw_src.write_text(LINK, encoding="utf-8")
    copy = world.take_copy(tmp_path / "copy")
    gw_src.write_text(LINK.replace("DUMMY-LINK-PRIVATE-KEY=", "DUMMY-LINK-NEW-PRIVATE-KEY=")
                      .replace("DUMMY-LINK-PSK=", "DUMMY-LINK-NEW-PSK="), encoding="utf-8")
    _root_reads(monkeypatch, world.gwroot)
    gw = world.db.gateway(1)
    live = world.s.gwcfg_doc(gw)
    assert "link" in live["sections"], "сценарий собран не так: в документе нет раздела link"
    have = {"gen": live["gen"], **live["prints"]}                  # шлюз применил текущее
    assert world.run(copy)["line"] == "same"
    from awgbot.domain.services import Services
    from awgbot.infra.db import Database
    _root_reads(monkeypatch, copy / "root")
    monkeypatch.setattr(config, "AWG_DIR", str(copy / "awg"))
    d = Database(str(copy / "state" / "bot.db"))
    try:
        s = Services(d)
        gw2 = d.gateway(1)
        doc = s.gwcfg_doc(gw2)
        assert "link" in doc["sections"], "по восстановленным файлам раздел link не собрался"
        assert doc["prints"]["link"] == live["prints"]["link"], "восстановление шлёт шлюзу ключи линка из бэкапа"
        assert s.gwcfg_diff(gw2, doc, have) == [], s.gwcfg_diff(gw2, doc, have)
    finally:
        d.close()
