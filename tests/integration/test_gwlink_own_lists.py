"""
Свои списки, общие для всех шлюзов, по каналу линка (концепт «синхронизация
своих списков» §2.4–§2.6, §3, §13; план тестов — §11, этап 2): `own_ev` от
шлюза, слияние в канон на ВПС, `own_set` отправителю и остальным шлюзам сразу,
`own_ack` назад, `own_hash` в `hello`.

Сокеты настоящие (петля), сервер — боевой LinkServer поверх временной БД; два
слота на одном слушателе, коннекты раздаются слотам по очереди (как в
test_gwlink_services). Второй шлюз — сырой клиент `_Gw`; в сквозных
сценариях на первом — боевой LinkClient, настоящий `GatewayServices` и
НАСТОЯЩИЙ awg-lan-domain.sh из скрипта обвязки (systemctl, dnsmasq, nft, dig,
id подменены в PATH).

Цена ошибки: канон, который ездит по кругу, — рестарт dnsmasq на каждом
такте у каждой квартиры и маячок в туннеле; правка, слитая дважды, —
воскресший удалённый домен; канон старому агенту — вечное «отправлены»;
правка, которую сервер счёл повтором, — домен есть на одном шлюзе и нет на
остальных, а агент шлёт её на каждом тике.
"""
from __future__ import annotations

import asyncio
import base64
import os
import threading
import types
from pathlib import Path

import pytest

from awgbot.core import config
from awgbot.domain import gwsnapshot
from awgbot.domain.gateway import GatewayServices
from awgbot.infra import gwguard
from awgbot.infra.db import Database
from awgbot.runtime import linkclient, linkserver
from awgbot.util import gwlink
from tests.integration.test_gwlink_channel import _Gw, _free_port, _until
from tests.integration.test_gwlink_services import _next

pytestmark = pytest.mark.integration

ADMIN = config.ADMIN_ID
PRIV = base64.b64encode(os.urandom(32)).decode()
KEY = gwlink.channel_key(PRIV)
VPS = "vps.example.net"
SCRIPT = Path(__file__).resolve().parents[2] / "install" / "routing-gw-setup.sh"


@pytest.fixture()
async def pair(services, make_active_client, monkeypatch):
    """Два слота в режиме без VPN; слушатель один, слот коннекту — из очереди
    `queue`. `sent` — что сервер отправил (слот, вид); `ev`/`acks` — что
    дошло от шлюзов до слияния и до отметки ответа."""
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    pi2 = services.add_device(admin.id, "Pi2")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    services.gateway_set_home_subnets(1, "192.168.1.0/24")
    services.gateway_set_home_subnets(2, "192.168.68.0/24")
    services.db.gateway_update(1, lan_mode=1)
    services.db.gateway_update(2, lan_mode=1)
    monkeypatch.setattr(config, "SERVER_HOST", VPS)
    monkeypatch.setattr(services, "peer_nets_enabled", lambda: False)
    monkeypatch.setattr(services, "gateway_resolver_addr", lambda g: "10.9.1.1")
    monkeypatch.setattr(services, "_link_privkey", lambda g=None: PRIV)
    # доставка настроек — не предмет этих тестов
    monkeypatch.setattr(services, "gwlink_settings_due", lambda gw: None)
    port = _free_port()
    monkeypatch.setattr(linkserver, "channel_port", lambda: port)
    srv = linkserver.LinkServer(services)
    monkeypatch.setattr(srv, "_wanted", lambda: {("127.0.0.1", port)})
    queue: list[int] = []
    monkeypatch.setattr(srv, "_slot_for", lambda local, peer: services.db.gateway(queue.pop(0)) if queue else None)
    await srv.ensure()
    assert srv._bound
    sent: list[tuple[int, str]] = []
    real_send = srv.send

    async def send(slot_id, kind, body=None, pad=gwlink.PAD_DELTA):
        ok = await real_send(slot_id, kind, body, pad=pad)
        if ok:
            sent.append((slot_id, kind))
        return ok
    monkeypatch.setattr(srv, "send", send)
    ev: list[tuple[int, str, list]] = []
    real_in = services.gwlink_own_in
    monkeypatch.setattr(services, "gwlink_own_in",
                        lambda slot, run, events: ev.append((slot, run, list(events))) or real_in(slot, run, events))
    acks: list[tuple[int, dict]] = []
    real_ack = services.gwlink_own_ack_in
    monkeypatch.setattr(services, "gwlink_own_ack_in",
                        lambda slot, body: acks.append((slot, dict(body))) or real_ack(slot, body))
    p = types.SimpleNamespace(srv=srv, queue=queue, services=services, sent=sent, port=port, ev=ev, acks=acks)
    try:
        yield p
    finally:
        await srv.stop()


