"""
Клиент канала на шлюзе (runtime/linkclient): жизнь сессии — бэкофф, ключ,
таймаут коннекта, когда клиент заводится, переподключение по восстановлению линка, снимок
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
from tests.linkstubs import AgentStub
from awgbot.runtime import linkclient
from awgbot.util import gwlink

pytestmark = pytest.mark.unit

PRIV = base64.b64encode(os.urandom(32)).decode()
PRIV2 = base64.b64encode(os.urandom(32)).decode()
KEY = gwlink.channel_key(PRIV)


class _Agent(AgentStub):
    """Агент шлюза: ровно то, что клиент канала у него спрашивает."""

    def __init__(self):
        self.snap = {"bundle": {"lan_mode": "1"}, "egress_ok": True, "agent_version": "3.1.0"}
        self.roles: list[bool] = []
        self.caps: list = []                      # что сервер сказал о своих возможностях
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

    def set_link_role(self, active: bool, standby=None, name=None, caps=None) -> None:
        self.roles.append(active)
        self.caps.append(caps)

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

    def answers(self, key: bytes = KEY, active: bool = True, caps=None, **kw) -> _Conn:
        """Настоящая сессия: сервер сказал роль (первое, что он говорит после hello);
        caps — возможности сервера в этом слове (None — сервер прежний, поля нет)."""
        body = {"active": active} if caps is None else {"active": active, "caps": caps}
        c = _Conn([_reply(key, "role", body)], **kw)
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
    обрыва шлюз не пошла бы за фидами никогда."""
    agent = _Agent()
    client = linkclient.LinkClient(agent)
    seen: list = []
    agent.set_link_role = lambda *a: seen.append(agent.channel.online)
    vps.answers()
    await client._connect_once()
    assert seen == [True], "во время сессии признак не выставлен"
    assert agent.channel.online is False, "сессия кончилась, а признак остался"
    assert agent.touched == 1, "конец сессии не отметил запас на своё скачивание фидов"


# ── когда клиент заводится ──────────────────────────────────────────────────

@pytest.fixture()
def gateway_role(monkeypatch):
    """Шлюз с юнитом обвязки без единой строки канала: канал обязателен, и
    держится он конфигом линка, а не переключателем в юните."""
    monkeypatch.setattr(config, "ROLE", "gateway")
    monkeypatch.setattr(gwguard, "unit_env", lambda k: "")
    monkeypatch.setattr(linkclient, "_client", None)
    yield
    c = linkclient._client
    if c is not None and c._task is not None:
        c._task.cancel()


async def _until_online(timeout: float = 0.5) -> None:
    for _ in range(int(timeout / 0.01)):
        if linkclient.online():
            return
        await asyncio.sleep(0.01)


async def test_a_unit_without_the_channel_line_still_keeps_the_channel(vps, gateway_role):
    """Юнит обвязки, где строки LINK_CHANNEL нет вовсе (собран до того, как
    канал стал обязательным, или потерял её): канал всё равно держится. Раньше
    такой шлюз молчал — карточка слота на ВПС не получала ни снимка, ни
    списков, а человек не видел, что дело в старой строке юнита."""
    agent = _Agent()
    conn = vps.answers(hang=True)
    client = linkclient.ensure(agent)
    assert client is not None, "конфиг линка на месте, а клиент канала не заведён"
    await _until_online()
    assert linkclient.online(), "канал не открылся при юните без строки LINK_CHANNEL"
    assert linkclient.ensure(agent) is client, "повторный ensure завёл второго клиента"
    assert vps.connects == 1, "повторный ensure открыл вторую сессию"
    assert not conn.closed


async def test_losing_the_link_conf_does_not_tear_down_a_running_client(vps, conf, gateway_role):
    """Конфиг линка пропал на ходу (бандл переписывает его, SD моргнула): новый
    клиент без ключа не заводится, но живую сессию тик гасить не должен —
    иначе каждый перевыпуск конфигурации рвал бы канал на полуслове."""
    agent = _Agent()
    conn = vps.answers(hang=True)
    client = linkclient.ensure(agent)
    await _until_online()
    assert linkclient.online(), "сессия не открылась — сценарий собран не так"
    task = client._task
    conf.unlink()
    assert linkclient.ensure(agent) is None
    await asyncio.sleep(0.05)
    assert not task.done(), "пропажа конфига линка погасила работающего клиента"
    assert linkclient.online() and not conn.closed, "сессия до ВПС закрыта без причины"
    assert linkclient._client is client


