"""
Сервисы соседних сетей по каналу линка (концепт «сервисы соседних сетей»
§2.3–§3.3, §8, §11): `svc` от шлюза-источника, чистка и хранение на ВПС,
`peer_svc` шлюзу-получателю по пересечению «выдано по доступу между
подсетями» ∩ «применено на шлюзе», `peer_svc_ack` назад.

Сокеты настоящие (петля), сервер — боевой LinkServer поверх временной БД; два
слота на одном слушателе, коннекты раздаются слотам по очереди (как в
test_gwlink_lists_role_diag). В сквозных сценариях на той стороне — боевой
LinkClient и настоящий `GatewayServices`, а у получателя файл записей ставит
НАСТОЯЩИЙ помощник awg-lan-services.sh из скрипта обвязки (dnsmasq, systemctl,
id подменены в PATH).

Цена ошибки: записи, которые ездят по кругу, — рестарт dnsmasq соседней сети
на каждом такте (кэш DNS всей квартиры); записи, которые не снимаются, —
серверы соседа в Finder после выключения доступа; запись на адрес вне
маршрута — Finder ведёт в никуда; незнакомый вид, рвущий сессию, — канал в
петле переподключений у всех, кто не обновился.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import os
import types
from pathlib import Path

import pytest

from awgbot.core import config
from awgbot.domain import gwservices, gwsnapshot
from awgbot.domain.gateway import GatewayServices
from awgbot.infra import gwguard
from awgbot.infra.db import Database
from awgbot.runtime import linkclient, linkserver
from awgbot.util import gwlink, timeutil
from tests.integration.test_gwlink_channel import _Gw, _free_port, _until

pytestmark = pytest.mark.integration

ADMIN = config.ADMIN_ID
PRIV = base64.b64encode(os.urandom(32)).decode()
KEY = gwlink.channel_key(PRIV)
X_NETS, Y_NETS = "192.168.1.0/24", "192.168.68.0/24"      # слот 1 — источник, слот 2 — получатель
NAS = {"t": "_smb._tcp", "n": "NASPi5", "h": "naspi5", "p": 445, "a": "192.168.1.10"}
BACKUP = {"t": "_smb._tcp", "n": "backup", "h": "backup", "p": 445, "a": "192.168.1.20"}
H_NAS = gwservices.feed_hash([NAS])
SCRIPT = Path(__file__).resolve().parents[2] / "install" / "routing-gw-setup.sh"


def _snap(home: str, peers: str, version: str = "3.1.0", rev: int = 1) -> dict:
    return {"bundle": {"lan_mode": "1", "home_subnets": home, "resolver": "10.9.1.1",
                       "peer_home_nets": peers, "admin_ips": "10.8.1.2"},
            "link_contract": "1", "plumbing_gen": "new", "mark_status": "confirmed",
            "agent_version": version, "awg_generation": 1, "egress_ok": True,
            "boot_id": "b" * 36, "ts": "2026-09-24T20:00:00+03:00", "rev": rev}


@pytest.fixture()
async def pair(services, make_active_client, monkeypatch):
    """Два слота в режиме без VPN с доступом между подсетями; тумблер —
    словарь `flag`. Слушатель один, слот коннекту — из очереди `queue`."""
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    pi2 = services.add_device(admin.id, "Pi2")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    services.gateway_set_home_subnets(1, X_NETS)
    services.gateway_set_home_subnets(2, Y_NETS)
    services.db.gateway_update(1, lan_mode=1)
    services.db.gateway_update(2, lan_mode=1)
    flag = {"on": True}
    monkeypatch.setattr(services, "peer_nets_enabled", lambda: flag["on"])
    monkeypatch.setattr(services, "gateway_resolver_addr", lambda g: "10.9.1.1")
    monkeypatch.setattr(services, "_link_privkey", lambda g=None: PRIV)
    # доставка настроек — не предмет этих тестов: снимки фикстур не совпадают
    # с выдаваемым, и сервер слал бы settings в каждую сессию
    monkeypatch.setattr(services, "gwlink_settings_due", lambda gw: None)
    assert services.gateway_peer_nets(2) == [X_NETS] and services.gateway_peer_nets(1) == [Y_NETS]
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
    p = types.SimpleNamespace(srv=srv, queue=queue, flag=flag, services=services, sent=sent, port=port)
    try:
        yield p
    finally:
        await srv.stop()


async def _hello(p, slot: int, svc_hash: str = "", agent: str = "3.1.0") -> _Gw:
    p.queue.append(slot)
    host, port = p.srv._bound[0]
    gw = _Gw(*await asyncio.open_connection(host, port, limit=8 * gwlink.MAX_LINE), key=KEY)
    await gw.send("hello", {"proto": gwlink.PROTO, "agent": agent, "lists_hash": "", "svc_hash": svc_hash})
    assert await _until(lambda: p.services.gwlink_session(slot)), "hello не принят"
    return gw


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


def _x_known(services, items=(NAS,), seen_hours: float = 0.0) -> None:
    """Сервисы слота 1 уже на ВПС; слот 1 был на связи `seen_hours` назад."""
    assert services.gwlink_services_in(1, list(items)) is True
    at = timeutil.now() - datetime.timedelta(hours=seen_hours)
    services.db.set_state(services._gwlink_key(services._GWLINK_SEEN_KEY, 1), timeutil.to_iso(at))


def _y_snap(services, peers: str = X_NETS, version: str = "3.1.0") -> None:
    """Снимок слота 2 с прошлой сессии: что стоит у него в юните."""
    assert services.gwlink_snapshot_in(2, _snap(Y_NETS, peers, version), 1, True)


# ── доставка ─────────────────────────────────────────────────────────────────

async def test_services_of_one_gateway_reach_the_neighbour_exactly_once(pair):
    """Главный путь: X назвал свой SMB-сервер — Y получает его одним `peer_svc`
    сразу, как только снимок показал, что подсети X у него применены. Тот же
    список от X повторно и такты живости не шлют ничего: каждый лишний
    `peer_svc` на малине — рестарт dnsmasq."""
    s = pair.services
    x = await _hello(pair, 1)
    await x.send("svc", {"items": [NAS]})
    assert await _until(lambda: s.gwlink_services(1)) == [NAS]
    y = await _hello(pair, 2)
    assert await _next(y, "peer_svc", 0.4) is None, "без снимка получателя записи ушли вслепую"
    await y.send("snap", _snap(Y_NETS, X_NETS))
    msg = await _next(y, "peer_svc")
    assert msg is not None, "сервисы соседа не дошли"
    assert msg["items"] == [NAS] and msg["hash"] == H_NAS
    await x.send("svc", {"items": [NAS]})
    for _ in range(3):
        await pair.srv.deliver_all()
    assert await _next(y, "peer_svc", 0.4) is None, "тот же список уехал в сессии второй раз"
    assert await _next(x, "peer_svc", 0.3) is None, "источнику вернули его же сервисы"
    # новый сервер у X — к Y сразу, без такта живости
    await x.send("svc", {"items": [NAS, BACKUP]})
    msg = await _next(y, "peer_svc")
    assert msg is not None and [r["a"] for r in msg["items"]] == ["192.168.1.10", "192.168.1.20"]
    assert [k for sl, k in pair.sent if sl == 2 and k == "peer_svc"] == ["peer_svc", "peer_svc"]
    await x.close(); await y.close()


@pytest.mark.parametrize("have,expect", [(H_NAS, False), ("", True), ("0" * 64, True)])
async def test_the_hash_named_in_hello_decides_whether_anything_goes(pair, have, expect):
    """Переподключение после моргнувшего линка: у шлюза уже стоят те же
    записи — сервер молчит. Другие или никаких — везёт."""
    s = pair.services
    _x_known(s)
    _y_snap(s)
    y = await _hello(pair, 2, svc_hash=have)
    await pair.srv.deliver_all()
    msg = await _next(y, "peer_svc", 0.6)
    assert (msg is not None) is expect, f"svc_hash={have!r}: доставка {msg}"
    if msg is not None:
        assert msg["hash"] == H_NAS
    await y.close()


async def test_peer_access_turned_off_sends_an_empty_list_at_once(pair):
    """Выключили доступ между подсетями на ВПС — записи у получателя
    снимаются сразу каналом, не дожидаясь перевыпуска конфигурации."""
    s = pair.services
    _x_known(s)
    _y_snap(s)
    y = await _hello(pair, 2)
    assert (await _next(y, "peer_svc"))["hash"] == H_NAS
    await y.send("peer_svc_ack", {"ok": True, "hash": H_NAS, "n": 1, "error": ""})
    await _until(lambda: s.gwlink_peer_services_ack(2))
    pair.flag["on"] = False
    await pair.srv.deliver_all()
    msg = await _next(y, "peer_svc")
    assert msg is not None, "выключение доступа не сняло записи у получателя"
    assert msg["hash"] == "" and msg["items"] == []
    await pair.srv.deliver_all()
    assert await _next(y, "peer_svc", 0.4) is None, "пустой список уехал второй раз"
    await y.close()


async def test_nothing_goes_until_the_subnets_are_applied_then_at_once_on_the_delta(pair):
    """До перевыпуска у Y нет маршрута к подсети X — записи вели бы в никуда.
    Дельта снимка «подсети соседей применены» — и записи уходят сразу."""
    s = pair.services
    _x_known(s)
    y = await _hello(pair, 2)
    await y.send("snap", _snap(Y_NETS, ""))
    await pair.srv.deliver_all()
    assert await _next(y, "peer_svc", 0.5) is None, "записи ушли шлюзу без маршрута к соседу"
    assert s.gwlink_services_card(s.db.gateway(2))["state"] == "reissue"
    delta = {"bundle": _snap(Y_NETS, X_NETS)["bundle"], "rev": 2}
    await y.send("delta", delta)
    msg = await _next(y, "peer_svc")
    assert msg is not None and msg["items"] == [NAS], "после дельты снимка записи не ушли"
    await y.close()


async def test_overlapping_subnets_share_nothing(pair):
    """Подсети пересеклись — доступа между ними нет, и сервисов тоже."""
    s = pair.services
    _x_known(s)
    s.gateway_set_home_subnets(2, "192.168.0.0/16")
    assert s.gateway_peer_nets(2) == []
    _y_snap(s)
    y = await _hello(pair, 2)
    await pair.srv.deliver_all()
    assert await _next(y, "peer_svc", 0.5) is None
    assert s.gwlink_peer_services_for(s.db.gateway(2)) == ("", [])
    await y.close()


@pytest.mark.parametrize("hours,served", [(23, True), (25, False)])
async def test_a_neighbour_silent_for_over_a_day_is_not_served(pair, hours, served):
    """Сосед молчит больше суток — его сервисы у получателей снимаются;
    меньше — нет: перезапуск бота ВПС рвёт все сессии на секунды и не должен
    давать двух рестартов dnsmasq (§12.5)."""
    s = pair.services
    _x_known(s, seen_hours=hours)
    _y_snap(s)
    y = await _hello(pair, 2, svc_hash=H_NAS)             # записи X у Y стоят
    await pair.srv.deliver_all()
    msg = await _next(y, "peer_svc", 0.6)
    if served:
        assert msg is None, "сосед на связи сутки назад — а его записи сняли"
    else:
        assert msg is not None and msg["items"] == [] and msg["hash"] == "", (
            f"сервисы соседа, молчащего {hours} ч, всё ещё раздаются: {msg}")
    await y.close()


async def test_the_server_drops_what_is_not_in_the_senders_subnet(pair):
    """С малины приходят недоверенные данные: адрес вне подсети слота (из БД,
    не из снимка), грязное имя, чужой тип — выброшены на ВПС и никуда не едут."""
    s = pair.services
    _y_snap(s)
    y = await _hello(pair, 2)
    x = await _hello(pair, 1)
    bad = [{**NAS, "a": "192.168.68.5"}, {**NAS, "n": 'x",server=/a/1.1.1.1', "a": "192.168.1.11"},
           {**NAS, "t": "_ssh._tcp", "a": "192.168.1.12"}, "мусор"]
    await x.send("svc", {"items": bad + [NAS]})
    assert await _until(lambda: s.gwlink_services(1)) == [NAS]
    msg = await _next(y, "peer_svc")
    assert msg is not None and msg["items"] == [NAS], msg
    await x.close(); await y.close()


async def test_a_failed_apply_is_not_resent_in_the_session_but_is_in_the_next(pair):
    """Шлюз не принял записи — повтор в той же сессии был бы тем же отказом
    по кругу; новая сессия — новая попытка. Карточка показывает ошибку."""
    s = pair.services
    _x_known(s)
    _y_snap(s)
    y = await _hello(pair, 2)
    assert await _next(y, "peer_svc") is not None
    await y.send("peer_svc_ack", {"ok": False, "hash": H_NAS, "n": 0, "error": "dnsmasq отверг записи"})
    await _until(lambda: s.gwlink_peer_services_ack(2))
    card = s.gwlink_services_card(s.db.gateway(2))
    assert card["state"] == "failed" and card["error"] == "dnsmasq отверг записи" and card["peer"] == 1
    await pair.srv.deliver_all()
    assert await _next(y, "peer_svc", 0.4) is None, "отвергнутые записи ушли в сессии снова"
    await y.close()
    await _until(lambda: not s.gwlink_session(2))
    y2 = await _hello(pair, 2)
    assert await _next(y2, "peer_svc") is not None, "новая сессия не получила записей"
    await y2.send("peer_svc_ack", {"ok": True, "hash": H_NAS, "n": 1, "error": ""})
    await _until(lambda: s.gwlink_peer_services_ack(2).get("ok"))
    assert s.gwlink_services_card(s.db.gateway(2))["state"] == "applied"
    await y2.close()


# ── совместимость ────────────────────────────────────────────────────────────

async def test_an_old_agent_ignores_peer_svc_and_keeps_its_session(pair):
    """ВПС 3.1, агент 3.0: `peer_svc` он пропускает и не отвечает. Сессия
    жива, повторов нет, карточка говорит «обнови агента»."""
    s = pair.services
    _x_known(s)
    _y_snap(s, version="3.0.2")
    y = await _hello(pair, 2, agent="3.0.2")
    assert await _next(y, "peer_svc") is not None
    for _ in range(3):
        await pair.srv.deliver_all()
    assert await _next(y, "peer_svc", 0.4) is None, "молчащему старому агенту записи шлются тактом"
    assert pair.srv.online(2) and s.gwlink_session(2)
    assert s.gwlink_services_card(s.db.gateway(2))["state"] == "old_agent"
    await y.close()


# ── снятие слота ─────────────────────────────────────────────────────────────

def test_forgetting_a_slot_clears_its_three_service_keys(pair):
    s = pair.services
    _x_known(s)
    s.gwlink_peer_services_ack_in(1, {"ok": True, "hash": H_NAS, "n": 1})
    keys = [s._gwlink_key(k, 1) for k in (s._GWLINK_SVC_KEY, s._GWLINK_SVC_AT_KEY, s._GWLINK_PEER_SVC_KEY)]
    assert all(s.db.get_state(k) for k in keys)
    s.gwlink_forget(1)
    assert [s.db.get_state(k) or "" for k in keys] == ["", "", ""], "ключи сервисов пережили снятие слота"
    assert s.gwlink_services(1) == [] and s.gwlink_peer_services_ack(1) == {}


# ── оба конца: настоящий агент ───────────────────────────────────────────────

class _Pi(GatewayServices):
    def gw_snapshot(self) -> dict:
        return gwsnapshot.collect(mark_status="confirmed", egress_ok=True, guard_info=None, peer_nets=None)

    def gateway_claim_if_needed(self):
        return None


class _Host:
    """Малина под агентом: юнит обвязки (`env`), avahi-browse (`browse`) и
    настоящий помощник awg-lan-services.sh с подменёнными dnsmasq/systemctl/id."""

    def __init__(self, tmp_path, monkeypatch, env: dict):
        self.env = env
        self.browse: str | None = ""
        self.dns_d = tmp_path / "dnsmasq.d"; self.dns_d.mkdir()
        self.conf = self.dns_d / gwservices.CONF_NAME
        self.log = tmp_path / "host.log"; self.log.write_text("", encoding="utf-8")
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        for name, body in (("systemctl", f'echo "systemctl $*" >> {self.log}\n'),
                           ("dnsmasq", f'echo "dnsmasq $*" >> {self.log}\n'),
                           ("id", "echo 0\n")):
            (bin_dir / name).write_text("#!/bin/sh\n" + body, encoding="utf-8")
            (bin_dir / name).chmod(0o755)
        script = SCRIPT.read_text(encoding="utf-8")
        helper = tmp_path / "awg-lan-services.sh"
        helper.write_text(script.split("cat > \"$LAN_SERVICES\" <<'SVCEOF'\n", 1)[1]
                          .split("\nSVCEOF\n", 1)[0] + "\n", encoding="utf-8")
        helper.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
        monkeypatch.setenv("AWG_DNSMASQ_D", str(self.dns_d))
        monkeypatch.setenv("AWG_LAN_DUMP", str(tmp_path / "dump"))
        monkeypatch.setattr(gwguard, "unit_env", lambda k: self.env.get(k, ""))
        monkeypatch.setattr(gwguard, "lan_mode", lambda: self.env.get("LAN_MODE") == "1")
        monkeypatch.setattr(gwguard, "avahi_browse", lambda timeout=15: self.browse)
        monkeypatch.setattr(gwguard, "LAN_SERVICES_SCRIPT", str(helper))
        monkeypatch.setattr(gwguard, "PEER_SERVICES_CONF", str(self.conf))
        monkeypatch.setattr(gwguard, "PEER_SERVICES_NEW", str(tmp_path / "lib" / "peer-services.conf.new"))

    def restarts(self) -> int:
        return self.log.read_text().count("systemctl restart dnsmasq")


@pytest.fixture()
async def real_agent(pair, tmp_path, monkeypatch):
    """Фабрика: настоящий агент слота `slot` с юнитом `env` и клиентом канала."""
    made: list = []
    conf = tmp_path / "awglink.conf"
    conf.write_text(f"# awg-bot: контракт линка 1\n[Interface]\nPrivateKey = {PRIV}\n", encoding="utf-8")
    monkeypatch.setattr(config, "GW_LINK_CONF", str(conf))
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.1.0")
    monkeypatch.setattr(linkclient, "server_address", lambda: "127.0.0.1")
    monkeypatch.setattr(linkclient, "server_port", lambda: pair.port)
    monkeypatch.setattr(linkclient, "_notify", None)

    def make(env: dict):
        host = _Host(tmp_path, monkeypatch, env)
        db = Database(str(tmp_path / "pi.db")); db.init_schema()
        agent = _Pi(db)
        client = linkclient.LinkClient(agent)
        monkeypatch.setattr(linkclient, "_client", client)
        made.append((client, db))
        return host, agent, client
    try:
        yield make
    finally:
        for client, db in made:
            await client.stop()
            db.close()


async def _up(pair, client, slot: int) -> None:
    pair.queue.append(slot)
    client.start()
    assert await _until(lambda: pair.services.gwlink_snapshot(slot) and client._writer is not None, timeout=5)


async def test_the_receiver_publishes_neighbour_services_through_the_real_helper(pair, real_agent, monkeypatch):
    """Вся польза функции на получателе: сервисы соседа доехали каналом,
    агент собрал файл, настоящий помощник проверил его белым списком и
    перезапустил dnsmasq один раз, сервер видит «опубликованы». Повтор тех же
    записей dnsmasq не трогает."""
    s = pair.services
    acks: list[dict] = []
    real_ack = s.gwlink_peer_services_ack_in
    monkeypatch.setattr(s, "gwlink_peer_services_ack_in", lambda slot, body: acks.append(body) or real_ack(slot, body))
    _x_known(s)
    host, agent, client = real_agent({"LAN_MODE": "1", "HOME_SUBNETS": Y_NETS, "PEER_HOME_NETS": X_NETS,
                                      "LINK_CHANNEL": "1"})
    await _up(pair, client, 2)
    assert await _until(lambda: s.gwlink_peer_services_ack(2).get("ok"), timeout=5), s.gwlink_peer_services_ack(2)
    ack = s.gwlink_peer_services_ack(2)
    assert ack["hash"] == H_NAS and ack["n"] == 1
    text = host.conf.read_text(encoding="utf-8")
    assert "host-record=naspi5.awg.internal,192.168.1.10" in text
    assert "ptr-record=lb._dns-sd._udp.0.68.168.192.in-addr.arpa,awg.internal" in text, \
        "обратная зона — своей подсети получателя, иначе Mac не найдёт домен обзора"
    assert host.restarts() == 1
    assert agent.services_applied_hash() == H_NAS
    assert s.gwlink_services_card(s.db.gateway(2))["state"] == "applied"
    # тот же список ещё раз (скажем, после переподключения без hello-хэша) — без рестарта
    assert len(acks) == 1
    assert await pair.srv.send(2, "peer_svc", {"hash": H_NAS, "items": [NAS]}, pad=gwlink.PAD_SNAP)
    assert await _until(lambda: len(acks) == 2, timeout=5), "на повтор агент не ответил"
    assert acks[-1]["ok"] is True and host.restarts() == 1, "те же записи перезапустили dnsmasq соседней сети"
    # пустой список — файл снят, один рестарт
    assert await pair.srv.send(2, "peer_svc", {"hash": "", "items": []}, pad=gwlink.PAD_SNAP)
    assert await _until(lambda: not host.conf.exists(), timeout=5), "пустой список не снял записи"
    await _until(lambda: s.gwlink_peer_services_ack(2).get("hash") == "")
    assert host.restarts() == 2
    assert s.gwlink_peer_services_ack(2)["ok"] is True and s.gwlink_peer_services_ack(2)["n"] == 0


async def test_the_receiver_drops_addresses_outside_its_own_peer_subnets(pair, real_agent):
    """Скомпрометированный ВПС шлёт записи на адреса вне подсетей соседей из
    ЮНИТА получателя (в свою сеть, в интернет) и с грязным именем — агент
    выбрасывает их до сборки файла, остальное публикует."""
    s = pair.services
    host, agent, client = real_agent({"LAN_MODE": "1", "HOME_SUBNETS": Y_NETS, "PEER_HOME_NETS": X_NETS,
                                      "LINK_CHANNEL": "1"})
    await _up(pair, client, 2)
    forged = [NAS, {**NAS, "a": "192.168.68.7", "h": "own"}, {**NAS, "a": "203.0.113.9", "h": "net"},
              {**NAS, "n": 'x"\nserver=/awg.internal/203.0.113.9', "a": "192.168.1.30"}]
    digest = "f" * 64
    assert await pair.srv.send(2, "peer_svc", {"hash": digest, "items": forged}, pad=gwlink.PAD_SNAP)
    assert await _until(lambda: s.gwlink_peer_services_ack(2).get("hash") == digest, timeout=5)
    ack = s.gwlink_peer_services_ack(2)
    assert ack["ok"] is True and ack["n"] == 1, ack
    text = host.conf.read_text(encoding="utf-8")
    assert "192.168.1.10" in text
    for leak in ("192.168.68.7", "203.0.113.9", "192.168.1.30", "server="):
        assert leak not in text, f"в конфиг dnsmasq малины попало «{leak}»"


async def test_the_source_sends_on_connect_and_a_day_of_unchanged_scans_is_silent(pair, real_agent):
    """Источник: свой список уходит при подключении, изменение — сразу. Сутки
    обзоров без изменений (96 по 15 минут) и тактов живости — ни байта в канал
    ни с малины, ни с ВПС: периодический обмен внутри туннеля — маячок."""
    s = pair.services
    _y_snap(s)
    y = await _hello(pair, 2)
    host, agent, client = real_agent({"LAN_MODE": "1", "HOME_SUBNETS": X_NETS, "PEER_HOME_NETS": Y_NETS,
                                      "LINK_CHANNEL": "1"})
    host.browse = "=;end0;IPv4;NASPi5;_smb._tcp;local;NASPi5.local;192.168.1.10;445;"
    assert agent.services_scan() is True
    await _up(pair, client, 1)
    msg = await _next(y, "peer_svc")
    assert msg is not None and msg["items"] == [NAS], f"список источника не доехал до соседа: {msg}"
    await pair.srv.deliver_all()
    await asyncio.sleep(0.3)
    before_agent, before_srv = client._sent_bytes, len(pair.sent)
    for _ in range(96):
        await linkclient.services_changed(agent)
        await pair.srv.deliver_all()
    await asyncio.sleep(0.2)
    assert client._sent_bytes == before_agent, (
        f"за сутки обзоров без изменений агент отправил {client._sent_bytes - before_agent} байт")
    assert pair.sent[before_srv:] == [], f"сервер в простое говорил: {pair.sent[before_srv:]}"
    assert await y.silent(0.3)
    # изменение — ровно одно сообщение соседу
    host.browse += "\n=;end0;IPv4;backup;_smb._tcp;local;backup.local;192.168.1.20;445;"
    await linkclient.services_changed(agent)
    msg = await _next(y, "peer_svc")
    assert msg is not None and len(msg["items"]) == 2
    await pair.srv.deliver_all()
    assert await _next(y, "peer_svc", 0.4) is None
    await y.close()


async def test_an_old_server_skips_svc_and_the_session_lives_on(pair, real_agent, monkeypatch):
    """ВПС 3.0, агент 3.1: `svc` и `peer_svc_ack` сервер не знает и
    пропускает; сессия канала от этого не рвётся, снимок доходит."""
    s = pair.services
    real = pair.srv._handle

    async def old_handle(gw, sess, msg):
        if msg.get("t") in ("svc", "peer_svc_ack"):
            msg = {**msg, "t": "незнакомое-3.0"}
        return await real(gw, sess, msg)
    monkeypatch.setattr(pair.srv, "_handle", old_handle)
    host, agent, client = real_agent({"LAN_MODE": "1", "HOME_SUBNETS": X_NETS, "PEER_HOME_NETS": Y_NETS,
                                      "LINK_CHANNEL": "1"})
    host.browse = "=;end0;IPv4;NASPi5;_smb._tcp;local;NASPi5.local;192.168.1.10;445;"
    agent.services_scan()
    await _up(pair, client, 1)
    writer = client._writer
    host.browse += "\n=;end0;IPv4;backup;_smb._tcp;local;backup.local;192.168.1.20;445;"
    await linkclient.services_changed(agent)
    await asyncio.sleep(0.5)
    assert client._writer is writer and pair.srv.online(1), "незнакомый старому серверу вид порвал сессию"
    assert s.gwlink_services(1) == [], "старый сервер не должен был ничего сохранить"
    assert s.gwlink_snapshot(1), "снимок не дошёл"