async def _hello(p, slot: int, own_hash: str | None = "", agent: str = "3.1.0") -> _Gw:
    """Сырой шлюз: own_hash=None — агент, который синхронизацию не знает."""
    p.queue.append(slot)
    host, port = p.srv._bound[0]
    gw = _Gw(*await asyncio.open_connection(host, port, limit=8 * gwlink.MAX_LINE), key=KEY)
    body = {"proto": gwlink.PROTO, "agent": agent, "lists_hash": "", "svc_hash": ""}
    if own_hash is not None:
        body["own_hash"] = own_hash
    await gw.send("hello", body)
    assert await _until(lambda: p.services.gwlink_session(slot)), "hello не принят"
    return gw


async def _ack(gw: _Gw, msg: dict, ok: bool = True, error: str = "") -> None:
    await gw.send("own_ack", {"ok": ok, "hash": msg["hash"], "n": len(msg["items"]), "error": error})


def _own_sets(p, slot: int) -> int:
    return p.sent.count((slot, "own_set"))


def _canon(s) -> dict:
    """{домен: вид} канона на ВПС."""
    from awgbot.domain import gwownlists
    return gwownlists.canon_items(s.gwlink_own_canon())


def _card(s, slot: int) -> dict:
    return s.gwlink_own_card(s.db.gateway(slot))


# ── доставка ─────────────────────────────────────────────────────────────────

async def test_an_edit_goes_back_to_its_sender_and_to_the_other_gateway_at_once_exactly_once(pair):
    """Кнопка на X: сервер сливает правку и сразу отвечает X каноном с его
    upto, а Y получает тот же канон тоже сразу, без такта живости (§13). Такты
    после этого не шлют ничего; правка без изменения канона уходит только
    отправителю — у остальных рестарта dnsmasq ради неё не будет."""
    s = pair.services
    x = await _hello(pair, 1)
    y = await _hello(pair, 2)
    mx, my = await _next(x, "own_set"), await _next(y, "own_set")
    assert mx is not None and my is not None, "при hello с пустым own_hash канон не ушёл"
    assert mx["items"] == [] and mx["ver"] == 0 and mx["gen"] == my["gen"] and mx["gen"]
    await _ack(x, mx); await _ack(y, my)
    assert await _until(lambda: len(pair.acks) == 2)
    await x.send("own_ev", {"run": "rx", "ev": [[1, "example.com", "vpn", False]]})
    mx = await _next(x, "own_set")
    assert mx is not None, "отправитель не получил канон со своей правкой"
    assert mx["items"] == [["example.com", "vpn"]] and mx["upto"] == ["rx", 1] and mx["ver"] == 1
    my = await _next(y, "own_set")
    assert my is not None, "второй шлюз не получил правку сразу"
    assert my["items"] == [["example.com", "vpn"]] and my["upto"] == ["", 0]
    for _ in range(3):
        await pair.srv.deliver_all()
    assert await _next(x, "own_set", 0.4) is None and await _next(y, "own_set", 0.4) is None, \
        "такт живости повторил ушедший канон"
    assert (_own_sets(pair, 1), _own_sets(pair, 2)) == (2, 2)
    # «уже в списке»: канон не изменился — отправителю новый upto, соседу ничего
    await x.send("own_ev", {"run": "rx", "ev": [[2, "example.com", "vpn", False]]})
    mx = await _next(x, "own_set")
    assert mx is not None and mx["upto"] == ["rx", 2] and mx["ver"] == 1
    assert await _next(y, "own_set", 0.4) is None, "неизменившийся канон ушёл соседу"
    assert _canon(s) == {"example.com": "vpn"}
    await x.close(); await y.close()


@pytest.mark.parametrize("have,expect", [("CURRENT", False), ("", True), ("0" * 64, True)])
async def test_the_hash_named_in_hello_decides_whether_the_canon_goes(pair, have, expect):
    """Переподключение после моргнувшего линка: канон у шлюза тот же — сервер
    молчит; другой или никакого — везёт."""
    s = pair.services
    s.gwlink_own_in(2, "ry", [[1, "a.com", "vpn", False]])
    current = s.gwlink_own_for(s.db.gateway(1))[0]
    x = await _hello(pair, 1, own_hash=current if have == "CURRENT" else have)
    await pair.srv.deliver_all()
    msg = await _next(x, "own_set", 0.6)
    assert (msg is not None) is expect, f"own_hash={have!r}: доставка {msg}"
    if msg is not None:
        assert msg["hash"] == current and msg["items"] == [["a.com", "vpn"]]
    await x.close()


