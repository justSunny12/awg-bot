"""Резерв на стороне агента шлюза (domain/gateway/reserve): раздел reserve
документа → reserve.json и линк awgrsv, решение о пересылке, взгляды на
основу и на резерв, гостевой канал (need → share, ping → pong), Т-РА3/Т-РА4,
молчание в простое; аплинк по умолчанию — не awgrsv.

Хост — `_Host`: скрипт линка к резерву (обёртка infra/gwguard/rsv) моделью
состояния — поднят ли линк, открыт ли набор rsv_open4 и для кого, счётчики;
счётчики линка слота; факт записи DNS у авторитетного сервера; строки юнита
обвязки. Модельные часы — аргумент `now` такта и подмена time в watch.

Цена ошибки: пересылка, открытая по одному слову резерва без факта записи,
— резерв пользуется шлюзами основы без переезда; закрытая по `idle` живой,
но не знающей о переезде основы — клиенты у резерва без РФ-доступа; доля
без `need` или при живой основе — у резерва ключ без аварии; проба шлюза,
ломающая гостевой канал, — шлюз выпадает из набора голосующих по одной
потерянной keepalive.
"""
from __future__ import annotations

import os
import stat
import types

import pytest

from awgbot.core import config
from awgbot.domain.gateway import GatewayServices
from awgbot.domain.gateway.reserve import record as recordmod
from awgbot.domain.gateway.reserve import uplink as upmod
from awgbot.domain.gateway.reserve import watch as watchmod
from awgbot.infra import gwguard
from awgbot.infra.db import Database
from awgbot.infra.gwguard import rsv as rsvlink
from awgbot.util import pairproto, wgkeys

pytestmark = pytest.mark.unit

SUBNET = "10.9.1.0/24"
RESERVE_IP = "198.51.100.7"
PRIMARY_IP = "203.0.113.10"
RECORD = "vpn-a.example.org"


class _Host:
    def __init__(self, monkeypatch, tmp_path):
        self.link = False
        self.open: list[str] = []
        self.rsv_rx: int | None = 100
        self.slot_rx = 100
        self.fact = False
        self.ups: list[str] = []
        self.removed = 0
        self.fact_asks = 0
        monkeypatch.setattr(rsvlink, "present", lambda: self.link)
        monkeypatch.setattr(rsvlink, "up", self._up)
        monkeypatch.setattr(rsvlink, "remove", self._remove)
        monkeypatch.setattr(rsvlink, "is_open", lambda: bool(self.open))
        monkeypatch.setattr(rsvlink, "counters", lambda: None if self.rsv_rx is None else (self.rsv_rx, 50))
        monkeypatch.setattr(recordmod, "points_to", self._points_to)
        monkeypatch.setattr(gwguard, "unit_env", lambda k: {"CLIENT_SUBNET": SUBNET, "ADMIN_IPS": "203.0.113.200"}.get(k, ""))

    def _up(self, conf, admin_ips=None):
        self.link = True
        self.ups.append(conf)
        return "up: ok"

    def _remove(self):
        self.link = False
        self.open = []
        self.removed += 1
        return "remove: ok"

    def _points_to(self, name, ip, now=None):
        self.fact_asks += 1
        return self.fact and name == RECORD and ip == RESERVE_IP

    def run(self, actions):
        """Действия пересылки — как их исполнил бы runtime/guestlink."""
        for a in actions or []:
            if a[0] == "open":
                self.open = list(a[1])
            elif a[0] == "close":
                self.open = []


def _conf() -> str:
    priv, _ = wgkeys.keypair()
    _, zone_pub = wgkeys.keypair()
    return (f"[Interface]\nAddress = 10.99.97.2/30\nPrivateKey = {priv}\nTable = off\nJc = 5\n\n"
            f"[Peer]\nPublicKey = {zone_pub}\nPresharedKey = {wgkeys.psk()}\nAllowedIPs = 10.99.97.1/32\n"
            f"Endpoint = {RESERVE_IP}:45001\nPersistentKeepalive = 25-35\n")


