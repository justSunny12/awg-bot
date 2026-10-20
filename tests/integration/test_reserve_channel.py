"""Канал пары на живых сокетах (runtime/pairlink): слушатель пригласившего
(PairServer) и клиент принявшего (PairClient) поверх временной БД.

Сокеты — настоящие (петля); подставлены только адреса /30 (на петле — 127.0.0.1)
и скрипт линка пары. Вторая сторона канала — модель из _reserve_common
(эфемерный ключ, код и ключ сопряжения — тем же util/joinsas), конверт —
тем же util/gwlink, ключ — тот, что вывела бы сторона из своего секрета.

Цена ошибки: сессия сопряжения, принявшая `st` или `ping` до кода, — чужая
машина со строкой приглашения говорит с ботом как вторая сторона пары;
третий неверный код, после которого линк под приглашение жив, — перебор;
`pair_admin` до кода — роль в боте у держателя строки; аварийный код,
пришедший в чат дважды, из которых одно сообщение не удаляется, — секрет
навсегда в истории чата.
"""
from __future__ import annotations

import asyncio
import base64

import pytest

from awgbot.runtime import pairlink
from awgbot.runtime.pairlink import common as plc
from awgbot.domain.services.reserve import invite as invitemod
from awgbot.util import gwlink, joinsas, pairtoken

from tests.conftest import FakeBot
from tests.integration import _reserve_common as rc
from tests.integration._reserve_common import ADDR_B, ADMIN, PEER_ADMIN, Acceptor, Inviter
from tests.integration.test_gwlink_channel import _free_port, _Gw, _until

pytestmark = pytest.mark.integration
rsv = rc.rsv


class _Hooks:
    """Крючки main: события сопряжения и уведомления — в журнал."""
    def __init__(self, monkeypatch):
        self.events: list[tuple[str, dict, dict]] = []
        self.notes: list = []

        async def on_event(name, st, extra):
            self.events.append((name, dict(st or {}), dict(extra or {})))

        async def on_notes(notes):
            self.notes.extend(notes)
        monkeypatch.setattr(pairlink, "_on_event", on_event)
        monkeypatch.setattr(pairlink, "_on_notes", on_notes)

    def names(self) -> list[str]:
        return [e[0] for e in self.events]


@pytest.fixture()
async def server(services, rsv, monkeypatch):
    """Слушатель пригласившего на петле: адрес есть, пока есть приглашение или пара."""
    port = _free_port()
    monkeypatch.setattr(services, "reserve_listen_address",
                        lambda: ("127.0.0.1", port) if services.reserve_link_role() == "inviter" else None)
    hooks = _Hooks(monkeypatch)
    from awgbot.runtime.pairlink.server import PairServer
    srv = PairServer(services)
    monkeypatch.setattr(pairlink, "_link", srv)
    srv.hooks = hooks
    yield srv
    await srv.stop()


async def _invite(services, srv):
    res = services.reserve_invite_issue()
    await srv.ensure()
    assert srv._bound, "слушатель сопряжения не поднялся"
    priv = base64.b64encode(pairtoken.decode(res["token"]).peer_priv).decode()
    return res, plc.channel_key(priv)


async def _pair_hello(srv, res, key, *, addr: str = ADDR_B) -> tuple[_Gw, Acceptor, dict]:
    acc = Acceptor(res["state"]["id"])
    gw = _Gw(*await asyncio.open_connection(*srv._bound), key=key)
    body = {k: v for k, v in acc.hello(addr=addr).items() if k not in ("_peer_addr", "nonce")}
    await gw.send("hello", body)
    first = await gw.recv()
    if first.get("t") == "pair_eph":
        acc.eph_in(first)
    return gw, acc, first