async def test_an_agent_without_own_hash_gets_no_canon_and_keeps_its_session(pair):
    """ВПС новый, агент старый: поля own_hash в hello нет — канон ему не шлётся
    вовсе (иначе вечное «отправлены»), сессия жива, карточка говорит
    «обнови шлюз», даже если снимок ещё не пришёл. Агент с полем — «отправлены»,
    после ответа — применены."""
    s = pair.services
    y = await _hello(pair, 2, own_hash=None)
    x = await _hello(pair, 1)
    mx = await _next(x, "own_set")
    await x.send("own_ev", {"run": "rx", "ev": [[1, "a.com", "vpn", False]]})
    mx = await _next(x, "own_set")
    assert mx is not None
    for _ in range(3):
        await pair.srv.deliver_all()
    assert await _next(y, "own_set", 0.5) is None, "канон ушёл агенту, который его не знает"
    assert pair.srv.online(2) and s.gwlink_session(2)
    assert _card(s, 2)["state"] == "old_agent", _card(s, 2)
    assert _card(s, 1)["state"] == "pending", _card(s, 1)
    await _ack(x, mx)
    assert await _until(lambda: _card(s, 1)["state"] == "applied"), _card(s, 1)
    await x.close(); await y.close()


async def test_lan_mode_off_gets_nothing_and_turning_it_on_sends_at_once(pair):
    """Режим без VPN выключен на Y — списков у него нет, канон не нужен. Режим
    включили каналом (ack с LAN_MODE) — канон уходит в ту же секунду, не
    дожидаясь такта."""
    s = pair.services
    s.db.gateway_update(2, lan_mode=0)
    y = await _hello(pair, 2)
    x = await _hello(pair, 1)
    await _next(x, "own_set")
    await x.send("own_ev", {"run": "rx", "ev": [[1, "a.com", "vpn", False]]})
    assert await _next(x, "own_set") is not None
    await pair.srv.deliver_all()
    assert await _next(y, "own_set", 0.5) is None, "канон ушёл шлюзу с выключенным режимом"
    s.db.gateway_update(2, lan_mode=1)
    await y.send("ack", {"ok": True, "changed": ["LAN_MODE"]})
    msg = await _next(y, "own_set", 1.0)
    assert msg is not None and msg["items"] == [["a.com", "vpn"]], "включение режима не привезло канон сразу"
    await x.close(); await y.close()


async def test_the_server_host_is_refused_and_named_back_to_the_sender(pair):
    s = pair.services
    x = await _hello(pair, 1)
    await _next(x, "own_set")
    await x.send("own_ev", {"run": "rx", "ev": [[1, "api." + VPS, "vpn", False], [2, "ok.com", "ru", False]]})
    msg = await _next(x, "own_set")
    assert msg is not None and msg["rej"] == [["api." + VPS, "хост сервера"]], msg
    assert _canon(s) == {"ok.com": "ru"}, "хост сервера попал в канон всех шлюзов"
    await x.close()


# ── гонки и повтор ───────────────────────────────────────────────────────────

async def test_a_replay_after_a_broken_session_is_not_merged_twice(pair):
    """Сессия X оборвалась между own_ev и own_set; пока X переподключался, Y
    удалил домен. Повтор той же правки X не воскрешает его: upto X помнит,
    что разобрано."""
    s = pair.services
    x = await _hello(pair, 1)
    await _next(x, "own_set")
    await x.send("own_ev", {"run": "rx", "ev": [[1, "a.com", "vpn", False]]})
    assert await _until(lambda: "a.com" in _canon(s))
    await x.close()
    await _until(lambda: not s.gwlink_session(1))
    y = await _hello(pair, 2)
    await _next(y, "own_set")
    await y.send("own_ev", {"run": "ry", "ev": [[1, "a.com", "del", False]]})
    assert await _until(lambda: "a.com" not in _canon(s))
    ver = s.gwlink_own_canon()["ver"]
    x2 = await _hello(pair, 1)
    await _next(x2, "own_set")
    await x2.send("own_ev", {"run": "rx", "ev": [[1, "a.com", "vpn", False]]})
    msg = await _next(x2, "own_set", 0.6)
    assert "a.com" not in _canon(s), "повтор после обрыва воскресил удалённый домен"
    assert s.gwlink_own_canon()["ver"] == ver
    assert msg is None or msg["upto"] == ["rx", 1]
    await x2.close(); await y.close()


def test_two_gateways_merging_at_once_lose_nothing(pair):
    """События двух слотов разбираются в разных потоках: без блокировки и
    транзакции чтение-правка-запись канона теряла бы правки друг друга."""
    s = pair.services
    s.gwlink_own_canon()
    errors: list[BaseException] = []

    def feed(slot: int) -> None:
        try:
            for i in range(40):
                s.gwlink_own_in(slot, f"r{slot}", [[i + 1, f"s{slot}-{i}.com", "vpn", False]])
        except BaseException as e:            # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=feed, args=(slot,)) for slot in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(_canon(s)) == 80, f"потеряны правки при параллельном слиянии: {len(_canon(s))} из 80"
    assert s.gwlink_own_canon()["ver"] == 80


