"""GitHub и лимит API: ассеты качаются по прямой ссылке
`browser_download_url` (releases/download/…, в лимит API не считается), API-адрес
`url` — только когда прямой нет; исчерпанный лимит (403/429 с
`x-ratelimit-remaining: 0`) называется словами со временем сброса, а не
«GitHub HTTP 403: rate limit exceeded». Повод: ВПС упёрся в 60 запросов в
час — адрес общий со шлюзами за NAT.

Сеть — подмена `updates._request` (список выпусков и ассеты) или
`net4.urlopen` (для `_request`).
"""
from __future__ import annotations

import hashlib
import json
import urllib.error
from email.message import Message
from zoneinfo import ZoneInfo

import pytest

from awgbot.core import config as cfg
from awgbot.infra import updates
from awgbot.infra.updates import github as gh
from awgbot.util import timeutil as t

pytestmark = pytest.mark.unit

DIRECT = "https://github.com/justSunny12/awg-bot/releases/download/v4.0.1/awg-bot.tgz"
DIRECT_META = "https://github.com/justSunny12/awg-bot/releases/download/v4.0.1/awg-bot-release.json"
API = "https://api.github.com/repos/justSunny12/awg-bot/releases/assets/101"
API_META = "https://api.github.com/repos/justSunny12/awg-bot/releases/assets/102"
_RESET = 1761000000                       # 2026-10-20 22:40 UTC


def _meta_blob(tag: str) -> bytes:
    return json.dumps({"schema": 1, "version": tag[1:], "awg_gen": 1,
                       "roles": {"main": {"requires": "4.0.0"}, "gw": {"requires": "4.0.0"}}}).encode()


def _release(tag: str, *, direct: bool) -> dict:
    """Ответ API: ассеты с прямой ссылкой (как отдаёт GitHub) или только с API-адресом."""
    meta = _meta_blob(tag)
    tgz = {"name": cfg.UPDATES_ASSET_NAME, "url": API, "digest": "sha256:" + "a" * 64}
    mj = {"name": cfg.UPDATES_META_ASSET_NAME, "url": API_META, "size": len(meta),
          "digest": "sha256:" + hashlib.sha256(meta).hexdigest()}
    if direct:
        tgz["browser_download_url"] = DIRECT
        mj["browser_download_url"] = DIRECT_META
    return {"tag_name": tag, "name": tag, "body": "", "draft": False, "prerelease": False, "assets": [tgz, mj]}