async def _closed(gw: _Gw, timeout: float = 2.0) -> bool:
    """Сервер закрыл сессию: ни слова, а затем конец потока. Молчание без
    закрытия — не закрытие (иначе «разрыв» был бы зелёным и у сессии, которая
    просто ничего не ответила)."""
    end = asyncio.get_running_loop().time() + timeout
    while True:
        left = end - asyncio.get_running_loop().time()
        if left <= 0:
            return False
        try:
            line = await asyncio.wait_for(gw.reader.readline(), timeout=left)
        except asyncio.TimeoutError:
            return False
        except ConnectionError:
            return True
        if not line:
            return True
        return False                                    # сервер что-то сказал — не закрыл молча


# ── сессия сопряжения у пригласившего ───────────────────────────────────────

async def test_the_pairing_session_gets_the_ephemeral_key_and_nothing_else(services, server):
    res, key = await _invite(services, server)
    gw, acc, first = await _pair_hello(server, res, key)
    assert first["t"] == "pair_eph" and acc.code == invitemod._live()["code"]
    assert "admin_id" not in first, "ADMIN_ID ушёл в pair_eph — до кода"
    assert await gw.silent(0.4), "в сессию сопряжения ушло что-то кроме pair_eph"
    assert await _until(lambda: server.hooks.events) and server.hooks.names()[0] == "connected"
    await gw.close()


@pytest.mark.parametrize("kind,body", [("st", {"ready": True}), ("ping", {"n": "x" * 16}), ("gone", {})],
                         ids=["st", "ping", "gone"])
async def test_any_ordinary_message_breaks_the_pairing_session(services, server, kind, body):
    res, key = await _invite(services, server)
    gw, _acc, _ = await _pair_hello(server, res, key)
    await gw.send(kind, body)
    assert await _closed(gw), f"сессия сопряжения ответила на «{kind}»"
    assert services.reserve_peer_state() == {} and services.reserve_paired() is False


async def test_pair_admin_before_the_code_writes_nothing(services, server):
    res, key = await _invite(services, server)
    gw, acc, _ = await _pair_hello(server, res, key)
    await gw.send("pair_admin", {"sealed": joinsas.seal(acc.kp, {"admin_id": 31337, "self_host": ADDR_B})})
    assert await _closed(gw)
    assert not services.reserve_paired(), "пара записана без кода"


async def test_a_hello_signed_with_another_key_gets_nothing(services, server):
    res, _key = await _invite(services, server)
    acc = Acceptor(res["state"]["id"])
    gw = _Gw(*await asyncio.open_connection(*server._bound), key=plc.channel_key(rc.key()))
    await gw.send("hello", {k: v for k, v in acc.hello().items() if k not in ("_peer_addr", "nonce")})
    assert await _closed(gw) and invitemod._live() == {}, "чужой ключ открыл сессию сопряжения"


async def test_a_second_pairing_session_pushes_out_the_first(services, server):
    res, key = await _invite(services, server)
    first, _acc1, _ = await _pair_hello(server, res, key)
    second, acc2, m = await _pair_hello(server, res, key)
    assert m["t"] == "pair_eph" and invitemod._live()["code"] == acc2.code
    assert await _closed(first), "прежняя сессия сопряжения жива рядом с новой"
    await second.close()


async def test_hello_from_a_second_address_is_the_two_event(services, server, monkeypatch):
    """На петле адрес один — второй адрес подставляем так, как его видел бы
    слушатель (peername)."""
    res, key = await _invite(services, server)
    gw1, _a, _ = await _pair_hello(server, res, key)
    real = services.reserve_pair_hello
    monkeypatch.setattr(services, "reserve_pair_hello", lambda msg: real({**msg, "_peer_addr": rc.ADDR_X}))
    gw2, _b, _ = await _pair_hello(server, res, key)
    assert await _until(lambda: "two" in server.hooks.names()), server.hooks.names()
    await gw2.close()