async def test_the_first_sync_of_two_gateways_is_a_union_and_direct_wins(pair):
    """Оба шлюза пришли со своими списками 3.0/3.1: канон — объединение; спор
    вида — «напрямую», в каком бы порядке шлюзы ни подключались."""
    s = pair.services
    x = await _hello(pair, 1)
    await _next(x, "own_set")
    await x.send("own_ev", {"run": "rx", "ev": [[1, "both.com", "vpn", True], [2, "shop.ru", "ru", True],
                                               [3, "x-only.com", "vpn", True]]})
    await _next(x, "own_set")
    y = await _hello(pair, 2)
    await _next(y, "own_set")
    await y.send("own_ev", {"run": "ry", "ev": [[1, "both.com", "ru", True], [2, "shop.ru", "vpn", True],
                                               [3, "y-only.com", "ru", True]]})
    my = await _next(y, "own_set")
    assert my is not None and my["upto"] == ["ry", 3]
    union = {"both.com": "ru", "shop.ru": "ru", "x-only.com": "vpn", "y-only.com": "ru"}
    assert dict(map(tuple, my["items"])) == union, my["items"]
    mx = await _next(x, "own_set")
    assert mx is not None and dict(map(tuple, mx["items"])) == union, "первый шлюз не получил объединение"
    assert _canon(s) == union
    await x.close(); await y.close()


# ── снятие слота ─────────────────────────────────────────────────────────────

def test_forgetting_a_slot_clears_its_keys_and_keeps_the_canon(pair):
    """Слот сняли — его upto, ответ, отвергнутое и признак «знает
    синхронизацию» уходят; канон общий и остаётся: вернувшийся шлюз получит его."""
    s = pair.services
    s.gwlink_own_in(1, "rx", [[1, "a.com", "vpn", False], [2, "bad_host", "vpn", False]])
    s.gwlink_own_ack_in(1, {"ok": True, "hash": "ab" * 32, "n": 1})
    s.gwlink_own_hello_in(1, True)
    keys = [s._gwlink_key(k, 1) for k in (s._GWLINK_OWN_UPTO_KEY, s._GWLINK_OWN_ACK_KEY,
                                          s._GWLINK_OWN_REJ_KEY, s._GWLINK_OWN_CAP_KEY)]
    assert all(s.db.get_state(k) for k in keys), [s.db.get_state(k) for k in keys]
    before = s.db.get_state(s._GWLINK_OWN_KEY)
    s.gwlink_forget(1)
    assert [s.db.get_state(k) or "" for k in keys] == ["", "", "", ""], "ключи своих списков пережили снятие слота"
    assert s.db.get_state(s._GWLINK_OWN_KEY) == before, "снятие слота тронуло общий канон"
    assert _canon(s) == {"a.com": "vpn"}


# ── оба конца: настоящий агент и настоящий скрипт ───────────────────────────

class _Pi(GatewayServices):
    def gw_snapshot(self) -> dict:
        return gwsnapshot.collect(mark_status="confirmed", egress_ok=True, guard_info=None, peer_nets=None)

    def gateway_claim_if_needed(self):
        return None


def _heredoc(tag: str, var: str) -> str:
    script = SCRIPT.read_text(encoding="utf-8")
    return script.split(f'cat > "${var}" <<\'{tag}\'\n', 1)[1].split(f"\n{tag}\n", 1)[0] + "\n"


