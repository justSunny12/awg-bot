"""
Клиент канала на шлюзе (runtime/linkclient): жизнь сессии — бэкофф, ключ,
таймаут коннекта, рубильник, переподключение по восстановлению линка, снимок
сразу после операции из чата агента.

Сети нет: ВПС на том конце — `_Vps`, подставленный вместо
`asyncio.open_connection`, он отдаёт заранее записанные строки и записывает,
что ему прислали. Всё остальное — боевой код клиента, включая настоящий
`_connect_once` и цикл `_run`.

Цена ошибок здесь — ритм в линке: сессия, которую сервер закрывает сразу
(чужой ключ, чужой слот), без роста шага
превращается в стук каждые пять секунд — ровный маячок внутри туннеля и запись
на SD на каждом коннекте. И обратное — полуоткрытая сессия после жёсткого
ребута ВПС, которая висит «на связи» днями и глушит своё скачивание фидов.
"""
from __future__ import annotations

import asyncio
import base64
import os
import time

import pytest

from awgbot.core import config
from awgbot.domain.channelstate import ChannelState
from awgbot.infra import gwguard
from awgbot.runtime import linkclient
from awgbot.util import gwlink

pytestmark = pytest.mark.unit

PRIV = base64.b64encode(os.urandom(32)).decode()
PRIV2 = base64.b64encode(os.urandom(32)).decode()
KEY = gwlink.channel_key(PRIV)


class _Agent:
    """Агент шлюза: ровно то, что клиент канала у него спрашивает."""

    def __init__(self):
        self.snap = {"bundle": {"lan_mode": "1"}, "egress_ok": True, "agent_version": "3.1.0"}
        self.roles: list[bool] = []
        self.link_up = True
        self.touched = 0
        self.channel = ChannelState()

    def lan_feeds_applied_hash(self) -> str:
        return ""

    def gw_snapshot(self) -> dict:
        return dict(self.snap)

    def gateway_claim_if_needed(self):
        return None

    def lan_feeds_touch(self, digest: str = "") -> None:
        self.touched += 1

    def set_link_role(self, active: bool) -> None:
        self.roles.append(active)

    # для on_tick: что показал последний тик монитора
    def cached_status(self, max_age):
        return object()

    def link_ok(self, st) -> bool:
        if isinstance(self.link_up, Exception):
            raise self.link_up
        return self.link_up


class _Conn:
    """Одна TCP-сессия с ВПС: читает записанные строки, потом EOF (или висит)."""

    def __init__(self, lines, hang: bool = False):
        self.lines = list(lines)
        self.hang = hang
        self.written: list[bytes] = []
        self.closed = False
        self._gone = asyncio.Event()

    async def readline(self) -> bytes:
        if self.lines:
            line = self.lines.pop(0)
            # ответ сервера собирается по hello, который клиент уже прислал:
            # подпись — нонсом клиента, первое слово несёт нонс сервера
            return line(self) if callable(line) else line
        if self.hang:
            await self._gone.wait()               # сессия открыта, ВПС молчит
        return b""

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        self._gone.set()

    async def wait_closed(self) -> None:
        pass

    def hello(self, key: bytes = KEY) -> dict:
        """Разобранный hello, пришедший в эту сессию (подписан без нонса)."""
        return gwlink.unpack(key, self.written[0])


SN = b"S" * gwlink.NONCE_BYTES            # нонс сервера в записанных сессиях


def _reply(key: bytes, kind: str, body: dict, *, seq: int = 1, first: bool = True,
           sign_key: bytes | None = None):
    """Строка сервера для этой сессии: подписана нонсом из hello клиента,
    первая несёт нонс сервера — как у боевого слушателя."""
    def make(conn: "_Conn") -> bytes:
        cn = gwlink.nonce_from(conn.hello(key).get("nonce"))
        out = dict(body)
        if first:
            out["nonce"] = gwlink.nonce_b64(SN)
        return gwlink.pack(sign_key or key, kind, out, seq=seq, nonce=cn)
    return make


