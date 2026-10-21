"""Правки границ и секретов резерва после двух ревью: токен в пакете одной
строкой (tools/rsvopen.py), реплика без локальных сетей шлюзов и строки
пары (util/rsvpkg/bot.py), ключи сторон зоны не в бэкап
(domain/backup/archive.py), фильтр вебхука по адресу сокета
(runtime/webhook.py), мусор от подписанного собеседника рвёт гостевой канал
(runtime/guestlink.py), одна сборка пакета на сессию
(runtime/pairlink/inbound.py), перепроверка getWebhookInfo в ожидании права
отвечать (runtime/pollctl.py), отметка `tgpool refresh` (tgpool.py), демпфер
после перезагрузки хоста (rfblock.py).

Цена ошибки: токен с переводом строки — в env юнита гостя дописывается
строка, которую положил собеседник (ADMIN_ID, путь, что угодно); локальные
сети шлюзов основы в реплике — резерв (другой человек) видит схему чужой
локальной сети; строка пары в реплике — бот основы на резерве считает себя
в паре со вторым сервером; PSK стороны зоны в бэкапе — из копии выводится
ключ гостевого канала; X-Forwarded-For, принятый за адрес, — в бота основы
на резерве стучится кто угодно; гостевой канал, глотающий мусор, — чужой
с ключом шлёт что угодно бесконечно; две сборки пакета в памяти сессии —
память резерва под чужой пакет вдвое; вебхук, пропавший без `wake`, — бот
основы молчит, пока его не перезапустят.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import types

import pytest

from awgbot.core import config
from awgbot.domain.backup import archive
from awgbot.domain.services.reserve import tgpool
from awgbot.infra import rsvzone
from awgbot.runtime import guestlink, pollctl, webhook
from awgbot.runtime.pairlink.inbound import InboundMixin
from awgbot.util.rsvpkg import bot as pkgbot

from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import box, sent
from tests.integration.test_reserve_pollctl import _update, lab, paired
from tests.integration.test_reserve_tgpool import pool
from tests.integration.test_rsvopen import TOKEN
from tests.integration.test_rsvopen import box as opener
from tests.integration.test_rsvpkg_bot import _open, live

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["box", "lab", "live", "opener", "paired", "pool"]


# ── токен в пакете — одной строкой ──────────────────────────────────────────

@pytest.mark.parametrize("token", [TOKEN + "\nADMIN_ID=1", TOKEN + " x", "\t" + TOKEN, "1:" + "A" * 140],
                         ids=["newline", "space", "tab", "too-long"])
def test_a_token_that_is_not_one_short_line_fails_before_the_store_is_written(opener, token):
    opener.write_package(env={"BOT_TOKEN": token, "ADMIN_ID": 77})
    assert opener.open(shares=opener.shares(1)) == 1
    assert "токен бота в пакете не того вида" in opener.out, opener.out
    assert not (rsvzone.RUN_DIR / "env").exists(), "env гостя записан с подложенной строкой"
    assert "take" not in opener.kinds()


def test_a_normal_token_still_opens(opener):
    opener.write_package(env={"BOT_TOKEN": TOKEN, "ADMIN_ID": 77})
    assert opener.open(shares=opener.shares(1)) == 0, opener.out
    assert (rsvzone.RUN_DIR / "env").read_text(encoding="utf-8") == f"BOT_TOKEN={TOKEN}\nADMIN_ID=77\n"


# ── реплика без локальных сетей шлюзов и строки пары ────────────────────────

def test_the_replica_carries_no_lan_of_the_gateways_and_no_pair_row(live, services, rsv, tmp_path):
    rc.pair_row(services)
    with sqlite3.connect(live.path) as con:
        con.execute("UPDATE gateways SET lan_subnets = '[\"192.168.50.0/24\"]', label = 'кладовка', lan_mode = 1")
    src = sqlite3.connect(live.path)
    assert src.execute("SELECT COUNT(*) FROM reserve_peer").fetchone()[0] == 1, "контроль: строка пары есть"
    con = _open(pkgbot.replica(live.path), tmp_path)
    rows = con.execute("SELECT lan_subnets, label, lan_mode FROM gateways").fetchall()
    assert rows and all(r == ("[]", "", 0) for r in rows), f"локальная сеть шлюза основы доехала до резерва: {rows}"
    assert con.execute("SELECT COUNT(*) FROM reserve_peer").fetchone()[0] == 0, "строка пары доехала в реплику"
    assert src.execute("SELECT lan_subnets FROM gateways").fetchone()[0] == '["192.168.50.0/24"]', \
        "живая база тронута сборкой реплики"


def test_the_strip_cleans_gateways_and_the_pair_row_even_without_the_key_column():
    """Чистка локальных сетей и строки пары не зависит от колонки закрытых
    ключей устройств (база прежней схемы, частичная)."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE gateways (id INTEGER PRIMARY KEY, lan_subnets TEXT, label TEXT, lan_mode INTEGER)")
    con.execute("INSERT INTO gateways VALUES (1, '[\"192.168.50.0/24\"]', 'кладовка', 1)")
    con.execute("CREATE TABLE reserve_peer (id INTEGER PRIMARY KEY, name TEXT)")
    con.execute("INSERT INTO reserve_peer VALUES (1, 'Beta')")
    pkgbot._strip(con)
    assert con.execute("SELECT lan_subnets, label, lan_mode FROM gateways").fetchall() == [("[]", "", 0)]
    assert con.execute("SELECT COUNT(*) FROM reserve_peer").fetchone()[0] == 0