class _Host:
    """Малина под агентом: юнит обвязки (`env`), dnsmasq.d и настоящий
    awg-lan-domain.sh; systemctl/dnsmasq/nft/dig/id подменены и пишут в лог.
    `old_script()` — обвязка старого образца (скрипт без sync), `reasserts` —
    сколько раз агент перевыставлял обвязку."""

    def __init__(self, tmp_path: Path, monkeypatch, env: dict):
        self.env = env
        self.dns_d = tmp_path / "dnsmasq.d"; self.dns_d.mkdir()
        self.log = tmp_path / "host.log"; self.log.write_text("", encoding="utf-8")
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        for name, body in (("systemctl", f'echo "systemctl $*" >> {self.log}\n'),
                           ("dnsmasq", f'echo "dnsmasq $*" >> {self.log}\n'),
                           ("nft", f'echo "nft $*" >> {self.log}\n'),
                           ("dig", ""), ("sleep", ""), ("id", "echo 0\n")):
            (bin_dir / name).write_text("#!/bin/sh\n" + body, encoding="utf-8")
            (bin_dir / name).chmod(0o755)
        self.tool = tmp_path / "awg-lan-domain.sh"
        self.fresh = _heredoc("DOMEOF", "LAN_DOMAIN")
        self.tool.write_text(self.fresh, encoding="utf-8"); self.tool.chmod(0o755)
        uplink = tmp_path / "awg0.conf"
        uplink.write_text(f"[Peer]\nEndpoint = {VPS}:51820\n", encoding="utf-8")
        self.reasserts = 0
        self.write()
        monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
        monkeypatch.setenv("AWG_DNSMASQ_D", str(self.dns_d))
        monkeypatch.setenv("AWG_UPLINK_CONF", str(uplink))
        monkeypatch.setenv("AWG_LAN_DUMP", str(tmp_path / "dump"))
        monkeypatch.setattr(gwguard, "unit_env", lambda k: self.env.get(k, ""))
        monkeypatch.setattr(gwguard, "lan_mode", lambda: self.env.get("LAN_MODE") == "1")
        monkeypatch.setattr(gwguard, "avahi_browse", lambda timeout=15: "")
        monkeypatch.setattr(gwguard, "LAN_DOMAIN_SCRIPT", str(self.tool))
        monkeypatch.setattr(gwguard, "DNSMASQ_D", str(self.dns_d))
        monkeypatch.setattr(gwguard, "OWN_LISTS_NEW", str(tmp_path / "lib" / "own-lists.new"))
        monkeypatch.setattr(gwguard, "unit_state", lambda: {"ActiveState": "active"})
        monkeypatch.setattr(gwguard, "reassert", self._reassert)

    def _reassert(self, timeout=90):
        self.reasserts += 1
        return True, ""

    def old_script(self) -> None:
        self.tool.write_text(self.fresh.replace("# awg-lan-domain: sync\n", ""), encoding="utf-8")

    def new_script(self) -> None:
        self.tool.write_text(self.fresh, encoding="utf-8")

    def write(self, vpn=(), ru=()) -> None:
        (self.dns_d / "awg-gw-vpn-user.conf").write_text(
            "".join(f"nftset=/{d}/inet#awg_home#lan_vpn4\n" for d in vpn), encoding="utf-8")
        (self.dns_d / "awg-gw-ru-user.conf").write_text(
            "".join(f"nftset=/{d}/inet#awg_home#lan_ru4\n" for d in ru), encoding="utf-8")

    def lists(self) -> dict:
        from awgbot.domain import gwownlists
        ok, out = gwguard.run_lan_domain("list", [])
        assert ok, out
        return gwownlists.parse_list(out)

    def restarts(self) -> int:
        return self.log.read_text().count("systemctl restart dnsmasq")


ENV = {"LAN_MODE": "1", "HOME_SUBNETS": "192.168.1.0/24", "LINK_CHANNEL": "1"}


@pytest.fixture()
async def pi(pair, tmp_path, monkeypatch):
    """Малина слота 1: `host` и фабрика `up()` — агент (на той же БД после
    «перезапуска») с клиентом канала, уже на связи."""
    conf = tmp_path / "awglink.conf"
    conf.write_text(f"# awg-bot: контракт линка 1\n[Interface]\nPrivateKey = {PRIV}\n", encoding="utf-8")
    monkeypatch.setattr(config, "GW_LINK_CONF", str(conf))
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.1.0")
    monkeypatch.setattr(config, "ROLE", "gateway")
    monkeypatch.setattr(linkclient, "server_address", lambda: "127.0.0.1")
    monkeypatch.setattr(linkclient, "server_port", lambda: pair.port)
    monkeypatch.setattr(linkclient, "_notify", None)
    monkeypatch.setattr(_Pi, "_own_run", "")
    host = _Host(tmp_path, monkeypatch, dict(ENV))
    db = Database(str(tmp_path / "pi.db")); db.init_schema()
    clients: list = []

    def agent() -> _Pi:
        a = _Pi(db)
        a._last_reassert = -1e9
        return a

    async def up(a: _Pi, slot: int = 1) -> linkclient.LinkClient:
        c = linkclient.LinkClient(a)
        monkeypatch.setattr(linkclient, "_client", c)
        clients.append(c)
        pair.queue.append(slot)
        c.start()
        assert await _until(lambda: pair.services.gwlink_snapshot(slot) and c._writer is not None, timeout=5)
        return c

    async def down(c: linkclient.LinkClient, slot: int = 1) -> None:
        await c.stop()
        assert await _until(lambda: not pair.services.gwlink_session(slot), timeout=5)

    ns = types.SimpleNamespace(host=host, db=db, agent=agent, up=up, down=down)
    try:
        yield ns
    finally:
        for c in clients:
            await c.stop()
        db.close()