async def test_without_the_link_conf_nothing_starts(gateway_role, monkeypatch, tmp_path):
    """Шлюз без конфига линка (обвязка ещё не применена): подписать hello нечем,
    и клиент не заводится вовсе — без стука в никуда каждые пять секунд."""
    monkeypatch.setattr(config, "GW_LINK_CONF", str(tmp_path / "нет-такого.conf"))
    assert linkclient.enabled() is False
    assert linkclient.ensure(_Agent()) is None
    assert linkclient._client is None, "клиент заведён без конфига линка"


async def test_the_channel_view_has_no_switch_left(gateway_role, conf):
    """Панель агента рисует строку канала всегда: в состоянии канала нет поля
    «включён», только «на связи» и роль, и роль без сессии пустая."""
    assert linkclient.view() == {"online": False, "role": ""}


# ── возможности сторон ──────────────────────────────────────────────────────

async def test_hello_names_the_agents_capabilities_as_a_list(vps):
    """Сервер решает, что слать агенту, одним `in` по списку возможностей из
    hello. Нет поля — сервер примет агента за прежнего и не пришлёт ничего
    нового; не список — проверка на ВПС упадёт на первом же подключении."""
    conn = vps.answers()
    await linkclient.LinkClient(_Agent())._connect_once()
    hello = conn.hello()
    assert "caps" in hello, f"hello без возможностей агента: {sorted(hello)}"
    assert hello["caps"] == list(linkclient.common.CAPS), hello["caps"]
    assert isinstance(hello["caps"], list) and all(isinstance(c, str) for c in hello["caps"])


async def test_the_servers_capabilities_from_its_role_reach_the_agent(vps):
    """Первое слово сервера — роль — несёт его возможности: по ним подсказки
    агента говорят, что сервер пришлёт сам. Прежний сервер поля не шлёт — до
    агента доходит пустой список: возможностей у такого сервера нет (откат или
    копия), и сказанное прежним сервером 3.3.0 стирается."""
    vps.answers(caps=["cfg1"])
    vps.answers()
    agent = _Agent()
    client = linkclient.LinkClient(agent)
    await client._connect_once()
    await client._connect_once()
    assert agent.caps == [["cfg1"], []], f"возможности сервера не переданы агенту: {agent.caps}"


def test_the_agent_keeps_the_servers_capabilities_and_drops_junk(tmp_path):
    """Возможности сервера лежат в базе агента до следующей роли. Прежний
    сервер (поля нет) их не стирает; мусор с ВПС — не список, не строки —
    не превращается в возможность, которой у сервера нет."""
    from awgbot.domain.gateway import GatewayServices
    from awgbot.infra.db import Database
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    try:
        svc = GatewayServices(d)
        assert svc.link_server_caps() == [], "сервер ничего не говорил — возможностей нет"
        svc.set_link_role(True, caps=["cfg1", "lists2"])
        assert svc.link_server_caps() == ["cfg1", "lists2"]
        svc.set_link_role(False)
        assert svc.link_server_caps() == ["cfg1", "lists2"], "роль прежнего сервера стёрла возможности"
        svc.set_link_role(True, caps=["cfg1", 7, None, {"x": 1}, ["y"]])
        assert svc.link_server_caps() == ["cfg1"], f"мусор принят за возможность: {svc.link_server_caps()}"
        svc.set_link_role(True, caps="cfg1 lists2")
        assert svc.link_server_caps() == [], "строка вместо списка разобрана как возможности"
        svc.set_link_role(True, caps=[])
        assert svc.link_server_caps() == [], "пустой список не сбросил прежние возможности"
    finally:
        d.close()


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