async def test_wrong_codes_and_the_third_closes_the_session_and_drops_the_link(services, server, rsv):
    res, key = await _invite(services, server)
    gw, acc, _ = await _pair_hello(server, res, key)
    wrong = "AAAAAA" if acc.code != "AAAAAA" else "BBBBBB"
    out = services.reserve_pair_code(wrong)
    await server.pair_wrong(out["left"])
    m = await gw.recv()
    assert m["t"] == "pair_wrong" and m["left"] == 2, m
    services.reserve_pair_code(wrong)
    assert services.reserve_pair_code(wrong)["result"] == "refused"
    await server.pair_close("code")
    msgs = []
    while (m := await gw.next_msg(1.0)) is not None:
        msgs.append(m)
    assert msgs and msgs[-1]["t"] == "pair_refused" and msgs[-1]["reason"] == "code", msgs
    assert services.reserve_invite() == {} and rsv.modes[-1] == "--rollback" and not rsv.conf.exists(), \
        "линк под приглашение пережил третий неверный код"
    assert await _until(lambda: not server._bound), "слушатель остался на адресе закрытого приглашения"


async def test_the_right_code_finishes_the_pairing_over_the_channel(services, server, rsv):
    res, key = await _invite(services, server)
    gw, acc, _ = await _pair_hello(server, res, key)
    assert services.reserve_pair_code(acc.code)["result"] == "ok"
    await server.pair_accepted()
    ok = await gw.recv()
    assert ok["t"] == "pair_ok" and set(ok) - {"t", "seq", "ts"} == {"sealed"}, ok
    secrets_ = joinsas.unseal(acc.kp, ok["sealed"])
    assert secrets_["admin_id"] == ADMIN and secrets_["psk"] == rsv.out["--rekey-stage"]["PAIR_PSK"]
    await gw.send("pair_admin", {"sealed": joinsas.seal(acc.kp, {"admin_id": PEER_ADMIN, "self_host": ADDR_B})})
    assert await _until(lambda: services.reserve_paired()), "пара не записана по pair_admin"
    assert services.reserve_peer()["peer_admin_id"] == PEER_ADMIN
    assert await _closed(gw), "сессия сопряжения не закрылась после pair_admin"
    assert await _until(lambda: "linked" in server.hooks.names())


# ── обычная сессия после сопряжения ─────────────────────────────────────────

@pytest.fixture()
async def linked(services, server, rsv):
    rc.pair_as_inviter(services, rsv)
    await server.ensure()
    assert server._bound, "после сопряжения слушатель не на месте"
    return plc.channel_key(services.reserve_channel_secret())


async def _session(server, key) -> tuple[_Gw, dict]:
    gw = _Gw(*await asyncio.open_connection(*server._bound), key=key)
    await gw.send("hello", {"name": "Beta", "version": "4.0.0", "caps": [], "gen": 0, "ports": [45871], "seen": "",
                            "bot": "beta_vpn_bot", "gw": True})
    return gw, await gw.recv()


async def test_an_ordinary_hello_gets_st_and_ping_gets_pong(services, server, linked):
    gw, first = await _session(server, linked)
    assert first["t"] == "st" and first["admin_id"] == ADMIN
    assert await _until(lambda: server.online)
    await gw.send("ping", {"n": "abcdef0123456789"})
    pong = await gw.recv()
    assert (pong["t"], pong["n"]) == ("pong", "abcdef0123456789")
    await gw.send("slots", {"slots": []})              # вид словаря, который этот этап не ведёт
    await gw.send("ping", {"n": "0123456789abcdef"})
    pong = await gw.recv()
    assert (pong["t"], pong["n"]) == ("pong", "0123456789abcdef"), "сессия не пережила вид следующего этапа"
    await gw.send("whatever", {})                       # вида нет в словаре — разрыв, как у канала шлюзов
    assert await _closed(gw), "на вид вне словаря сессия продолжилась"
    await gw.close()
    assert await _until(lambda: not server.online)


