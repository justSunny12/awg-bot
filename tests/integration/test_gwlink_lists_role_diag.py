"""
Канал линка, этапы 3 и 4: фиды локальной сети
качает ВПС и возит каналом; роль слота доезжает до агентов; просьба о диагностике
на малине ничего не запускает; автомат переключения снимку шлюза не верит.

Сокеты настоящие (петля), сервер — боевой LinkServer поверх временной БД, в
сквозных сценариях на той стороне — боевой LinkClient и настоящий
`GatewayServices` поверх юнита во временном файле. Сеть наружу подменена:
`routing.fetch` отдаёт фиды из словаря и записывает, куда за ними ходили.

Цена ошибки: фид, который рвёт сессию, превращает канал в петлю
переподключений; фиды, которые перестали доезжать, возвращают адрес квартиры
на GitHub; роль, которая не доехала, — панель агента врёт «несёт трафик» о
резерве; просьба диагностики, исполненная малиной, — чтение с неё того, что
ВПС не положено.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import time
import zlib

import pytest

from awgbot.core import config
from awgbot.domain import gateway as gwmod
from awgbot.domain import gwsnapshot
from awgbot.domain.gateway import GatewayServices
from awgbot.infra import gwguard, routing
from awgbot.infra.db import Database
from awgbot.runtime import linkclient, linkserver
from awgbot.util import gwlink
from tests.integration.test_gwlink_channel import _Gw, _free_port, _until
from tests.unit.test_gwlink_settings_apply import UNIT_TEXT, _Unit

pytestmark = pytest.mark.integration

ADMIN = config.ADMIN_ID
PRIV = base64.b64encode(os.urandom(32)).decode()
KEY = gwlink.channel_key(PRIV)
ITDOG = "https://raw.githubusercontent.com/itdoginfo/allow-domains/main"
DOMAINS = "".join(f"ipset=/site{i}.org/vpn_domains\n" for i in range(30))


@pytest.fixture()
def two(services, make_active_client, monkeypatch):
    """Два слота, как в тестах автомата (test_gateway_slots.py): зонд по слоту —
    словарь probe[slot], переключения записываются в switched."""
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    pi2 = services.add_device(admin.id, "Pi2")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    probe = {1: "ok", 2: "ok"}
    monkeypatch.setattr(services, "_probe_slot", lambda g, active=False: probe[g.id])
    monkeypatch.setattr(services, "_rt_standby_interval", lambda: 0)
    monkeypatch.setattr(services, "_rt_window_size", lambda: 10)
    switched: list[str] = []
    monkeypatch.setattr(routing, "switch_active", lambda iface: switched.append(iface))
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: None)
    services.probe, services.switched = probe, switched
    return services


def _sha(domains: str, nets: str) -> str:
    return hashlib.sha256((domains + "\n--\n" + nets).encode()).hexdigest()


class _Net:
    """Интернет для ВПС: URL → тело; каждый запрос записан."""

    def __init__(self, monkeypatch):
        self.urls: list[str] = []
        self.body = {
            f"{ITDOG}/Russia/inside-dnsmasq-ipset.lst": DOMAINS,
            f"{ITDOG}/Subnets/IPv4/telegram.lst": "91.108.4.0/22\n149.154.160.0/20\n",
            f"{ITDOG}/Subnets/IPv4/meta.lst": "31.13.24.0/21\n",
            "https://www.gstatic.com/ipranges/goog.json":
                '{"prefixes":[{"ipv4Prefix": "8.8.8.0/24"},{"ipv6Prefix": "2001:4860::/32"}]}',
        }

        def fetch(url, timeout=15):
            self.urls.append(url)
            if url in self.body:
                return self.body[url], "", 200
            return None, "404", 404

        monkeypatch.setattr(routing, "fetch", fetch)


@pytest.fixture()
async def link(services, make_active_client, monkeypatch):
    """Слушатель канала и слот с режимом «за шлюзом — без VPN»."""
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 443, "127.0.0.0/30", slot_id=1)
    services.gateway_set_home_subnets(1, "192.168.68.0/24")
    services.gateway_set_lan_mode(1, True)
    monkeypatch.setattr(services, "gateway_resolver_addr", lambda g: "10.9.1.1" if g.lan_mode else "")
    monkeypatch.setattr(services, "_link_privkey", lambda g=None: PRIV)
    port = _free_port()
    monkeypatch.setattr(linkserver, "channel_port", lambda: port)
    monkeypatch.setattr(linkserver, "gw_address", lambda cidr: "127.0.0.1")
    srv = linkserver.LinkServer(services)
    await srv.ensure()
    assert srv._bound
    yield srv
    await srv.stop()


async def _next(gw: _Gw, kind: str, timeout: float = 2.0) -> dict | None:
    """Следующее сообщение вида `kind` или None; прочие виды пропускаются."""
    end = asyncio.get_running_loop().time() + timeout
    while (left := end - asyncio.get_running_loop().time()) > 0:
        msg = await gw.next_msg(left)
        if msg is None:
            return None
        if msg.get("t") == kind:
            return msg
    return None


async def _hello(srv, lists_hash: str = "") -> _Gw:
    host, port = srv._bound[0]
    gw = _Gw(*await asyncio.open_connection(host, port, limit=8 * gwlink.MAX_LINE), key=KEY)
    await gw.send("hello", {"proto": gwlink.PROTO, "agent": "3.4.0", "lists_hash": lists_hash})
    return gw


def _unpack_lists(msg: dict) -> dict:
    return json.loads(zlib.decompress(base64.b64decode(msg["z"])).decode())


# ── фиды на ВПС ──────────────────────────────────────────────────────────────

def test_the_server_fetches_lan_feeds_only_when_a_slot_needs_them(services, link, monkeypatch):
    """Без слота с режимом без VPN фиды не нужны никому — и сервер за ними не
    ходит. С ним — один раз за период; негодное не заменяет годного."""
    net = _Net(monkeypatch)
    services.gateway_set_lan_mode(1, False)
    assert services.gwlink_lan_feeds_update() is False
    assert net.urls == [], "фиды качаются, хотя их некому везти"

    services.gateway_set_lan_mode(1, True)
    assert services.gwlink_lan_feeds_update() is True
    feeds = services.gwlink_lan_feeds()
    assert feeds["domains"] == DOMAINS
    assert set(feeds["nets"].split()) == {"91.108.4.0/22", "149.154.160.0/20", "31.13.24.0/21", "8.8.8.0/24"}
    assert feeds["hash"] == _sha(feeds["domains"], feeds["nets"]), (
        "отпечаток посчитан не так, как его сверяет агент — каждый фид будет отвергнут")

    n = len(net.urls)
    assert services.gwlink_lan_feeds_update() is False
    assert len(net.urls) == n, "второй такт в том же периоде снова пошёл в сеть"
    assert services.gwlink_lan_feeds_update(force=True) is False, "то же содержимое — не «сменились»"


def test_the_server_goes_for_feeds_exactly_where_the_gateway_script_would(services, link, monkeypatch):
    """ВПС качает фиды вместо шлюза. Каждый адрес, куда он ходит, — тот же, что
    в скрипте списков шлюза (константы берём прогоном его строк). Разойдись
    они — состав списков в квартире тихо поменяется при переходе на канал."""
    from pathlib import Path
    script = (Path(__file__).resolve().parents[2] / "install" / "routing-gw-setup.sh").read_text(encoding="utf-8")
    lists = script.split("cat > \"$LAN_LISTS.new\" <<'LISTSEOF'\n", 1)[1].split("\nLISTSEOF\n", 1)[0]
    lines = [ln for ln in lists.splitlines()
             if ln.startswith(("ITDOG=", "DOMAINS_URL=", "SUBNET_SERVICES=", "GOOG_URL="))]
    prog = "\n".join(lines) + ('\nprintf "%s\\n" "$DOMAINS_URL" "$GOOG_URL"'
                              '\nfor s in $SUBNET_SERVICES; do printf "%s\\n" "$ITDOG/Subnets/IPv4/$s.lst"; done')
    r = subprocess.run(["sh", "-c", prog], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    gateway_urls = set(r.stdout.split())
    net = _Net(monkeypatch)
    services.gwlink_lan_feeds_update(force=True)
    assert set(net.urls) == gateway_urls, (
        f"ВПС ходит не туда, куда ходил шлюз: лишние {sorted(set(net.urls) - gateway_urls)}, "
        f"пропущены {sorted(gateway_urls - set(net.urls))}")


def test_the_next_feeds_download_is_jittered_not_a_metronome(services, link, monkeypatch):
    """Семь одних и тех же адресов по часам с адреса ВПС — метроном. Момент
    следующего похода берётся с разбросом ±40 % от периода, а не «прошло
    шесть часов» внутри тика."""
    import random
    _Net(monkeypatch)
    period = 6 * 3600
    for factor in (0.6, 1.4):
        monkeypatch.setattr(random, "uniform", lambda a, b, f=factor: f)
        now = int(time.time())
        services.gwlink_lan_feeds_update(force=True)
        nxt = int(services.db.get_state(services._LAN_FEEDS_AT_KEY))
        assert abs(nxt - (now + int(period * factor))) <= 2, (
            f"следующий поход через {nxt - now} с при множителе {factor}")


@pytest.mark.parametrize("breakage", ["short_domains", "no_nets"])
def test_a_bad_download_keeps_the_previous_feeds(services, link, monkeypatch, breakage):
    """Источник отдал заглушку провайдера или пустоту. Застывший фид лучше
    пустого: ни одна малина не должна получить «ноль доменов» каналом."""
    net = _Net(monkeypatch)
    services.gwlink_lan_feeds_update(force=True)
    before = services.gwlink_lan_feeds()
    if breakage == "short_domains":
        net.body[f"{ITDOG}/Russia/inside-dnsmasq-ipset.lst"] = "<html>blocked</html>\n"
    else:
        for url in list(net.body):
            if "Subnets" in url or "goog" in url:
                del net.body[url]
    assert services.gwlink_lan_feeds_update(force=True) is False
    assert services.gwlink_lan_feeds() == before


# ── доставка фидов ───────────────────────────────────────────────────────────

async def test_feeds_go_to_a_gateway_whose_fingerprint_differs_and_only_once(services, link, monkeypatch):
    """Шлюз назвал в hello другой отпечаток — получает фиды одним сообщением;
    ответ «ок» запоминается, и повтор в той же сессии не уходит ни тактом, ни
    после отказа (иначе отвергнутый фид гонялся бы по кругу)."""
    _Net(monkeypatch)
    services.gwlink_lan_feeds_update(force=True)
    feeds = services.gwlink_lan_feeds()
    gw = await _hello(link, lists_hash="старый")
    msg = await _next(gw, "lists")
    assert msg is not None, "фиды не ушли шлюзу со старым отпечатком"
    assert msg["hash"] == feeds["hash"]
    assert _unpack_lists(msg) == {"domains": feeds["domains"], "nets": feeds["nets"]}

    await gw.send("lists_ack", {"ok": False, "hash": feeds["hash"], "error": "dnsmasq --test"})
    await _until(lambda: services.gwlink_lists(1))
    assert services.gwlink_lists(1)["ok"] is False and "dnsmasq" in services.gwlink_lists(1)["error"]
    await link.deliver_all()
    assert await _next(gw, "lists", 0.4) is None, "отвергнутый фид отправлен в сессии ещё раз"
    await gw.close()


async def test_a_gateway_that_already_has_the_feeds_gets_nothing(services, link, monkeypatch):
    _Net(monkeypatch)
    services.gwlink_lan_feeds_update(force=True)
    gw = await _hello(link, lists_hash=services.gwlink_lan_feeds()["hash"])
    assert await _next(gw, "lists", 0.4) is None, "шлюзу с теми же фидами их прислали снова"
    await link.deliver_all()
    assert await _next(gw, "lists", 0.3) is None
    await gw.close()


async def test_a_gateway_without_local_network_mode_never_gets_feeds(services, link, monkeypatch):
    _Net(monkeypatch)
    services.gwlink_lan_feeds_update(force=True)
    services.gateway_set_lan_mode(1, False)
    gw = await _hello(link, lists_hash="")
    await link.deliver_all()
    assert await _next(gw, "lists", 0.4) is None
    await gw.close()


async def test_new_feeds_on_the_server_go_out_on_the_next_tick(services, link, monkeypatch):
    """Фиды обновились на ВПС посреди сессии — такт живости довозит их, не
    дожидаясь переподключения."""
    net = _Net(monkeypatch)
    services.gwlink_lan_feeds_update(force=True)
    gw = await _hello(link, lists_hash=services.gwlink_lan_feeds()["hash"])
    net.body[f"{ITDOG}/Russia/inside-dnsmasq-ipset.lst"] = DOMAINS + "ipset=/new.org/vpn_domains\n"
    assert services.gwlink_lan_feeds_update(force=True) is True
    await link.deliver_all()
    msg = await _next(gw, "lists")
    assert msg is not None and "new.org" in _unpack_lists(msg)["domains"]
    await gw.close()


# ── оба конца: настоящий клиент и агент ─────────────────────────────────────

class _Pi(GatewayServices):
    def gw_snapshot(self) -> dict:
        return gwsnapshot.collect(mark_status="confirmed", egress_ok=True, guard_info=None,
                                  peer_nets=None)

    def gateway_claim_if_needed(self):
        return None


@pytest.fixture()
async def pi(services, link, tmp_path, monkeypatch):
    """Малина: юнит совпадает с выдаваемым (настройки не едут), скрипт списков
    записывает, откуда его попросили брать фиды, journalctl/nft подменены."""
    want = services.gwlink_settings_want(services.db.gateway(1))
    assert want["LAN_MODE"] == "1" and want["RESOLVER"] == "10.9.1.1"
    unit = _Unit(tmp_path, monkeypatch, text=UNIT_TEXT.replace(
        '"ADMIN_IPS=10.8.1.2"', f'"ADMIN_IPS={want["ADMIN_IPS"]}"'))
    script = tmp_path / "awg-lan-lists.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(gwguard, "LAN_LISTS_SCRIPT", str(script))
    monkeypatch.setattr(gwguard, "LAN_FEED_DIR", str(tmp_path / "feed"))
    lists_runs: list[str] = []
    monkeypatch.setattr(gwguard, "run_lan_lists",
                        lambda timeout=600, from_dir="": lists_runs.append(from_dir) or (True, ""))
    ran: list[list[str]] = []
    monkeypatch.setattr(gwmod, "_run", lambda a, timeout=10: ran.append(list(a)) or
                        subprocess.CompletedProcess(a, 0, stdout=b"journal line\n", stderr=b""))
    conf = tmp_path / "awglink.conf"
    conf.write_text(f"[Interface]\nPrivateKey = {PRIV}\n", encoding="utf-8")
    monkeypatch.setattr(config, "GW_LINK_CONF", str(conf))
    host, port = link._bound[0]
    monkeypatch.setattr(linkclient, "server_address", lambda: host)
    monkeypatch.setattr(linkclient, "server_port", lambda: port)
    monkeypatch.setattr(linkclient, "_notify", None)
    pidb = Database(str(tmp_path / "pi.db"))
    pidb.init_schema()
    agent = _Pi(pidb)
    client = linkclient.LinkClient(agent)
    # клиент процесса — тот, про которого спрашивают linkclient.online()/role()
    monkeypatch.setattr(linkclient, "_client", client)
    state = {"unit": unit, "agent": agent, "client": client, "lists_runs": lists_runs, "ran": ran}
    try:
        yield state
    finally:
        await client.stop()
        pidb.close()


async def _up(services, pi) -> None:
    pi["client"].start()
    await _until(lambda: services.gwlink_snapshot(1) and pi["client"]._writer is not None)


async def test_feeds_from_the_server_are_applied_and_confirmed(services, link, pi, monkeypatch):
    """Вся польза этапа 3: фиды качает ВПС, шлюз применяет их без сети, сервер
    видит «ок», а своё скачивание агента молчит."""
    _Net(monkeypatch)
    services.gwlink_lan_feeds_update(force=True)
    await _up(services, pi)
    await _until(lambda: services.gwlink_lists(1))
    assert services.gwlink_lists(1)["ok"] is True, services.gwlink_lists(1)
    assert services.gwlink_lists(1)["hash"] == services.gwlink_lan_feeds()["hash"]
    assert pi["lists_runs"] == [str(gwguard.LAN_FEED_DIR)], "фиды применены не из каталога канала"
    assert pi["agent"].lan_lists_update() == []
    assert "" not in pi["lists_runs"], "фиды привёз канал, а агент всё равно пошёл в сеть"
    assert pi["unit"].restarts == 0, "доставка фидов перезапустила обвязку"


async def test_a_live_channel_with_unchanged_feeds_keeps_the_agent_off_the_network(
        services, link, pi, monkeypatch):
    """Фиды на ВПС не менялись полсуток — бывает (источник обновляется не каждый
    день). Канал всё это время жив и при каждом подключении слышит от агента тот
    же отпечаток. Агент не должен решить, что канал «замолчал», и вернуться к
    скачиванию сам: иначе адрес квартиры снова ходит на GitHub и в Google при
    живом канале, ради которого всё затевалось."""
    _Net(monkeypatch)
    services.gwlink_lan_feeds_update(force=True)
    digest = services.gwlink_lan_feeds()["hash"]
    agent = pi["agent"]
    # фиды того же отпечатка применены из канала тринадцать часов назад
    agent.db.set_state(agent._LAN_CHANNEL_HASH_KEY, digest)
    agent.db.set_state(agent._LAN_CHANNEL_AT_KEY, str(int(time.time()) - 13 * 3600))
    await _up(services, pi)
    await _until(lambda: agent.lan_feeds_from_channel_fresh())
    agent.lan_lists_update()
    assert pi["lists_runs"] == [], "те же фиды применены повторно или скачаны сами"
    # сессия живёт уже больше полусуток, фиды всё это время не менялись, и
    # подтверждение при подключении было давно — периодического нет и не нужно
    agent.db.set_state(agent._LAN_CHANNEL_AT_KEY, str(int(time.time()) - 13 * 3600))
    assert pi["client"]._writer is not None
    agent.lan_lists_update()
    assert "" not in pi["lists_runs"], (
        "канал жив полсуток с теми же фидами, а агент решил, что он замолчал, и пошёл в сеть сам")


async def test_after_the_session_ends_the_agent_waits_half_a_day_then_downloads(
        services, link, pi, monkeypatch):
    """Канал оборвался — запас на своё скачивание отсчитывается от конца сессии:
    линк моргнул — агент не бежит на GitHub; канал молчит полсуток — бежит, иначе
    квартира так и жила бы с застывшими фидами."""
    _Net(monkeypatch)
    services.gwlink_lan_feeds_update(force=True)
    agent = pi["agent"]
    agent.db.set_state(agent._LAN_CHANNEL_HASH_KEY, services.gwlink_lan_feeds()["hash"])
    agent.db.set_state(agent._LAN_CHANNEL_AT_KEY, str(int(time.time()) - 30 * 3600))
    await _up(services, pi)
    await _until(lambda: agent.lan_feeds_from_channel_fresh())
    # сессия продлилась больше полусуток — подтверждение при подключении давнее
    agent.db.set_state(agent._LAN_CHANNEL_AT_KEY, str(int(time.time()) - 13 * 3600))
    await pi["client"].stop()
    assert linkclient.online() is False
    agent.lan_lists_update()
    assert "" not in pi["lists_runs"], "сессия только что закрылась, а агент уже качает сам"
    agent.db.set_state(agent._LAN_CHANNEL_AT_KEY, str(int(time.time()) - 13 * 3600))
    agent.lan_lists_update()
    assert pi["lists_runs"].count("") == 1, "канал молчит полсуток, а агент так и не качает сам"


async def test_the_same_feeds_are_confirmed_once_per_session_not_by_the_clock(services, link, monkeypatch):
    """Подтверждение «у тебя те же фиды» — событие подключения, а не метроном:
    один раз за сессию, такты живости его не повторяют; новая сессия — снова."""
    _Net(monkeypatch)
    services.gwlink_lan_feeds_update(force=True)
    digest = services.gwlink_lan_feeds()["hash"]
    gw = await _hello(link, lists_hash=digest)
    ok = await _next(gw, "lists_ok")
    assert ok is not None and ok["hash"] == digest
    for _ in range(3):
        await link.deliver_all()
    assert await _next(gw, "lists_ok", 0.4) is None, "подтверждение фидов повторяется тактом живости"
    await gw.close()
    await _until(lambda: not services.gwlink_session(1))
    gw2 = await _hello(link, lists_hash=digest)
    assert await _next(gw2, "lists_ok") is not None, "новая сессия не получила подтверждения"
    await gw2.close()


async def test_a_large_feed_does_not_break_the_session(services, link, pi, monkeypatch):
    """Доменный фид растёт. Пока сообщение в пределах того, что агент готов
    принять (8 МБ), оно обязано дойти и получить ответ — отказ или «ок», — а
    не порвать сессию: порванная сессия переподключается, сервер шлёт тот же фид
    новой сессии, и канал превращается в петлю."""
    net = _Net(monkeypatch)
    big = "".join(f"ipset=/{os.urandom(16).hex()}.org/vpn_domains\n" for i in range(12_000))
    net.body[f"{ITDOG}/Russia/inside-dnsmasq-ipset.lst"] = big
    services.gwlink_lan_feeds_update(force=True)
    feeds = services.gwlink_lan_feeds()
    packed = len(base64.b64encode(zlib.compress(json.dumps(
        {"domains": feeds["domains"], "nets": feeds["nets"]}).encode(), 9)))
    assert gwlink.MAX_LINE < packed < GatewayServices._LAN_FEED_MAX, packed
    await _up(services, pi)
    writer = pi["client"]._writer
    await _until(lambda: services.gwlink_lists(1) or pi["client"]._writer is not writer, timeout=6)
    assert pi["client"]._writer is writer, "большой фид порвал сессию канала"
    assert services.gwlink_lists(1), "на доставку фидов шлюз так и не ответил"


# ── роль слота ───────────────────────────────────────────────────────────────

async def test_the_role_reaches_the_agent_right_after_hello(services, link, pi):
    """Агент сам не знает, несёт ли он трафик: решает автомат на ВПС. Роль
    приходит первой после hello — и панель агента перестаёт гадать."""
    await _up(services, pi)
    await _until(lambda: pi["agent"].link_role())
    assert pi["agent"].link_role() == "active"


async def test_a_switch_reaches_both_agents_once(services, two, monkeypatch):
    """Переключение слота доезжает до обоих агентов тактом живости: прежний
    активный узнаёт, что он теперь резерв, новый — что несёт трафик. И ровно
    один раз: в простое канал молчит."""
    monkeypatch.setattr(services, "_link_privkey", lambda g=None: PRIV)
    port = _free_port()
    monkeypatch.setattr(linkserver, "channel_port", lambda: port)
    # Два слота на одной петле: различать их по адресам здесь нечем, поэтому
    # слушатель один, а коннекты раздаются слотам по очереди (сверку пары
    # адресов проверяет свой тест).
    srv = linkserver.LinkServer(services)
    monkeypatch.setattr(srv, "_wanted", lambda: (("127.0.0.1", port),))
    await srv.ensure()
    queue = [services.db.gateway(1), services.db.gateway(2)]
    monkeypatch.setattr(srv, "_slot_for", lambda local, peer: queue.pop(0) if queue else None)
    try:
        host, port = srv._bound[0]
        a = _Gw(*await asyncio.open_connection(host, port), key=KEY)
        await a.send("hello", {"proto": gwlink.PROTO, "agent": "3.4.0"})
        assert (await _next(a, "role"))["active"] is True
        b = _Gw(*await asyncio.open_connection(host, port), key=KEY)
        await b.send("hello", {"proto": gwlink.PROTO, "agent": "3.4.0"})
        assert (await _next(b, "role"))["active"] is False

        services.gateway_switch(2, manual=True)
        await srv.deliver_all()
        assert (await _next(a, "role"))["active"] is False, "прежний активный не узнал, что он в резерве"
        assert (await _next(b, "role"))["active"] is True, "новый активный не узнал о своей роли"
        await srv.deliver_all()
        assert await _next(a, "role", 0.3) is None and await _next(b, "role", 0.3) is None, (
            "роль без смены шлётся каждым тактом")
        await a.close()
        await b.close()
    finally:
        await srv.stop()


def test_the_agent_panel_names_the_role_only_while_the_channel_is_up(monkeypatch):
    """Роль из прошлой сессии могла смениться без нас — при оборванном канале
    её не показываем вовсе, а не выдаём старую за текущую."""
    from awgbot.bot.texts.gateway import channel_panel_line
    monkeypatch.setattr(linkclient, "enabled", lambda: True)
    monkeypatch.setattr(linkclient, "online", lambda: True)
    monkeypatch.setattr(linkclient, "role", lambda: "active")
    assert channel_panel_line() == "🔗 Канал до сервера AWG: 🟢 на связи · несёт трафик"
    monkeypatch.setattr(linkclient, "role", lambda: "standby")
    assert channel_panel_line() == "🔗 Канал до сервера AWG: 🟢 на связи · в резерве"
    monkeypatch.setattr(linkclient, "role", lambda: "")
    assert channel_panel_line() == "🔗 Канал до сервера AWG: 🟢 на связи"
    monkeypatch.setattr(linkclient, "online", lambda: False)
    monkeypatch.setattr(linkclient, "role", lambda: "active")
    assert channel_panel_line() == "🔗 Канал до сервера AWG: ⚪ нет связи"


# ── диагностики по каналу нет ────────────────────────────────────────────────

async def test_a_server_asking_for_a_diagnostic_gets_nothing_run_and_no_answer(services, link, pi):
    """Диагностику по каналу сняли. Старый или взломанный ВПС всё ещё может
    прислать «ask tail» — малина обязана ничего не запустить, ничего не
    прочесть и не оборвать сессию: неизвестная просьба пропускается."""
    await _up(services, pi)
    before = list(pi["ran"])
    for name in ("unit", "table", "../../etc/shadow"):
        assert await link.send(1, "ask", {"what": "tail", "name": name}), "сессия есть — отправка должна пройти"
    await asyncio.sleep(0.5)
    assert pi["ran"] == before, f"по просьбе диагностики на малине что-то запустилось: {pi['ran']}"
    assert pi["client"]._writer is not None and link.online(1), "просьба диагностики оборвала сессию"


# ── автомат переключения снимку не верит ─────────────────────────────────────

def _snap_egress(services, slot_id: int, ok: bool) -> None:
    services.gwlink_snapshot_in(slot_id, {"agent_version": "3.4.0", "egress_ok": ok,
                                          "rev": 1}, 1, True)
    services.gwlink_session_opened(slot_id, "3.4.0", gwlink.PROTO)


def test_the_gateways_own_egress_view_does_not_move_the_traffic(services, two):
    """Снимок приехал с чужой машины: «у меня нет выхода» от шлюза не
    переключает трафик, пока улики и зонд ВПС говорят, что путь жив."""
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    _snap_egress(services, 1, False)
    _snap_egress(services, 2, True)
    for _ in range(services._rt_fail_need() * 2):
        services.routing_liveness_tick()
    assert services.active_gateway().id == 1 and services.switched == [], (
        "автомат переключился по слову шлюза")


def test_the_gateways_own_egress_view_does_not_hold_the_traffic(services, two):
    """Обратное: шлюз уверяет «выход есть», а зонд ВПС видит, что путь мёртв, —
    автомат переключает так же, как без канала."""
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    _snap_egress(services, 1, True)
    _snap_egress(services, 2, False)
    services.probe[1] = "down"
    for _ in range(services._rt_fail_need()):
        services.routing_liveness_tick()
    assert services.active_gateway().id == 2, "снимок шлюза удержал трафик на мёртвом пути"