class _Vps:
    """ВПС на том конце: очередь сессий. Каждое подключение забирает следующую."""

    def __init__(self, monkeypatch):
        self.sessions: list[_Conn] = []
        self.connects = 0

        async def open_connection(host, port, limit=None):
            self.connects += 1
            conn = self.sessions.pop(0)
            return conn, conn

        monkeypatch.setattr(linkclient.asyncio, "open_connection", open_connection)
        monkeypatch.setattr(linkclient, "server_address", lambda: "10.99.99.1")
        monkeypatch.setattr(linkclient, "server_port", lambda: gwlink.DEFAULT_PORT)

    def hangs_up(self) -> _Conn:
        """Сервер закрыл сразу, не сказав ни слова: чужая подпись, чужой слот."""
        c = _Conn([])
        self.sessions.append(c)
        return c

    def answers(self, key: bytes = KEY, active: bool = True, **kw) -> _Conn:
        """Настоящая сессия: сервер сказал роль (первое, что он говорит после hello)."""
        c = _Conn([_reply(key, "role", {"active": active})], **kw)
        self.sessions.append(c)
        return c

    def forged(self) -> _Conn:
        """На том конце не наш ВПС: подпись чужим ключом."""
        other = gwlink.channel_key(base64.b64encode(os.urandom(32)).decode())
        c = _Conn([_reply(KEY, "role", {"active": True}, sign_key=other)])
        self.sessions.append(c)
        return c


@pytest.fixture()
def conf(tmp_path, monkeypatch):
    f = tmp_path / "awglink.conf"
    f.write_text(f"[Interface]\nPrivateKey = {PRIV}\n", encoding="utf-8")
    monkeypatch.setattr(config, "GW_LINK_CONF", str(f))
    return f


@pytest.fixture()
def vps(monkeypatch, conf):
    return _Vps(monkeypatch)