async def test_the_old_invitation_key_does_not_open_a_session_after_pairing(services, server, rsv):
    res, key = await _invite(services, server)
    gw, acc, _ = await _pair_hello(server, res, key)
    services.reserve_pair_code(acc.code)
    await server.pair_accepted()
    await gw.recv()
    await gw.send("pair_admin", {"sealed": joinsas.seal(acc.kp, {"admin_id": PEER_ADMIN, "self_host": ADDR_B})})
    assert await _until(lambda: services.reserve_paired())
    old = _Gw(*await asyncio.open_connection(*server._bound), key=key)
    await old.send("hello", {"name": "Beta", "version": "4.0.0", "caps": [], "gen": 0, "ports": [45871], "seen": "",
                             "bot": "beta_vpn_bot", "gw": True})
    assert await _closed(old), "ключ из приглашения открыл обычную сессию после сопряжения"


async def test_st_from_the_peer_lands_and_its_notes_go_out(services, server, linked):
    gw, _first = await _session(server, linked)
    await gw.send("st", {"ready": True, "name": "Beta", "version": "4.0.0", "caps": [], "gen": 0, "ports": [45871],
                         "self": rc.HOST_B, "out": "home", "in": "idle", "pkg": {}, "voters": [],
                         "admin_id": 555, "admin_opened": True, "gw": True})
    assert await _until(lambda: services.reserve_peer()["peer_admin_id"] == 555)
    assert await _until(lambda: any("сменился админ" in n.text for n in server.hooks.notes))
    await gw.close()


async def test_a_bad_st_breaks_the_session_without_writing(services, server, linked):
    gw, _ = await _session(server, linked)
    await gw.send("st", {"ready": True})
    assert await _closed(gw), "st без обязательных полей принят"
    assert services.reserve_peer()["peer_admin_id"] == PEER_ADMIN


# ── код в чате после сопряжения ─────────────────────────────────────────────

async def _linked_through_main_hooks(services, server, monkeypatch) -> tuple[FakeBot, list]:
    """Сопряжение через настоящие крючки main (events.make_on_event и
    рассылку уведомлений) с ботом-журналом; удаление через 10 минут — журнал."""
    from awgbot.bot.handlers.settings.reserve import events
    from awgbot.bot.notifier import send_notifications
    bot = FakeBot()
    deleted: list = []

    async def later(bot_, chat_id, message_id, delay=events.CODE_TTL):
        deleted.append(message_id)
    monkeypatch.setattr(events, "_delete_later", later)
    monkeypatch.setattr(pairlink, "_on_event", events.make_on_event(bot, services))

    async def on_notes(notes):
        await send_notifications(bot, notes)
    monkeypatch.setattr(pairlink, "_on_notes", on_notes)
    res, key = await _invite(services, server)
    services.reserve_invite_set_message(ADMIN, 777)
    gw, acc, _ = await _pair_hello(server, res, key)
    services.reserve_pair_code(acc.code)
    await server.pair_accepted()
    await gw.recv()
    await gw.send("pair_admin", {"sealed": joinsas.seal(acc.kp, {"admin_id": PEER_ADMIN, "self_host": ADDR_B})})
    assert await _until(lambda: services.reserve_paired())
    code = services.reserve_code()
    assert await _until(lambda: any(code in str(r) for r in bot.records))
    await asyncio.sleep(0.2)
    return bot, deleted


async def test_after_pairing_the_code_reaches_the_chat_once(services, server, monkeypatch):
    """Т-Р18: после сопряжения аварийный код приходит одним сообщением со
    звуком, и именно оно удаляется через 10 минут. Второе сообщение с тем же
    кодом никто не удалит — секрет навсегда в истории чата."""
    bot, deleted = await _linked_through_main_hooks(services, server, monkeypatch)
    code = services.reserve_code()
    with_code = [r for r in bot.records if r[0] == "send_message" and code in r[2]]
    assert len(with_code) == 1, f"аварийный код в чате {len(with_code)} раз(а), удаление запланировано {len(deleted)}"
    assert len(deleted) == 1


