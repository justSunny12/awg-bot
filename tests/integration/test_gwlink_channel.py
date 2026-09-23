"""
Канал ВПС ↔ шлюз на живых сокетах: сессия слота, приём снимка и дельт,
починка разрыва нумерации, вытеснение второй сессии, хранение в БД.

Сокеты здесь настоящие (петля), сервер — боевой LinkServer поверх временной
БД; подставлены только адреса /30 (на петле источник и назначение совпадают,
поэтому сверку пары «локальный/пир» проверяет отдельный тест без сокета) и
приватный ключ линка. Всё, что приезжает по каналу, — недоверенные данные с
чужой машины: цена ошибки здесь — снимок одного шлюза в карточке другого или
упавший процесс бота от присланного мусора.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import socket

import pytest

from awgbot.core import config
from awgbot.domain import gwsnapshot
from awgbot.domain.channelstate import ChannelState
from awgbot.runtime import linkserver
from awgbot.util import gwlink, gwsign

pytestmark = pytest.mark.integration

ADMIN = config.ADMIN_ID
PRIV = base64.b64encode(os.urandom(32)).decode()
KEY = gwlink.channel_key(PRIV)

SNAP = {"bundle": {"lan_mode": "1", "home_subnets": "192.168.68.0/24",
                   "resolver": "10.9.1.1", "peer_home_nets": "", "admin_ips": "10.8.1.2"},
        "link_contract": "1", "plumbing_gen": "new", "mark_status": "confirmed",
        "agent_version": "3.1.0", "awg_generation": 1, "egress_ok": True,
        "boot_id": "b" * 36, "ts": "2026-09-22T20:00:00+03:00"}


def _free_port() -> int:
    """Свободный порт у ядра: фиксированный номер ловил бы чужой прогон."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Gw:
    """Агент шлюза на том конце: шлёт подписанные строки и читает ответы.

    Протокол нонсов (proto 2) — как у боевого клиента: hello подписан без
    нонса и несёт свой нонс `n` (cn); первое сообщение сервера подписано cn и
    несёт нонс сервера `n` (sn); дальше свои сообщения подписываем sn, ответы
    сервера проверяем cn. Первое слово сервера читается лениво — перед первой
    отправкой после hello — и откладывается в `inbox`, чтобы тесты, ждущие
    роль, получили её как прежде."""

    def __init__(self, reader, writer, key=KEY):
        self.reader, self.writer, self.key = reader, writer, key
        self.seq = 0
        self.cn = gwlink.new_nonce()
        self.sn = b""
        self.hello_sent = False
        self.inbox: list[dict] = []

    def pack(self, kind: str, body: dict | None = None, *, key=None, seq=None,
             nonce: bytes | None = None, now: float | None = None) -> bytes:
        """Строка так, как её подписал бы агент в этой сессии."""
        self.seq = self.seq + 1 if seq is None else seq
        body = dict(body or {})
        if kind == "hello":
            body.setdefault("nonce", gwlink.nonce_b64(self.cn))
            sign = b"" if nonce is None else nonce
        else:
            sign = self.sn if nonce is None else nonce
        return gwlink.pack(key or self.key, kind, body, seq=self.seq, nonce=sign, now=now)

    async def send(self, kind: str, body: dict | None = None, *, key=None, seq=None,
                   nonce: bytes | None = None, now: float | None = None):
        if kind != "hello" and self.hello_sent and not self.sn:
            await self._first()
        self.writer.write(self.pack(kind, body, key=key, seq=seq, nonce=nonce, now=now))
        await self.writer.drain()
        if kind == "hello":
            self.hello_sent = True

    async def _first(self, timeout: float = 2.0) -> None:
        """Первое слово сервера после hello: из него — нонс сервера."""
        line = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
        msg = self._take(line)
        self.inbox.append(msg)

    def _take(self, line: bytes) -> dict:
        msg = gwlink.unpack(self.key, line, nonce=self.cn)
        if not self.sn and self.hello_sent:
            self.sn = gwlink.nonce_from(msg.get("nonce"))
        return msg

    async def raw(self, line: bytes):
        self.writer.write(line)
        await self.writer.drain()

    async def recv(self, timeout: float = 2.0) -> dict:
        if self.inbox:
            return self.inbox.pop(0)
        line = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
        return self._take(line)

    async def recv_raw(self, timeout: float = 2.0) -> dict:
        """Следующее слово сервера, как есть: для повторщика, у которого нонс
        шлюза — чужой, записанный из hello."""
        line = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
        head = line.decode().strip()[len(gwlink.PREFIX):].partition(".")[0]
        return json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))

    async def next_msg(self, timeout: float = 2.0) -> dict | None:
        """Следующее сообщение сервера или None — молчание или закрытый сокет."""
        if self.inbox:
            return self.inbox.pop(0)
        try:
            line = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return self._take(line) if line else None

    async def role(self) -> bool:
        """Первое, что сервер говорит после `hello`, — роль слота (активный или
        резерв), один раз за сессию. Прочитать её и вернуть, пришла ли."""
        return (await self.recv())["t"] == "role"

    async def silent(self, seconds: float = 0.3) -> bool:
        """Правда ли, что сервер не сказал ни слова: в простое канал молчит."""
        if self.inbox:
            return False
        try:
            await asyncio.wait_for(self.reader.readline(), timeout=seconds)
        except asyncio.TimeoutError:
            return True
        return False

    async def close(self):
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()


