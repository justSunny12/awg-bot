"""Список выпусков постранично (`updates.list_releases`): GitHub отдаёт не больше
100 выпусков на страницу; установка, отставшая больше чем на 100 выпусков, не
нашла бы своей версии на первой странице — и с этого момента считалась бы
«нерелизной сборкой», то есть навсегда переставала обновляться. Стережём: своей
версии на первой странице нет — вторая по ссылке `rel="next"` из заголовка
Link (только https на api.github.com); есть — лишнего запроса из общего лимита
в 60 в час нет; двойник по тегу при сдвиге страниц не задваивает выпуск.

Сеть — подмена `updates._request`: ответ `github.Body` с заголовками, как у
настоящего `_request`; заголовки настоящего `_request` — подмена `net4.urlopen`.
"""
from __future__ import annotations

import json
from email.message import Message

import pytest

from awgbot.core import config as cfg
from awgbot.infra import updates
from awgbot.infra.updates import github as gh

pytestmark = pytest.mark.unit

_FIRST = f"{gh._API}/repos/{cfg.UPDATES_REPO}/releases?per_page=100"
_SECOND = f"{gh._API}/repos/{cfg.UPDATES_REPO}/releases?per_page=100&page=2"


def _rel(tag: str) -> dict:
    return {"tag_name": tag, "name": tag, "body": "", "draft": False, "prerelease": False, "assets": []}


def _body(items: list, link: str = "") -> gh.Body:
    b = gh.Body(json.dumps(items).encode())
    hdrs = Message()
    if link:
        hdrs["Link"] = link
    b.headers = hdrs
    return b


def _next_link(url: str = _SECOND) -> str:
    return f'<{url}>; rel="next", <{gh._API}/repos/x/y/releases?per_page=100&page=3>; rel="last"'


@pytest.fixture()
def net(monkeypatch):
    """Подставной GitHub: страницы по адресу, запросы записываются."""
    state = {"pages": {}, "calls": []}

    def request(url: str, accept: str) -> bytes:
        state["calls"].append(url)
        return state["pages"][url]
    monkeypatch.setattr(updates, "_request", request)
    return state


def _first_page(newest: int = 300) -> list:
    """Сто новейших выпусков v4.<n>.0, от newest вниз."""
    return [_rel(f"v4.{n}.0") for n in range(newest, newest - 100, -1)]


def test_installed_version_beyond_the_first_page_is_found_on_the_second(net, monkeypatch):
    """Отстали больше чем на 100 выпусков: своя версия — со второй страницы,
    установка остаётся релизной и получает цель обновления."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "4.150.0")
    net["pages"][_FIRST] = _body(_first_page(), _next_link())
    net["pages"][_SECOND] = _body([_rel("v4.200.0"), _rel("v4.150.0"), _rel("v4.149.0")])
    tags = [r.tag for r in updates.list_releases(fetch_meta=False)]
    assert net["calls"] == [_FIRST, _SECOND], net["calls"]
    assert "v4.150.0" in tags, "своя версия со второй страницы потеряна — «нерелизная сборка»"
    assert tags.count("v4.200.0") == 1, "хвост первой страницы при сдвиге задвоен"
    assert updates._is_release_build((4, 150, 0), updates.list_releases(fetch_meta=False))


def test_installed_version_on_the_first_page_costs_no_second_request(net, monkeypatch):
    """Своя версия уже есть — вторая страница не нужна: лимит API общий со
    шлюзами за NAT, лишний запрос на каждую проверку его съедает."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "4.250.0")
    net["pages"][_FIRST] = _body(_first_page(), _next_link())
    updates.list_releases(fetch_meta=False)
    assert net["calls"] == [_FIRST], net["calls"]


def test_local_build_of_a_release_on_the_first_page_costs_no_second_request(net, monkeypatch):
    """Локальная сборка X.Y.Z.N той же базы, что выпуск первой страницы, —
    релизная: второй страницы не просим."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "4.250.0.3")
    net["pages"][_FIRST] = _body(_first_page(), _next_link())
    updates.list_releases(fetch_meta=False)
    assert net["calls"] == [_FIRST], net["calls"]


def test_without_a_next_link_there_is_no_second_request(net, monkeypatch):
    """Выпусков меньше страницы — Link без rel="next" (или его нет): чужая
    сборка остаётся нерелизной без лишних запросов."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.0.0")
    net["pages"][_FIRST] = _body([_rel("v4.1.0"), _rel("v4.0.0")],
                                 f'<{gh._API}/repos/x/y/releases?per_page=100&page=1>; rel="first"')
    assert [r.tag for r in updates.list_releases(fetch_meta=False)] == ["v4.0.0", "v4.1.0"]
    assert net["calls"] == [_FIRST], net["calls"]


@pytest.mark.parametrize("url", [
    "http://api.github.com/repos/x/y/releases?page=2",
    "https://evil.example/repos/x/y/releases?page=2",
    "https://api.github.com.evil.example/releases?page=2",
])
def test_next_link_off_the_api_host_is_not_followed(net, monkeypatch, url):
    """Ссылка из ответа ведёт не на https://api.github.com — не идём: адрес
    второго запроса задаёт не ответ, а мы."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "4.150.0")
    net["pages"][_FIRST] = _body(_first_page(), _next_link(url))
    updates.list_releases(fetch_meta=False)
    assert net["calls"] == [_FIRST], net["calls"]


def test_second_page_network_failure_is_a_network_error(net, monkeypatch):
    """Вторая страница не пришла — сетевая ошибка, а не тихое «нерелизная
    сборка» по неполному списку."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "4.150.0")
    net["pages"][_FIRST] = _body(_first_page(), _next_link())

    def request(url, accept):
        net["calls"].append(url)
        if url == _FIRST:
            return net["pages"][_FIRST]
        raise updates.UpdateError("сеть недоступна (api.github.com): timed out")
    monkeypatch.setattr(updates, "_request", request)
    with pytest.raises(updates.UpdateError):
        updates.list_releases(fetch_meta=False)


def test_plain_bytes_from_a_substitute_request_mean_no_next_page(net, monkeypatch):
    """Подмена `_request`, отдающая голые байты (без заголовков), — одна
    страница, как раньше: ссылки на следующую нет."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "1.0.0")
    net["pages"][_FIRST] = json.dumps(_first_page()).encode()
    updates.list_releases(fetch_meta=False)
    assert net["calls"] == [_FIRST], net["calls"]


def test_real_request_returns_the_response_headers(monkeypatch):
    """Настоящий `_request` отдаёт тело вместе с заголовками ответа — без них
    ссылки на вторую страницу не прочитать; таймаут — модульный (≤ 10 с)."""
    hdrs = Message()
    hdrs["Link"] = _next_link()
    seen = {}

    class _Resp:
        headers = hdrs

        def read(self):
            return b"[]"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=None, data=None):
        seen["timeout"] = timeout
        return _Resp()
    monkeypatch.setattr(gh.net4, "urlopen", fake)
    body = gh._request(_FIRST, "application/vnd.github+json")
    assert body == b"[]"
    assert gh._next_page(body) == _SECOND
    assert seen["timeout"] is not None and seen["timeout"] <= 10, seen


def test_not_an_array_is_a_bad_answer(net, monkeypatch):
    """Ответ не массив (ошибка API объектом) — «некорректный ответ GitHub»,
    а не падение на разборе."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "4.0.0")
    net["pages"][_FIRST] = json.dumps({"message": "Not Found"}).encode()
    with pytest.raises(updates.UpdateError, match="некорректный ответ GitHub"):
        updates.list_releases(fetch_meta=False)