def _delays(monkeypatch, stop_after: int) -> list[float]:
    """Паузы между попытками; после `stop_after`-й цикл останавливается."""
    got: list[float] = []

    async def fake_sleep(seconds):
        got.append(seconds)
        if len(got) >= stop_after:
            raise asyncio.CancelledError

    monkeypatch.setattr(linkclient.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(linkclient.random, "uniform", lambda lo, hi: 1.0)
    return got


# ── бэкофф ───────────────────────────────────────────────────────────────────

async def test_a_server_that_hangs_up_at_once_does_not_reset_the_backoff(vps, monkeypatch):
    """Чужой ключ или чужой слот: сервер отвергает hello и закрывает сессию
    сразу. Раньше любая «состоявшаяся»
    сессия обнуляла счёт — выходил стук каждые пять секунд без роста шага.
    Теперь шаг растёт, пока сервер не ответит хоть одним подписанным словом."""
    for _ in range(4):
        vps.hangs_up()
    delays = _delays(monkeypatch, stop_after=4)
    with pytest.raises(asyncio.CancelledError):
        await linkclient.LinkClient(_Agent())._run()
    assert vps.connects == 4
    assert delays == [5, 15, 45, 135], f"шаг не растёт у сессий, закрытых сразу: {delays}"


async def test_a_forged_answer_counts_as_no_answer(vps, monkeypatch):
    """Ответ чужим ключом — не ответ: сессия рвётся, и сбрасывать шаг не за что."""
    vps.forged(); vps.forged(); vps.forged()
    agent = _Agent()
    delays = _delays(monkeypatch, stop_after=3)
    with pytest.raises(asyncio.CancelledError):
        await linkclient.LinkClient(agent)._run()
    assert delays == [5, 15, 45], f"чужая подпись сбросила бэкофф: {delays}"
    assert agent.roles == [], "роль из сообщения с чужой подписью применена"


async def test_one_real_answer_starts_the_count_over(vps, monkeypatch):
    """Сервер ответил подписанным сообщением — сессия была настоящей, и после
    её конца незачем ждать пять минут: следующая попытка снова с малого шага."""
    vps.hangs_up(); vps.hangs_up(); vps.answers(); vps.hangs_up(); vps.hangs_up()
    agent = _Agent()
    delays = _delays(monkeypatch, stop_after=5)
    with pytest.raises(asyncio.CancelledError):
        await linkclient.LinkClient(agent)._run()
    assert delays == [5, 15, 5, 15, 45], f"счёт после настоящей сессии: {delays}"
    assert agent.roles == [True]


# ── ключ и коннект ───────────────────────────────────────────────────────────

async def test_the_link_key_is_read_anew_for_every_connection(vps, conf, monkeypatch):
    """Бандл из чата привёз новый ключ линка, а процесс агента тот же. С ключом,
    закешированным с прошлой сессии, каждая следующая отвергалась бы — канал
    мёртв до перезапуска агента, и никто не понимает почему."""
    agent = _Agent()
    client = linkclient.LinkClient(agent)
    first = vps.answers(active=True)
    await client._connect_once()
    assert agent.roles == [True]
    conf.write_text(f"[Interface]\nPrivateKey = {PRIV2}\n", encoding="utf-8")   # применили новый бандл
    second = vps.answers(key=gwlink.channel_key(PRIV2), active=False)
    await client._connect_once()
    assert agent.roles == [True, False], "вторая сессия подписана новым ключом, а клиент держит старый"
    hello = gwlink.unpack(gwlink.channel_key(PRIV2), second.written[0])
    assert hello["t"] == "hello", "hello новой сессии подписан старым ключом"
    assert gwlink.unpack(KEY, first.written[0])["t"] == "hello"


async def test_a_connect_that_hangs_is_given_up_in_bounded_time(monkeypatch, conf):
    """SYN дропнут где-то по пути: без таймаута попытка висела бы до системного
    — минуты, — и бэкофф считал бы не то. Здесь коннект не отвечает вовсе;
    клиент обязан сдаться сам, за ограниченное время."""
    seen: list = []
    real_wait_for = asyncio.wait_for

    async def open_connection(host, port, limit=None):
        await asyncio.Event().wait()             # ни ответа, ни отказа

    async def fast_wait_for(aw, timeout):
        seen.append(timeout)                     # сколько клиент готов ждать
        return await real_wait_for(aw, 0.05 if timeout is not None else None)

    monkeypatch.setattr(linkclient.asyncio, "open_connection", open_connection)
    monkeypatch.setattr(linkclient.asyncio, "wait_for", fast_wait_for)
    monkeypatch.setattr(linkclient, "server_address", lambda: "10.99.99.1")
    client = linkclient.LinkClient(_Agent())
    t0 = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await client._connect_once()
    assert time.monotonic() - t0 < 2, "висящий коннект не брошен"
    assert seen and seen[0] is not None and 0 < seen[0] <= 60, (
        f"коннект без таймаута или с таймаутом в {seen}: попытка висела бы минутами")
    assert client._writer is None


async def test_the_session_mark_follows_the_session(vps):
    """Признак «сессия открыта» живёт у домена агента: пока канал на связи, своё
    скачивание фидов молчит. Сессия кончилась — признак снят, иначе после
    обрыва малина не пошла бы за фидами никогда."""
    agent = _Agent()
    client = linkclient.LinkClient(agent)
    seen: list = []
    agent.set_link_role = lambda a: seen.append(agent.channel.online)
    vps.answers()
    await client._connect_once()
    assert seen == [True], "во время сессии признак не выставлен"
    assert agent.channel.online is False, "сессия кончилась, а признак остался"
    assert agent.touched == 1, "конец сессии не отметил запас на своё скачивание фидов"


# ── рубильник ────────────────────────────────────────────────────────────────

@pytest.fixture()
def gateway_role(monkeypatch):
    env = {"LINK_CHANNEL": "1"}
    monkeypatch.setattr(config, "ROLE", "gateway")
    monkeypatch.setattr(gwguard, "unit_env", lambda k: env.get(k, ""))
    monkeypatch.setattr(linkclient, "_client", None)
    yield env
    c = linkclient._client
    if c is not None and c._task is not None:
        c._task.cancel()


async def test_a_bundle_that_turns_the_channel_off_stops_a_running_client(vps, gateway_role):
    """Рубильник канала — перевыпуск конфигурации с LINK_CHANNEL=0. Раньше
    клиент, раз поднявшись, жил до перезапуска агента: человек выключил
    функцию, а малина продолжала держать сессию до ВПС."""
    agent = _Agent()
    conn = vps.answers(hang=True)
    client = linkclient.ensure(agent)
    assert client is not None
    for _ in range(50):
        if linkclient.online():
            break
        await asyncio.sleep(0.01)
    assert linkclient.online(), "сессия не открылась — сценарий собран не так"
    task = client._task

    gateway_role["LINK_CHANNEL"] = "0"
    assert linkclient.ensure(agent) is None
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)           # не погашена — TimeoutError, а не вечное ожидание
    assert conn.closed, "сессия до ВПС осталась открытой после выключения канала"
    assert not linkclient.online() and client._task is None
    assert agent.channel.online is False


