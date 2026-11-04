"""Потолки зоны резерва от размера базы основы, а не сжатого пакета: основа при сборке
пакета знает размер реплики (несжатой) и шлёт его необязательным полем `db_size` слова
`pkg` (util/pairproto.py OPTIONAL, runtime/pairlink `send_package` / inbound `_in_pkg`);
резерв хранит его в `rsv_pkg_in` (package.py `reserve_pkg_in`) и по нему ставит потолок
памяти процесса открытия (`rsvzone.open_memory_mb`, openproc `rsvopen_sandbox_props`),
размер хранилища гостя при монтировании до запуска (`rsvzone.store_mb` в
`_rsvopen_launch`) и строку памяти в карточке резерва (screen.py `_reserve_pkg_mem_view`).
Без поля (пакет прежней основы) — всё, как раньше, от размера пакета.

Пакет формата 2 (util/rsvpkg/seal): база — двоичная часть, JSON — только метаданные;
процесс открытия (tools/rsvopen.py) держит базу только сжатой и распаковывает её потоком
прямо в хранилище гостя — потолок 128 МБ + 4 × (пакет + снимок `rep.bin`) + 2 × база
(хранилище в cgroup записавшего и журнал SQLite поверх него), а не прежние 14 × база;
пик памяти меряется здесь же по настоящему коду сборки и открытия пакета. Снимок
`rep.bin` новее пакета (перезагрузка резерва посреди обслуживания) несёт базу гостя —
она больше ушедшей в пакете: потолок и хранилище при запуске — по большей из баз
(размер базы снимка в его открытом заголовке) и с размером файла снимка.

Цена ошибки: база в памяти процесса открытия целиком (hex, JSON, копии) — OOM посреди
аварийного переезда на сервере в 1 ГБ при базе от ~60 МБ — пользователи без VPN, когда
чинить поздно; потолок по базе пакета при открытии из большего снимка — тот же OOM после
перезагрузки резерва; карточка не предупреждает админа резерва о памяти, которую возьмёт переезд к
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

@pytest.mark.parametrize("pkg, db, rep, want", [
    (0, 0, 0, 192), (1 * MB, 0, 0, 192), (30 * MB, 0, 0, 128 + 120),          # базы не знаю — от пакета
    (1 * MB, 12 * MB, 0, 192), (1 * MB, 30 * MB, 0, 192),                     # маленькая база — прежний пол
    (1 * MB, 40 * MB, 0, 128 + 4 + 80), (16 * MB, 100 * MB, 0, 128 + 64 + 200),
    (2 * MB, 50 * MB + 1, 0, 128 + 8 + 102),
    (1 * MB, 40 * MB, 10 * MB, 128 + 44 + 80),                                 # снимок rep.bin — своим файлом
])
def test_the_open_ceiling_follows_the_package_the_snapshot_and_the_base(pkg, db, rep, want):
    assert rsvzone.open_memory_mb(pkg, db, rep) == want


def test_the_ceiling_no_longer_grows_fourteen_times_the_base():
    """База 100 МБ: прежний потолок — 128 + 3 × пакет + 14 × база (1,5 ГБ на сервере в 1 ГБ),
    новый — две базы и пакет."""
    assert rsvzone.open_memory_mb(2 * MB, 100 * MB) < 128 + 6 + 3 * 100


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


def _compressible(n: int) -> bytes:
    return (os.urandom(256) + b"\0" * 3840) * (n // 4096)


@pytest.mark.parametrize("label", ["1 месяц", "1 месяц 🙂"])
@pytest.mark.parametrize("path", ["package", "snapshot"])
@pytest.mark.parametrize("kind", ["compressible", "random"])
def test_the_ceiling_covers_what_the_open_process_really_holds(tmp_path, label, path, kind):
    """Пик памяти шагов процесса открытия с базой (open_parts, двоичная часть или снимок
    `rep.bin` — потоком в файл хранилища) по настоящему коду пакета: хорошо сжимаемая база
    40 МБ (пакет меньше двух мегабайт) и несжимаемая 10 МБ. Потолок покрывает пик с запасом
    не меньше 64 МБ на интерпретатор, а сам пик не держит базу целиком: распаковка — потоком.
    Широкие знаки в настройках (кириллица, эмодзи) пика больше не растят — база не в JSON."""
    db = _compressible(40 * MB) if kind == "compressible" else os.urandom(10 * MB)
    k, m = seal.new_k(), bytes(16)
    bot = {"conf": {"subscription": {"period_labels": {"month": label}}}, "app_guest": {}, "env": {}, "lists": {}}
    blob = seal.seal({"vpn": {}, "bot": bot}, k=k, m=m, pkg_gen=1, m_gen=1, blobs={seal.DB_PART: db})
    rep = seal.snapshot_seal(k, {"pkg_gen": 2, "at": ""}, db) if path == "snapshot" else b""
    if kind == "compressible":
        assert len(blob) < 2 * MB, "база сжалась хуже, чем нужно сценарию"
    out = tmp_path / "bot.db"

    def opening():
        pkg = bytes(blob)                                     # файл пакета — своя копия в памяти
        parts = seal.open_parts(pkg, k)
        assert "db" not in parts["bot"]
        if path == "snapshot":
            _meta, src = seal.snapshot_open(k, bytes(rep))
        else:
            src = seal.open_blob(pkg, k, seal.DB_PART)
        del pkg
        with open(out, "wb") as f:
            assert src.write_to(f) == len(db)
    peak = _peak_mb(opening)
    assert out.stat().st_size == len(db)
    ceiling = rsvzone.open_memory_mb(len(blob), len(db), len(rep))
    assert peak + 64 <= ceiling, (peak, ceiling)
    if kind == "compressible":
        assert peak < len(db) / MB / 4, f"распаковка держит базу в памяти: пик {peak:.1f} МБ при базе 40 МБ"
    else:
        assert peak <= 5 * len(db) / MB, f"больше пяти копий несжимаемой базы: {peak:.1f} МБ"


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
    db = seal.open_blob(bytes.fromhex(built["blob"]), paired.reserve_k())
    assert built["db_size"] == db.size == len(db.read()) > 0, built.get("db_size")


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
    assert f"MemoryMax={128 + 4 + 2 * 40}M" in openproc.rsvopen_sandbox_props(str(tmp_path), db_size=40 * MB)


def _rep_file(db_size: int) -> bytes:
    """`rep.bin` со сжимаемой базой `db_size` под чужим K: боту резерва нужен только заголовок."""
    blob = seal.snapshot_seal(seal.new_k(), {"pkg_gen": 9, "at": ""}, b"\0" * db_size)
    rsvzone.REP_PATH.parent.mkdir(parents=True, exist_ok=True)
    rsvzone.REP_PATH.write_bytes(blob)
    return blob


def test_the_sandbox_ceiling_counts_a_snapshot_whose_base_grew_during_service(tmp_path, monkeypatch):
    """Перезагрузка резерва посреди обслуживания: процесс открытия берёт базу из `rep.bin`, а она
    больше ушедшей в пакете — потолок по большей из баз и с файлом снимка, не по `db_size`."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "reserve").mkdir()
    with open(tmp_path / "reserve" / "guest.pkg", "wb") as f:
        f.truncate(1 * MB)
    rep = _rep_file(90 * MB)
    want = rsvzone.open_memory_mb(1 * MB, 90 * MB, len(rep))
    assert want == 128 + 4 * (1 + 1) + 2 * 90, want
    assert f"MemoryMax={want}M" in openproc.rsvopen_sandbox_props(str(tmp_path), db_size=40 * MB)
    assert rsvzone.rep_sizes() == (len(rep), 90 * MB)
    rsvzone.REP_PATH.write_bytes(b"\x00" * 100)
    assert rsvzone.rep_sizes() == (0, 0), "мусор вместо снимка — размер из чужих байт"


