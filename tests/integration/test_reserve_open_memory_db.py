"""Потолки зоны резерва от размера базы основы, а не сжатого пакета: основа при сборке
пакета знает размер реплики (несжатой) и шлёт его необязательным полем `db_size` слова
`pkg` (util/pairproto.py OPTIONAL, runtime/pairlink `send_package` / inbound `_in_pkg`);
резерв хранит его в `rsv_pkg_in` (package.py `reserve_pkg_in`) и по нему ставит потолок
памяти процесса открытия (`rsvzone.open_memory_mb`, openproc `rsvopen_sandbox_props`),
размер хранилища гостя при монтировании до запуска (`rsvzone.store_mb` в
`_rsvopen_launch`) и строку памяти в карточке резерва (screen.py `_reserve_pkg_mem_view`).
Без поля (пакет прежней основы) — всё, как раньше, от размера пакета.

Процесс открытия (tools/rsvopen.py) держит базу не сжатой: часть бота — JSON с hex базы
(2 знака на байт), распаковка держит байты JSON и строку из них, а строка растёт ступенями
по самому широкому знаку части (кириллица настроек — 2 байта на знак, эмодзи — 4); пик
памяти меряется здесь же по настоящему коду сборки и открытия пакета.

Цена ошибки: хорошо сжимаемая база больше «пакет + ~40 МБ» убивает процесс открытия по
OOM посреди аварийного переезда при любом хранилище — пользователи без VPN, когда чинить
поздно; карточка не предупреждает админа резерва о памяти, которую возьмёт переезд к
нему (пакет ≤ 16 МБ, порог 64 МБ — строка по пакету не появлялась никогда); новое поле
обязательным — прежний резерв рвал бы сессию на каждом пакете; мусор в поле — потолок
из чужого числа.
"""
from __future__ import annotations

import os
import tracemalloc
import types

import pytest

from awgbot.bot.texts.reserve.section import card_text
from awgbot.core import config
from awgbot.domain.services.reserve import openproc
from awgbot.infra import rsvzone
from awgbot.util import pairproto
from awgbot.util.rsvpkg import seal

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration.test_reserve_hosting import host
from tests.integration.test_reserve_package import _tick, clock, paired

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["host", "clock", "paired"]
MB = 1024 * 1024
PKG = {"gen": 1, "fp": "ab" * 32, "i": 0, "n": 1, "blob": ""}


# ── формула ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("pkg, db, want", [
    (0, 0, 192), (1 * MB, 0, 192), (30 * MB, 0, 218),          # базы не знаю — от пакета, как раньше
    (1 * MB, 1 * MB, 192), (1 * MB, 4 * MB, 192),              # маленькая база — прежний пол
    (1 * MB, 12 * MB, 128 + 3 + 14 * 12), (16 * MB, 100 * MB, 128 + 48 + 1400),
    (1 * MB, 12 * MB + 1, 128 + 3 + 14 * 13),
])
def test_the_open_ceiling_follows_the_base_when_its_size_is_known_and_the_package_otherwise(pkg, db, want):
    assert rsvzone.open_memory_mb(pkg, db) == want


@pytest.mark.parametrize("v, want", [(12 * MB, 12 * MB), (0, 0), (None, 0), (-5, 0), (True, 0), ("много", 0),
                                     ("123", 123), (rsvzone.DB_SIZE_CEIL + 1, 0)])
def test_a_base_size_that_is_garbage_counts_as_unknown(v, want):
    assert rsvzone.db_size_of(v) == want


