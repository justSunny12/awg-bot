"""
Клиент канала на шлюзе: когда он заводится и гасится, возможности сторон,
переподключение по восстановлению линка.
"""
from __future__ import annotations

import asyncio

import pytest

from awgbot.core import config
from awgbot.runtime import linkclient
from awgbot.util import gwlink
from tests.unit import _linkclient_session_common as _c
from tests.unit._linkclient_session_common import KEY, SN, _Agent, _live_client

pytestmark = pytest.mark.unit

conf, vps, gateway_role = _c.conf, _c.vps, _c.gateway_role


# ── когда клиент заводится ──────────────────────────────────────────────────

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


async def test_a_link_conf_gone_for_good_retires_the_client(vps, conf, gateway_role, monkeypatch):
    """Перезапись конфига — мгновение, снятие обвязки — насовсем: тик гасит
    клиента, только когда конфига нет дольше CONF_GONE_STOP, и сессия до ВПС
    закрывается — без стука в несуществующий линк до конца процесса."""
    import time as _time
    agent = _Agent()
    conn = vps.answers(hang=True)
    client = linkclient.ensure(agent)
    await _until_online()
    clock = [_time.monotonic()]
    monkeypatch.setattr(linkclient.time, "monotonic", lambda: clock[0])
    conf.unlink()
    await linkclient.on_tick(agent)
    clock[0] += linkclient.CONF_GONE_STOP - 1
    await linkclient.on_tick(agent)
    assert linkclient._client is client and not client._task.done(), "клиент погашен раньше срока"
    clock[0] += 2
    await linkclient.on_tick(agent)
    assert linkclient._client is None, "клиент жив без конфига линка дольше срока"
    assert client._task is None and not linkclient.online()
    for _ in range(50):
        if conn.closed:
            break
        await asyncio.sleep(0.01)
    assert conn.closed, "сессия до ВПС не закрыта"


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


async def test_after_the_code_the_hello_carries_no_settings_copy(vps):
    """flow-2: после верного кода, до полного старта агента — hello без копии
    настроек и без keep1: установка ещё не «новая» (отметки первого старта
    нет), и умолчания затёрли бы блок слота на сервере. Полный агент — как
    обычно."""
    class _Pairing(_Agent):
        active = True

        def join_hello(self):
            return {}

        def join_active(self):
            return self.active

        def keep_hello(self):
            return {"keep_hash": "0" * 16, "keep_new": False, "keep_fresh": False}
    agent = _Pairing()
    conn = vps.answers()
    await linkclient.LinkClient(agent)._connect_once()
    hello = conn.hello()
    assert "keep1" not in hello["caps"] and "keep_hash" not in hello, hello
    agent.active = False
    conn = vps.answers()
    await linkclient.LinkClient(agent)._connect_once()
    hello = conn.hello()
    assert "keep1" in hello["caps"] and hello["keep_hash"] == "0" * 16


async def test_a_join_hello_answered_by_a_plain_session_goes_on_as_usual(vps):
    """join-1: после кода hello несёт join, пока агент не на новых ключах; ВПС
    уже переключился и ответил обычной сессией (ролью) — клиент не сидит в
    цикле сопряжения, а ведёт сессию как обычно: снимок уходит."""
    class _Accepted(_Agent):
        def join_hello(self):
            return {"join": "0123456789abcdef", "eph": "DUMMY"}

        def join_active(self):
            return True
    agent = _Accepted()
    conn = vps.answers()
    await linkclient.LinkClient(agent)._connect_once()
    hello = conn.hello()
    assert hello.get("join") and "keep1" not in hello["caps"], hello
    kinds = [gwlink.unpack(KEY, line, nonce=SN).get("t") for line in conn.written[1:]]
    assert agent.roles and "snap" in kinds, (agent.roles, kinds)


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