@pytest.fixture()
async def link(services, make_active_client, monkeypatch):
    """Поднятый слушатель канала на петле и один слот шлюза."""
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 443, "127.0.0.0/30", slot_id=1)
    services.gateway_set_home_subnets(1, "192.168.68.0/24")
    services.gateway_set_lan_mode(1, True)
    monkeypatch.setattr(services, "gateway_resolver_addr", lambda g: "10.9.1.1" if g.lan_mode else "")
    # Снимок фикстуры — ровно то, что ВПС выдаёт этому слоту: иначе с этапа 2
    # сервер сразу отвечает на снимок настройками, а тесты здесь не про них.
    want = services.gwlink_settings_want(services.db.gateway(1))
    want = {k.lower(): v for k, v in want.items()}
    want["peer_home_nets"] = " ".join(services.gateway_peer_nets(1))   # едет только бандлом
    assert want == SNAP["bundle"], "фикстурный снимок разошёлся с выдаваемым — поправь SNAP['bundle']"
    monkeypatch.setattr(services, "_link_privkey", lambda g=None: PRIV)
    port = _free_port()
    monkeypatch.setattr(linkserver, "channel_port", lambda: port)
    # На петле адрес источника и назначения один и тот же; сверку пары
    # «локальный адрес — адрес пира» проверяет test_a_connection_from_...
    monkeypatch.setattr(linkserver, "gw_address", lambda cidr: "127.0.0.1")
    srv = linkserver.LinkServer(services)
    await srv.ensure()
    assert srv._bound, "слушатель канала не поднялся на адресе линка"
    yield srv
    await srv.stop()


async def _connect(srv) -> _Gw:
    host, port = srv._bound[0]
    return _Gw(*await asyncio.open_connection(host, port))


async def _until(cond, timeout: float = 3.0):
    """Дождаться, пока сервер разберёт сообщение в своём потоке."""
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        value = cond()
        if value:
            return value
        await asyncio.sleep(0.02)
    return cond()


# ── сессия и первый снимок ───────────────────────────────────────────────────