def _checked(conf: str, **over) -> dict:
    sec = {"conf_text": conf, "share": {"x": 2, "y": "cd" * 16, "gen": 1}, "state": "idle", "move": 0,
           "self": PRIMARY_IP, "name": "Beta", "slot": 1, "record": RECORD, "peer_host": RESERVE_IP}
    sec.update(over)
    return sec


@pytest.fixture()
def host(monkeypatch, tmp_path):
    return _Host(monkeypatch, tmp_path)


@pytest.fixture()
def gw(tmp_path, host, monkeypatch):
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    svc = GatewayServices(d)
    svc._RSV_STORE = tmp_path / "reserve.json"
    monkeypatch.setattr(svc, "link_status", lambda: (True, 10, host.slot_rx, 0))
    monkeypatch.setattr(svc, "_uplink_probe", lambda: False)
    svc.conf = _conf()
    svc.reserve_apply(_checked(svc.conf))
    return svc


def _decide(gw, host):
    acts = gw.reserve_forward_decide()
    host.run(acts)
    return acts


# ── раздел и файл ───────────────────────────────────────────────────────────

def test_the_section_lands_in_a_private_file_and_raises_the_link_once(gw, host, tmp_path):
    p = tmp_path / "reserve.json"
    assert p.exists() and stat.S_IMODE(os.stat(p).st_mode) == 0o600, "reserve.json с долей ключа не 0600"
    assert host.ups == [gw.conf], "линк к резерву не поднят конфигом раздела"
    res = gw.reserve_apply(_checked(gw.conf))
    assert host.ups == [gw.conf] and not res["changed"], "тот же раздел переподнял линк"
    assert gw.reserve_section()["share"]["y"] == "cd" * 16


def test_without_the_section_the_agent_removes_the_file_and_the_link(gw, host, tmp_path):
    res = gw.reserve_apply(None)
    assert res["changed"] and "снят" in res["note"]
    assert host.removed == 1 and not (tmp_path / "reserve.json").exists(), "без раздела файл с долей остался"
    assert gw.reserve_apply(None) == {"changed": False, "note": ""}, "повторное снятие что-то делает"


# ── пересылка ───────────────────────────────────────────────────────────────

def test_forwarding_is_closed_by_default(gw, host):
    assert not _decide(gw, host) and host.open == []


def test_serving_without_the_record_fact_keeps_forwarding_closed(gw, host):
    """Слова одного резерва мало: запись он без M не сменит, а без записи
    клиенты основы не у него."""
    host.run(gw.reserve_state_in("serving", 3))
    assert host.open == [], "пересылка открыта по serving без факта записи"
    assert host.fact_asks >= 1, "факт записи не спрашивали"


def test_serving_with_the_record_fact_opens_even_for_a_gateway_that_sees_the_primary(gw, host):
    assert gw.reserve_primary_seen()[0] != "down"
    host.fact = True
    host.run(gw.reserve_state_in("serving", 3))
    assert host.open == [SUBNET], "видящий шлюз с serving и фактом записи пересылку не открыл"


def test_the_primarys_away_opens_without_the_reserve(gw, host):
    gw.reserve_apply(_checked(gw.conf, state="away", move=4))
    host.run(gw.reserve_forward_decide())
    assert host.open == [SUBNET]


def test_idle_of_the_primary_newer_than_serving_closes(gw, host):
    host.fact = True
    host.run(gw.reserve_state_in("serving", 3))
    gw.reserve_apply(_checked(gw.conf, state="idle", move=3))
    host.run(gw.reserve_forward_decide())
    assert host.open == [], "idle основы после возврата (move ≥ serving) не закрыл пересылку"


