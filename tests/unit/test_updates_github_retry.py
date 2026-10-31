"""Запросы к GitHub (`awgbot.infra.updates.github._request`): сетевая неудача
(нет соединения, таймаут, обрыв) повторяется до `_ATTEMPTS` раз, ответ HTTP с
кодом ошибки — не повторяется. Повод: аплинк сервера терял часть SYN, и
ступень обновления падала на одном неудачном соединении.

Сеть — подмена `net4.urlopen` (опенер «только IPv4», которым ходит модуль)
контекстным менеджером с `.read()`;
пауза между попытками обнулена через модуль.
"""
import hashlib
import socket
import urllib.error

import pytest

from awgbot.infra import updates
from awgbot.infra.updates import github as gh

pytestmark = pytest.mark.unit

_URL = "https://api.github.com/repos/x/y/releases"
_ACCEPT = "application/vnd.github+json"


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _net(monkeypatch, outcomes):
    """urlopen по сценарию: очередной элемент outcomes — байты ответа или
    исключение. Возвращает список вызовов (по одному на попытку)."""
    calls = []
    queue = list(outcomes)

    def fake(req, timeout=None, data=None):
        calls.append(req)
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return _Resp(item)
    monkeypatch.setattr(gh.net4, "urlopen", fake)
    monkeypatch.setattr(gh, "_RETRY_PAUSE", 0)
    return calls


def _timeout_err():
    return urllib.error.URLError(socket.timeout("timed out"))


def test_network_failure_twice_then_success_returns_body(monkeypatch):
    """Аплинк потерял два SYN подряд, третье соединение удалось — ступень
    обновления не должна падать «сеть недоступна», когда сеть доступна."""
    calls = _net(monkeypatch, [_timeout_err(), _timeout_err(), b"ok-body"])
    assert gh._request(_URL, _ACCEPT) == b"ok-body", "тело с третьей попытки"
    assert len(calls) == 3, "две неудачи и один успех — ровно три соединения"


def test_three_network_failures_raise_with_last_error_text(monkeypatch):
    """Сеть действительно лежит: после _ATTEMPTS попыток — UpdateError с
    текстом последней ошибки, чтобы в уведомлении была причина, а не «что-то
    пошло не так»; и попыток не больше лимита — иначе запрос висит вечно."""
    last = urllib.error.URLError(ConnectionResetError("connection reset by peer"))
    calls = _net(monkeypatch, [_timeout_err(), _timeout_err(), last])
    with pytest.raises(updates.UpdateError) as ei:
        gh._request(_URL, _ACCEPT)
    assert "сеть недоступна (api.github.com): " in str(ei.value), "в тексте — хост запроса в скобках"
    assert "connection reset by peer" in str(ei.value), "в тексте — последняя ошибка, не первая"
    assert "timed out" not in str(ei.value), "в тексте — последняя ошибка, не первая"
    assert len(calls) == gh._ATTEMPTS == 3, "ровно _ATTEMPTS попыток, и это 3"


def test_http_error_is_not_retried(monkeypatch):
    """404/403 от GitHub — ответ сервера, а не потеря пакета: повторять его
    бессмысленно (и опасно по rate limit); сразу «GitHub HTTP <код>»."""
    err = urllib.error.HTTPError(_URL, 404, "Not Found", hdrs=None, fp=None)
    calls = _net(monkeypatch, [err, b"never"])
    with pytest.raises(updates.UpdateError) as ei:
        gh._request(_URL, _ACCEPT)
    assert "GitHub HTTP 404 (api.github.com): Not Found" in str(ei.value), "в тексте — код, хост в скобках и причина"
    assert len(calls) == 1, "ответ с кодом ошибки — без повторов"


def test_success_first_try_is_single_request(monkeypatch):
    """Норма: один запрос — одно соединение, никаких лишних обращений к API."""
    calls = _net(monkeypatch, [b"first"])
    assert gh._request(_URL, _ACCEPT) == b"first"
    assert len(calls) == 1
    req = calls[0]
    assert req.full_url == _URL
    assert req.get_header("Accept") == _ACCEPT, "заголовок Accept уходит в каждом запросе"