async def test_the_channel_off_from_the_start_starts_nothing(gateway_role):
    gateway_role["LINK_CHANNEL"] = "0"
    assert linkclient.ensure(_Agent()) is None
    assert linkclient._client is None, "клиент заведён при выключенном канале"


# ── переподключение по восстановлению линка ─────────────────────────────────

class _Wire(_Conn):
    """Сокет открытой сессии для тестов тика: записывает всё, что ушло."""

    def __init__(self):
        super().__init__([])


def _live_client(agent) -> linkclient.LinkClient:
    """Клиент с «открытой» сессией и задачей, которую ensure() не перезапустит."""
    client = linkclient.LinkClient(agent)
    client._writer = _Wire()
    client._sn = SN                              # сессия открыта: нонс сервера известен
    client._task = asyncio.get_running_loop().create_future()
    linkclient._client = client
    return client


async def test_a_link_that_came_back_reconnects_the_channel(gateway_role, conf):
    """Жёсткий ребут ВПС не шлёт FIN, а агент в простое ничего не отправляет и
    RST не получит: сессия висела бы «открытой» днями, подавляя своё скачивание
    фидов. Линк лёг и поднялся — прежней сессии на той стороне нет наверняка."""
    agent = _Agent()
    client = _live_client(agent)
    await client.push(full=True)
    wire = client._writer
    agent.link_up = False
    await linkclient.on_tick(agent)
    assert not wire.closed, "лежащий линк — ещё не повод рвать сессию"
    agent.link_up = True
    sent = len(wire.written)
    await linkclient.on_tick(agent)
    assert wire.closed, "линк вернулся, а полуоткрытая сессия осталась висеть"
    assert len(wire.written) == sent, "в сессию, которую рвём, ещё что-то отправили"


async def test_a_steady_link_never_reconnects_the_channel(gateway_role, conf):
    """Переподключение — только по переходу «лежал → поднялся». Линк просто жив
    (или о прошлом тике ничего не известно) — сессию не трогаем: лишний
    коннект — лишний повод заговорить."""
    agent = _Agent()
    client = _live_client(agent)
    await client.push(full=True)
    wire = client._writer
    for _ in range(3):
        await linkclient.on_tick(agent)
    assert not wire.closed
    agent.link_up = RuntimeError("снимок тика не прочитался")
    await linkclient.on_tick(agent)
    agent.link_up = True
    await linkclient.on_tick(agent)
    assert not wire.closed, "непрочитанный тик принят за «линк лежал»"


async def test_a_tick_still_sends_what_changed(gateway_role, conf):
    agent = _Agent()
    client = _live_client(agent)
    await client.push(full=True)
    agent.snap["egress_ok"] = False
    await linkclient.on_tick(agent)
    kinds = [gwlink.unpack(KEY, x, nonce=SN)["t"] for x in client._writer.written]
    assert kinds == ["snap", "delta"], f"тик не отправил изменившееся: {kinds}"


# ── снимок сразу после операции из чата ─────────────────────────────────────

async def test_poke_sends_the_change_right_away(gateway_role, conf):
    """Человек применил бандл или мастер восстановления и смотрит на карточку
    слота на ВПС. Ждать тика монитора (минуты) — значит показывать ему старое."""
    agent = _Agent()
    client = _live_client(agent)
    await client.push(full=True)
    agent.snap["bundle"] = {"lan_mode": "0"}
    await linkclient.poke(agent)
    msgs = [gwlink.unpack(KEY, x, nonce=SN) for x in client._writer.written]
    assert [m["t"] for m in msgs] == ["snap", "delta"]
    assert msgs[-1]["bundle"] == {"lan_mode": "0"}
    await linkclient.poke(agent)
    assert len(client._writer.written) == 2, "повторный poke без изменений что-то отправил"