def _seed(s, items: dict, slot: int = 2, run: str = "seed") -> None:
    """Канон на ВПС уже есть: его набрал шлюз слота `slot`."""
    s.gwlink_own_in(slot, run, [[i + 1, d, k, False] for i, (d, k) in enumerate(sorted(items.items()))])


async def test_a_canon_is_applied_by_the_real_script_and_a_button_reaches_the_other_gateway(pair, pi):
    """Главный путь: новый шлюз получил канон при подключении — настоящий sync
    записал оба файла, один рестарт dnsmasq, сервер видит «применены». Кнопка
    на нём — правка у другого шлюза сразу, а у самого шлюза свой же канон
    файлов не трогает."""
    s = pair.services
    _seed(s, {"news.org": "vpn", "shop.ru": "ru"})
    y = await _hello(pair, 2)
    await _ack(y, await _next(y, "own_set"))
    a = pi.agent()
    await pi.up(a)
    assert await _until(lambda: _card(s, 1)["state"] == "applied", timeout=5), (_card(s, 1), pair.acks)
    assert pi.host.lists() == {"news.org": "vpn", "shop.ru": "ru"}
    assert pi.host.restarts() == 1
    assert [e for e in pair.ev if e[0] == 1] == [], "первый шлюз без своих списков прислал правки"
    # кнопка «➕ В туннель»
    ok, out = a.lan_domains("add", ["example.com"])
    assert ok and "example.com: добавлен" in out, out
    restarts = pi.host.restarts()
    await linkclient.own_changed(a)
    my = await _next(y, "own_set")
    assert my is not None and ["example.com", "vpn"] in my["items"], f"правка не дошла до соседа: {my}"
    assert await _until(lambda: len([k for sl, k in pair.acks if sl == 1]) == 2, timeout=5), pair.acks
    assert pi.host.restarts() == restarts, "свой же канон перезапустил dnsmasq"
    assert _card(s, 1)["state"] == "applied"
    info, _ = a.own_status()
    assert info["state"] == "synced" and info["pending"] == 0, info
    await y.close()


async def test_a_second_button_in_the_same_run_also_reaches_the_canon(pair, pi):
    """Два нажатия подряд в одном запуске агента, между ними канон применён.
    Вторая правка обязана дойти до канона: сервер помнит upto этого запуска,
    и номер, начатый заново с единицы, он отбросит как повтор."""
    s = pair.services
    a = pi.agent()
    await pi.up(a)
    assert await _until(lambda: _card(s, 1)["state"] == "applied", timeout=5), _card(s, 1)
    for d in ("one.com", "two.com"):
        ok, out = a.lan_domains("add", [d])
        assert ok, out
        await linkclient.own_changed(a)
        assert await _until(lambda: d in _canon(s), timeout=3), (
            f"правка {d} не вошла в канон: события {pair.ev}, upto {s._gwlink_own_upto(1)}")
        assert await _until(lambda: a.own_status()[0]["state"] == "synced", timeout=3), a.own_status()[0]


async def test_the_first_sync_merges_the_gateways_list_and_restarts_dnsmasq_once(pair, pi):
    """Шлюз со своими списками 3.0/3.1 встречает канон другого шлюза: пустой
    чужой канон не стирает его список, весь список уходит init, спор вида —
    «напрямую»; итог — объединение одним рестартом, и другой шлюз получает
    его сразу."""
    s = pair.services
    y = await _hello(pair, 2)
    await _next(y, "own_set")
    await y.send("own_ev", {"run": "ry", "ev": [[1, "both.com", "vpn", True], [2, "shop.ru", "ru", True],
                                               [3, "y-only.com", "vpn", True]]})
    await _ack(y, await _next(y, "own_set"))
    pi.host.write(vpn=["x-only.com", "shop.ru"], ru=["both.com"])
    a = pi.agent()
    await pi.up(a)
    union = {"both.com": "ru", "shop.ru": "ru", "x-only.com": "vpn", "y-only.com": "vpn"}
    assert await _until(lambda: _card(s, 1)["state"] == "applied", timeout=5), (_card(s, 1), pair.ev)
    assert _canon(s) == union
    assert pi.host.lists() == union, "шлюз не применил объединение"
    assert pi.host.restarts() == 1, f"первая синхронизация — {pi.host.restarts()} рестартов dnsmasq"
    init = [e for e in pair.ev if e[0] == 1]
    assert len(init) == 1 and all(ev[3] for ev in init[0][2]), f"список ушёл не одним пакетом init: {init}"
    my = await _next(y, "own_set")
    assert my is not None and dict(map(tuple, my["items"])) == union
    await y.close()