def test_the_launch_mounts_the_store_and_sets_the_ceiling_by_the_base_size(host):
    host.svc.rsv_update("rsv_pkg_in", db_size=40 * MB)
    assert host.svc.reserve_go_req_in(7, "button") == ""
    assert host.zone.store_sizes == [rsvzone.store_mb(40 * MB)], host.zone.store_sizes
    pkg = len(b"DUMMY-package")
    assert f"MemoryMax={rsvzone.open_memory_mb(pkg, 40 * MB)}M" in host.spawn.popen[0]["cmd"], host.spawn.popen[0]["cmd"]


def test_the_launch_mounts_the_store_by_the_snapshot_base_when_it_is_bigger(host):
    host.svc.rsv_update("rsv_pkg_in", db_size=40 * MB)
    rep = _rep_file(70 * MB)
    assert host.svc.reserve_go_req_in(7, "button") == ""
    assert host.zone.store_sizes == [rsvzone.store_mb(70 * MB)], host.zone.store_sizes
    pkg = len(b"DUMMY-package")
    assert f"MemoryMax={rsvzone.open_memory_mb(pkg, 70 * MB, len(rep))}M" in host.spawn.popen[0]["cmd"]


def test_the_launch_without_a_known_base_size_stays_by_the_package(host):
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
    assert need == 128 + 8 + 200 + 512, need
    assert d["pkg_mem"] == {"size": "0,1 ГБ", "need": "0,83 ГБ"}, d["pkg_mem"]
    assert "└ ⚠️ данные для переезда «" in card_text(d) and "понадобится около 0,83 ГБ RAM" in card_text(d), card_text(d)
