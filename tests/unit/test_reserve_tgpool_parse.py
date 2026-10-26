"""Пул адресов Telegram для порта вебхука бота основы на резерве — чистая
часть (domain/services/reserve/tgpool.py): разбор ответа RIPEstat, самый
узкий анонс для адреса, покрытие ядра анонсами, запрос по одной AS; и
чтение динамического набора со счётчиками (infra/nftguard.live_set_counters).

Цена ошибки: снятый с анонса префикс в пуле — DNAT вебхука в бота основы
(с токеном и данными пользователей) открыт сети, которая Telegram больше не
принадлежит; IPv6 или мусор в пуле — nft не примет таблицу, и вебхук
закрыт вовсе; «ядро покрыто» при частичном покрытии — Т-РУ25 молчит, когда
страница Bot API устарела; «ядро потеряно» при полном покрытии мелкими
анонсами — ложная тревога админу резерва; сбой источника, принятый за
пустой анонс, — снимает всё выученное.
"""
from __future__ import annotations

import io
import json
import subprocess
import urllib.error

import pytest

from awgbot.domain.services.reserve import tgpool
from awgbot.infra import nftguard

pytestmark = pytest.mark.unit

CORE = list(nftguard.DEFAULT_TG_POOL)          # 149.154.160.0/20, 91.108.4.0/22 — константы кода


def _doc(prefixes, latest="2026-10-21T00:00:00"):
    """Ответ RIPEstat announced-prefixes: [(префикс, конец последнего отрезка)]."""
    return {"data": {"latest_time": latest,
                     "prefixes": [{"prefix": p, "timelines": [{"starttime": "2026-01-01T00:00:00", "endtime": e}]}
                                  for p, e in prefixes]}}


# ── разбор RIPEstat ──────────────────────────────────────────────────────────

def test_only_prefixes_announced_at_the_latest_time_are_taken():
    doc = _doc([("149.154.160.0/20", "2026-10-21T00:00:00"),
                ("203.0.113.0/24", "2026-09-01T00:00:00")])          # снят с анонса месяц назад
    assert tgpool.parse_ripestat(doc) == ["149.154.160.0/20"], "снятый с анонса префикс попал в пул"


def test_ipv6_and_garbage_are_dropped_and_duplicates_collapse():
    doc = _doc([("2001:db8::/32", "2026-10-21T00:00:00"), ("не префикс", "2026-10-21T00:00:00"),
                ("203.0.113.7/24", "2026-10-21T00:00:00"), ("203.0.113.0/24", "2026-10-21T00:00:00")])
    doc["data"]["prefixes"].append("строка вместо записи")
    assert tgpool.parse_ripestat(doc) == ["203.0.113.0/24"], "в набор ipv4_addr пошло бы то, что nft не примет"


@pytest.mark.parametrize("doc", [None, [], {}, {"data": None}, {"data": "x"}, {"data": {"prefixes": None}}])
def test_a_broken_answer_gives_an_empty_list(doc):
    assert tgpool.parse_ripestat(doc) == []


def test_without_timelines_the_prefix_is_taken():
    """Нет отрезков — сравнивать не с чем: префикс есть в ответе — значит анонс."""
    doc = {"data": {"latest_time": "2026-10-21T00:00:00", "prefixes": [{"prefix": "198.51.100.0/24"}]}}
    assert tgpool.parse_ripestat(doc) == ["198.51.100.0/24"]


# ── запрос одной AS ─────────────────────────────────────────────────────────

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_puts_the_asn_into_the_url_and_parses_the_answer(monkeypatch):
    urls = []

    def urlopen(url, timeout=None):
        urls.append((url, timeout))
        return _Resp(json.dumps(_doc([("91.108.4.0/22", "2026-10-21T00:00:00")])).encode())
    monkeypatch.setattr("awgbot.util.net4.urlopen", urlopen)
    got = tgpool.fetch_announced(62041, "https://stat.example.org/data.json?resource=AS{asn}")
    assert got == (["91.108.4.0/22"], "")
    assert urls == [("https://stat.example.org/data.json?resource=AS62041", tgpool.FETCH_TIMEOUT)], \
        "запрос без таймаута повесил бы медленный такт резерва"