async def test_an_offline_edit_and_a_restart_reach_the_canon_on_connect(pair, pi):
    """Канала нет: `awg-bot lan add` на малине ложится в очередь сразу (тик
    монитора), монитор честно пишет «нет связи». Агент перезапустился —
    правка уходит под новой меткой при подключении, канон применяется без
    лишнего рестарта dnsmasq."""
    s = pair.services
    _seed(s, {"news.org": "vpn"})
    a = pi.agent()
    c = await pi.up(a)
    assert await _until(lambda: _card(s, 1)["state"] == "applied", timeout=5)
    await pi.down(c)
    ok, out = a.lan_domains("ru", ["shop.ru"])
    assert ok, out
    await c.own_tick()
    info, checks = a.own_status()
    assert info["state"] == "no_link", info
    assert checks[0].detail == "ждут синхронизации (1 правка): нет связи с сервером AWG", checks[0].detail
    restarts = pi.host.restarts()
    old_run = a._own_run_id()
    # перезапуск агента: новый процесс — новая метка
    type(a)._own_run = ""
    b = pi.agent()
    await pi.up(b)
    assert await _until(lambda: "shop.ru" in _canon(s), timeout=5), f"офлайн-правка не дошла: {pair.ev}"
    assert await _until(lambda: b.own_status()[0]["state"] == "synced", timeout=5), b.own_status()[0]
    runs = [run for sl, run, _ in pair.ev if sl == 1]
    assert len(runs) == 1 and runs[0] != old_run, f"правка ушла не под новой меткой запуска: {runs}, прежняя {old_run}"
    assert pi.host.restarts() == restarts, "канон с той же правкой перезапустил dnsmasq"
    assert _card(s, 1)["state"] == "applied"


async def test_a_restored_agent_copy_does_not_bring_back_a_deleted_domain(pair, pi):
    """Копию БД агента восстановили вместе с файлами: база и файлы из одного
    момента, сверка пуста — удалённый с тех пор на другом шлюзе домен не
    уходит правкой, свежий канон снимает его и здесь."""
    s = pair.services
    _seed(s, {"a.com": "vpn", "b.com": "vpn"})
    a = pi.agent()
    c = await pi.up(a)
    assert await _until(lambda: _card(s, 1)["state"] == "applied", timeout=5)
    copy = {k: pi.db.get_state(k) for k in ("gw_own_base", "gw_own_fp", "gw_own_pending")}
    files = pi.host.lists()
    await pi.down(c)
    s.gwlink_own_in(2, "ry", [[1, "b.com", "del", False]])
    # восстановление: БД и файлы — из копии
    for k, v in copy.items():
        pi.db.set_state(k, v or "")
    pi.host.write(vpn=[d for d, k in files.items() if k == "vpn"], ru=[d for d, k in files.items() if k == "ru"])
    await c.own_tick()
    await pi.up(a)
    assert await _until(lambda: pi.host.lists() == {"a.com": "vpn"}, timeout=5), pi.host.lists()
    assert "b.com" not in _canon(s), "восстановленная копия воскресила удалённый домен"
    assert [e for e in pair.ev if e[0] == 1] == [], f"восстановленный шлюз прислал правки: {pair.ev}"


async def test_a_server_restored_from_an_old_copy_gets_the_lists_back_as_init(pair, pi):
    """ВПС восстановлен из копии, где канон старше: шлюз не принимает его на
    веру, а присылает свой список init — канон снова полон."""
    s = pair.services
    old = s.gwlink_own_canon()
    _seed(s, {"a.com": "vpn", "b.ru": "ru"})
    a = pi.agent()
    c = await pi.up(a)
    assert await _until(lambda: _card(s, 1)["state"] == "applied", timeout=5)
    await pi.down(c)
    import json
    s.db.set_state(s._GWLINK_OWN_KEY, json.dumps(old))        # старая копия: то же поколение, ver 0
    s.db.set_state(s._gwlink_key(s._GWLINK_OWN_UPTO_KEY, 1), "")
    assert _canon(s) == {}
    await pi.up(a)
    assert await _until(lambda: _canon(s) == {"a.com": "vpn", "b.ru": "ru"}, timeout=5), (_canon(s), pair.ev)
    assert await _until(lambda: _card(s, 1)["state"] == "applied", timeout=5)
    assert pi.host.lists() == {"a.com": "vpn", "b.ru": "ru"}, "старый канон стёр списки шлюза"
    assert all(ev[3] for _, _, evs in pair.ev[-1:] for ev in evs), "список ушёл не init"