async def test_poke_without_the_channel_is_quiet(monkeypatch):
    """Канал не включён бандлом (или это не шлюз) — операция из чата не должна
    ни падать, ни поднимать клиента."""
    monkeypatch.setattr(linkclient, "_client", None)
    monkeypatch.setattr(config, "ROLE", "client")
    await linkclient.poke(_Agent())
    assert linkclient._client is None
    monkeypatch.setattr(config, "ROLE", "gateway")
    monkeypatch.setattr(gwguard, "unit_env", lambda k: "")
    await linkclient.poke(_Agent())
    assert linkclient._client is None


async def test_poke_survives_a_dead_socket(gateway_role, conf):
    """Сессия умерла между тиками: итог операции в чате агента важнее снимка на
    ВПС — исключение из poke уронило бы хендлер после успешного применения."""
    agent = _Agent()
    client = _live_client(agent)
    await client.push(full=True)

    def broken(data):
        raise ConnectionResetError("линк упал")
    client._writer.write = broken
    agent.snap["egress_ok"] = False
    await linkclient.poke(agent)                     # не бросает


async def test_the_settings_answer_carries_the_outcome_and_no_fingerprint(gateway_role, conf):
    """Ответ на настройки — только итог применения. Что доставка завершена,
    сервер видит по снимку, который уходит следом: отпечаток в ответе был
    вторым источником правды, расходившимся со снимком после отката."""
    agent = _Agent()
    agent.apply_link_settings = lambda values: {"ok": True, "changed": ["HOME_SUBNETS"], "error": ""}
    agent.link_settings_note = lambda result: "применены"
    client = _live_client(agent)
    await client.push(full=True)
    agent.snap["bundle"] = {"home_subnets": "192.168.70.0/24"}
    await client._apply_settings({"HOME_SUBNETS": "192.168.70.0/24"})
    msgs = [gwlink.unpack(KEY, x, nonce=SN) for x in client._writer.written]
    ack = next(m for m in msgs if m["t"] == "ack")
    assert set(ack) - {"t", "seq", "ts"} == {"ok", "changed", "error"}, f"в ответе лишнее: {sorted(ack)}"
    assert ack["ok"] is True and ack["changed"] == ["HOME_SUBNETS"]
    assert msgs[-1]["t"] == "delta" and msgs[-1]["bundle"] == {"home_subnets": "192.168.70.0/24"}, (
        "снимок с новыми значениями не ушёл следом за ответом")


# ── нонсы сессии (proto 2) ───────────────────────────────────────────────────

async def test_hello_names_our_nonce_and_everything_after_is_signed_with_the_servers(vps):
    """Повтор отсекают нонсы: hello несёт нонс шлюза, дальше шлюз подписывает
    нонсом сервера. Подпиши клиент снимок чем-то другим — слушатель на ВПС
    оборвёт каждую сессию на первом же снимке, и канал не поднимется ни на
    одной малине."""
    conn = vps.answers()
    await linkclient.LinkClient(_Agent())._connect_once()
    hello = conn.hello()
    assert hello["t"] == "hello" and hello["proto"] == gwlink.PROTO
    assert len(gwlink.nonce_from(hello.get("nonce"))) == gwlink.NONCE_BYTES, "hello не назвал нонс шлюза"
    assert len(conn.written) >= 2, "после ответа сервера снимок не ушёл"
    snap = gwlink.unpack(KEY, conn.written[1], nonce=SN)
    assert snap["t"] == "snap", f"вторым ушло «{snap['t']}»"
    with pytest.raises(gwlink.ProtocolError):
        gwlink.unpack(KEY, conn.written[1])      # без нонса сервера подпись не сходится


async def test_every_session_gets_a_fresh_nonce(vps):
    """Нонс шлюза — свой у каждой сессии. Повтори его клиент, записанный ответ
    сервера из прошлой сессии прошёл бы проверку в новой."""
    agent = _Agent()
    client = linkclient.LinkClient(agent)
    first, second = vps.answers(), vps.answers()
    await client._connect_once()
    await client._connect_once()
    assert first.hello()["nonce"] != second.hello()["nonce"], "нонс шлюза повторился между сессиями"