async def test_after_pairing_the_invitation_message_gets_the_result(services, server, monkeypatch):
    """Т-Р8 правит живое сообщение приглашения: итог «Резерв … подключён» без
    кнопок. Без правки в чате остаются строка приглашения и «✏️ Ввести код» у
    уже записанной пары."""
    bot, _ = await _linked_through_main_hooks(services, server, monkeypatch)
    finals = [r for r in bot.records if r[0] == "edit_message_text" and "подключён" in r[2]]
    assert finals, "сообщение приглашения не получило итог Т-Р8"


# ── клиент принявшего ───────────────────────────────────────────────────────

class _InviterSide:
    """Слушатель пригласившего моделью: принимает hello сопряжения и ведёт
    сессию по командам теста (конверт — как у PairServer)."""

    def __init__(self, inv: Inviter):
        self.inv = inv
        self.key = plc.channel_key(inv.peer_priv)
        self.hello: dict = {}
        self.sess = None
        self.reader = None
        self.got = asyncio.Event()
        self.connects = 0

    async def serve(self, reader, writer):
        self.connects += 1
        line = await reader.readline()
        msg = gwlink.unpack(self.key, line)
        self.hello = msg
        self.sess = plc._Session(writer, self.key, listener=True)
        self.sess.cn = gwlink.nonce_from(msg["nonce"])
        self.reader = reader
        self.got.set()

    async def send(self, kind: str, body: dict) -> None:
        await plc.send_line(self.sess, kind, body)

    async def recv(self, timeout: float = 2.0) -> dict:
        line = await asyncio.wait_for(self.reader.readline(), timeout)
        return gwlink.unpack(self.key, line, nonce=self.sess.sn) if line else {}