# ── бэкап: ключи сторон зоны ────────────────────────────────────────────────

LINKS = {"1": {"cidr": "10.99.97.0/30", "pub": "DUMMY-pub", "priv": "DUMMY-priv", "psk": "DUMMY-psk", "port": 45901}}


def _state(pkg_in):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE server_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("INSERT INTO server_state VALUES ('rsv_guest_links', ?)", (json.dumps(LINKS),))
    if pkg_in is not None:
        con.execute("INSERT INTO server_state VALUES ('rsv_pkg_in', ?)", (pkg_in,))
    return con


def _links(con) -> dict:
    return json.loads(con.execute("SELECT value FROM server_state WHERE key = 'rsv_guest_links'").fetchone()[0])


@pytest.mark.parametrize("pkg_in", [json.dumps({"gen": 4, "share": "DUMMY-share"}), json.dumps({"gen": 4}), "", None],
                         ids=["with-share", "without-share", "dropped", "never"])
def test_the_keys_of_the_zone_sides_do_not_go_into_the_backup(pkg_in):
    """Пакет второй стороны снят (после возврата rsv_pkg_in пуст) или ещё не
    приходил — стороны линков шлюзов в зоне от этого не исчезают, и их ключи
    в копию идти не должны всё равно."""
    con = _state(pkg_in)
    archive._strip_foreign_share(con)
    rec = _links(con)["1"]
    assert "priv" not in rec and "psk" not in rec, f"ключи стороны зоны в бэкапе (rsv_pkg_in={pkg_in!r}): {rec}"
    assert (rec["cidr"], rec["pub"], rec["port"]) == ("10.99.97.0/30", "DUMMY-pub", 45901), "вычищено лишнее"


# ── фильтр вебхука по адресу сокета ─────────────────────────────────────────

def _request(peer, xff: str = ""):
    transport = types.SimpleNamespace(get_extra_info=lambda k: peer if k == "peername" else None) if peer else None
    return types.SimpleNamespace(transport=transport, headers={"X-Forwarded-For": xff} if xff else {})


async def _through(peer, xff=""):
    from aiogram.webhook.security import IPFilter
    mw = webhook._peer_filter(IPFilter(["149.154.160.0/20", "91.108.4.0/22"]))
    seen = []

    async def handler(req):
        seen.append(req)
        return "ok"
    return await mw(_request(peer, xff), handler), seen


async def test_a_telegram_socket_address_passes():
    res, seen = await _through(("149.154.167.50", 41000))
    assert res == "ok" and seen


@pytest.mark.parametrize("peer, xff", [(("198.51.100.9", 41000), "149.154.167.50"), (("203.0.113.7", 1), ""),
                                       (None, "149.154.167.50")], ids=["forged-xff", "stranger", "no-socket"])
async def test_a_foreign_socket_address_is_401_whatever_the_header_says(peer, xff):
    from aiohttp import web
    with pytest.raises(web.HTTPUnauthorized):
        await _through(peer, xff)


# ── гостевой канал: мусор рвёт сессию ───────────────────────────────────────

async def test_a_message_that_does_not_parse_breaks_the_guest_session():
    calls = []
    svc = types.SimpleNamespace(reserve_guest_in=lambda *a: calls.append(a) or [])
    client = guestlink.GuestClient(svc)
    with pytest.raises(ConnectionError):
        await client._handle({"t": "нет-такого", "seq": 1})
    assert calls == [], "мусор дошёл до сервисов"


# ── пакет: одна сборка на сессию ────────────────────────────────────────────