async def test_a_connecting_gateway_gets_a_session_and_its_snapshot_is_stored(services, link):
    """Подключение — самая важная точка канала: после ребута малины, обновления
    агента или восстановления линка сессия открывается заново и приносит полный
    снимок. Не сохранись он — карточка слота осталась бы с догадкой вместо
    ответа."""
    gw = await _connect(link)
    await gw.send("hello", {"proto": gwlink.PROTO, "agent": "3.1.0"})
    await gw.send("snap", {**SNAP, "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1))

    sess = services.gwlink_session(1)
    assert sess["agent"] == "3.1.0" and sess["proto"] == str(gwlink.PROTO) and sess["since"]
    snap = services.gwlink_snapshot(1)
    assert snap["bundle"]["home_subnets"] == "192.168.68.0/24"
    assert snap["agent_version"] == "3.1.0" and snap["egress_ok"] is True
    assert services.gwlink_snapshot_age(1) is not None and services.gwlink_snapshot_age(1) < 60
    assert link.online(1) is True
    assert await gw.role(), "после hello сервер обязан один раз сказать роль слота"
    assert await gw.silent(), "в простое сервер не шлёт ни одного пакета"
    await gw.close()


async def test_the_session_closes_when_the_gateway_goes_away_but_the_snapshot_stays(services, link):
    """Канал упал — последнее известное полезнее пустого экрана, особенно когда
    человек разбирается, почему шлюз молчит. Уходит только отметка «на связи»."""
    gw = await _connect(link)
    await gw.send("hello", {"proto": 1, "agent": "3.1.0"})
    await gw.send("snap", {**SNAP, "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1))
    await gw.close()
    await _until(lambda: not services.gwlink_session(1))

    assert services.gwlink_session(1) == {}, "сессия осталась висеть после разрыва"
    assert services.gwlink_snapshot(1)["agent_version"] == "3.1.0", "снимок выбросили вместе с сессией"
    assert services.db.get_state("gwlink_seen_1"), "не записали, когда шлюз был последний раз на связи"
    assert link.online(1) is False


# ── дельты и починка ─────────────────────────────────────────────────────────

async def test_a_delta_lands_on_top_of_the_stored_snapshot(services, link):
    """По каналу едет только разница — десятки байт вместо снимка целиком.
    Наложись она мимо, и карточка показала бы смесь старого с новым."""
    gw = await _connect(link)
    await gw.send("hello", {"proto": 1, "agent": "3.1.0"})
    await gw.send("snap", {**SNAP, "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1))
    await gw.send("delta", {"bundle": {**SNAP["bundle"], "lan_mode": "0"}, "rev": 2})
    await _until(lambda: services.gwlink_snapshot(1)["bundle"]["lan_mode"] == "0")

    snap = services.gwlink_snapshot(1)
    assert snap["bundle"]["lan_mode"] == "0"
    assert snap["agent_version"] == "3.1.0", "дельта затёрла то, о чём не говорила"
    assert services.db.get_state("gwlink_snap_rev_1") == "2", (
        "номер принятого сообщения не сдвинулся — следующая дельта будет считаться разрывом")
    await gw.close()


async def test_a_gap_in_numbering_makes_the_server_ask_for_a_full_snapshot(services, link):
    """Дельта потерялась или пришла не по порядку. Молча приняв её, ВПС
    показывал бы вечно неверную картинку; вместо этого он просит полный снимок
    и начинает счёт заново."""
    gw = await _connect(link)
    await gw.send("hello", {"proto": 1, "agent": "3.1.0"})
    await gw.send("snap", {**SNAP, "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1))
    await gw.send("delta", {"egress_ok": False, "rev": 9})

    assert await gw.role()
    ask = await gw.recv()
    assert ask["t"] == "ask" and ask["what"] == "snap"
    assert services.gwlink_snapshot(1)["egress_ok"] is True, "дельта не по порядку всё-таки легла"
    await gw.send("snap", {**SNAP, "egress_ok": False, "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1)["egress_ok"] is False)
    assert services.gwlink_snapshot(1)["egress_ok"] is False
    await gw.close()


async def test_a_delta_before_any_snapshot_is_refused_and_a_full_one_is_asked(services, link):
    """Сессия начинается с полного снимка. Дельта, пришедшая первой (бота
    перезапустили, у агента свой счёт), накладываться не на что."""
    gw = await _connect(link)
    await gw.send("hello", {"proto": 1, "agent": "3.1.0"})
    await gw.send("delta", {"egress_ok": False, "rev": 2})
    assert await gw.role()
    ask = await gw.recv()
    assert ask["t"] == "ask" and ask["what"] == "snap"
    assert services.gwlink_snapshot(1) == {}
    await gw.close()


# ── подпись, размер, мусор ───────────────────────────────────────────────────

async def test_a_message_signed_by_another_key_ends_the_session(services, link):
    """Клиент туннеля, дотянувшийся до порта канала, не имеет права
    рассказывать про шлюз. Подпись не сошлась — сессию рвём, а не «пропускаем
    сообщение»."""
    gw = await _connect(link)
    await gw.send("hello", {"proto": 1, "agent": "3.1.0"})
    await gw.send("snap", {**SNAP, "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1))
    other = gwlink.channel_key(base64.b64encode(os.urandom(32)).decode())
    await gw.send("delta", {"bundle": {**SNAP["bundle"], "lan_mode": "0"}, "rev": 2}, key=other)
    await _until(lambda: not services.gwlink_session(1))

    assert services.gwlink_session(1) == {} and link.online(1) is False
    assert services.gwlink_snapshot(1)["bundle"]["lan_mode"] == "1", "чужая дельта легла в снимок"
    await gw.close()


async def test_a_replayed_message_ends_the_session(services, link):
    """Записанное сообщение, поданное заново, — это старая правда под верной
    подписью: наложить её поверх свежего снимка хуже, чем потерять сессию."""
    gw = await _connect(link)
    await gw.send("hello", {"proto": 1, "agent": "3.1.0"})
    await gw.send("snap", {**SNAP, "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1))
    await gw.send("delta", {"egress_ok": False, "rev": 2}, seq=1)   # номер уже был
    await _until(lambda: not services.gwlink_session(1))
    assert services.gwlink_session(1) == {}
    assert services.gwlink_snapshot(1)["egress_ok"] is True
    await gw.close()


async def test_a_message_of_a_past_session_ends_the_session(services, link):
    """Сообщение, записанное в прошлой сессии, подписано прошлым нонсом
    сервера. Под верным ключом и со свежим номером оно — старая правда
    (вчерашний «выход наружу есть»), и сервер обязан его отвергнуть, а не
    наложить поверх свежего снимка."""
    gw = await _connect(link)
    await gw.send("hello", {"proto": gwlink.PROTO, "agent": "3.1.0"})
    await gw.send("snap", {**SNAP, "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1))
    await gw.send("delta", {"egress_ok": False, "rev": 2}, nonce=gwlink.new_nonce())
    await _until(lambda: not services.gwlink_session(1))
    assert services.gwlink_session(1) == {} and link.online(1) is False, (
        "сообщение чужой сессии не порвало эту")
    assert services.gwlink_snapshot(1)["egress_ok"] is True, "дельта прошлой сессии легла в снимок"
    assert "подпись" in services.db.get_state("gwlink_error_1"), "причина разрыва не записана"
    await gw.close()


async def test_a_replayed_hello_opens_a_session_but_the_next_recorded_line_does_not_pass(
        services, link):
    """hello подписан без нонса и повторяем: записавший его откроет сессию —
    и только. Всё, что он записал следом, подписано нонсом прошлой сессии
    сервера, а у новой сессии нонс свой; снимок из записи не ложится, сессия
    рвётся."""
    gw = await _connect(link)
    hello = gw.pack("hello", {"proto": gwlink.PROTO, "agent": "3.0.0"})
    await gw.raw(hello)
    gw.hello_sent = True
    await gw._first()                             # нонс сервера этой сессии
    snap = gw.pack("snap", {**SNAP, "agent_version": "3.0.0", "rev": 1})
    await gw.raw(snap)
    await _until(lambda: services.gwlink_snapshot(1).get("agent_version") == "3.0.0")
    await gw.close()
    await _until(lambda: not services.gwlink_session(1))
    services.gwlink_snapshot_in(1, {**SNAP, "agent_version": "3.1.0", "rev": 1}, 1, True)

    replay = await _connect(link)
    await replay.raw(hello)
    first = await replay.recv_raw()
    assert first["t"] == "role" and first.get("nonce"), "повторённый hello не открыл сессию"
    await _until(lambda: services.gwlink_session(1))
    await replay.raw(snap)
    await _until(lambda: not services.gwlink_session(1))
    assert services.gwlink_session(1) == {}, "записанный снимок прошлой сессии прошёл в новой"
    assert services.gwlink_snapshot(1)["agent_version"] == "3.1.0", "снимок из записи лёг в карточку"
    await replay.close()


async def test_the_first_server_word_carries_its_nonce_and_is_signed_with_ours(services, link):
    """Первое слово сервера — единственное место, где шлюз узнаёт нонс сервера.
    Не будь его там или подпиши сервер его не нонсом шлюза — боевой клиент
    рвёт сессию, и канал не поднимается ни у кого."""
    gw = await _connect(link)
    await gw.send("hello", {"proto": gwlink.PROTO, "agent": "3.1.0"})
    line = await asyncio.wait_for(gw.reader.readline(), timeout=2)
    with pytest.raises(gwlink.ProtocolError):
        gwlink.unpack(KEY, line)                  # без нонса шлюза подпись не сходится
    first = gwlink.unpack(KEY, line, nonce=gw.cn)
    assert len(gwlink.nonce_from(first.get("nonce"))) == gwlink.NONCE_BYTES
    gw.sn = gwlink.nonce_from(first["nonce"])
    await gw.send("snap", {**SNAP, "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1))
    assert services.gwlink_snapshot(1)["agent_version"] == "3.1.0", (
        "снимок, подписанный нонсом сервера, не принят")
    await gw.close()


async def test_every_session_gets_its_own_server_nonce(services, link):
    """Нонс сервера — свой у каждой сессии, иначе запись прошлой сессии
    проходила бы в следующей."""
    seen = []
    for _ in range(2):
        gw = await _connect(link)
        await gw.send("hello", {"proto": gwlink.PROTO, "agent": "3.1.0"})
        await gw.role()
        seen.append(gw.sn)
        await gw.close()
        await _until(lambda: not services.gwlink_session(1))
    assert seen[0] and seen[0] != seen[1], "нонс сервера повторился между сессиями"


@pytest.mark.parametrize("shift", [3600, -3600])
async def test_gateway_clock_an_hour_off_does_not_break_the_session(services, link, shift):
    """Малина без RTC ушла на час в любую сторону. Канал её не отвергает:
    снимок ложится, сессия живёт, а расхождение видно в карточке."""
    import time
    gw = await _connect(link)
    await gw.send("hello", {"proto": gwlink.PROTO, "agent": "3.1.0"}, now=time.time() + shift)
    await gw.send("snap", {**SNAP, "rev": 1}, now=time.time() + shift)
    await _until(lambda: services.gwlink_snapshot(1))
    assert services.gwlink_snapshot(1)["agent_version"] == "3.1.0", f"часы {shift:+} с погасили канал"
    assert link.online(1) is True
    skew = services.gwlink_card(services.db.gateway(1), 10)["clock_skew"]
    assert skew is not None and abs(skew - shift) < 30, f"расхождение часов не измерено: {skew}"
    await gw.close()


async def test_a_tick_before_hello_does_not_spoil_the_first_word(services, link):
    """Шлюз подключился, hello ещё в пути, а такт живости (раз в полминуты)
    уже обходит сессии. Сказанное в этот момент слово ушло бы без нонса шлюза
    — клиент отвергнет его и порвёт сессию, а роль, отмеченная «сказанной»,
    не придёт и после hello. До hello серверу говорить нечего."""
    gw = await _connect(link)
    await _until(lambda: 1 in link._sessions)
    await link.deliver_all()
    assert await link.send(1, "ask", {"what": "snap"}) is False, (
        "до hello сервер отправил слово без нонса шлюза")
    await gw.send("hello", {"proto": gwlink.PROTO, "agent": "3.1.0"})
    first = await gw.recv()
    assert first["t"] == "role" and first.get("nonce"), (
        f"первым после hello пришло «{first['t']}» без нонса сервера")
    await gw.send("snap", {**SNAP, "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1))
    assert services.gwlink_snapshot(1)["agent_version"] == "3.1.0"
    await gw.close()


async def test_an_oversized_line_ends_the_session_without_being_parsed(services, link):
    """Единственный источник на том конце — наш же агент. Мегабайтная строка
    означает поломку или попытку засадить память: читать её незачем."""
    gw = await _connect(link)
    await gw.send("hello", {"proto": 1, "agent": "3.1.0"})
    await _until(lambda: services.gwlink_session(1))
    await gw.raw(b"GL1:" + b"A" * (gwlink.MAX_LINE + 1024) + b"\n")
    await _until(lambda: not services.gwlink_session(1))
    assert services.gwlink_session(1) == {} and link.online(1) is False
    await gw.close()


async def test_a_snapshot_full_of_junk_is_trimmed_and_the_bot_keeps_serving(services, link):
    """Малина может быть скомпрометирована: поле на мегабайт, разметка в
    значении и незнакомые ключи не должны ни лечь в БД, ни уронить бота."""
    gw = await _connect(link)
    await gw.send("hello", {"proto": 1, "agent": "3.1.0"})
    await gw.send("snap", {**SNAP, "rev": 1,
                           "bundle": {**SNAP["bundle"], "home_subnets": "Я" * 40_000},
                           "agent_version": "<b>3.1.0</b>", "temp": 61.2,
                           "ssh": {"port": 2222}, "будущее": {"a": [1, 2, 3]}})
    await _until(lambda: services.gwlink_snapshot(1))

    snap = services.gwlink_snapshot(1)
    assert len(snap["bundle"]["home_subnets"]) == 512, "монстр доехал до БД целиком"
    assert snap["agent_version"] == "<b>3.1.0</b>", "экранирует экран, а не хранилище"
    assert "temp" not in snap and "ssh" not in snap and "будущее" not in snap
    # сессия жива и продолжает работать
    await gw.send("delta", {"egress_ok": False, "rev": 2})
    await _until(lambda: services.gwlink_snapshot(1).get("egress_ok") is False)
    assert services.gwlink_snapshot(1)["egress_ok"] is False
    await gw.close()


async def test_an_unknown_kind_of_message_is_ignored_and_the_session_lives(services, link):
    """Новый агент, старый ВПС: незнакомое сообщение не повод рвать канал —
    иначе выпуск агента ронял бы связь у всех, кто ещё не обновил сервер."""
    gw = await _connect(link)
    await gw.send("hello", {"proto": 1, "agent": "3.1.0"})
    await gw.send("tail", {"lines": ["что-то из будущего"]})
    await gw.send("snap", {**SNAP, "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1))
    assert services.gwlink_snapshot(1)["agent_version"] == "3.1.0"
    await gw.close()


async def test_a_gateway_speaking_another_protocol_version_is_still_heard(services, link):
    """Старый агент, новый ВПС: расхождение `proto` пишем в сессию и работаем
    дальше. Отказ здесь означал бы, что обновление сервера обрывает канал до
    перевыпуска конфигурации на каждой малине."""
    gw = await _connect(link)
    await gw.send("hello", {"proto": 99, "agent": "9.9.9"})
    await gw.send("snap", {**SNAP, "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1))
    assert services.gwlink_session(1)["proto"] == "99"
    assert services.gwlink_snapshot(1)["agent_version"] == "3.1.0"
    await gw.close()


async def test_a_garbage_claim_does_not_break_the_session(services, link):
    """Токен пометки едет тем же каналом, разбирается общим кодом и может быть
    любым. Отказ в разборе — запись в журнал, а не разрыв."""
    gw = await _connect(link)
    await gw.send("hello", {"proto": 1, "agent": "3.1.0"})
    await gw.send("claim", {"token": "GW1:не-токен.совсем"})
    await gw.send("claim", {"token": gwsign.sign(PRIV, "claim", "ЧУЖОЙ-КЛЮЧ==", "pi")})
    await gw.send("snap", {**SNAP, "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1))
    assert services.gwlink_snapshot(1)["agent_version"] == "3.1.0", "claim увёл сессию с собой"
    await gw.close()


# ── слоты ────────────────────────────────────────────────────────────────────

async def test_the_second_session_of_a_slot_evicts_the_first(services, link):
    """Малина перезагрузилась, старая сессия висит полуоткрытой. Вторая обязана
    вытеснить первую: иначе снимки поехали бы в закрытый сокет, а канал
    считался бы живым."""
    first = await _connect(link)
    await first.send("hello", {"proto": 1, "agent": "3.1.0"})
    await first.send("snap", {**SNAP, "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1))

    second = await _connect(link)
    await second.send("hello", {"proto": 1, "agent": "3.1.0"})
    await second.send("snap", {**SNAP, "agent_version": "3.2.0", "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1)["agent_version"] == "3.2.0")

    assert services.gwlink_snapshot(1)["agent_version"] == "3.2.0"
    assert services.gwlink_session(1), "вытеснение первой сессии погасило и вторую"
    assert link.online(1) is True
    assert await first.role()
    assert b"" == await first.reader.read(1), "старую сессию не закрыли"
    await first.close()
    await second.close()


def test_a_slot_is_recognised_by_the_pair_of_addresses_not_by_one_of_them(
        services, make_active_client):
    """Слот — это пара «на какой адрес линка пришли» и «с какого адреса».
    Хватило бы одного адреса, и сосед по туннелю, подделав источник, оказался бы
    в чужом слоте."""
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    a = services.add_device(admin.id, "NASPi")
    b = services.add_device(admin.id, "Pi2")
    services.db.gateway_add(a.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.db.gateway_add(b.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    srv = linkserver.LinkServer(services)

    assert srv._slot_for("10.99.99.1", "10.99.99.2").id == 1
    assert srv._slot_for("10.99.99.5", "10.99.99.6").id == 2
    assert srv._slot_for("10.99.99.1", "10.99.99.6") is None, "адрес пира из чужого линка"
    assert srv._slot_for("10.99.99.5", "10.99.99.2") is None, "пришли не на тот адрес линка"
    assert srv._slot_for("10.8.1.5", "10.8.1.6") is None, "клиентская подсеть — не линк"
    assert srv._slot_for("10.99.99.1", "10.99.99.1") is None, "адрес ВПС выдан за адрес шлюза"


async def test_the_listener_follows_the_slots(services, link):
    """Привязка к адресу существует, только пока поднят линк, а слот могли
    завести или снять. Такт живости перевешивает слушатель — иначе новый слот
    остался бы без канала до перезапуска бота."""
    before = link._bound
    services.db.gateway_delete(1)
    await link.ensure()
    assert link._bound == () and link._servers == {}, "слушатель остался на снятом слоте"
    admin = services.db.get_client_by_tg(ADMIN)
    pi = services.add_device(admin.id, "NASPi-2")
    services.db.gateway_add(pi.device_id, "awglink", 443, "127.0.0.0/30", slot_id=1)
    await link.ensure()
    assert link._bound == before, "слушатель не вернулся на адрес заведённого слота"


async def test_removing_the_slot_forgets_everything_the_channel_left(services, link, monkeypatch):
    """Снятие слота уносит снимок и сессию вместе с ключами бандла: чужая
    квартира не должна проступать в карточке следующего шлюза."""
    gw = await _connect(link)
    await gw.send("hello", {"proto": 1, "agent": "3.1.0"})
    await gw.send("snap", {**SNAP, "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1))
    await gw.close()

    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: None)
    services.gateway_remove(1)
    assert services.gwlink_snapshot(1) == {} and services.gwlink_session(1) == {}
    assert services.gwlink_snapshot_age(1) is None
    for key in ("gwlink_snap_1", "gwlink_snap_rev_1", "gwlink_snap_at_1",
                "gwlink_snap_ts_1", "gwlink_seen_1"):
        assert not services.db.get_state(key), f"ключ {key} пережил снятие слота"


# ── хранение ─────────────────────────────────────────────────────────────────

async def test_the_snapshot_survives_a_restart_of_the_bot(services, link, db):
    """Процесс бота умер — сокеты закрыло ядро, агент вернётся сам с полным
    снимком. Но до его возвращения экран обязан показывать последнее известное,
    а не пустоту."""
    gw = await _connect(link)
    await gw.send("hello", {"proto": 1, "agent": "3.1.0"})
    await gw.send("snap", {**SNAP, "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1))
    await gw.close()

    from awgbot.domain.services import Services
    fresh = Services(db)
    assert fresh.gwlink_snapshot(1)["bundle"]["home_subnets"] == "192.168.68.0/24"
    assert fresh.gwlink_snapshot_age(1) is not None
    assert fresh.gwlink_snapshot(1)["agent_version"] == "3.1.0"


async def test_a_session_left_in_the_state_must_not_survive_the_restart_of_the_bot(services, link, db):
    """Процесс бота умер — сокетов больше нет, и ни одной живой сессии тоже.
    Строка `gwlink_session_<id>` в БД при этом остаётся от прошлой жизни, и
    карточка слота по ней зажигает «🟢 на связи» вместе с кнопкой «Обновить с
    шлюза», которая отвечает «канал не на связи». Пока агент возвращается
    (5–15 с) это неприятно, а если канал у него выключен перевыпуском или линк
    лежит — карточка врёт до бесконечности."""
    gw = await _connect(link)
    await gw.send("hello", {"proto": 1, "agent": "3.1.0"})
    await gw.send("snap", {**SNAP, "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1))
    # процесс бота исчез, не закрыв ничего: сокеты закрыло ядро, БД осталась;
    # новый процесс первым делом сбрасывает сессии (main, до поллинга)
    from awgbot.domain.services import Services
    fresh = Services(db)
    assert fresh.gwlink_session(1), "строка прошлой жизни в БД есть — это и есть ловушка"
    fresh.gwlink_sessions_reset()
    assert fresh.gwlink_session(1) == {}, "после рестарта бота сессий нет — спрашивать некого"
    assert fresh.gwlink_card(fresh.db.gateway(1), 10)["online"] is False
    await gw.close()


def test_age_is_counted_by_the_server_clock_not_by_the_clock_of_the_raspberry(services, db):
    """Машина без RTC, поднявшаяся раньше сети, ушла на час вперёд. Считай ВПС
    возраст по её `ts`, свежий снимок выглядел бы часовой давностью — или,
    при ошибке в другую сторону, «минус три минуты»."""
    import datetime

    from awgbot.util import timeutil
    ahead = timeutil.to_iso(timeutil.now() + datetime.timedelta(hours=1))
    services.gwlink_snapshot_in(1, {**SNAP, "ts": ahead}, 1, True)
    age = services.gwlink_snapshot_age(1)
    assert age is not None and age < 60, f"возраст поехал за часами малины: {age} с"
    assert services.db.get_state("gwlink_snap_ts_1") == ahead, (
        "часы малины хранятся справочно — по ним и видно, что они разошлись")


def test_without_a_snapshot_there_is_no_age_and_no_verdict(services):
    """Шлюз ещё не сообщал, что у него стоит. Пустой список расхождений значит
    «сказать нечего», а не «всё сошлось»: выдумывать вердикт из ничего —
    худшее, что может сделать экран."""
    assert services.gwlink_snapshot(1) == {} and services.gwlink_snapshot_age(1) is None
    assert services.gwlink_card(type("G", (), {"id": 1, "lan_mode": 0})(), 10)["has_snap"] is False


def test_a_broken_row_in_the_state_does_not_take_the_screen_down(services):
    """В БД могла остаться битая строка (откат версии, ручная правка). Экран
    обязан открыться и показать «снимка нет», а не упасть."""
    services.db.set_state("gwlink_snap_1", "{это не json")
    assert services.gwlink_snapshot(1) == {}
    services.db.set_state("gwlink_snap_1", '["список"]')
    assert services.gwlink_snapshot(1) == {}
    services.db.set_state("gwlink_snap_at_1", "вчера")
    assert services.gwlink_snapshot_age(1) is None


def test_sanitized_snapshot_and_deltas_add_up_to_the_same_object(services):
    """Сквозная проверка нумерации: снимок плюс три дельты — ровно тот же
    объект, что полный снимок в конце. Иначе через неделю дельт карточка
    показывала бы то, чего на шлюзе нет."""
    services.gwlink_snapshot_in(1, {**SNAP, "rev": 1}, 1, True)
    steps = [{"bundle": {**SNAP["bundle"], "lan_mode": "0"}},
             {"agent_version": "3.2.0"},
             {"egress_ok": False}]
    for rev, patch in enumerate(steps, start=2):
        assert services.gwlink_snapshot_in(1, {**patch, "rev": rev}, rev, False) is True
    final = {**SNAP, "bundle": {**SNAP["bundle"], "lan_mode": "0"},
             "agent_version": "3.2.0", "egress_ok": False}
    got = services.gwlink_snapshot(1)
    assert {k: v for k, v in got.items() if k != "rev"} == gwsnapshot.sanitize(final)
    assert services.db.get_state("gwlink_snap_rev_1") == "4", "номер последнего сообщения сессии"


def test_a_delta_out_of_order_is_refused_and_the_stored_snapshot_stands(services):
    """Пропуск номера — сигнал попросить полный снимок, а не повод наложить
    что дали."""
    services.gwlink_snapshot_in(1, {**SNAP, "rev": 1}, 1, True)
    assert services.gwlink_snapshot_in(1, {"egress_ok": False, "rev": 3}, 3, False) is False
    assert services.gwlink_snapshot_in(1, {"egress_ok": False, "rev": 1}, 1, False) is False
    assert services.gwlink_snapshot(1)["egress_ok"] is True
    assert services.gwlink_snapshot_in(1, {"egress_ok": False, "rev": 2}, 2, False) is True
    assert services.gwlink_snapshot(1)["egress_ok"] is False


def test_a_new_session_starts_the_numbering_over(services):
    """`rev` — счётчик сессии, а не вечный: каждое подключение начинается с
    полного снимка, и хранить номер значило бы писать в БД на флеш-карте на
    каждое изменение."""
    services.gwlink_snapshot_in(1, {**SNAP, "rev": 1}, 1, True)
    services.gwlink_snapshot_in(1, {"egress_ok": False, "rev": 2}, 2, False)
    services.gwlink_session_closed(1)
    assert services.db.get_state("gwlink_snap_rev_1") == ""
    assert services.gwlink_snapshot_in(1, {"agent_version": "3.2.0", "rev": 1}, 1, False) is False, (
        "дельта новой сессии легла без полного снимка")
    assert services.gwlink_snapshot_in(1, {**SNAP, "rev": 1}, 1, True) is True


# ── оба конца сразу ──────────────────────────────────────────────────────────

class _Agent:
    """Агент на том конце: отдаёт снимок и токен пометки, больше ничего."""

    def __init__(self, snap: dict, token: str = ""):
        self.snap = snap
        self.token = token
        self.channel = ChannelState()      # сюда клиент пишет «на связи» и байты канала

    def gw_snapshot(self) -> dict:
        import copy
        return copy.deepcopy(self.snap)

    def gateway_claim_if_needed(self):
        return self.token or None

    def lan_feeds_applied_hash(self) -> str:
        return ""                      # фидов локальной сети из канала не применяли

    def set_link_role(self, active: bool) -> None:
        self.role = active


async def test_the_real_client_and_the_real_server_agree_on_the_wire(
        services, link, tmp_path, monkeypatch):
    """Конверт и состав снимка живут в общем коде, но исполняются на разных
    хостах и обновляются порознь. Разойдись формат на концах — канал молча не
    поднимется, и увидят это на живой малине, куда ещё надо дойти.

    Здесь оба конца настоящие: боевой клиент коннектится к боевому слушателю
    через петлю и проходит весь сценарий этапа — подключение, снимок, дельта по
    событию, просьба с ВПС прислать полный."""
    from awgbot.runtime import linkclient
    conf = tmp_path / "awglink.conf"
    conf.write_text(f"[Interface]\nPrivateKey = {PRIV}\n", encoding="utf-8")
    monkeypatch.setattr(config, "GW_LINK_CONF", str(conf))
    host, port = link._bound[0]
    monkeypatch.setattr(linkclient, "server_address", lambda: host)
    monkeypatch.setattr(linkclient, "server_port", lambda: port)

    agent = _Agent({**SNAP})
    client = linkclient.LinkClient(agent)
    client.start()
    try:
        await _until(lambda: services.gwlink_snapshot(1))
        assert services.gwlink_snapshot(1)["bundle"]["home_subnets"] == "192.168.68.0/24"
        assert services.gwlink_session(1)["agent"] == config.INSTALLED_VERSION
        sent_after_hello = client._sent_bytes

        # событие на малине: шлюз потерял выход наружу. Событие — не из
        # настроек: смена подсетей на шлюзе с этапа 2 вызывает ответ сервера
        # настройками, это проверяет test_gwlink_settings_delivery.py.
        agent.snap["egress_ok"] = False
        await client.push()
        await _until(lambda: services.gwlink_snapshot(1)["egress_ok"] is False)
        assert services.gwlink_snapshot(1)["agent_version"] == "3.1.0", "дельта унесла лишнее"

        # тик без событий — по каналу не уходит ни байта
        bytes_before_idle = client._sent_bytes
        assert await client.push() is False
        assert client._sent_bytes == bytes_before_idle > sent_after_hello

        # человек нажал «Обновить»: ВПС просит полный снимок
        agent.snap["plumbing_gen"] = "old"
        assert await link.send(1, "ask", {"what": "snap"}) is True
        await _until(lambda: services.gwlink_snapshot(1)["plumbing_gen"] == "old")
        assert services.db.get_state("gwlink_snap_rev_1") == "1", "нумерация началась заново"

        # оба конца считают байты канала там, где их читает зонд живости:
        # агент — слот 0 своих сервисов, ВПС — слот шлюза
        assert agent.channel.online is True, "домен агента не знает, что сессия открыта"
        gw_io, vps_io = agent.channel.traffic.get(0), services.channel.traffic.get(1)
        assert gw_io and gw_io.tx >= client._sent_bytes and gw_io.rx > 0, "агент не считает байты канала"
        assert vps_io and vps_io.rx >= client._sent_bytes and vps_io.tx > 0, "ВПС не считает байты канала"
    finally:
        await client.stop()
    await _until(lambda: not services.gwlink_session(1))
    assert services.gwlink_session(1) == {}, "остановленный агент оставил сессию открытой"
    assert agent.channel.online is False, "сессия кончилась, а домен агента считает её открытой"