def test_idle_of_the_primary_older_than_serving_does_not_close(gw, host):
    """Живая, но не знающая о переезде основа шлёт idle прежнего переезда —
    РФ-доступ клиентам у резерва не закрываем."""
    gw.reserve_apply(_checked(gw.conf, state="idle", move=2))
    host.fact = True
    host.run(gw.reserve_state_in("serving", 3))
    gw.reserve_apply(_checked(gw.conf, state="idle", move=2))
    host.run(gw.reserve_forward_decide())
    assert host.open == [SUBNET], "старое idle основы закрыло пересылку при serving резерва"


def test_idle_from_the_reserve_closes(gw, host):
    host.fact = True
    host.run(gw.reserve_state_in("serving", 3))
    host.run(gw.reserve_state_in("idle", 3))
    assert host.open == []


def test_after_a_reboot_the_decision_comes_back_from_reserve_json(gw, host, tmp_path, monkeypatch):
    host.fact = True
    host.run(gw.reserve_state_in("serving", 3))
    host.open = []                                      # перезагрузка: набор пуст до решения агента
    fresh = GatewayServices(gw.db)
    fresh._RSV_STORE = tmp_path / "reserve.json"
    host.run(fresh.reserve_forward_decide())
    assert host.open == [SUBNET], "после перезагрузки пересылка не восстановилась из reserve.json"


def test_unknown_state_words_are_ignored(gw, host):
    host.fact = True
    host.run(gw.reserve_state_in("relay", 9))
    host.run(gw.reserve_state_in("rm -rf", 9))
    assert host.open == [] and gw.reserve_section()["reserve_state"] == "idle"


# ── взгляд на основу ────────────────────────────────────────────────────────

def _watch(gw, t0: float, seconds: int, step: int = 5) -> list[tuple]:
    acts = []
    for t in range(0, seconds + 1, step):
        acts += gw.reserve_watch_tick(now=t0 + t)
    return acts


def test_silence_of_the_slot_link_probes_the_server_by_the_channel_at_40_seconds(gw, host):
    acts = _watch(gw, 1000.0, 35)
    assert not [a for a in acts if a[0] == "gw_ping"], "проба основы без подозрения"
    acts = gw.reserve_watch_tick(now=1040.0)
    assert [a for a in acts if a[0] == "gw_ping"], "40 с тишины линка слота — пробы нет"


def test_a_pong_on_the_channel_keeps_the_primary_seen_even_if_counters_are_unknown(gw, host, monkeypatch):
    """`awg show` не прочитался — это не «мёртв»: проба по каналу отвечает."""
    monkeypatch.setattr(gw, "link_status", lambda: (_ for _ in ()).throw(RuntimeError("awg show: unknown")))
    clock = types.SimpleNamespace(monotonic=lambda: clock.t)
    clock.t = 1000.0
    monkeypatch.setattr(watchmod, "time", clock)
    pings = []
    for t in range(1000, 1061, 5):
        clock.t = float(t)
        acts = gw.reserve_watch_tick(now=float(t))
        for a in acts:
            if a[0] == "gw_ping":
                pings.append(a[1])
                gw.reserve_gw_pong_in(a[1])
    assert pings, "при неизвестных счётчиках не было пробы"
    assert gw.reserve_primary_seen()[0] != "down", "неизвестные счётчики при ответе на пробу дали «не вижу»"


def test_no_answer_on_either_path_makes_unseen_and_tells_the_reserve(gw, host):
    acts = _watch(gw, 1000.0, 60)
    seen = [a for a in acts if a[0] == "guest" and a[1] == "seen"]
    assert seen and seen[-1][2]["seen"], f"«не вижу» не ушёл резерву: {acts}"
    assert gw.reserve_primary_seen()[0] == "down"


def test_an_answering_uplink_keeps_the_primary_seen(gw, host, monkeypatch):
    monkeypatch.setattr(gw, "_uplink_probe", lambda: True)
    acts = _watch(gw, 1000.0, 120)
    assert gw.reserve_primary_seen()[0] != "down", "клиентский путь ответил, а шлюз «не видит» основу"
    assert not [a for a in acts if a[0] == "guest" and a[1] == "seen"]