def _peak_mb(fn) -> float:
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1] / MB
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("label", ["1 месяц", "1 месяц 🙂"])
@pytest.mark.parametrize("path", ["package", "snapshot"])
def test_the_ceiling_covers_what_the_open_process_really_holds_for_a_well_compressed_base(label, path):
    """Пик памяти шагов процесса открытия с базой (open_parts, hex → байты; путь снимка
    `rep.bin` — ещё и его распаковка) по настоящему коду пакета: хорошо сжимаемая база
    20 МБ в пакете меньше мегабайта. Потолок от базы покрывает пик с запасом не меньше
    64 МБ на интерпретатор; потолок от пакета (прежний) — нет."""
    db = (os.urandom(256) + b"\0" * 3840) * (20 * MB // 4096)
    k, m = seal.new_k(), bytes(16)
    bot = {"conf": {"subscription": {"period_labels": {"month": label}}}, "app_guest": {}, "env": {}, "lists": {},
           "db": db.hex()}
    blob = seal.seal({"vpn": {}, "bot": bot}, k=k, m=m, pkg_gen=1, m_gen=1)
    rep = seal.snapshot_seal(k, {"pkg_gen": 2, "at": "", "db": db.hex()})
    del bot
    assert len(blob) < MB, "база сжалась хуже, чем нужно сценарию"

    def opening():
        parts = seal.open_parts(bytes(blob), k)               # файл пакета — своя копия в памяти
        if path == "snapshot":
            snap = seal.snapshot_open(k, bytes(rep))
            got = bytes.fromhex(snap["db"])
            del snap
        else:
            got = bytes.fromhex(parts["bot"]["db"])
        assert len(got) == len(db)
    peak = _peak_mb(opening)
    assert peak + 64 <= rsvzone.open_memory_mb(len(blob), len(db)), (peak, rsvzone.open_memory_mb(len(blob), len(db)))
    assert peak + 64 > rsvzone.open_memory_mb(len(blob)), "сценарий не воспроизводит недобор потолка по пакету"


# ── канал пары: поле необязательное ─────────────────────────────────────────

def test_the_base_size_is_an_optional_integer_of_the_pkg_word():
    assert "db_size" in pairproto.PAIR_KINDS["pkg"][1] and "db_size" in pairproto.OPTIONAL["pkg"], \
        "размер базы не необязательное поле — прежняя основа рвала бы сессию новому резерву"
    assert "db_size" not in pairproto.check(pairproto.PAIR_PROTO, "pkg", dict(PKG), pairproto.INVITER), "слово прежней основы"
    assert pairproto.check(pairproto.PAIR_PROTO, "pkg", {**PKG, "db_size": 5 * MB}, pairproto.INVITER)["db_size"] == 5 * MB
    for bad in (True, "5", 1.5):
        with pytest.raises(pairproto.ProtocolError):
            pairproto.check(pairproto.PAIR_PROTO, "pkg", {**PKG, "db_size": bad}, pairproto.INVITER)


def test_a_reserve_of_the_previous_release_drops_the_new_field_instead_of_breaking_the_session(monkeypatch):
    """Прежний резерв знает `pkg` без `db_size`: лишнее поле разбор отбрасывает молча."""
    old = {**pairproto.PAIR_KINDS["pkg"][1]}
    old.pop("db_size")
    monkeypatch.setitem(pairproto.PAIR_KINDS, "pkg", (pairproto.PAIR_KINDS["pkg"][0], old))
    got = pairproto.check(pairproto.PAIR_PROTO, "pkg", {**PKG, "db_size": 5 * MB}, pairproto.INVITER)
    assert got == PKG, got


# ── основа: размер базы со сборкой ──────────────────────────────────────────

def test_the_primary_sends_the_size_of_the_replica_that_went_into_the_package(paired, make_active_client, clock):
    make_active_client(name="Профиль", tg_id=7001)
    built = _tick(paired, clock)
    parts = seal.open_parts(bytes.fromhex(built["blob"]), paired.reserve_k())
    assert built["db_size"] == len(bytes.fromhex(parts["bot"]["db"])) > 0, built.get("db_size")


async def test_the_size_rides_with_every_chunk_and_reaches_the_reserve_and_an_old_chunk_passes_without_it(monkeypatch):
    from awgbot.runtime import pairlink
    from awgbot.runtime.pairlink import inbound
    sent = []

    async def fake_send(kind, body=None):
        sent.append(body)
        return True
    monkeypatch.setattr(pairlink, "send", fake_send)
    monkeypatch.setattr(pairproto, "MAX_BLOB", 64 * 1024)     # несколько частей
    blob = seal.seal({"vpn": {}}, k=seal.new_k(), m=bytes(16), pkg_gen=7, m_gen=1)
    assert await pairlink.send_package({"gen": 7, "fp": seal.digest(blob), "blob": blob.hex(), "db_size": 3 * MB})
    assert len(sent) > 1 and all(b["db_size"] == 3 * MB for b in sent), "часть пакета без размера базы"

    calls = []

    def reserve_pkg_in(gen, fp, blob, jrn=None, **kw):
        calls.append(kw)
        return "pkg_ok", ""

    async def fake_line(sess, kind, body=None):
        pass
    monkeypatch.setattr(inbound, "send_line", fake_line)
    obj = types.SimpleNamespace(services=types.SimpleNamespace(reserve_pkg_in=reserve_pkg_in, reserve_st_body=lambda: {}))
    sess = types.SimpleNamespace(parts={})
    for body in sent:
        await inbound.InboundMixin._in_pkg(obj, sess, dict(body))
    await inbound.InboundMixin._in_pkg(obj, types.SimpleNamespace(parts={}),
                                       {k: v for k, v in sent[0].items() if k != "db_size"} | {"n": 1, "i": 0})
    assert calls == [{"db_size": 3 * MB}, {}], calls


# ── резерв: запись, потолки, хранилище ──────────────────────────────────────

def test_the_reserve_keeps_the_base_size_of_the_package_and_forgets_it_with_an_old_one(services, rsv, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    rc.pair_row(services)
    blob = seal.seal({"vpn": {}}, k=seal.new_k(), m=bytes(16), pkg_gen=7, m_gen=1)
    assert services.reserve_pkg_in(7, seal.digest(blob), blob, None, db_size=40 * MB) == ("pkg_ok", "")
    assert services.rsv_get("rsv_pkg_in")["db_size"] == 40 * MB
    assert services.reserve_pkg_in(7, seal.digest(blob), blob, None) == ("pkg_ok", ""), "пакет прежней основы"
    assert services.rsv_get("rsv_pkg_in")["db_size"] == 0, "размер базы прежнего пакета остался у нового"
    assert services.reserve_pkg_in(7, seal.digest(blob), blob, None, db_size=-1) == ("pkg_ok", "")
    assert services.rsv_get("rsv_pkg_in")["db_size"] == 0, "мусор записан как размер базы"


def test_the_sandbox_ceiling_is_taken_from_the_base_size_and_from_the_package_without_it(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "reserve").mkdir()
    with open(tmp_path / "reserve" / "guest.pkg", "wb") as f:
        f.truncate(1 * MB)
    assert "MemoryMax=192M" in openproc.rsvopen_sandbox_props(str(tmp_path)), "без размера базы — по пакету"
    assert f"MemoryMax={128 + 3 + 14 * 40}M" in openproc.rsvopen_sandbox_props(str(tmp_path), db_size=40 * MB)


def test_the_launch_mounts_the_store_and_sets_the_ceiling_by_the_base_size(host):
    host.svc.rsv_update("rsv_pkg_in", db_size=40 * MB)
    assert host.svc.reserve_go_req_in(7, "button") == ""
    assert host.zone.store_sizes == [rsvzone.store_mb(40 * MB)], host.zone.store_sizes
    pkg = len(b"DUMMY-package")
    assert f"MemoryMax={rsvzone.open_memory_mb(pkg, 40 * MB)}M" in host.spawn.popen[0]["cmd"], host.spawn.popen[0]["cmd"]


def test_the_launch_of_a_package_from_an_old_primary_stays_by_the_package(host):
    assert host.svc.reserve_go_req_in(7, "button") == ""
    pkg = len(b"DUMMY-package")
    assert host.zone.store_sizes == [rsvzone.store_mb(pkg)], host.zone.store_sizes
    assert f"MemoryMax={rsvzone.open_memory_mb(pkg)}M" in host.spawn.popen[0]["cmd"]


# ── карточка резерва ────────────────────────────────────────────────────────

def test_the_card_warns_by_the_base_size_when_the_primary_sends_it(services, rsv):
    rc.pair_row(services)
    services.rsv_put("rsv_pkg_in", {"gen": 7, "fp": "DUMMY-fp", "size": 2 * MB, "db_size": 64 * MB})
    assert "памяти" not in card_text(services.reserve_card_data()), "база 64 МБ ровно — ещё без строки"
    services.rsv_put("rsv_pkg_in", {"gen": 7, "fp": "DUMMY-fp", "size": 2 * MB, "db_size": 100 * MB})
    d = services.reserve_card_data()
    need = rsvzone.open_memory_mb(2 * MB, 100 * MB) + rsvzone.guest_memory_mb(100 * MB)
    assert need == 128 + 6 + 1400 + 512, need
    assert d["pkg_mem"] == {"size": "0,1 ГБ", "need": "2 ГБ"}, d["pkg_mem"]
    assert "└ ⚠️ данные для переезда «" in card_text(d) and "понадобится около 2 ГБ памяти" in card_text(d), card_text(d)