@pytest.mark.parametrize("exc", [urllib.error.URLError("нет сети"), TimeoutError("timed out"), ValueError("x")])
def test_a_failing_source_is_an_error_not_an_empty_announce(monkeypatch, exc):
    """Ошибка источника отличима от «анонсов нет»: иначе такт снял бы всё выученное."""
    def urlopen(url, timeout=None):
        raise exc
    monkeypatch.setattr("awgbot.util.net4.urlopen", urlopen)
    prefixes, err = tgpool.fetch_announced(62041, "https://stat.example.org/?AS{asn}")
    assert prefixes == [] and err and type(exc).__name__ in err, err


def test_a_non_json_answer_is_an_error(monkeypatch):
    monkeypatch.setattr("awgbot.util.net4.urlopen", lambda url, timeout=None: _Resp(b"<html>502</html>"))
    prefixes, err = tgpool.fetch_announced(1, "https://stat.example.org/{asn}")
    assert prefixes == [] and err


# ── самый узкий анонс ───────────────────────────────────────────────────────

def test_narrowest_picks_the_longest_prefix_that_holds_the_address():
    prefixes = ["198.51.100.0/24", "198.51.100.0/25", "203.0.113.0/24"]
    assert tgpool.narrowest("198.51.100.9", prefixes) == "198.51.100.0/25"
    assert tgpool.narrowest("198.51.100.200", prefixes) == "198.51.100.0/24"
    assert tgpool.narrowest("203.0.113.9", prefixes) == "203.0.113.0/24"


@pytest.mark.parametrize("addr, prefixes", [("198.51.100.9", ["203.0.113.0/24"]), ("198.51.100.9", []),
                                            ("не адрес", ["203.0.113.0/24"]), ("198.51.100.9", ["мусор"])])
def test_narrowest_is_empty_when_nothing_holds_the_address(addr, prefixes):
    assert tgpool.narrowest(addr, prefixes) == ""


# ── покрытие ядра ───────────────────────────────────────────────────────────

def test_the_core_is_covered_by_a_wider_announce_or_by_its_parts():
    assert tgpool.core_covered(CORE, ["149.154.0.0/16", "91.108.4.0/22"]) == []
    parts = ["149.154.160.0/21", "149.154.168.0/21", "91.108.4.0/23", "91.108.6.0/23"]
    assert tgpool.core_covered(CORE, parts) == [], "ядро, покрытое мелкими анонсами, — ложная тревога"


def test_a_partly_announced_core_prefix_is_lost():
    lost = tgpool.core_covered(CORE, ["149.154.160.0/21", "91.108.4.0/22"])      # вторая половина /20 пропала
    assert lost == ["149.154.160.0/20"], lost


def test_without_announces_the_whole_core_is_lost_and_garbage_is_ignored():
    assert tgpool.core_covered(CORE, []) == CORE
    assert tgpool.core_covered(CORE, ["мусор", "91.108.4.0/22"]) == ["149.154.160.0/20"]


# ── счётчики динамического набора ───────────────────────────────────────────

def _nft_answer(doc, rc=0):
    return lambda args, **k: subprocess.CompletedProcess(args, rc, json.dumps(doc).encode(), b"")


def test_live_set_counters_reads_the_packets_of_each_address(monkeypatch):
    doc = {"nftables": [{"metainfo": {}}, {"set": {"name": nftguard.SET_TG_SEEN, "elem": [
        {"elem": {"val": "203.0.113.5", "expires": 500, "counter": {"packets": 4, "bytes": 240}}},
        {"elem": {"val": "198.51.100.9", "counter": {"packets": 1, "bytes": 60}}},
        "198.51.100.10"]}}]}
    calls = []

    def nft(args, **k):
        calls.append(args)
        return _nft_answer(doc)(args)
    monkeypatch.setattr(nftguard, "_nft", nft)
    assert nftguard.live_set_counters(nftguard.SET_TG_SEEN) == {"203.0.113.5": 4, "198.51.100.9": 1, "198.51.100.10": 0}
    assert calls[0][-1] == "tg_seen4" and calls[0][:3] == ["-j", "list", "set"]


def test_live_set_counters_of_a_missing_set_is_empty(monkeypatch):
    """Набора нет (зона не serving, таблица другой формы) — пусто, без исключения."""
    monkeypatch.setattr(nftguard, "_nft", _nft_answer({}, rc=1))
    assert nftguard.live_set_counters(nftguard.SET_TG_SEEN) == {}


def test_live_set_counters_on_broken_json_raises_the_guard_error(monkeypatch):
    monkeypatch.setattr(nftguard, "_nft", lambda args, **k: subprocess.CompletedProcess(args, 0, b"{oops", b""))
    with pytest.raises(nftguard.GuardError):
        nftguard.live_set_counters(nftguard.SET_TG_SEEN)