def test_retry_does_not_sleep_after_last_failure(monkeypatch):
    """Пауза — только между попытками: после последней неудачи ждать нечего,
    иначе ступень обновления тянет лишнюю секунду перед сообщением об ошибке."""
    naps = []
    import time
    monkeypatch.setattr(time, "sleep", lambda s: naps.append(s))
    monkeypatch.setattr(gh.net4, "urlopen",
                        lambda req, timeout=None, data=None: (_ for _ in ()).throw(_timeout_err()))
    monkeypatch.setattr(gh, "_RETRY_PAUSE", 0.25)
    with pytest.raises(updates.UpdateError):
        gh._request(_URL, _ACCEPT)
    assert naps == [0.25, 0.25], "пауз на одну меньше, чем попыток, и каждая — _RETRY_PAUSE"


def test_download_asset_survives_two_network_failures_and_checks_sha256(monkeypatch):
    """Скачивание поставки: первые два соединения оборвались, третье отдало
    архив — он сверяется с digest релиза и возвращается; подмена поставки
    при повторе всё так же ловится по sha256."""
    blob = b"tgz-bytes"
    rel = updates.Release(tag="v1.2.0", version=(1, 2, 0), body="",
                          asset_url="https://api/assets/v1.2.0",
                          sha256=hashlib.sha256(blob).hexdigest())
    calls = _net(monkeypatch, [_timeout_err(), _timeout_err(), blob])
    assert updates.download_asset(rel) == blob
    assert len(calls) == 3

    bad = updates.Release(tag="v1.2.0", version=(1, 2, 0), body="",
                          asset_url="https://api/assets/v1.2.0", sha256="a" * 64)
    _net(monkeypatch, [_timeout_err(), b"other-bytes"])
    with pytest.raises(updates.UpdateError, match="sha256 не совпал"):
        updates.download_asset(bad)


_ASSET_URL = "https://objects.githubusercontent.com/github-production-release-asset/1/2?X-Amz-Expires=300"


def test_the_host_in_the_error_is_the_one_of_the_request_not_the_api_by_default(monkeypatch):
    """Поставка качается не с api.github.com, а с objects.githubusercontent.com:
    в «сеть недоступна (…)» — хост именно этого запроса, иначе по журналу
    ищут неполадку не у того адреса (API отвечает, а CDN поставки — нет)."""
    _net(monkeypatch, [_timeout_err(), _timeout_err(), _timeout_err()])
    with pytest.raises(updates.UpdateError) as ei:
        gh._request(_ASSET_URL, "application/octet-stream")
    assert "сеть недоступна (objects.githubusercontent.com): " in str(ei.value), str(ei.value)
    assert "api.github.com" not in str(ei.value), "хост взят не из URL запроса, а по умолчанию"


def test_an_http_error_on_the_asset_names_the_asset_host(monkeypatch):
    """403 на скачивании поставки (истёкшая подпись ссылки) — «GitHub HTTP 403
    (objects.githubusercontent.com)», не api.github.com: это другой хост
    и другая причина."""
    err = urllib.error.HTTPError(_ASSET_URL, 403, "Forbidden", hdrs=None, fp=None)
    calls = _net(monkeypatch, [err, b"never"])
    with pytest.raises(updates.UpdateError) as ei:
        gh._request(_ASSET_URL, "application/octet-stream")
    assert str(ei.value) == "GitHub HTTP 403 (objects.githubusercontent.com): Forbidden", str(ei.value)
    assert len(calls) == 1


class _BrokenBody(_Resp):
    """Соединение есть, заголовки пришли, тело оборвалось посреди ассета."""

    def read(self):
        import http.client
        raise http.client.IncompleteRead(b"half", 1000)


def test_a_body_cut_mid_asset_is_retried_like_any_network_failure(monkeypatch):
    """Обрыв посреди тела (http.client.IncompleteRead — HTTPException, не
    OSError) — тот же сетевой сбой: повтор, а не ступень набора «не
    запустилась» с сырым текстом исключения человеку."""
    calls = []
    queue = [_BrokenBody(b""), _Resp(b"whole")]

    def fake(req, timeout=None, data=None):
        calls.append(req)
        return queue.pop(0)
    monkeypatch.setattr(gh.net4, "urlopen", fake)
    monkeypatch.setattr(gh, "_RETRY_PAUSE", 0)
    assert gh._request(_ASSET_URL, "application/octet-stream") == b"whole"
    assert len(calls) == 2

    monkeypatch.setattr(gh.net4, "urlopen", lambda req, timeout=None, data=None: _BrokenBody(b""))
    with pytest.raises(updates.UpdateError) as ei:
        gh._request(_ASSET_URL, "application/octet-stream")
    assert str(ei.value).startswith("сеть недоступна (objects.githubusercontent.com): "), str(ei.value)
