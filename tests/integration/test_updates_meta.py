"""Метаданные релиза в выборе цели: ассет `awg-bot-release.json` качается только
для релизов новее установленного и только мимо кэша; нет ассета до 3.3.2 —
читается тело, с 3.3.2 — релиз пропущен и не участвует в цепочке; битый —
пропущен, тело не читается. Метаданные, повторяющие прежние хэштеги, дают ту
же цель. Переход: читатель 3.2.x (разбор тела дословно из тега v3.2.1) на
релизах 3.3.0 и 3.3.1 с хэштегами и 3.3.2+ без них встаёт на 3.3.0, а дальше
идёт уже читателем метаданных.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Optional

import pytest

import awgbot.core.config as cfg
from awgbot.infra import updates

pytestmark = pytest.mark.integration

META = cfg.UPDATES_META_ASSET_NAME


def _meta(version: str, roles: dict, gen: int = 1) -> bytes:
    return json.dumps({"schema": 1, "version": version, "awg_gen": gen, "roles": roles}).encode()


class _GitHub:
    """Сеть: /releases и ассеты метаданных; счёт запросов за метаданными."""

    def __init__(self):
        self.releases: list = []
        self.blobs: dict = {}
        self.meta_hits: list = []

    def add(self, tag: str, body: str = "", meta: Optional[bytes] = None, digest: Optional[str] = ...):
        assets = [{"name": cfg.UPDATES_ASSET_NAME, "url": f"https://api/assets/{tag}",
                   "digest": "sha256:" + "a" * 64}]
        if meta is not None:
            url = f"https://api/assets/{tag}/meta"
            self.blobs[url] = meta
            dg = hashlib.sha256(meta).hexdigest() if digest is ... else digest
            assets.append({"name": META, "url": url, "digest": f"sha256:{dg}" if dg else None,
                           "size": len(meta)})
        self.releases.append({"tag_name": tag, "body": body, "draft": False, "assets": assets,
                              "name": tag})

    def request(self, url: str, accept: str) -> bytes:
        if url.endswith("/releases"):
            return json.dumps(self.releases).encode()
        if url in self.blobs:
            self.meta_hits.append(url)
            return self.blobs[url]
        raise updates.UpdateError(f"unexpected url {url}")


@pytest.fixture()
def gh(monkeypatch):
    net = _GitHub()
    monkeypatch.setattr(updates, "_request", net.request)
    return net


BOTH_320 = {"main": {"requires": "3.2.0"}, "gw": {"requires": "3.2.0"}}
BOTH_330 = {"main": {"requires": "3.3.0"}, "gw": {"requires": "3.3.0"}}


def test_metadata_is_fetched_only_for_newer_releases_and_only_past_the_cache(gh, monkeypatch):
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.3.0")
    gh.add("v3.2.1", "#requires_main_3.2.0\n#awg_gen1")
    gh.add("v3.3.0", meta=_meta("3.3.0", BOTH_320))
    gh.add("v3.3.1", meta=_meta("3.3.1", BOTH_330))
    cache: dict = {}
    rel = updates.list_releases(meta_cache=cache)
    assert gh.meta_hits == ["https://api/assets/v3.3.1/meta"], "метаданные качаются только для новее установленного"
    assert rel[-1].meta["version"] == "3.3.1" and set(cache) == {"v3.3.1"}
    updates.list_releases(meta_cache=cache)
    assert len(gh.meta_hits) == 1, "тот же digest — метаданные из кэша, запроса нет"
    gh.releases[-1]["assets"][-1]["digest"] = "sha256:" + hashlib.sha256(b"x").hexdigest()
    gh.blobs["https://api/assets/v3.3.1/meta"] = b"x"
    bad = updates.list_releases(meta_cache=cache)[-1]
    assert len(gh.meta_hits) == 2 and bad.meta is None and bad.meta_bad, "другой digest — перечитать"


def test_without_the_asset_before_332_the_body_is_read(gh, monkeypatch):
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.2.1")
    gh.add("v3.2.1", "#requires_main_3.2.0\n#awg_gen1")
    gh.add("v3.3.0", "#requires_main_3.2.0\n#requires_gw_3.2.0\n#awg_gen1")
    gh.add("v3.3.1", "#requires_main_3.3.0\n#awg_gen1")
    assert updates.next_release("client", meta_cache={}).tag == "v3.3.0", "минимум 3.3.0 из тела 3.3.1 учтён"


def test_without_the_asset_from_332_the_release_is_a_barrier(gh, monkeypatch):
    """Релиз с 3.3.2 без метаданных пропущен — и цель только ниже него: его
    адресаты, минимум и условие неизвестны, перешагивать его нельзя."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.3.0")
    gh.add("v3.3.0", meta=_meta("3.3.0", BOTH_320))
    gh.add("v3.3.1", meta=_meta("3.3.1", BOTH_330))
    gh.add("v3.3.2", "#requires_main_3.3.1\n#awg_gen1")          # тело с 3.3.2 не читается
    gh.add("v3.3.3", meta=_meta("3.3.3", BOTH_330))
    releases = updates.list_releases(meta_cache={})
    for role in ("client", "gateway"):
        assert updates.pick_target((3, 3, 0), releases, role).tag == "v3.3.1", role
    assert [r.tag for r in updates.skipped((3, 3, 0), releases)] == ["v3.3.2"]