@pytest.fixture()
def uplink(gw, monkeypatch, tmp_path):
    """Аплинк awg0 с Endpoint по имени записи в конфиге; живой Endpoint и
    резолв — подменой; DNS-проба — журнал."""
    st = {"live": PRIMARY_IP, "dns": {RECORD: PRIMARY_IP}, "probes": []}
    monkeypatch.setattr(config, "GW_UPLINK_IF", "awg0")
    monkeypatch.setattr(upmod, "conf_endpoint", lambda iface: (RECORD, 443))
    monkeypatch.setattr(upmod, "resolve", lambda h: h if h[0].isdigit() else st["dns"].get(h, ""))
    monkeypatch.setattr(gwguard, "_endpoint_host", lambda iface: st["live"])

    def probe(resolver, iface, timeout=2.0):
        st["probes"].append((resolver, iface))
        return True
    monkeypatch.setattr(watchmod, "_dns_probe", probe)
    monkeypatch.delattr(gw, "_uplink_probe")
    return st


def test_the_uplink_probe_asks_the_servers_resolver_through_the_uplink(gw, uplink):
    assert gw._uplink_probe() is True
    assert uplink["probes"] == [("10.9.1.1", "awg0")], "проба не к резолверу сервера через аплинк"


def test_the_uplink_probe_does_not_count_when_the_uplink_points_at_the_reserve(gw, uplink):
    """После перерезолва на клон живой Endpoint аплинка — резерв: клон
    ответит на <подсеть>.1:53, и «вижу основу» было бы ложью."""
    gw.reserve_apply(_checked(gw.conf, **{"self": RECORD}))      # self_host не задан — адрес для клиентов
    uplink["dns"][RECORD] = RESERVE_IP                           # запись уже на резерве
    uplink["live"] = RESERVE_IP                                  # аплинк перерезолвлен на клон
    assert gw._uplink_probe() is False, "проба через аплинк, смотрящий на резерв, засчитана как жизнь основы"
    assert not uplink["probes"]


# ── резерв по гостевому каналу ──────────────────────────────────────────────

def test_need_gives_the_share_only_while_the_primary_is_unseen(gw, host):
    assert gw.reserve_guest_in("need", {"case": "1", "since": "x"}) == [], "доля при живой для шлюза основе"
    _watch(gw, 1000.0, 60)
    assert gw.reserve_primary_seen()[0] == "down"
    acts = gw.reserve_guest_in("need", {"case": "1", "since": "x"})
    assert acts == [("guest", "share", {"share": "cd" * 16, "m_gen": 1})], acts
    pairproto.check(pairproto.GUEST_PROTO, "share", acts[0][2], pairproto.GATEWAY)


def test_without_a_share_need_gives_nothing(gw, host):
    gw.reserve_apply(_checked(gw.conf, share={}))
    _watch(gw, 1000.0, 60)
    assert gw.reserve_guest_in("need", {"case": "1", "since": "x"}) == []


def test_without_need_the_gateway_never_offers_its_share(gw, host):
    acts = _watch(gw, 1000.0, 3600)
    assert not [a for a in acts if a[0] == "guest" and a[1] == "share"], "доля ушла без need"


def test_ping_from_the_reserve_gets_pong_with_the_same_nonce(gw):
    assert gw.reserve_guest_in("ping", {"n": "0123456789abcdef"}) == [("guest", "pong", {"n": "0123456789abcdef"})]


