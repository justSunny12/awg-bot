"""Снятый выпуск: флаг prerelease и заголовок «снят: причина» →
Release.withdrawn. Не цель кнопки и не ступень вверх (ни у бота, ни у набора),
но и не барьер — метаданные у него прочитаны, и кто на нём стоит, тот на
сборке из выпуска и обновляется дальше. Восстановить бэкап на него можно —
с предупреждением; удалённый тег — отказ.
"""
from __future__ import annotations

import hashlib
import json

import pytest

import awgbot.core.config as cfg
from awgbot.infra import updates
from awgbot.infra.updates.github import withdrawn_reason

pytestmark = pytest.mark.unit

META = cfg.UPDATES_META_ASSET_NAME


class _GitHub:
    def __init__(self):
        self.releases: list = []
        self.blobs: dict = {}

    def add(self, tag: str, *, name: str = "", prerelease: bool = False):
        meta = json.dumps({"schema": 1, "version": tag[1:], "awg_gen": 1,
                           "roles": {"main": {"requires": "3.4.0"}, "gw": {"requires": "3.4.0"}}}).encode()
        url = f"https://api/assets/{tag}/meta"
        self.blobs[url] = meta
        self.releases.append({"tag_name": tag, "name": name or tag, "body": "", "draft": False,
                              "prerelease": prerelease,
                              "assets": [{"name": cfg.UPDATES_ASSET_NAME, "url": f"https://api/assets/{tag}",
                                          "digest": "sha256:" + hashlib.sha256(tag.encode()).hexdigest()},
                                         {"name": META, "url": url, "size": len(meta),
                                          "digest": "sha256:" + hashlib.sha256(meta).hexdigest()}]})

    def request(self, url: str, accept: str) -> bytes:
        if url.endswith("/releases?per_page=100"):
            return json.dumps(self.releases).encode()
        return self.blobs[url]


@pytest.fixture()
def gh(monkeypatch):
    net = _GitHub()
    monkeypatch.setattr(updates, "_request", net.request)
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.4.0")
    net.add("v3.4.0")
    net.add("v3.4.1", name="v3.4.1 — снят: ломает маршруты шлюза", prerelease=True)
    return net


def test_the_mark_is_prerelease_plus_title():
    assert withdrawn_reason({"prerelease": True}, "снят: ломает маршруты шлюза") == "ломает маршруты шлюза"
    assert withdrawn_reason({"prerelease": True}, "Снят:") == "без причины"
    assert withdrawn_reason({"prerelease": True}, "бета перед выпуском") == "", "просто prerelease — не снят"
    assert withdrawn_reason({"prerelease": False}, "снят: ломает") == "", "без флага — не снят"


def test_withdrawn_is_parsed_and_its_metadata_is_read(gh):
    rels = {r.tag: r for r in updates.list_releases()}
    assert rels["v3.4.1"].withdrawn == "ломает маршруты шлюза" and rels["v3.4.1"].meta is not None
    assert rels["v3.4.0"].withdrawn == ""


def test_not_a_target_and_not_a_barrier(gh):
    gh.add("v3.4.2")
    assert updates.next_release().tag == "v3.4.2", "снятый стал целью или барьером"
    gh.releases.pop()
    assert updates.next_release() is None, "снятый выпуск предложен кнопкой"


def test_who_stands_on_it_is_on_a_release_build(gh, monkeypatch):
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.4.1")
    gh.add("v3.4.2")
    assert updates.next_release().tag == "v3.4.2", "стоящий на снятом не обновляется дальше"


def test_the_fleet_does_not_pick_it(services, monkeypatch):
    from tests.unit._fleet_common import rel
    monkeypatch.setattr(cfg, "ROLE", "client")
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.4.0")
    w = rel("v3.4.1", both=True)
    w = updates.Release(**{**w.__dict__, "withdrawn": "ломает маршруты шлюза"})
    services._update_store([rel("v3.4.0", both=True), w], {})
    assert services.fleet_target() is None, "снятый выпуск — цель набора"
    services._update_store([rel("v3.4.0", both=True), w, rel("v3.4.2", both=True)], {})
    assert services.fleet_target().tag == "v3.4.2"
    stored = {r.tag: r for r in services.fleet_releases()}
    assert stored["v3.4.1"].withdrawn == "ломает маршруты шлюза", "пометка не пережила сохранённый список"


def test_restore_on_it_warns_and_a_deleted_tag_refuses(gh, monkeypatch):
    from awgbot.domain.fleet import restore as fr
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.4.2")
    gh.add("v3.4.2")
    digest = "sha256:" + hashlib.sha256(b"v3.4.1").hexdigest()
    r = fr.plan_release({"version": "3.4.1", "digest": digest})
    assert not r["error"] and r["withdrawn"] == "ломает маршруты шлюза"
    assert fr.version_lines(r)[1].startswith("⚠️ Выпуск v3.4.1 снят: ломает маршруты шлюза.")
    gh.releases = [x for x in gh.releases if x["tag_name"] != "v3.4.1"]
    assert fr.plan_release({"version": "3.4.1", "digest": digest})["error"].startswith("выпуска v3.4.1 на GitHub нет")