async def test_poke_without_the_channel_is_quiet(monkeypatch, tmp_path):
    """Конфига линка нет (или это не шлюз) — операция из чата не должна
    ни падать, ни поднимать клиента."""
    monkeypatch.setattr(linkclient, "_client", None)
    monkeypatch.setattr(config, "ROLE", "client")
    await linkclient.poke(_Agent())
    assert linkclient._client is None
    monkeypatch.setattr(config, "ROLE", "gateway")
    monkeypatch.setattr(config, "GW_LINK_CONF", str(tmp_path / "нет-такого.conf"))
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
    одной шлюзе."""
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
    """Шлюз без RTC поднялась раньше NTP и ушла на час. Раньше канал молча
    умирал ровно тогда, когда должен был сказать «часы разошлись»; теперь часы
    в проверку не входят, и сессия живёт."""
    real_pack = gwlink.pack
    monkeypatch.setattr(linkclient.gwlink, "pack",
                        lambda *a, **kw: real_pack(*a, **{**kw, "now": time.time() + shift}))
    vps.answers()
    agent = _Agent()
    await linkclient.LinkClient(agent)._connect_once()
    assert agent.roles == [True], f"слово сервера с часами {shift:+} с отвергнуто"


# ── тик и внеочередной обзор: отказ шага — в журнал, не молча ───────────────

async def test_a_failing_tick_step_is_logged_and_the_next_step_still_runs(gateway_role, conf, caplog):
    """Повтор записей SMB упал (исключение в агенте): сверка своих списков на
    том же тике всё равно идёт, а отказ виден в журнале — раньше его глушили
    молча, и «записи не применяются» нечем было объяснить."""
    import logging
    agent = _Agent()
    reconciled: list[int] = []

    def boom():
        raise RuntimeError("помощник упал")
    agent.services_retry = boom
    agent.own_reconcile = lambda: reconciled.append(1) or False
    agent.own_unsent = lambda: False
    agent.own_retry = lambda: None
    _live_client(agent)
    with caplog.at_level(logging.WARNING, logger="awgbot"):
        await linkclient.on_tick(agent)
    assert reconciled == [1], "после упавшего шага сверка своих списков не пошла"
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("retry_peer_services" in m and "помощник упал" in m for m in warns), warns


async def test_a_failing_smb_scan_on_poke_is_logged(gateway_role, conf, caplog):
    import logging
    agent = _Agent()

    def boom():
        raise OSError("avahi-browse: нет прав")
    agent.services_scan = boom
    _live_client(agent)
    with caplog.at_level(logging.WARNING, logger="awgbot"):
        await linkclient.poke(agent)                  # не бросает
    assert any("нет прав" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING), \
        [r.getMessage() for r in caplog.records]


async def test_the_smb_list_goes_on_every_change_and_never_without_one(gateway_role, conf):
    """Обзор нашёл изменение — список уходит тут же; не нашёл — ни байта.
    Повторно тот же список отправляется только при переподключении (hello)."""
    agent = _Agent()
    changes = iter([True, False, True])
    agent.services_scan = lambda: next(changes)
    agent.services_local = lambda: [{"t": "_smb._tcp", "n": "nas", "h": "nas", "p": 445, "a": "192.168.1.10"}]
    client = _live_client(agent)
    for _ in range(3):
        await linkclient.services_changed(agent)
    msgs = [gwlink.unpack(KEY, x, nonce=SN) for x in client._writer.written]
    assert [m["t"] for m in msgs] == ["svc", "svc"], [m["t"] for m in msgs]
    assert msgs[0]["items"] == agent.services_local()


async def test_reconcile_never_runs_in_the_middle_of_applying_the_canon(gateway_role, conf):
    """Сверка файлов посреди `sync` сочла бы строки канона своими правками и
    отправила бы их серверу: применение канона и сверка идут по очереди."""
    import threading
    agent = _Agent()
    started, release = threading.Event(), threading.Event()
    order: list[str] = []

    def apply(msg):
        order.append("apply:start"); started.set()
        release.wait(5)
        order.append("apply:end")
        return {"ok": True, "hash": "ab", "n": 0, "error": ""}
    agent.apply_own_lists = apply
    agent.own_reconcile = lambda: order.append("reconcile") or False
    agent.own_unsent = lambda: False
    agent.own_retry = lambda: None
    client = _live_client(agent)
    t_apply = asyncio.create_task(client._apply_own({"hash": "ab"}))
    await asyncio.to_thread(started.wait, 5)
    t_tick = asyncio.create_task(client.own_tick())
    await asyncio.sleep(0.1)
    assert order == ["apply:start"], f"сверка пошла посреди применения канона: {order}"
    release.set()
    await asyncio.wait_for(asyncio.gather(t_apply, t_tick), 5)
    assert order == ["apply:start", "apply:end", "reconcile"], order


# ── fill после own_ack: наполнение набора — фоном ───────────────────────────

def _kinds_sent(client) -> list[str]:
    return [gwlink.unpack(KEY, x, nonce=SN)["t"] for x in client._writer.written]


def _fill_agent(fill_result=(True, "набор lan_vpn4: адреса 3 доменов взяты в обработку")):
    """Агент, у которого канон применился с новыми «в туннель»; fill ждёт
    `release` (dig по сотням доменов) и записывает, что и когда звали."""
    import threading
    agent = _Agent()
    agent.release = threading.Event()
    agent.started = threading.Event()
    agent.fills: list[str] = []
    agent.fill_result = fill_result
    agent.apply_own_lists = lambda msg: {"ok": True, "hash": msg.get("hash", "ab"), "n": 1, "error": "",
                                         "fill": f"/tmp/fill-{msg.get('hash', 'ab')}"}

    def own_fill(path):
        agent.fills.append(f"start:{path}")
        agent.started.set()
        agent.release.wait(5)
        agent.fills.append(f"end:{path}")
        if isinstance(agent.fill_result, Exception):
            raise agent.fill_result
        return agent.fill_result
    agent.own_fill = own_fill
    return agent


async def test_own_ack_goes_before_the_fill_is_done(gateway_role, conf):
    """Сервер видит «применены» сразу после sync, а не через полчаса dig:
    канал не ждёт наполнения набора."""
    agent = _fill_agent()
    client = _live_client(agent)
    await asyncio.wait_for(client._apply_own({"hash": "ab"}), 2)
    assert "own_ack" in _kinds_sent(client), "own_ack ждёт fill"
    await asyncio.to_thread(agent.started.wait, 5)
    assert agent.fills == ["start:/tmp/fill-ab"], agent.fills
    assert client._fill_task is not None and not client._fill_task.done()
    agent.release.set()
    await asyncio.wait_for(client._fill_task, 5)
    assert agent.fills == ["start:/tmp/fill-ab", "end:/tmp/fill-ab"]


async def test_a_failed_fill_is_logged_and_changes_nothing_else(gateway_role, conf, caplog):
    """fill отказал (dig упал, скрипт без fill) — предупреждение в журнал;
    итог канона уже ушёл «применены», повторного ответа нет."""
    import logging
    agent = _fill_agent((False, "usage: awg-lan-domain.sh add|ru|del"))
    agent.release.set()
    client = _live_client(agent)
    with caplog.at_level(logging.INFO, logger="awgbot"):
        await client._apply_own({"hash": "ab"})
        await asyncio.wait_for(client._fill_task, 5)
    assert _kinds_sent(client) == ["own_ack"], _kinds_sent(client)
    ack = gwlink.unpack(KEY, client._writer.written[0], nonce=SN)
    assert ack["ok"] is True and "fill" not in ack, f"итог fill или путь уехали серверу: {ack}"
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("набор не пополнен" in m and "usage" in m for m in warns), warns


async def test_a_successful_fill_is_logged_as_info(gateway_role, conf, caplog):
    import logging
    agent = _fill_agent()
    agent.release.set()
    client = _live_client(agent)
    with caplog.at_level(logging.INFO, logger="awgbot"):
        await client._apply_own({"hash": "ab"})
        await asyncio.wait_for(client._fill_task, 5)
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert "свои списки: набор lan_vpn4: адреса 3 доменов взяты в обработку" in infos, infos
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], "удачный fill — с предупреждением"


async def test_a_fill_that_raises_does_not_break_the_next_one(gateway_role, conf):
    """Исключение в fill (скрипт пропал) не должно остановить следующий fill:
    второй ждёт первого и идёт своим ходом."""
    agent = _fill_agent(RuntimeError("скрипта нет"))
    agent.release.set()
    client = _live_client(agent)
    await client._apply_own({"hash": "a1"})
    first = client._fill_task
    agent.fill_result = (True, "ok")
    await client._apply_own({"hash": "a2"})
    await asyncio.wait_for(client._fill_task, 5)
    assert first.done()
    assert agent.fills[-1] == "end:/tmp/fill-a2", agent.fills


async def test_two_canons_in_a_row_fill_one_after_the_other(gateway_role, conf):
    """Два канона подряд: файл для fill один и тот же — второй fill начинается
    только после первого, иначе два dig-прохода читали бы полупереписанный файл."""
    agent = _fill_agent()
    client = _live_client(agent)
    await client._apply_own({"hash": "a1"})
    await asyncio.to_thread(agent.started.wait, 5)
    await client._apply_own({"hash": "a2"})
    assert _kinds_sent(client) == ["own_ack", "own_ack"], "второй ответ ждал первый fill"
    await asyncio.sleep(0.2)
    assert agent.fills == ["start:/tmp/fill-a1"], f"второй fill пошёл, не дождавшись первого: {agent.fills}"
    agent.release.set()
    await asyncio.wait_for(client._fill_task, 5)
    assert agent.fills == ["start:/tmp/fill-a1", "end:/tmp/fill-a1", "start:/tmp/fill-a2", "end:/tmp/fill-a2"]


async def test_no_fill_key_means_no_task(gateway_role, conf):
    agent = _fill_agent()
    agent.apply_own_lists = lambda msg: {"ok": True, "hash": "ab", "n": 1, "error": ""}
    client = _live_client(agent)
    await client._apply_own({"hash": "ab"})
    assert client._fill_task is None and agent.fills == []
    assert _kinds_sent(client) == ["own_ack"]


async def test_a_skipped_or_failed_canon_starts_no_fill(gateway_role, conf):
    """Канон пропущен по правилам сверки — ответа нет, и fill тоже."""
    agent = _fill_agent()
    agent.apply_own_lists = lambda msg: {"ok": False, "skipped": "pending", "hash": "ab", "n": 0, "error": ""}
    agent.own_pending_events = lambda: ("r", [])
    client = _live_client(agent)
    await client._apply_own({"hash": "ab"})
    assert client._fill_task is None and agent.fills == []


async def test_the_deferred_canon_on_a_tick_also_fills(gateway_role, conf):
    """Отложенный канон (обвязка обновилась) применяется на тике — новые «в
    туннель» так же уходят в fill после ответа."""
    agent = _fill_agent()
    agent.release.set()
    agent.own_reconcile = lambda: False
    agent.own_unsent = lambda: False
    agent.own_retry = lambda: {"ok": True, "hash": "cd", "n": 1, "error": "", "fill": "/tmp/fill-cd"}
    client = _live_client(agent)
    await client.own_tick()
    assert _kinds_sent(client) == ["own_ack"]
    assert client._fill_task is not None
    await asyncio.wait_for(client._fill_task, 5)
    assert agent.fills == ["start:/tmp/fill-cd", "end:/tmp/fill-cd"]


async def test_stop_lets_go_of_a_running_fill(gateway_role, conf):
    """Агент останавливается посреди fill: остановка не ждёт dig (поток не
    прервать) и не падает; задача-обёртка снята."""
    agent = _fill_agent()
    client = _live_client(agent)
    client._task = None
    await client._apply_own({"hash": "ab"})
    await asyncio.to_thread(agent.started.wait, 5)
    t0 = time.monotonic()
    await asyncio.wait_for(client.stop(), 2)
    assert time.monotonic() - t0 < 1, "stop ждал fill"
    assert client._fill_task is None
    agent.release.set()


async def test_the_feed_grace_is_extended_only_by_a_real_session(vps, monkeypatch):
    """lan_feeds_touch в finally каждой попытки продлевал 12-часовой запас и на
    отвергнутых сессиях (старый ключ, чужой слот): фиды застывали навсегда."""
    vps.hangs_up(); vps.hangs_up(); vps.answers(); vps.hangs_up()
    agent = _Agent()
    _delays(monkeypatch, stop_after=4)
    with pytest.raises(asyncio.CancelledError):
        await linkclient.LinkClient(agent)._run()
    assert agent.touched == 1, f"запас продлён {agent.touched} раз — считаются и отвергнутые сессии"
    assert agent.channel.last_word > 0, "слово сервера не отмечено"


async def test_link_recovery_wakes_the_backoff_and_resets_the_step(vps, monkeypatch):
    """Бэкофф спит до 5 минут; линк вернулся — on_tick будит его, и следующая
    попытка идёт сразу и с малого шага."""
    c = linkclient.LinkClient(_Agent())
    monkeypatch.setattr(linkclient.random, "uniform", lambda lo, hi: 1.0)
    loop = asyncio.get_running_loop()
    loop.call_later(0.05, c._wake.set)
    t0 = loop.time()
    assert await c._sleep_or_wake(30) is True
    assert loop.time() - t0 < 1, "побудка не прервала сон"
    assert await c._sleep_or_wake(0.01) is False


# ── документ конфигурации: ответ cfg_ack и его очередь ──────────────────────

class _CfgAgent(_Agent):
    """Агент с документом: apply_cfg отдаёт заданный итог и считает вызовы,
    очередь ответа — в памяти (как ключ в БД агента)."""

    def __init__(self, result: dict | None = None):
        super().__init__()
        self.pending: dict = {}
        self.docs: list[dict] = []
        self.result = result or {"ack": {"gen": 7, "hash": "ab" * 8, "applied": ["env"], "wait": {},
                                         "declined": {}, "failed": {}}, "note": "", "ask": []}

    def apply_cfg(self, msg: dict) -> dict:
        self.docs.append(msg)
        return self.result

    def cfg_ack_pending_put(self, ack: dict) -> None:
        self.pending = {**ack, "at": "2026-10-10T12:00:00+03:00"}

    def cfg_ack_pending_get(self) -> dict:
        return dict(self.pending)

    def cfg_ack_pending_clear(self) -> None:
        self.pending = {}

    def cfg_answer_ack(self, answer: dict):
        if not answer.get("ok"):
            return None
        return {"gen": answer["gen"], "hash": answer["hash"], "applied": [answer["section"]],
                "wait": {}, "declined": {}, "failed": {}}


@pytest.fixture()
def cfg_hooks(monkeypatch):
    """Крючки уведомления и вопроса — записывают, что ушло человеку."""
    got = {"notes": [], "asks": []}

    async def notify(text):
        got["notes"].append(text)

    async def ask(section):
        got["asks"].append(section)
    monkeypatch.setattr(linkclient, "_notify", notify)
    monkeypatch.setattr(linkclient, "_ask", ask)
    return got


def _sent(client) -> list[dict]:
    return [gwlink.unpack(KEY, x, nonce=SN) for x in client._writer.written]


async def test_a_document_is_applied_answered_and_the_queue_emptied(gateway_role, conf, cfg_hooks):
    """Документ с сервера: применён, ответ cfg_ack ушёл ровно один раз и
    очередь пуста — иначе он ушёл бы повторно в следующей сессии и сервер
    принял бы старый итог за новый."""
    agent = _CfgAgent()
    client = _live_client(agent)
    await client._apply_cfg({"t": "cfg", "gen": 7, "hash": "ab" * 8, "env": {}})
    assert len(agent.docs) == 1
    acks = [m for m in _sent(client) if m["t"] == "cfg_ack"]
    assert len(acks) == 1 and acks[0]["gen"] == 7 and acks[0]["applied"] == ["env"], acks
    assert "at" not in acks[0], "служебная отметка очереди ушла серверу"
    assert agent.pending == {}, "ответ ушёл, а в очереди остался"
    assert cfg_hooks == {"notes": [], "asks": []}, "человеку сказали, хотя сказать нечего"


async def test_the_note_goes_to_the_human_and_questions_to_the_menu(gateway_role, conf, cfg_hooks):
    """Что применено — одним уведомлением; почта и фраза, ждущие решения, —
    вопросами живого меню, по разделу на вызов, в порядке очереди."""
    agent = _CfgAgent({"ack": {"gen": 7, "hash": "ab" * 8, "applied": ["env"], "wait": {"mail": "cd" * 8},
                               "declined": {}, "failed": {}},
                       "note": "⚙️ Сервер AWG прислал конфигурацию шлюза — применено: резолвер",
                       "ask": ["mail", "backup"]})
    client = _live_client(agent)
    await client._apply_cfg({"t": "cfg", "gen": 7})
    assert cfg_hooks["notes"] == ["⚙️ Сервер AWG прислал конфигурацию шлюза — применено: резолвер"]
    assert cfg_hooks["asks"] == ["mail", "backup"]


async def test_a_broken_hook_does_not_lose_the_answer(gateway_role, conf, monkeypatch):
    """Чат недоступен (Telegram не ответил) — ответ серверу всё равно ушёл."""
    agent = _CfgAgent({**_CfgAgent().result, "note": "x", "ask": ["mail"]})

    async def boom(*a):
        raise RuntimeError("Telegram недоступен")
    monkeypatch.setattr(linkclient, "_notify", boom)
    monkeypatch.setattr(linkclient, "_ask", boom)
    client = _live_client(agent)
    await client._apply_cfg({"t": "cfg", "gen": 7})
    assert [m["t"] for m in _sent(client)].count("cfg_ack") == 1 and agent.pending == {}


async def test_an_answer_lost_with_the_session_waits_in_the_queue(gateway_role, conf, cfg_hooks):
    """Транзакция перезапустила линк — сессия, по которой пришёл документ,
    умерла: ответ остаётся в очереди, а не пропадает."""
    agent = _CfgAgent()
    client = _live_client(agent)

    def broken(data):
        raise ConnectionResetError("линк перезапущен")
    client._writer.write = broken
    await client._apply_cfg({"t": "cfg", "gen": 7})
    assert agent.pending.get("gen") == 7, "ответ, не ушедший с сессией, потерян"


async def test_the_queued_answer_goes_first_in_the_next_session(vps):
    """Новая сессия: ответ из очереди уходит первым сообщением, следом —
    снимок. Снимок раньше ответа показал бы серверу расхождение отпечатков
    без итога документа — и тот повторил бы доставку, не дождавшись причины."""
    agent = _CfgAgent()
    agent.pending = {"gen": 7, "hash": "ab" * 8, "applied": [], "wait": {}, "declined": {},
                     "failed": {"env": "линк не поднялся"}, "at": "2026-10-10T12:00:00+03:00"}
    conn = vps.answers()
    await linkclient.LinkClient(agent)._connect_once()
    kinds = [gwlink.unpack(KEY, x, nonce=SN)["t"] for x in conn.written[1:]]
    assert "cfg_ack" in kinds, f"ответ из очереди не ушёл в новой сессии: {kinds}"
    assert agent.pending == {}, "ответ ушёл, а очередь не пуста"
    assert kinds[0] == "cfg_ack", f"первым в новой сессии ушёл не ответ: {kinds}"


async def test_an_empty_queue_sends_nothing_in_a_new_session(vps):
    agent = _CfgAgent()
    conn = vps.answers()
    await linkclient.LinkClient(agent)._connect_once()
    kinds = [gwlink.unpack(KEY, x, nonce=SN)["t"] for x in conn.written[1:]]
    assert "cfg_ack" not in kinds, kinds


async def test_the_humans_answer_without_a_session_only_queues(monkeypatch):
    """Человек ответил на вопрос, а сессии нет: ответ — в очередь, уйдёт
    в следующей; ни исключения, ни попытки писать в несуществующий сокет."""
    monkeypatch.setattr(linkclient, "_client", None)
    agent = _CfgAgent()
    await linkclient.report_cfg_answer(agent, {"ok": True, "section": "mail", "accepted": True,
                                               "gen": 9, "hash": "ab" * 8, "print": "cd" * 8})
    assert agent.pending.get("applied") == ["mail"] and agent.pending.get("gen") == 9
    await linkclient.report_cfg_answer(agent, {"ok": False, "section": "mail"})
    assert agent.pending.get("gen") == 9, "пустой ответ переписал очередь"


async def test_the_humans_answer_with_a_session_goes_right_away(gateway_role, conf):
    agent = _CfgAgent()
    client = _live_client(agent)
    await linkclient.report_cfg_answer(agent, {"ok": True, "section": "backup", "accepted": True,
                                               "gen": 9, "hash": "ab" * 8, "print": "cd" * 8})
    acks = [m for m in _sent(client) if m["t"] == "cfg_ack"]
    assert len(acks) == 1 and acks[0]["applied"] == ["backup"] and agent.pending == {}
