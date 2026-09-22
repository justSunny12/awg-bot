"""
Канал в простое молчит — главный тест этапа (концепт «канал линка», §4.3, §4.8).

Тик монитора агента снимает всё: температуру, счётчики линка, миллисекунды
зонда наружу, пакеты с роутера. Снимок для канала обязан ничего из этого не
заметить — иначе канал превращается в метроном с периодом тика, то есть ровно в
ту сигнатуру внутри туннеля, ради избавления от которой переписывали keepalive
линка и зонд живости. Здесь проверяется это и с той стороны, где считают байты:
виртуальные сутки тиков без событий — ноль отправленных пакетов.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import random

import pytest

from awgbot.core import config
from awgbot.domain import gateway as gw
from awgbot.domain import gwsnapshot
from awgbot.domain.gateway import GatewayServices
from awgbot.infra import awglock, gwguard
from awgbot.infra.db import Database
from awgbot.runtime import hostmetrics, linkclient
from awgbot.util import gwlink

PRIV = base64.b64encode(os.urandom(32)).decode()


class _Wire:
    """Сокет до ВПС, которого нет: считает, что и сколько ушло в линк."""

    def __init__(self):
        self.lines: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.lines.append(data)

    async def drain(self) -> None:
        pass

    def messages(self, key: bytes) -> list[dict]:
        return [gwlink.unpack(key, line) for line in self.lines]


class _Agent:
    """Агент шлюза со снятым хостом: железо, счётчики линка, зонд наружу,
    таблица обвязки и юнит — под рукой. Каждый тик двигает всё, что на живой
    малине двигается само."""

    def __init__(self, svc, monkeypatch, tmp_path):
        self.svc = svc
        self.now = 10_000.0
        self.rx = self.tx = 0
        # Клиенты шлют в линк, обратно тихо — худший случай для зонда наружу:
        # он идёт каждым тиком и каждым тиком меряет новое число. Ровно то, что
        # снимок обязан не заметить.
        self.rx_step, self.tx_step = 1 << 20, 0
        self.temp = 48.0
        self.egress_ms: float | None = 30.0
        self.lan_pkts, self.dns_pkts = 1000, 100
        self.env = {"LAN_MODE": "1", "HOME_SUBNETS": "192.168.68.0/24",
                    "RESOLVER": "10.9.1.1", "PEER_HOME_NETS": "192.168.2.0/24"}
        self.admin_ips = ["10.9.1.2"]
        self.chains = {"input", "tunnel_in", "forward", "ssh_in"}
        self.peer_nets4 = {"192.168.2.0/24"}
        self.generation = 1
        self.conf = tmp_path / "awglink.conf"
        self.conf.write_text(f"# awg-bot: контракт линка 1\n[Interface]\nPrivateKey = {PRIV}\n",
                             encoding="utf-8")

        monkeypatch.setattr(gw.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(config, "GW_LINK_CONF", str(self.conf))
        monkeypatch.setattr(config, "INSTALLED_VERSION", "3.1.0")
        monkeypatch.setattr(gwsnapshot, "boot_id", lambda: "b" * 36)
        monkeypatch.setattr(awglock, "generation", lambda: self.generation)
        # ── линк, зонд, статика: как на живой малине, только без хоста ───────
        monkeypatch.setattr(svc, "link_status", lambda: (True, 5.0, self.rx, self.tx))
        monkeypatch.setattr(svc, "plumbing_checks", self._plumbing)
        monkeypatch.setattr(svc, "tg_mark_missing", lambda info=None: [])
        monkeypatch.setattr(svc, "tg_mark_ensure", lambda missing=None: 0)
        monkeypatch.setattr(svc, "uplink_policy_heal", lambda: [])
        monkeypatch.setattr(svc, "versions", lambda: ("1.0", "src"))
        monkeypatch.setattr(svc, "kernel_coverage", lambda: ([], 1))
        monkeypatch.setattr(svc, "egress_probe", lambda: self.egress_ms)
        monkeypatch.setattr(svc, "ssh_port_fact", lambda: None)
        monkeypatch.setattr(svc, "ssh_reconcile", lambda info, fact: [])
        monkeypatch.setattr(svc, "ssh_checks", lambda info, fact: [])
        monkeypatch.setattr(svc, "ssh_screen", lambda info, fact, conf=False: {
            "port": 22, "owner": "", "filter": False, "allow": [], "sshd_down": False,
            "new_plumbing": True})
        # ── железо: всё меняется каждым тиком ────────────────────────────────
        monkeypatch.setattr(hostmetrics, "read_soc_temp", lambda: self.temp)
        monkeypatch.setattr(hostmetrics, "read_pi_throttled", lambda: {"now": [], "since": []})
        monkeypatch.setattr(hostmetrics, "read_cpu_percent", lambda: self.temp * 0.5)
        monkeypatch.setattr(hostmetrics, "read_ram", lambda: (self.temp, int(self.temp * 10)))
        monkeypatch.setattr(hostmetrics, "read_disk", lambda: (self.temp, self.temp))
        monkeypatch.setattr(hostmetrics, "read_uptime_seconds", lambda: int(self.now))
        monkeypatch.setattr(hostmetrics, "read_smart_health", lambda: "OK")
        # ── юнит обвязки, таблицы, локальная сеть ────────────────────────────
        monkeypatch.setattr(gwguard, "unit_env", lambda k: self.env.get(k, ""))
        monkeypatch.setattr(gwguard, "unit_admin_ips", lambda: list(self.admin_ips))
        monkeypatch.setattr(gwguard, "lan_mode", lambda: self.env.get("LAN_MODE") == "1")
        monkeypatch.setattr(gwguard, "script_status",
                            lambda: {"LAN_IF": "end0", "LAN_ADDR": "192.168.68.222"})
        monkeypatch.setattr(gwguard, "dnsmasq_active", lambda: True)
        monkeypatch.setattr(gwguard, "resolve_via_local", lambda name="github.com": True)
        monkeypatch.setattr(gwguard, "home_table_info", self._home)
        monkeypatch.setattr(gwguard, "lists_status",
                            lambda: {"domains": "1180", "nets": "412", "rc": "0"})
        monkeypatch.setattr(gwguard, "lan_own_lists", lambda: (2, 1))
        monkeypatch.setattr(gwguard, "iface_for_subnet", lambda net: ("end0", "192.168.68.222"))
        monkeypatch.setattr(gwguard, "reassert", lambda: (True, ""))
        svc.db.set_state(svc._GW_MARK_KEY, "confirmed")

    # ── то, что снимает тик ──────────────────────────────────────────────────

    def _plumbing(self):
        self.svc._guard_info = {"sets": {"peer_nets4": set(self.peer_nets4)},
                                "chains": set(self.chains), "masq_ifaces": set(),
                                "ssh_ports": {}}
        return []

    def _home(self):
        return {"sets": {"lan_vpn4": 50, "lan_vpn_nets4": 412, "lan_ru4": 3},
                "chains": {"prerouting", "input", "forward", "postrouting"},
                "lan_pkts": self.lan_pkts, "dns_pkts": self.dns_pkts}

    def tick(self) -> None:
        """Три минуты спустя: нагрелись, наездили, роутер прислал пакеты, зонд
        намерил другое число. Ни одно из этого не факт конфигурации."""
        self.now += 180.0
        self.temp += 0.7
        self.rx += self.rx_step
        self.tx += self.tx_step
        self.lan_pkts += 137
        self.dns_pkts += 11
        if self.egress_ms is not None:
            self.egress_ms += 23.0
        self.svc.monitor_tick()


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    db = Database(tmp_path / "gw.db")
    db.init_schema()
    svc = GatewayServices(db)
    a = _Agent(svc, monkeypatch, tmp_path)
    yield a
    db.close()


@pytest.fixture()
def client(agent, monkeypatch):
    """Клиент канала с подставленным сокетом: сессия «открыта», байты считаем."""
    c = linkclient.LinkClient(agent.svc)
    c._writer = _Wire()
    monkeypatch.setattr(random, "uniform", lambda a, b: 1.0)
    return c


def _wire(client) -> _Wire:
    return client._writer


# ── главный тест этапа ───────────────────────────────────────────────────────

def test_a_tick_that_only_moves_hardware_produces_no_delta(agent):
    """Главный сторож правила «нет периодического обмена». Между двумя тиками
    изменились температура, счётчики линка, миллисекунды зонда наружу и пакеты
    с роутера — то есть всё, что на живой малине меняется само каждые три
    минуты. Дельта обязана быть пустой: иначе канал начнёт говорить ровно по
    расписанию тика, и наблюдателю внутри туннеля останется считать интервалы."""
    def _moving(st):
        return (st.temp, st.cpu, st.ram, st.disk, st.rx, st.egress_ms,
                st.uptime_seconds, st.lan.get("lan_pkts"), st.lan.get("dns_pkts"))

    agent.tick()
    first, before = agent.svc.gw_snapshot(), agent.svc.cached_status(1e9)
    agent.tick()
    second, after = agent.svc.gw_snapshot(), agent.svc.cached_status(1e9)
    assert all(a != b for a, b in zip(_moving(before), _moving(after))), (
        f"тик ничего не сдвинул — проверять нечего: {_moving(before)} → {_moving(after)}")
    assert gwsnapshot.delta(first, second) == {}, (
        "снимок услышал то, что меняется каждым тиком: канал стал метрономом")


async def test_a_virtual_day_of_ticks_sends_not_a_single_byte(client, agent):
    """Простой = ноль пакетов. Сутки тиков по три минуты, ни одного события —
    после первого снимка в линк не уходит ничего. У резервного слота, где
    клиентского трафика нет вовсе, такой обмен был бы единственным содержимым
    туннеля и идеальной мишенью."""
    agent.tick()
    assert await client.push(full=True) is True
    after_hello = client._sent_bytes
    assert after_hello > 0 and len(_wire(client).lines) == 1

    for _ in range(480):                       # 24 ч по тику в три минуты
        agent.tick()
        assert await client.push() is False, "в простое ушёл пакет"
    assert client._sent_bytes == after_hello, (
        f"за сутки простоя в линк ушло {client._sent_bytes - after_hello} лишних байт")
    assert len(_wire(client).lines) == 1


async def test_the_first_snapshot_is_full_padded_and_numbered_from_one(client, agent):
    """Подключение — самая важная точка: любое событие, после которого
    состояние могло измениться целиком (ребут, обновление агента, восстановление
    линка), заканчивается новым подключением. Значит, первое сообщение сессии —
    полный снимок."""
    agent.tick()
    await client.push(full=True)
    msg = _wire(client).messages(gwlink.channel_key(PRIV))[0]
    assert msg["t"] == "snap" and msg["rev"] == 1 and msg["seq"] == 1
    assert msg["bundle"]["home_subnets"] == "192.168.68.0/24"
    assert len(_wire(client).lines[0]) % gwlink.PAD_SNAP == 0, "снимок не добит до кратности"


async def test_a_changed_fact_wakes_the_channel_up_exactly_once(client, agent):
    """Событие — вот единственный повод заговорить. Перевыпуск конфигурации
    поменял локальные подсети в юните: уходит дельта с одним полем и следующим
    номером, а дальше канал снова молчит."""
    agent.tick()
    await client.push(full=True)
    agent.env["HOME_SUBNETS"] = "192.168.1.0/24"
    agent.tick()
    assert await client.push() is True
    agent.tick()
    assert await client.push() is False, "дельта повторилась на следующем тике"

    msgs = _wire(client).messages(gwlink.channel_key(PRIV))
    assert len(msgs) == 2
    delta = msgs[1]
    assert delta["t"] == "delta" and delta["rev"] == 2 and delta["seq"] == 2
    assert delta["bundle"]["home_subnets"] == "192.168.1.0/24"
    assert set(delta) - {"t", "seq", "ts", "rev"} == {"bundle"}, (
        "в дельте поехало больше, чем изменилось")
    assert len(_wire(client).lines[1]) % gwlink.PAD_DELTA == 0, "дельта не добита до кратности"


async def test_a_failed_probe_outside_sends_the_verdict_and_not_the_milliseconds(client, agent):
    """Канал квартиры лёг: агент видит это своим зондом, и ВПС складывает его
    наблюдение со своим — совпали оба «нет», значит отказ точно у шлюза. Едет
    при этом только булево."""
    agent.tick()
    await client.push(full=True)
    agent.egress_ms = None                      # зонд не прошёл
    agent.tick()
    assert await client.push() is True
    delta = _wire(client).messages(gwlink.channel_key(PRIV))[-1]
    assert delta["egress_ok"] is False
    assert "egress_ms" not in delta, "миллисекунды зонда поехали на ВПС"


async def test_ask_snap_is_answered_with_a_full_snapshot_and_a_fresh_numbering(client, agent):
    """Починка: ВПС увидел разрыв нумерации или человек нажал «Обновить». Ответ
    — полный снимок с нумерацией заново, иначе дельты продолжат ложиться на
    снимок, которого у сервера нет."""
    agent.tick()
    await client.push(full=True)
    agent.env["LAN_MODE"] = "0"
    agent.tick()
    await client.push()
    await client._handle({"t": "ask", "what": "snap"})
    msgs = _wire(client).messages(gwlink.channel_key(PRIV))
    assert [m["t"] for m in msgs] == ["snap", "delta", "snap"]
    assert msgs[-1]["rev"] == 1 and msgs[-1]["bundle"]["lan_mode"] == "0"


async def test_an_unknown_message_from_the_server_is_ignored_silently(client, agent):
    """Старый агент, новый ВПС: незнакомое сообщение не повод рвать сессию и уж
    тем более падать — иначе выпуск сервера ронял бы канал у всех, кто ещё не
    обновил шлюз."""
    agent.tick()
    await client.push(full=True)
    before = client._sent_bytes
    await client._handle({"t": "будущее", "payload": {"x": 1}})
    assert client._sent_bytes == before and len(_wire(client).lines) == 1


async def test_a_dead_socket_does_not_break_the_tick(agent, monkeypatch):
    """Линк упал между тиками: отправка не прошла, но тик монитора обязан
    доработать до конца — агент живёт своей жизнью и без ВПС."""
    class _Broken(_Wire):
        def write(self, data):
            raise ConnectionResetError("линк упал")

    c = linkclient.LinkClient(agent.svc)
    c._writer = _Broken()
    agent.tick()
    assert await c.push(full=True) is False
    assert c._sent_bytes == 0


async def test_without_a_session_nothing_is_even_collected(agent, monkeypatch):
    """Сессии нет — снимок не собирается вовсе: собирать его «на всякий
    случай» значит платить чтениями на каждом тике лежащего канала."""
    c = linkclient.LinkClient(agent.svc)
    monkeypatch.setattr(agent.svc, "gw_snapshot",
                        lambda: pytest.fail("снимок собран при закрытом канале"))
    assert await c.push(full=True) is False


# ── рубильник и адреса ───────────────────────────────────────────────────────

def test_the_channel_is_switched_on_only_by_the_bundle(monkeypatch):
    """Канал включается тем, что привёз бандл, и только им: без перевыпуска
    конфигурации агент никуда не ходит. Это и есть рубильник функции."""
    env = {}
    monkeypatch.setattr(gwguard, "unit_env", lambda k: env.get(k, ""))
    assert linkclient.enabled() is False, "старый бандл без строки канала — канала нет"
    env["LINK_CHANNEL"] = "1"
    assert linkclient.enabled() is True
    env["LINK_CHANNEL"] = "0"
    assert linkclient.enabled() is False


def test_the_channel_port_falls_back_to_the_default_on_junk(monkeypatch):
    """Порт приезжает строкой из юнита: мусор в ней не должен превращаться в
    исключение посреди подключения."""
    env = {"LINK_CHANNEL_PORT": "9001"}
    monkeypatch.setattr(gwguard, "unit_env", lambda k: env.get(k, ""))
    assert linkclient.server_port() == 9001
    for bad in ("", "порт", "-1", "80 "):
        env["LINK_CHANNEL_PORT"] = bad
        assert linkclient.server_port() == linkclient.DEFAULT_PORT


def test_the_server_address_is_taken_from_the_kernel_not_from_a_config(monkeypatch):
    """Свой адрес в линке спрашиваем у ядра: конфиг могли и не применить. Из
    /30 второй хост считается однозначно — адресов там ровно два."""
    import subprocess

    out = {"text": "2: awglink    inet 10.99.99.2/30 scope global awglink\\       valid_lft forever"}

    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout=out["text"].encode(), stderr=b"")
    monkeypatch.setattr(subprocess, "run", run)
    assert linkclient.server_address() == "10.99.99.1"
    out["text"] = "2: awglink    inet 10.99.99.6/30 scope global awglink"
    assert linkclient.server_address() == "10.99.99.5", "второй слот линка"


@pytest.mark.parametrize("broken", ["", "2: awglink    inet 10.99.99.2/24 scope global awglink",
                                    "2: awglink    inet6 fe80::1/64 scope link"])
def test_an_unusable_link_address_yields_no_server_address(monkeypatch, broken):
    """Линк не поднят или поднят не так — идти некуда. Пустая строка честнее
    выдуманного адреса: клиент просто подождёт следующей попытки."""
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, stdout=broken.encode(), stderr=b""))
    assert linkclient.server_address() == ""


def test_a_missing_ip_binary_is_not_an_exception(monkeypatch):
    """На малине без iproute2 (и в контейнере теста) вызов просто не удаётся:
    клиент обязан пережить это бэкоффом, а не падением задачи."""
    import subprocess

    def boom(*a, **k):
        raise FileNotFoundError("ip")
    monkeypatch.setattr(subprocess, "run", boom)
    assert linkclient.server_address() == ""


def test_backoff_grows_to_five_minutes_and_is_always_jittered():
    """Ровный ритм попыток переподключения — тот же маячок, только на уровне
    TCP. Шаги растут и каждый умножается на случайный множитель."""
    assert linkclient._BACKOFF == (5, 15, 45, 135, 300)
    assert all(b < a for a, b in zip(linkclient._BACKOFF[1:], linkclient._BACKOFF)), (
        "бэкофф обязан расти")
    lo = [round(d * 0.6) for d in linkclient._BACKOFF]
    hi = [round(d * 1.4) for d in linkclient._BACKOFF]
    assert lo[-1] == 180 and hi[-1] == 420, "потолок пять минут ± 40 %"


def test_json_of_the_snapshot_stays_far_under_a_kilobyte(agent):
    """Вес снимка — свойство безопасности, а не бухгалтерия: чем меньше
    сообщение, тем меньше поводов говорить. Разрастание в килобайты означает,
    что в снимок опять что-то натекло."""
    agent.tick()
    raw = json.dumps(agent.svc.gw_snapshot(), ensure_ascii=False).encode()
    assert len(raw) < 700, f"снимок распух до {len(raw)} байт"


# ── пометка шлюза по каналу ──────────────────────────────────────────────────

def _uplink(monkeypatch, pub="UPLINKPUB=="):
    monkeypatch.setattr(gwguard, "uplink_pubkey", lambda: ("awg0", pub))


async def test_an_unmarked_gateway_sends_its_claim_token_over_the_channel(
        client, agent, monkeypatch):
    """Пометку шлюза человек до сих пор переносил руками из чата в чат. Канал
    уже открыт и подписан тем же ключом линка — заставлять человека копировать
    сообщение незачем. Токен подписан ключом claim (не ключом канала) и
    проверяется на ВПС тем же кодом, что пересланный руками."""
    from awgbot.util import gwsign
    agent.svc.db.set_state(agent.svc._GW_MARK_KEY, "unmarked")
    _uplink(monkeypatch)
    agent.tick()
    await client.push(full=True)
    await client._maybe_claim()

    msgs = _wire(client).messages(gwlink.channel_key(PRIV))
    assert [m["t"] for m in msgs] == ["snap", "claim"]
    data = gwsign.verify(PRIV, msgs[-1]["token"])
    assert data["act"] == "claim" and data["pub"] == "UPLINKPUB=="
    assert len(_wire(client).lines[-1]) % gwlink.PAD_SNAP == 0, "claim не добит до кратности"


async def test_a_gateway_that_is_already_marked_says_nothing(client, agent, monkeypatch):
    """Шлюз помечен — говорить не о чем. Повторный токен ВПС всё равно
    отвергнет по списку нонсов, а лишнее сообщение в канале — лишний повод
    заговорить."""
    _uplink(monkeypatch)
    agent.tick()
    await client.push(full=True)
    before = client._sent_bytes
    await client._maybe_claim()
    assert client._sent_bytes == before and len(_wire(client).lines) == 1


async def test_without_an_uplink_key_there_is_nothing_to_claim(client, agent, monkeypatch):
    """Аплинк ещё не поднят (первая установка, ребут) — ключа нет, и выдумывать
    его нельзя: неподписываемый токен всё равно не примут."""
    agent.svc.db.set_state(agent.svc._GW_MARK_KEY, "unmarked")
    _uplink(monkeypatch, pub="")
    agent.tick()
    await client.push(full=True)
    await client._maybe_claim()
    assert len(_wire(client).lines) == 1


async def test_a_changed_mark_takes_the_claim_with_the_delta(client, agent, monkeypatch):
    """Применили бандл нового слота — пометка на малине сменилась. Токен уходит
    следом за дельтой, чтобы человек не возвращался к ручной пересылке ровно в
    тот момент, когда канал уже жив."""
    _uplink(monkeypatch)
    agent.tick()
    await client.push(full=True)
    agent.svc.db.set_state(agent.svc._GW_MARK_KEY, "unconfirmed")
    agent.tick()
    assert await client.push() is True

    kinds = [m["t"] for m in _wire(client).messages(gwlink.channel_key(PRIV))]
    assert kinds == ["snap", "delta", "claim"], f"после смены пометки ушло {kinds}"


async def test_backoff_between_attempts_grows_and_is_jittered_every_step(agent, monkeypatch):
    """Ровный ритм попыток переподключения — тот же маячок, только на уровне
    TCP: линк лежит, а агент раз в пять секунд ровно стучится в одну точку.
    Проверяем на детерминированном random, что шаг растёт, упирается в потолок
    и каждый раз умножается на джиттер."""
    delays: list[float] = []

    async def fake_sleep(seconds):
        delays.append(seconds)
        if len(delays) >= 6:
            raise asyncio.CancelledError

    async def never_connects():
        raise OSError("адрес ВПС в линке не определён")

    monkeypatch.setattr(linkclient.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(linkclient.random, "uniform", lambda lo, hi: hi)   # верхний край
    c = linkclient.LinkClient(agent.svc)
    monkeypatch.setattr(c, "_connect_once", never_connects)
    with pytest.raises(asyncio.CancelledError):
        await c._run()
    assert [round(d, 3) for d in delays] == [7.0, 21.0, 63.0, 189.0, 420.0, 420.0], (
        f"шаги бэкоффа разъехались: {delays}")

    delays.clear()
    monkeypatch.setattr(linkclient.random, "uniform", lambda lo, hi: lo)   # нижний край
    with pytest.raises(asyncio.CancelledError):
        await c._run()
    assert delays[0] == 3.0 and delays[-1] == 180.0, "джиттер не применяется к шагу"