@pytest.fixture()
def net(monkeypatch):
    """Подставной GitHub: список выпусков и ассеты метаданных по любому из адресов;
    запросы записываются."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "4.0.0")
    state = {"releases": [], "calls": []}
    blobs = {DIRECT_META: _meta_blob("v4.0.1"), API_META: _meta_blob("v4.0.1")}

    def request(url: str, accept: str) -> bytes:
        state["calls"].append(url)
        if url.endswith("/releases"):
            return json.dumps(state["releases"]).encode()
        return blobs[url]
    monkeypatch.setattr(updates, "_request", request)
    return state


def test_the_asset_url_is_the_direct_link_when_github_gives_one(net):
    """Поставка качается по releases/download/…, не через API-адрес: иначе каждая
    ступень тратит запрос из общего на адрес лимита в 60 в час."""
    net["releases"] = [_release("v4.0.0", direct=True), _release("v4.0.1", direct=True)]
    rel = {r.tag: r for r in updates.list_releases()}
    assert rel["v4.0.1"].asset_url == DIRECT, rel["v4.0.1"].asset_url
    assert rel["v4.0.1"].sha256 == "a" * 64, "digest ассета при переходе на прямую ссылку потерян"


def test_without_a_direct_link_the_api_url_is_kept(net):
    """Ответ API без browser_download_url (подставные фикстуры, нестандартный
    ответ) — прежний API-адрес, а не пустой asset_url, с которым поставка
    «не найдена»."""
    net["releases"] = [_release("v4.0.0", direct=False), _release("v4.0.1", direct=False)]
    rel = {r.tag: r for r in updates.list_releases()}
    assert rel["v4.0.1"].asset_url == API, rel["v4.0.1"].asset_url
    assert rel["v4.0.1"].meta is not None and rel["v4.0.1"].meta_bad == "", "метаданные по API-адресу не прочитаны"
    assert API_META in net["calls"] and DIRECT_META not in net["calls"], net["calls"]


def test_release_metadata_are_fetched_by_the_direct_link(net):
    """Метаданные выпуска (awg-bot-release.json) — тоже по прямой ссылке:
    проверка обновлений ходит за ними на каждый новый тег, это второй
    потребитель лимита."""
    net["releases"] = [_release("v4.0.0", direct=True), _release("v4.0.1", direct=True)]
    rel = {r.tag: r for r in updates.list_releases()}
    assert rel["v4.0.1"].meta is not None and rel["v4.0.1"].meta_bad == "", rel["v4.0.1"].meta_bad
    assert DIRECT_META in net["calls"], net["calls"]
    assert API_META not in net["calls"], "метаданные ушли через API-адрес, хотя прямая ссылка есть"


def test_the_asset_helper_prefers_the_direct_link_and_handles_an_asset_without_any():
    assert gh._asset_url({"url": API, "browser_download_url": DIRECT}) == DIRECT
    assert gh._asset_url({"url": API}) == API
    assert gh._asset_url({"url": API, "browser_download_url": ""}) == API, "пустая прямая ссылка — откат на API"
    assert gh._asset_url({"name": "x"}) is None


# ── лимит запросов ──────────────────────────────────────────────────────────

def _http(code: int, reason: str, headers: dict | None = None) -> urllib.error.HTTPError:
    hdrs = Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    return urllib.error.HTTPError("https://api.github.com/x", code, reason, hdrs=hdrs, fp=None)


@pytest.mark.parametrize("code", [403, 429])
def test_an_exhausted_rate_limit_is_told_with_the_reset_time_in_the_ui_zone(monkeypatch, code):
    """403/429 с remaining=0 — «лимит запросов к GitHub исчерпан — сброс в
    HH:MM (UTC+5)»: администратор видит, когда снова можно, вместо «rate limit
    exceeded» без времени. Время — в поясе показа."""
    monkeypatch.setattr(t, "TZ", ZoneInfo("Asia/Yekaterinburg"))
    err = _http(code, "rate limit exceeded", {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(_RESET)})
    assert gh._http_error(err, "api.github.com") == "лимит запросов к GitHub исчерпан — сброс в 03:40 (UTC+5)"
    monkeypatch.setattr(t, "TZ", ZoneInfo("Europe/Berlin"))
    assert gh._http_error(err, "api.github.com").endswith("сброс в 00:40 (UTC+2)"), "время не в поясе показа"


def test_a_403_without_rate_limit_headers_is_the_plain_http_text():
    """403 по другой причине (нет заголовков лимита или remaining не ноль) —
    прежний «GitHub HTTP 403 (host): reason», не ложный «лимит исчерпан»."""
    assert gh._http_error(_http(403, "Forbidden"), "objects.githubusercontent.com") == \
        "GitHub HTTP 403 (objects.githubusercontent.com): Forbidden"
    err = _http(403, "Forbidden", {"x-ratelimit-remaining": "17", "x-ratelimit-reset": str(_RESET)})
    assert gh._http_error(err, "api.github.com") == "GitHub HTTP 403 (api.github.com): Forbidden", \
        "лимит не исчерпан, а текст про лимит"
    err = _http(403, "Forbidden", {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "soon"})
    assert gh._http_error(err, "api.github.com") == "GitHub HTTP 403 (api.github.com): Forbidden", \
        "сброс не число — текст про лимит без времени"


def test_a_404_with_zero_remaining_is_still_a_404():
    """Лимит — только 403/429: 404 с нулевым remaining (хвост прошлого ответа) —
    это «нет такого выпуска», а не лимит."""
    err = _http(404, "Not Found", {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(_RESET)})
    assert gh._http_error(err, "api.github.com") == "GitHub HTTP 404 (api.github.com): Not Found"


def test_a_rate_limited_request_is_not_retried(monkeypatch):
    """Исчерпанный лимит — ответ сервера: повтор его только усугубляет;
    `_request` отдаёт UpdateError с текстом лимита после одного соединения."""
    monkeypatch.setattr(t, "TZ", ZoneInfo("Asia/Yekaterinburg"))
    monkeypatch.setattr(gh, "_RETRY_PAUSE", 0)
    calls = []

    def fake(req, timeout=None, data=None):
        calls.append(req)
        raise _http(403, "rate limit exceeded", {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(_RESET)})
    monkeypatch.setattr(gh.net4, "urlopen", fake)
    with pytest.raises(updates.UpdateError, match="^лимит запросов к GitHub исчерпан — сброс в 03:40 \\(UTC\\+5\\)$"):
        gh._request("https://api.github.com/repos/x/y/releases", "application/vnd.github+json")
    assert len(calls) == 1, "ответ с лимитом повторяется"