def test_a_broken_release_with_a_floor_behind_it_stops_the_host(gh, monkeypatch):
    """(а) битый v3.4.0, за ним v3.4.1 с минимумом 3.4.0: хост 3.3.0 не идёт на
    v3.4.1 — ступень 3.4.0 ему неизвестна."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.3.0")
    gh.add("v3.3.0", meta=_meta("3.3.0", BOTH_320))
    gh.add("v3.4.0", meta=b"{broken")
    gh.add("v3.4.1", meta=_meta("3.4.1", {"main": {"requires": "3.4.0"}, "gw": {"requires": "3.4.0"}}))
    for role in ("client", "gateway"):
        assert updates.next_release(role, meta_cache={}) is None, role


def test_a_broken_release_with_a_condition_behind_it_stops_the_host(gh, monkeypatch):
    """(б) битый v3.3.2 (а в нём могло быть условие на другую роль), за ним
    v3.3.3 без условий: хост не идёт на v3.3.3 — условие по цепочке не
    проверить."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.3.1")
    gh.add("v3.3.1", meta=_meta("3.3.1", BOTH_330))
    blob = _meta("3.3.2", {"main": {"requires": "3.3.0", "needs_gw": "3.3.2"}})
    gh.add("v3.3.2", meta=blob, digest="b" * 64)                  # digest не совпал
    gh.add("v3.3.3", meta=_meta("3.3.3", BOTH_330))
    assert updates.next_release("client", meta_cache={}, peer=lambda need: False) is None
    assert updates.next_release("client", meta_cache={}) is None


def test_a_non_release_build_fetches_no_metadata(gh, monkeypatch):
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.3.0.17")
    gh.add("v3.2.1", "#requires_main_3.2.0")
    gh.add("v3.3.1", meta=_meta("3.3.1", BOTH_330))
    cache: dict = {}
    assert updates.next_release("client", meta_cache=cache, decide=lambda *a: "решал") is None
    assert gh.meta_hits == [] and cache == {}, "нерелизной сборке метаданные не нужны"


@pytest.mark.parametrize("why, blob, digest", [
    ("digest не совпал", _meta("3.3.1", {"main": {"requires": "3.2.0"}}), "b" * 64),
    ("нет digest", _meta("3.3.1", {"main": {"requires": "3.2.0"}}), None),
    ("не JSON", b"{not json", ...),
    ("версия не тег", _meta("3.3.9", {"main": {"requires": "3.2.0"}}), ...),
])
def test_a_broken_asset_skips_the_release_and_the_body_is_not_trusted(gh, monkeypatch, why, blob, digest):
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.3.0")
    gh.add("v3.3.0", meta=_meta("3.3.0", BOTH_320))
    gh.add("v3.3.1", "#requires_main_3.2.0\n#awg_gen1", meta=blob, digest=digest)
    rel = updates.list_releases(meta_cache={})[-1]
    assert rel.meta is None and rel.meta_bad and not rel.usable(), why
    assert updates.next_release("client", meta_cache={}) is None, f"{why}: битый ассет — не повод верить телу"


def test_metadata_repeating_the_old_hashtags_gives_the_same_target(gh, monkeypatch):
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.0.0")
    bodies = [("v3.0.0", "#requires_main_2.18.0\n#requires_gw_2.18.0\n#awg_gen1"),
              ("v3.1.0", "#requires_main_3.0.0\n#awg_gen1"),
              ("v3.2.0", "#requires_main_3.1.0\n#requires_gw_3.0.0\n#awg_gen1"),
              ("v3.2.1", "#requires_gw_3.2.0\n#awg_gen1")]
    for tag, body in bodies:
        gh.add(tag, body)
    by_body = {role: updates.next_release(role).tag for role in ("client", "gateway")}
    gh.releases.clear()
    for tag, body in bodies:
        roles = {k: {"requires": v} for k, v in re.findall(r"#requires_(main|gw)_(\S+)", body)}
        gh.add(tag, body, meta=_meta(tag[1:], roles))
    by_meta = {role: updates.next_release(role, meta_cache={}).tag for role in ("client", "gateway")}
    assert by_body == by_meta == {"client": "v3.1.0", "gateway": "v3.2.0"}, "минимум 3.1.0 держит основного"