@pytest.fixture()
async def acceptor(services, rsv, monkeypatch):
    """Этот сервер принял приглашение (запись, эфемерная пара, конфиг на
    ключе из приглашения — как после «🛟 Подключиться»), второй — модель на петле."""
    inv = Inviter()
    side = _InviterSide(inv)
    srv = await asyncio.start_server(side.serve, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    rsv.run("--join", {"PAIR_ID": inv.pair_id, "PAIR_PRIV": inv.peer_priv, "PAIR_PEER_PUB": inv.inviter_pub,
                       "PAIR_ENDPOINT": f"{rc.HOST_B}:47101"})
    services.rsv_put("rsv_invite", {"id": inv.pair_id, "role": "acceptor", "pair_if": "awgpair", "port": 47101,
                                    "peer_port": 47101, "cidr": "10.99.98.0/30", "host": rc.HOST_B, "state": "waiting",
                                    "issued": "2026-10-21T10:00:00+03:00", "expires": "2099-01-01T00:00:00+03:00",
                                    "msg": [ADMIN, 888]})
    priv, pub = joinsas.new_pair()
    invitemod._live_set({"id": inv.pair_id, "priv": priv, "pub": pub})
    monkeypatch.setattr(services, "reserve_connect_address", lambda: ("127.0.0.1", port))
    hooks = _Hooks(monkeypatch)
    from awgbot.runtime.pairlink.client import PairClient
    cl = PairClient(services)
    cl.start()
    await asyncio.wait_for(side.got.wait(), 5)
    side.hooks, side.client = hooks, cl
    yield side
    await cl.stop()
    srv.close()


async def test_the_client_hello_is_the_pairing_hello_without_the_admin_id(services, acceptor):
    h = acceptor.hello
    assert h["t"] == "hello" and h["pair"] == acceptor.inv.pair_id and h["eph"]
    assert "admin_id" not in h, "ADMIN_ID ушёл в hello до кода"


async def test_pair_eph_shows_the_code_to_the_admin(services, acceptor):
    await acceptor.send("pair_eph", acceptor.inv.eph_reply(acceptor.hello))
    assert await _until(lambda: "code" in acceptor.hooks.names())
    name, _st, extra = acceptor.hooks.events[-1]
    assert extra["code"] == acceptor.inv.code, "у принявшего не тот код"


async def test_pair_wrong_is_shown_with_the_attempts_left(services, acceptor):
    await acceptor.send("pair_eph", acceptor.inv.eph_reply(acceptor.hello))
    await acceptor.send("pair_wrong", {"left": 1})
    assert await _until(lambda: "wrong" in acceptor.hooks.names())
    assert acceptor.hooks.events[-1][2]["left"] == 1


async def test_pair_refused_cancels_the_acceptance_and_stops_reconnecting(services, acceptor, rsv):
    await acceptor.send("pair_eph", acceptor.inv.eph_reply(acceptor.hello))
    await acceptor.send("pair_refused", {"reason": "code"})
    assert await _until(lambda: services.reserve_invite() == {}), "отказ пригласившего не снял подключение"
    assert rsv.modes[-1] == "--rollback"
    assert await _until(lambda: acceptor.client._dead)
    acceptor.client.wake()
    await asyncio.sleep(0.3)
    assert acceptor.connects == 1, "после отказа клиент подключается снова"


async def test_the_refusal_after_three_codes_is_shown_as_a_refusal(services, acceptor, monkeypatch):
    """Пригласивший закрыл приглашение после трёх неверных кодов (reason
    «code») — живое сообщение принявшего говорит об отказе (Т-Р4-подобный
    итог), а не рисует экран кода с пустым кодом."""
    from awgbot.bot.handlers.settings.reserve import events
    bot = FakeBot()
    monkeypatch.setattr(pairlink, "_on_event", events.make_on_event(bot, services))
    await acceptor.send("pair_eph", acceptor.inv.eph_reply(acceptor.hello))
    await acceptor.send("pair_refused", {"reason": "code"})
    assert await _until(lambda: services.reserve_invite() == {})
    await asyncio.sleep(0.1)
    texts = [r[2] for r in bot.records if r[0] == "edit_message_text"]
    assert texts and "трижды" in texts[-1], f"последний экран принявшего: {texts[-1:] or 'нет'}"


async def test_a_nogw_refusal_is_shown_as_such(services, acceptor, monkeypatch):
    """Пригласивший отказал: шлюзов нет ни у кого — у принявшего итог Т-Р4
    этими словами, не общий отказ."""
    from awgbot.bot.handlers.settings.reserve import events
    from awgbot.bot.texts.reserve import notices
    bot = FakeBot()
    monkeypatch.setattr(pairlink, "_on_event", events.make_on_event(bot, services))
    await acceptor.send("pair_refused", {"reason": "nogw"})
    assert await _until(lambda: services.reserve_invite() == {})
    await asyncio.sleep(0.1)
    texts = [r[2] for r in bot.records if r[0] == "edit_message_text"]
    assert texts and texts[-1] == notices.refused_by_inviter("nogw"), texts[-1:]


async def test_pair_ok_answers_with_pair_admin_and_writes_the_pair(services, acceptor, rsv):
    await acceptor.send("pair_eph", acceptor.inv.eph_reply(acceptor.hello))
    assert await _until(lambda: "code" in acceptor.hooks.names())
    ok = acceptor.inv.ok_body()
    await acceptor.send("pair_ok", {"sealed": ok["sealed"]})
    reply = await acceptor.recv()
    assert reply["t"] == "pair_admin" and joinsas.unseal(acceptor.inv.kp, reply["sealed"])["admin_id"] == ADMIN
    assert await _until(lambda: services.reserve_paired())
    peer = services.reserve_peer()
    assert (peer["listener"], peer["peer_admin_id"]) == (0, PEER_ADMIN)
    assert rsv.env("--set-peer")["PAIR_PSK"] == ok["_data"]["psk"]
    assert await _until(lambda: "linked" in acceptor.hooks.names())


async def test_an_unexpected_message_in_pairing_drops_the_connection(services, acceptor):
    await acceptor.send("st", {"ready": True})
    assert await acceptor.recv(1.0) == {}, "клиент сопряжения продолжил после постороннего сообщения"
    assert not services.reserve_paired()