async def test_an_old_script_is_reasserted_and_the_canon_lands_on_the_next_tick(pair, pi):
    """Агент обновился, юнит обвязки ещё старый — скрипт без sync: канон
    откладывается, обвязка перевыставляется, сервер видит отказ. На тике со
    свежим скриптом канон применяется сам и ответ уходит тем же отпечатком;
    дальше тишина."""
    s = pair.services
    _seed(s, {"news.org": "vpn"})
    pi.host.old_script()
    a = pi.agent()
    c = await pi.up(a)
    assert await _until(lambda: pair.acks, timeout=5), "на канон агент не ответил"
    slot, ack = pair.acks[0]
    assert ack["ok"] is False and "старого образца" in ack["error"], ack
    assert pi.host.reasserts == 1 and pi.host.lists() == {}
    assert _card(s, 1)["state"] == "failed"
    await linkclient.on_tick(a)
    await asyncio.sleep(0.3)
    assert len(pair.acks) == 1, f"тик со старым скриптом отправил серверу {pair.acks[1:]}"
    pi.host.new_script()
    await linkclient.on_tick(a)
    assert await _until(lambda: len(pair.acks) == 2, timeout=5), "после обновления обвязки итог не ушёл"
    assert pair.acks[1][1]["ok"] is True and pair.acks[1][1]["hash"] == ack["hash"]
    assert pi.host.lists() == {"news.org": "vpn"} and pi.host.restarts() == 1
    assert _card(s, 1)["state"] == "applied"
    sets = _own_sets(pair, 1)
    for _ in range(3):
        await pair.srv.deliver_all()
        await linkclient.on_tick(a)
    await asyncio.sleep(0.3)
    assert _own_sets(pair, 1) == sets and len(pair.acks) == 2, "после применения канал не затих"
    assert c._writer is not None


@pytest.mark.parametrize("seeded", [False, True])
async def test_a_day_of_ticks_without_edits_sends_not_a_byte(pair, pi, seeded):
    """Простой — ноль пакетов от синхронизации: сутки тиков монитора (480 по
    три минуты) и тактов живости без правок — ни байта с малины, ни с ВПС.
    В том числе на свежем сервере, где канона ещё не было: поколение,
    созданное при первом чтении, обязано сохраниться, иначе каждый такт — новый
    отпечаток и новый канон."""
    s = pair.services
    if seeded:
        _seed(s, {"news.org": "vpn", "shop.ru": "ru"})
    a = pi.agent()
    c = await pi.up(a)
    assert await _until(lambda: _card(s, 1)["state"] == "applied", timeout=5), _card(s, 1)
    await pair.srv.deliver_all()
    await asyncio.sleep(0.3)
    before_agent, before_srv = c._sent_bytes, len(pair.sent)
    for _ in range(480):
        await linkclient.on_tick(a)
        await pair.srv.deliver_all()
    await asyncio.sleep(0.2)
    assert c._sent_bytes == before_agent, f"за сутки простоя агент отправил {c._sent_bytes - before_agent} байт"
    assert pair.sent[before_srv:] == [], f"сервер в простое говорил: {pair.sent[before_srv:]}"
    # правка мимо агента — ровно одно сообщение, и снова тишина
    ok, _ = a.lan_domains("add", ["example.com"])
    assert ok
    await linkclient.on_tick(a)
    assert await _until(lambda: "example.com" in _canon(s), timeout=3), "правка мимо агента не ушла на тике"
    await asyncio.sleep(0.3)
    after = len(pair.ev)
    for _ in range(5):
        await linkclient.on_tick(a)
        await pair.srv.deliver_all()
    await asyncio.sleep(0.2)
    assert len(pair.ev) == after, "правка ушла повторно"


async def test_an_old_server_skips_own_ev_and_the_session_lives_on(pair, pi, monkeypatch):
    """ВПС 3.0, агент новый: own_ev сервер не знает и пропускает; сессия канала
    не рвётся, правка остаётся в очереди, монитор агента ждёт ответа."""
    s = pair.services
    real = pair.srv._handle

    async def old_handle(gw, sess, msg):
        if msg.get("t") in ("own_ev", "own_ack"):
            msg = {**msg, "t": "незнакомое-3.0"}
        return await real(gw, sess, msg)
    monkeypatch.setattr(pair.srv, "_handle", old_handle)
    monkeypatch.setattr(s, "gwlink_own_for", None)             # старый сервер канона не шлёт
    a = pi.agent()
    c = await pi.up(a)
    writer = c._writer
    ok, _ = a.lan_domains("add", ["example.com"])
    assert ok
    a.db.set_state("gw_own_base", '{"gen": "g", "ver": 1, "hash": "", "items": {}}')
    await linkclient.own_changed(a)
    await asyncio.sleep(0.5)
    assert c._writer is writer and pair.srv.online(1), "незнакомый старому серверу вид порвал сессию"
    assert pair.ev == [] and _canon(s) == {}
    assert a.own_status()[0]["state"] == "pending"
