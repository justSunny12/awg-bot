"""
Слушатели канала линка на ВПС (runtime/linkserver): по серверу на адрес линка,
сессия начинается с hello, ключ слота читается на каждую сессию, «Обновить»
ждёт снимок сам.

Сокеты настоящие (петля) там, где адрес можно поднять на любой машине, —
127.0.0.1. Второй линк (10.99.99.5) на машине теста не поднят: его bind
подменён — отказ или подставной сервер, по сценарию. Всё остальное — боевой
LinkServer поверх временной БД.

Цена ошибок: лежащий второй линк оставлял без канала здоровый первый (bind
списка адресов — «всё или ничего»); снятие одного слота рвало сессии всех;
коннект, отвергнутый на подписи, освежал «последний раз на связи», и
напоминания перевыпустить файл молчали вечно, хотя канал не доставил ни разу.
"""
from __future__ import annotations

import asyncio
import base64
import os

import pytest

from awgbot.core import config
from awgbot.runtime import linkserver
from awgbot.util import gwlink
from tests.integration.test_gwlink_channel import SNAP, _free_port, _Gw, _until

pytestmark = pytest.mark.integration

ADMIN = config.ADMIN_ID
PRIV = base64.b64encode(os.urandom(32)).decode()
PRIV2 = base64.b64encode(os.urandom(32)).decode()
KEY = gwlink.channel_key(PRIV)
SECOND = "10.99.99.5"                     # адрес ВПС во втором линке — на машине теста его нет