# ── переход: читатель 3.2.x ──────────────────────────────────────────────────
# Разбор тела и выбор цели — дословно из тега v3.2.1 (awgbot/infra/updates.py).

_REQUIRES_RE = re.compile(r"#requires_(main|gw)_(v?\d+\.\d+\.\d+(?:\.\d+)?)\b")
_AUDIENCE_RE = re.compile(r"#(main_bot|gw_bot|all_bots)\b")
_ROLE_KEY = {"gateway": "gw"}
_ROLE_TAG = {"gateway": "gw_bot"}
_GENERATION_RE = re.compile(r"#awg_gen(\d+)\b")


@dataclass(frozen=True)
class _Release321:
    tag: str
    version: tuple
    body: str

    def awg_generation(self) -> int:
        m = _GENERATION_RE.search(self.body or "")
        return int(m.group(1)) if m else 0

    def requirements(self) -> dict:
        return {k: updates.parse_version(v) for k, v in _REQUIRES_RE.findall(self.body or "")}

    def requires(self, role: str = "") -> Optional[tuple]:
        return self.requirements().get(_ROLE_KEY.get(role, "main"))

    def audience(self) -> frozenset:
        req = self.requirements()
        if req:
            return frozenset({"main_bot" if k == "main" else "gw_bot" for k in req})
        return frozenset(_AUDIENCE_RE.findall(self.body or ""))

    def applies_to(self, role: str) -> bool:
        tags = self.audience()
        if not tags:
            return True
        return "all_bots" in tags or _ROLE_TAG.get(role, "main_bot") in tags


def _pick_321(installed: tuple, releases: list, role: str,
              max_generation: Optional[int] = None) -> Optional[_Release321]:
    newer = [r for r in releases if r.version > installed and r.applies_to(role)]
    if max_generation is not None:
        newer = [r for r in newer if r.awg_generation() <= max_generation]
    if not newer:
        return None
    target = newer[-1]
    while True:
        chain = [r for r in newer if r.version <= target.version]
        required = max((r.requires(role) for r in chain if r.requires(role)), default=None)
        if required is None or installed >= required:
            break
        lowered = next((r for r in chain if r.version >= required), None)
        if lowered is None or lowered.version == target.version:
            break
        target = lowered
    return target


def test_a_32x_reader_lands_on_330_and_the_metadata_reader_goes_on(gh, monkeypatch):
    """Ступень перехода: 3.3.0 и 3.3.1 несут и метаданные, и хэштеги
    минимумов в теле; с 3.3.2 тело — только для людей. Хост 3.2.1 видит
    3.3.2–3.4.0 «общими без минимума», но цепочка через 3.3.1 опускает его на
    3.3.0 — обе роли; с 3.3.0 цель выбирают уже метаданные."""
    gh.add("v3.2.1", "#requires_main_3.2.0\n#requires_gw_3.2.0\n#awg_gen1")
    gh.add("v3.3.0", "#requires_main_3.2.0\n#requires_gw_3.2.0\n#awg_gen1\n\n- адреса в туннеле",
           meta=_meta("3.3.0", BOTH_320))
    gh.add("v3.3.1", "#requires_main_3.3.0\n#requires_gw_3.3.0\n#awg_gen1\n\n- без шимов",
           meta=_meta("3.3.1", {"main": {"requires": "3.3.0", "needs_gw": "3.3.0"},
                                "gw": {"requires": "3.3.0", "needs_main": "3.3.0"}}))
    gh.add("v3.3.2", "- только для людей", meta=_meta("3.3.2", BOTH_330))
    gh.add("v3.4.0", "- только для людей", meta=_meta("3.4.0", {"main": {"requires": "3.3.1"},
                                                                "gw": {"requires": "3.3.1"}}))
    old = [_Release321(r["tag_name"], updates.parse_version(r["tag_name"]), r["body"]) for r in gh.releases]
    for role in ("client", "gateway"):
        assert _pick_321((3, 2, 1), old, role).tag == "v3.3.0", f"читатель 3.2.x ({role}) перепрыгнул ступень"
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.3.0")
    for role in ("client", "gateway"):
        assert updates.next_release(role, meta_cache={}).tag == "v3.3.1", \
            "с 3.3.0 — по метаданным: 3.4.0 требует 3.3.1"