def test_every_word_the_gateway_puts_on_the_guest_channel_is_allowed_from_the_gateway(gw, host):
    """Проба резерва по подозрению (awgrsv молчит) — тоже слово гостевого
    канала: слушатель резерва проверяет каждое сообщение шлюза по словарю, и
    вид «не с той стороны» рвёт сессию — шлюз выпадает из набора голосующих."""
    acts = _watch(gw, 1000.0, 60) + [("guest", "hello", gw.reserve_guest_hello_body() | {"nonce": "x"})]
    words = [a for a in acts if a[0] == "guest"]
    assert any(w[1] == "ping" for w in words), "проба резерва не ушла (тест не проверил бы ничего)"
    for _, kind, body in words:
        try:
            pairproto.check(pairproto.GUEST_PROTO, kind, body, pairproto.GATEWAY)
        except pairproto.ProtocolError as e:
            pytest.fail(f"шлюз шлёт «{kind}», которого слушатель резерва от шлюза не примет: {e}")


def test_the_guest_channel_is_silent_for_an_hour_of_idle(gw, host):
    acts = []
    for i, t in enumerate(range(0, 3600, 5)):
        if i % 6 == 0:
            host.slot_rx += 148
            host.rsv_rx += 148
        acts += gw.reserve_watch_tick(now=5000.0 + t)
    assert not acts, f"в простое такт агента что-то делал: {acts[:5]}"


def test_hello_carries_the_share_generation_and_the_view(gw, host):
    body = gw.reserve_guest_hello_body()
    assert body == {"slot": 1, "caps": ["rsv1"], "m_gen": 1, "seen": ""}
    _watch(gw, 1000.0, 60)
    assert gw.reserve_guest_hello_body()["seen"], "hello после «не вижу» несёт «вижу»"


# ── Т-РА3 / Т-РА4 ───────────────────────────────────────────────────────────

def test_case_3_view_tells_the_gateway_admin_once(gw, host):
    _watch(gw, 1000.0, 60)
    view = {"seen": "", "voters": [{"slot": 1, "see": False}], "in": "idle", "pkg_gen": 3}
    first = gw.reserve_guest_in("view", view)
    told = [n for a in first if a[0] == "notify" for n in a[1]]
    assert len(told) == 1 and "недоступен из РФ" in told[0].text and told[0].silent
    assert not [a for a in gw.reserve_guest_in("view", view) if a[0] == "notify"], "Т-РА4 повторилось"


def test_opening_by_serving_tells_the_gateway_admin_that_clients_moved(gw, host):
    _watch(gw, 1000.0, 60)
    host.fact = True
    gw.reserve_guest_in("view", {"seen": "2026-10-21T03:10:00+03:00", "voters": [], "in": "taking", "pkg_gen": 3})
    acts = gw.reserve_guest_in("state", {"state": "serving", "move": 3})
    host.run(acts)
    told = [n for a in acts if a[0] == "notify" for n in a[1]]
    assert host.open == [SUBNET] and len(told) == 1 and "переехали на резерв" in told[0].text
    assert told[0].force_sound, "Т-РА3 «сервер не отвечает» без звука"
    assert ("push",) in acts, "серверу не ушёл снимок с новым состоянием"


# ── аплинк по умолчанию ─────────────────────────────────────────────────────

def test_the_default_uplink_is_never_the_reserve_link(monkeypatch):
    """Шлюз с линком к резерву и одним клиентским туннелем: аплинк — туннель;
    без него — пусто, а не awgrsv."""
    monkeypatch.setattr(config, "GW_UPLINK_IF", "")
    monkeypatch.setattr(gwguard, "script_status", lambda: {})
    ifaces = {"v": "awglink awgrsv awg0"}
    monkeypatch.setattr(gwguard, "_awg", lambda args: ifaces["v"] if args == ["show", "interfaces"] else "")
    monkeypatch.setattr(gwguard, "_endpoint_host", lambda iface: RESERVE_IP if iface == "awgrsv" else "")
    assert gwguard.uplink_interface() == "awg0"
    ifaces["v"] = "awglink awgrsv"
    assert gwguard.uplink_interface() == "", "аплинком выбран линк к резерву"