async def test_a_package_of_a_new_generation_drops_the_unfinished_one():
    obj = types.SimpleNamespace(services=None)
    sess = types.SimpleNamespace(parts={("rep", 7): ["aa", None]})
    body = lambda gen, i: {"gen": gen, "fp": "x", "i": i, "n": 2, "blob": "ab", "share": "", "m_gen": 1}  # noqa: E731
    await InboundMixin._in_pkg(obj, sess, body(5, 0))
    await InboundMixin._in_pkg(obj, sess, body(6, 0))
    assert 5 not in sess.parts and 6 in sess.parts, f"недособранный пакет прежнего поколения в памяти: {sess.parts}"
    assert ("rep", 7) in sess.parts, "сборка реплики снесена сборкой пакета"
    await InboundMixin._in_pkg(obj, sess, body(6, 0))
    assert list(k for k in sess.parts if not isinstance(k, tuple)) == [6]


# ── поллинг: перепроверка права отвечать ────────────────────────────────────

async def test_a_webhook_gone_without_wake_is_noticed_by_the_recheck(lab, paired, monkeypatch):
    """Вебхук резерва снят, а слова «wake» не было (обрыв канала пары):
    основа сама перепроверяет getWebhookInfo и снова поллит."""
    monkeypatch.setattr(pollctl, "RECHECK_S", 0.3)
    monkeypatch.setattr(pollctl.random, "uniform", lambda a, b: 1.0)
    lab.webhook()
    lab.mock.inject(_update("после"))
    lab.run()
    assert await lab.until(lambda: paired.reserve_poll() == "away")
    lab.mock.delete_webhook(False)
    assert await lab.until(lambda: lab.handled == ["после"], timeout=4), "без wake поллинг не вернулся"


async def test_without_the_recheck_the_polling_waits_for_wake(lab, paired, monkeypatch):
    """Контроль предыдущего: с долгой перепроверкой без wake — ждёт."""
    monkeypatch.setattr(pollctl, "RECHECK_S", 3600)
    lab.webhook()
    lab.mock.inject(_update("после"))
    lab.run()
    assert await lab.until(lambda: paired.reserve_poll() == "away")
    lab.mock.delete_webhook(False)
    await asyncio.sleep(1.0)
    assert lab.handled == []


# ── tgpool refresh ──────────────────────────────────────────────────────────

def test_the_refresh_mark_makes_the_announces_due_once_and_is_removed(pool):
    s = pool
    s.reserve_tg_pool_due(1000.0)                           # заводит срок по расписанию
    assert s.reserve_tg_pool_due(1001.0) is False, "контроль: по расписанию ещё рано"
    flag = tgpool.pool_path().with_name("tg_pool.refresh")
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("")
    assert s.reserve_tg_pool_due(1002.0) is True, "отметка `awg-bot reserve tgpool refresh` не сработала"
    assert not flag.exists(), "отметка не снята — анонсы на каждом такте"
    assert s.reserve_tg_pool_due(1003.0) is False


def test_the_refresh_mark_without_a_pair_does_nothing(services, rsv):
    flag = tgpool.pool_path().with_name("tg_pool.refresh")
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("")
    assert services.reserve_tg_pool_due(1.0) is False
    _ = config


# ── демпфер случаев 3/Н3 после перезагрузки хоста ───────────────────────────

def test_the_damper_starts_over_after_a_host_reboot(box, tmp_path, monkeypatch):
    """Отметка демпфера из прежней загрузки (monotonic в базе — «из будущего»)
    — счёт заново: просьба основе не раньше rf_block_minutes после старта."""
    monkeypatch.setattr(rsvzone, "STATE_DIR", tmp_path / "rsv")
    monkeypatch.setattr(rsvzone, "OPEN_STATUS", tmp_path / "rsv" / "open.status")
    box.primary(True)
    box.hello(1, sees=False)
    box.hello(2, sees=False)
    box.tick()
    view = box.s.rsv_get("rsv_view")
    box.s.rsv_update("rsv_view", rf={**view["rf"], "mono": box.clock.t + 10 ** 7})   # прежняя загрузка
    acts = []
    for _ in range(int(10 * 60 / 5)):
        box.advance(5)
        box.keepalive()
        acts += box.tick()
    assert not sent(acts, "ask"), "после перезагрузки демпфер обойдён — просьба основе сразу"
    for _ in range(int(6 * 60 / 5)):
        box.advance(5)
        box.keepalive()
        acts += box.tick()
    assert sent(acts, "ask"), "демпфер после перезагрузки не досчитал"