async def test_a_first_server_word_without_its_nonce_ends_the_session(vps, monkeypatch):
    """Первое слово сервера обязано нести его нонс: без него клиенту нечем
    подписывать, и любая его отправка была бы отвергнута. Такой ответ — не
    ответ: сессия рвётся, роль не применяется, шаг бэкоффа растёт."""
    for _ in range(3):
        vps.sessions.append(_Conn([_reply(KEY, "role", {"active": True}, first=False)]))
    agent = _Agent()
    delays = _delays(monkeypatch, stop_after=3)
    with pytest.raises(asyncio.CancelledError):
        await linkclient.LinkClient(agent)._run()
    assert agent.roles == [], "роль из сообщения без нонса сервера применена"
    assert agent.channel.online is False
    assert delays == [5, 15, 45], f"ответ без нонса сбросил бэкофф: {delays}"


async def test_a_server_word_from_another_session_is_refused(vps, monkeypatch):
    """Записанный ответ сервера из прошлой сессии (подписан прошлым нонсом
    шлюза) под верным ключом — это старая правда: роль «несёшь трафик» от
    вчерашнего переключения. Клиент обязан его отвергнуть."""
    old = gwlink.new_nonce()
    stale = gwlink.pack(KEY, "role", {"active": True, "nonce": gwlink.nonce_b64(SN)}, seq=1, nonce=old)
    vps.sessions.append(_Conn([stale]))
    agent = _Agent()
    client = linkclient.LinkClient(agent)
    with pytest.raises(gwlink.ProtocolError):
        await client._connect_once()
    assert agent.roles == [], "ответ чужой сессии применён"
    assert client._confirmed is False and agent.channel.online is False


async def test_later_server_words_from_another_session_end_it(vps):
    """Внутри сессии каждое слово сервера проверяется нонсом шлюза: подсунутое
    посреди живой сессии сообщение прошлой рвёт её, а не применяется."""
    old = gwlink.new_nonce()
    stale = gwlink.pack(KEY, "role", {"active": False}, seq=2, nonce=old)
    vps.sessions.append(_Conn([_reply(KEY, "role", {"active": True}), stale,
                               _reply(KEY, "role", {"active": False}, seq=3, first=False)]))
    agent = _Agent()
    await linkclient.LinkClient(agent)._connect_once()
    assert agent.roles == [True], f"слово чужой сессии применено или сессия не порвана: {agent.roles}"


async def test_nothing_goes_out_before_the_server_named_its_nonce(vps):
    """Пока сервер не ответил, нонса для подписи нет: снимок из тика или poke,
    ушедший в этот момент, оборвал бы только что открытую сессию. Поэтому до
    первого ответа клиент не «на связи» и отправить ничего не может."""
    conn = _Conn([], hang=True)
    vps.sessions.append(conn)
    agent = _Agent()
    client = linkclient.LinkClient(agent)
    task = asyncio.create_task(client._connect_once())
    for _ in range(50):
        if conn.written:
            break
        await asyncio.sleep(0.01)
    assert len(conn.written) == 1, "hello не ушёл"
    assert agent.channel.online is False, "клиент «на связи», а сервер ещё не ответил"
    assert await client.push(full=True) is False
    assert await client._send("claim", {"token": "x"}) is False
    assert len(conn.written) == 1, "до ответа сервера ушло что-то кроме hello"
    conn.close()
    await asyncio.wait_for(task, 2)


@pytest.mark.parametrize("shift", [3600, -3600])
async def test_server_clock_an_hour_off_does_not_break_the_session(vps, monkeypatch, shift):
    """Малина без RTC поднялась раньше NTP и ушла на час. Раньше канал молча
    умирал ровно тогда, когда должен был сказать «часы разошлись»; теперь часы
    в проверку не входят, и сессия живёт."""
    real_pack = gwlink.pack
    monkeypatch.setattr(linkclient.gwlink, "pack",
                        lambda *a, **kw: real_pack(*a, **{**kw, "now": time.time() + shift}))
    vps.answers()
    agent = _Agent()
    await linkclient.LinkClient(agent)._connect_once()
    assert agent.roles == [True], f"слово сервера с часами {shift:+} с отвергнуто"