class _FakeServer:
    """Слушатель на адресе, которого на машине теста нет."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class _FakeWriter:
    """Сокет сессии второго слота: знает свой локальный адрес и закрытие."""

    def __init__(self, local: str, port: int):
        self.local, self.port = local, port
        self.closed = False

    def get_extra_info(self, name):
        return (self.local, self.port) if name == "sockname" else None

    def write(self, data):
        pass

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class _Binds:
    """asyncio.start_server с управляемым вторым адресом: поднят (подставной
    сервер) или нет (OSError, как у ядра при не поднятом линке)."""

    def __init__(self, monkeypatch):
        self.second_up = False
        self.fakes: list[_FakeServer] = []
        real = asyncio.start_server

        async def start_server(cb, host=None, port=None, **kw):
            if host == SECOND:
                if not self.second_up:
                    raise OSError(49, "Can't assign requested address")
                srv = _FakeServer()
                self.fakes.append(srv)
                return srv
            return await real(cb, host, port, **kw)
        monkeypatch.setattr(linkserver.asyncio, "start_server", start_server)


@pytest.fixture()
async def two(services, make_active_client, monkeypatch):
    """Два слота: первый на петле (слушатель настоящий), второй на адресе,
    которого на машине нет."""
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    pi2 = services.add_device(admin.id, "Pi2")
    services.db.gateway_add(pi.device_id, "awglink", 443, "127.0.0.0/30", slot_id=1)
    services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    keys = {1: PRIV, 2: PRIV2}
    monkeypatch.setattr(services, "_link_privkey", lambda g=None: keys[g.id])
    services.keys = keys
    port = _free_port()
    monkeypatch.setattr(linkserver, "channel_port", lambda: port)
    real_gw = linkserver.gw_address
    monkeypatch.setattr(linkserver, "gw_address",
                        lambda cidr: "127.0.0.1" if cidr.startswith("127.") else real_gw(cidr))
    binds = _Binds(monkeypatch)
    srv = linkserver.LinkServer(services)
    yield srv, binds, port
    await srv.stop()


async def _hello(srv, key=KEY, agent: str = "3.1.0") -> _Gw:
    host, port = next(a for a in srv._bound if a[0] == "127.0.0.1")
    gw = _Gw(*await asyncio.open_connection(host, port), key=key)
    await gw.send("hello", {"proto": gwlink.PROTO, "agent": agent})
    return gw


# ── слушатели по адресам ─────────────────────────────────────────────────────

async def test_a_link_that_is_down_does_not_take_the_other_slots_channel_with_it(services, two):
    """Резервный линк лежит (малина выключена, линк не встал после ребута ВПС).
    Раньше bind списка адресов падал целиком, и без канала оставался и
    здоровый активный слот — до тех пор, пока не поднимется второй."""
    srv, binds, port = two
    await srv.ensure()
    assert srv._bound == (("127.0.0.1", port),), f"поднялось не то: {srv._bound}"
    gw = await _hello(srv)
    assert await gw.role(), "здоровый слот без канала из-за соседа"
    await gw.send("snap", {**SNAP, "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1))
    sess = srv._sessions[1]

    await srv.ensure()                            # такт живости: второй всё ещё лежит
    assert srv._sessions.get(1) is sess, "повторная попытка поднять соседа порвала живую сессию"
    binds.second_up = True                        # второй линк поднялся
    await srv.ensure()
    assert srv._bound == (("10.99.99.5", port), ("127.0.0.1", port))
    assert srv._sessions.get(1) is sess, "подъём соседа перевесил и первый слушатель"
    await gw.send("delta", {"egress_ok": False, "rev": 2})
    await _until(lambda: services.gwlink_snapshot(1).get("egress_ok") is False)
    assert services.gwlink_snapshot(1)["egress_ok"] is False, "сессия первого слота умерла"
    await gw.close()


async def test_removing_a_slot_closes_only_its_listener_and_its_sessions(services, two):
    """Сняли резервный слот. Его слушатель и его сессия уходят; активный слот не
    замечает ничего — ни разрыва, ни повторного снимка."""
    srv, binds, port = two
    binds.second_up = True
    await srv.ensure()
    assert len(srv._bound) == 2
    gw = await _hello(srv)
    assert await gw.role()
    sess1 = srv._sessions[1]
    w2 = _FakeWriter(SECOND, port)
    srv._sessions[2] = linkserver._Session(w2, gwlink.channel_key(PRIV2))

    services.db.gateway_delete(2)
    await srv.ensure()
    assert srv._bound == (("127.0.0.1", port),)
    assert binds.fakes[0].closed, "слушатель снятого слота остался"
    assert w2.closed and 2 not in srv._sessions, "сессия снятого слота пережила его"
    assert srv._sessions.get(1) is sess1, "снятие соседа тронуло сессию активного слота"
    assert await gw.silent(), "активному слоту что-то прислали при снятии соседа"
    await gw.send("snap", {**SNAP, "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1))
    assert services.gwlink_snapshot(1), "сессия активного слота после снятия соседа не работает"
    await gw.close()


# ── сессия начинается с hello ────────────────────────────────────────────────

async def test_a_connection_rejected_before_hello_does_not_count_as_contact(services, two):
    """На том конце не ключ этого слота (малину заменили, а бандл не
    перевыпустили): hello не проходит подпись. Раньше даже такой коннект
    освежал «последний раз на связи» — и напоминание перевыпустить файл молчало
    сутки за сутками, хотя канал не доставил ни разу. Причина разрыва при этом
    записывается — её покажет карточка."""
    srv, binds, port = two
    await srv.ensure()
    services.db.set_state("gwlink_seen_1", "2026-01-01T00:00:00+03:00")
    other = gwlink.channel_key(base64.b64encode(os.urandom(32)).decode())
    gw = await _hello(srv, key=other)
    assert await asyncio.wait_for(gw.reader.read(), 2) == b"", "сервер не закрыл чужой коннект"
    await _until(lambda: 1 not in srv._sessions)
    assert 1 not in srv._sessions
    assert services.db.get_state("gwlink_seen_1") == "2026-01-01T00:00:00+03:00", (
        "отвергнутый коннект записан как «был на связи»")
    assert services.gwlink_session(1) == {}
    assert services.db.get_state("gwlink_error_1"), "причина разрыва не записана"
    await gw.close()


async def test_a_session_that_did_say_hello_is_remembered_as_contact(services, two):
    srv, binds, port = two
    await srv.ensure()
    services.db.set_state("gwlink_seen_1", "2026-01-01T00:00:00+03:00")
    gw = await _hello(srv)
    assert await gw.role()
    await gw.close()
    await _until(lambda: not services.gwlink_session(1))
    assert services.db.get_state("gwlink_seen_1") != "2026-01-01T00:00:00+03:00"


async def test_a_rejected_newcomer_does_not_leave_the_old_session_on_record(services, two):
    """Малина перезагрузилась, старая сессия висит полуоткрытой; новая
    вытесняет её и тут же отвергается (чужая подпись). Живых сессий
    нет — и запись о сессии в БД обязана это знать: иначе карточка горит
    «на связи» (хендшейк линка свежий, малина-то жива), а напоминания
    перевыпустить файл молчат, пока канал «покрывает» слот."""
    srv, binds, port = two
    await srv.ensure()
    old = await _hello(srv)
    assert await old.role()
    await _until(lambda: services.gwlink_session(1))
    other = gwlink.channel_key(base64.b64encode(os.urandom(32)).decode())
    new = await _hello(srv, key=other)
    assert await asyncio.wait_for(new.reader.read(), 2) == b"", "сервер не закрыл чужой коннект"
    await _until(lambda: 1 not in srv._sessions)
    await asyncio.sleep(0.2)
    assert srv.online(1) is False
    assert services.gwlink_session(1) == {}, (
        "живых сессий нет, а в БД осталась запись вытесненной — канал «на связи» навсегда")
    await old.close()
    await new.close()


async def test_a_snapshot_before_hello_ends_the_session(services, two):
    """Сессия начинается с hello: до него сервер не назвал своего нонса, и
    всё, кроме hello, подписано «ни для кого» — это либо повтор записанного,
    либо не наш агент. Такой снимок не ложится в карточку, сервер не отвечает
    на него ни словом и рвёт сессию, не записав её как «была на связи»."""
    srv, binds, port = two
    await srv.ensure()
    services.db.set_state("gwlink_seen_1", "2026-01-01T00:00:00+03:00")
    host, port_ = srv._bound[0]
    gw = _Gw(*await asyncio.open_connection(host, port_), key=KEY)
    await gw.send("snap", {**SNAP, "rev": 1})
    assert await asyncio.wait_for(gw.reader.read(), 2) == b"", (
        "сервер ответил на снимок до hello или оставил сессию открытой")
    await _until(lambda: 1 not in srv._sessions)
    assert 1 not in srv._sessions
    assert services.gwlink_snapshot(1) == {}, "снимок до hello лёг в карточку"
    assert services.db.get_state("gwlink_seen_1") == "2026-01-01T00:00:00+03:00", (
        "сессия без hello записана как «была на связи»")
    assert "hello" in services.db.get_state("gwlink_error_1"), "причина разрыва не записана"
    await gw.close()

    gw = await _hello(srv)                        # следующая сессия — как положено
    assert await gw.role()
    await gw.send("snap", {**SNAP, "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1))
    assert services.gwlink_snapshot(1)["agent_version"] == "3.1.0"
    await gw.close()


async def test_the_first_word_after_hello_is_the_role_even_with_feeds_due(services, two, monkeypatch):
    """Роль — первое сообщение сервера: по нему клиент понимает, что сессия
    настоящая, и сбрасывает бэкофф. Уйди первыми фиды на сотни килобайт —
    сессия, оборванная посреди них, оставляла бы клиента с растущим шагом."""
    from tests.integration.test_gwlink_lists_role_diag import _Net
    srv, binds, port = two
    services.gateway_set_home_subnets(1, "192.168.68.0/24")
    services.gateway_set_lan_mode(1, True)
    _Net(monkeypatch)
    assert services.gwlink_lan_feeds_update(force=True) is True
    await srv.ensure()
    host, port_ = srv._bound[0]
    gw = _Gw(*await asyncio.open_connection(host, port_, limit=8 * gwlink.MAX_LINE), key=KEY)
    await gw.send("hello", {"proto": gwlink.PROTO, "agent": "3.1.0", "lists_hash": "старый"})
    first = await gw.recv()
    second = await gw.recv()
    assert "nonce" in first, "первое слово сервера не несёт его нонс — клиент порвёт сессию"
    assert first["t"] == "role", f"первым после hello ушло «{first['t']}»"
    assert second["t"] in ("lists", "lists_part"), "фиды шлюзу со старым отпечатком не ушли"
    await gw.close()


# ── ключ слота — на каждую сессию ────────────────────────────────────────────

async def test_a_replaced_machine_in_the_slot_is_heard_without_a_restart(services, two):
    """В слот поставили другую малину — ключ линка новый, номер слота тот же.
    Ключ, закешированный по номеру слота, держал бы канал мёртвым до
    перезапуска бота: каждое сообщение новой машины — «чужая подпись»."""
    srv, binds, port = two
    await srv.ensure()
    gw = await _hello(srv)
    assert await gw.role()
    await gw.close()
    await _until(lambda: not services.gwlink_session(1))

    new_priv = base64.b64encode(os.urandom(32)).decode()
    services.keys[1] = new_priv                   # перевыпуск ключей линка слота
    gw = await _hello(srv, key=gwlink.channel_key(new_priv), agent="3.2.0")
    assert await gw.role(), "новая машина в слоте не услышана"
    await gw.send("snap", {**SNAP, "agent_version": "3.2.0", "rev": 1})
    await _until(lambda: services.gwlink_snapshot(1).get("agent_version") == "3.2.0")
    assert services.gwlink_session(1)["agent"] == "3.2.0"
    await gw.close()


# ── «Обновить»: запрос снимка с ожиданием ────────────────────────────────────

async def test_ask_snap_waits_for_the_gateway_answer(services, two):
    """Кнопка «Обновить» говорит «Снимок обновлён» только если снимок
    действительно пришёл: иначе человек смотрит на старое и думает, что видит
    результат своего последнего действия."""
    srv, binds, port = two
    await srv.ensure()
    gw = await _hello(srv)
    assert await gw.role()

    async def answer():
        ask = await gw.recv()
        assert ask["t"] == "ask" and ask["what"] == "snap"
        await gw.send("snap", {**SNAP, "agent_version": "3.1.1", "rev": 1})

    task = asyncio.create_task(answer())
    assert await srv.ask_snap(1, timeout=2.0) is True
    await task
    assert services.gwlink_snapshot(1)["agent_version"] == "3.1.1"
    await gw.close()


async def test_ask_snap_says_false_when_the_gateway_is_silent(services, two):
    srv, binds, port = two
    await srv.ensure()
    gw = await _hello(srv)
    assert await gw.role()
    assert await srv.ask_snap(1, timeout=0.5) is False, "молчание шлюза выдано за свежий снимок"
    ask = await gw.recv()
    assert ask["t"] == "ask" and ask["what"] == "snap", "запрос снимка не ушёл"
    await gw.close()


async def test_ask_snap_says_none_without_a_session(services, two):
    """Сессии нет — «не на связи», а не «не ответил»: это разные советы человеку."""
    srv, binds, port = two
    await srv.ensure()
    assert await srv.ask_snap(1, timeout=0.3) is None
    assert await srv.ask_snap(2, timeout=0.3) is None
