"""Резерв на стороне агента шлюза (domain/gateway/reserve): раздел reserve
документа → reserve.json и линк awgrsv, решение о пересылке, взгляд на
резерв, гостевой канал (`hello{slot, caps}`, ping → pong; слов матрицы
`need`/`view` агент не понимает), Т-РА3 по своему взгляду зонда кворума,
молчание в простое; аплинк по умолчанию — не awgrsv.

Хост — `_Host`: скрипт линка к резерву (обёртка infra/gwguard/rsv) моделью
состояния — поднят ли линк, открыт ли набор rsv_open4 и для кого, счётчики;
счётчики линка слота; факт записи DNS у авторитетного сервера; строки юнита
обвязки; взгляд зонда на сервер AWG — файл quorum.json во временном каталоге.
Модельные часы — аргумент `now` такта.

Цена ошибки: пересылка, открытая по одному слову резерва без факта записи,
— резерв пользуется шлюзами основы без переезда; закрытая по `idle` живой,
но не знающей о переезде основы — пользователи у резерва без РФ-доступа; доля
по слову `need` прежнего резерва — у резерва ключ без решения большинства;
проба шлюза, ломающая гостевой канал, — шлюз выпадает из голосующих по одной
потерянной keepalive; Т-РА3 «сервер не отвечает» со звуком шлюзу, которому
сервер отвечает, — ложная тревога ночью.
"""
from __future__ import annotations

import os
import stat

import pytest

from awgbot.core import config
from awgbot.domain.gateway import GatewayServices
from awgbot.domain.gateway.reserve import record as recordmod
from awgbot.infra import gwguard
from awgbot.infra.db import Database
from awgbot.infra.gwguard import rsv as rsvlink
from awgbot.util import pairproto, wgkeys

pytestmark = pytest.mark.unit

SUBNET = "10.9.1.0/24"
OPEN = ["10.99.97.1/32", SUBNET]          # с awgrsv: адрес зоны (зонды бота основы) и подсеть пользователей
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
            f"[Peer]\nPublicKey = {zone_pub}\nPresharedKey = {wgkeys.psk()}\nAllowedIPs = 10.99.97.1/32, {SUBNET}\n"
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
    svc._QUO_STORE = tmp_path / "quorum.json"
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
    assert p.exists() and stat.S_IMODE(os.stat(p).st_mode) == 0o600, "reserve.json не 0600"
    assert host.ups == [gw.conf], "линк к резерву не поднят конфигом раздела"
    res = gw.reserve_apply(_checked(gw.conf))
    assert host.ups == [gw.conf] and not res["changed"], "тот же раздел переподнял линк"
    assert "share" not in gw.reserve_section(), "доля прямой в разделе reserve — её больше нет (точка — в quorum)"


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
    пользователи основы не у него."""
    host.run(gw.reserve_state_in("serving", 3))
    assert host.open == [], "пересылка открыта по serving без факта записи"
    assert host.fact_asks >= 1, "факт записи не спрашивали"


def test_serving_with_the_record_fact_opens_even_for_a_gateway_that_sees_the_primary(gw, host):
    _own_view(gw, "see")
    host.fact = True
    host.run(gw.reserve_state_in("serving", 3))
    assert host.open == OPEN, "видящий шлюз с serving и фактом записи пересылку не открыл"


def test_the_primarys_away_opens_without_the_reserve(gw, host):
    gw.reserve_apply(_checked(gw.conf, state="away", move=4))
    host.run(gw.reserve_forward_decide())
    assert host.open == OPEN


def test_idle_of_the_primary_newer_than_serving_closes(gw, host):
    host.fact = True
    host.run(gw.reserve_state_in("serving", 3))
    gw.reserve_apply(_checked(gw.conf, state="idle", move=3))
    host.run(gw.reserve_forward_decide())
    assert host.open == [], "idle основы после возврата (move ≥ serving) не закрыл пересылку"


def test_idle_of_the_primary_older_than_serving_does_not_close(gw, host):
    """Живая, но не знающая о переезде основа шлёт idle прежнего переезда —
    РФ-доступ пользователям у резерва не закрываем."""
    gw.reserve_apply(_checked(gw.conf, state="idle", move=2))
    host.fact = True
    host.run(gw.reserve_state_in("serving", 3))
    gw.reserve_apply(_checked(gw.conf, state="idle", move=2))
    host.run(gw.reserve_forward_decide())
    assert host.open == OPEN, "старое idle основы закрыло пересылку при serving резерва"


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
    assert host.open == OPEN, "после перезагрузки пересылка не восстановилась из reserve.json"


def test_unknown_state_words_are_ignored(gw, host):
    host.fact = True
    host.run(gw.reserve_state_in("relay", 9))
    host.run(gw.reserve_state_in("rm -rf", 9))
    assert host.open == [] and gw.reserve_section()["reserve_state"] == "idle"


# ── взгляд зонда на сервер AWG ─────────────────────────────────────────────

def _own_view(gw, w: str, since: str = "2026-10-30T10:05:00+03:00") -> None:
    """Взгляд своего зонда кворума на сервер AWG линии — как его хранит quorum.json."""
    from awgbot.domain.gateway.reserve.store import _write
    _write({"l": "a", "views": {"awgqah": {"w": w, "since": since}}}, gw._QUO_STORE)


def _watch(gw, t0: float, seconds: int, step: int = 5) -> list[tuple]:
    acts = []
    for t in range(0, seconds + 1, step):
        acts += gw.reserve_watch_tick(now=t0 + t)
    return acts


def test_the_watch_tick_does_not_probe_the_primary_any_more(gw, host):
    """Наблюдателя основы нет: ни пробы основы по каналу конфигурации, ни слова
    `seen` резерву — тишина линка слота ничего не шлёт."""
    host.slot_rx = None
    acts = _watch(gw, 1000.0, 120)
    assert not [a for a in acts if a[0] == "gw_ping"], f"проба основы по каналу конфигурации: {acts}"
    assert not [a for a in acts if a[0] == "guest" and a[1] in ("seen", "share", "need", "view")], acts


# ── резерв по гостевому каналу ──────────────────────────────────────────────

@pytest.mark.parametrize("kind, body", [("need", {"case": "1", "since": "x", "m_gen": 1}),
                                        ("view", {"seen": "", "voters": [{"slot": 1, "see": False}], "in": "idle",
                                                  "pkg_gen": 3})])
def test_matrix_words_of_an_older_reserve_get_nothing(gw, host, kind, body):
    """Слово матрицы — ни доли, ни уведомления: долю (точку кворума) шлюз отдаёт
    только на `qneed` решения большинства."""
    _own_view(gw, "uns")
    acts = gw.reserve_guest_in(kind, body)
    assert not [a for a in acts if a[0] in ("guest", "notify")], f"на {kind} шлюз ответил: {acts}"
    assert "share_given_at" not in gw.reserve_section() or not gw.reserve_section()["share_given_at"]


def test_ping_from_the_reserve_gets_pong_with_the_same_nonce(gw):
    assert gw.reserve_guest_in("ping", {"n": "0123456789abcdef"}) == [("guest", "pong", {"n": "0123456789abcdef"})]


def test_every_word_the_gateway_puts_on_the_guest_channel_is_allowed_from_the_gateway(gw, host):
    """Проба резерва по подозрению (awgrsv молчит) — тоже слово гостевого
    канала: слушатель резерва проверяет каждое сообщение шлюза по словарю, и
    вид «не с той стороны» рвёт сессию — шлюз выпадает из голосующих."""
    host.rsv_rx = 100
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


def test_hello_is_ready_at_once_and_carries_only_the_slot_and_caps(gw, host):
    """Ждать «первого взгляда на основу» больше не нужно: взгляд — `qview` зонда
    первым словом сессии, hello — только слот и возможности."""
    body = gw.reserve_guest_hello_body()
    assert body == {"slot": 1, "caps": ["rsv1", "quo1", "rsvtg1"]}, body   # quo1 — кворум, rsvtg1 — Telegram через awgrsv
    pairproto.check(pairproto.GUEST_PROTO, "hello", body | {"nonce": ""}, pairproto.GATEWAY)


def test_hello_without_the_section_is_none(gw):
    gw.reserve_apply(None)
    assert gw.reserve_guest_hello_body() is None


# ── Т-РА3 по взгляду зонда ──────────────────────────────────────────────────

def _move(gw, host) -> list:
    host.fact = True
    acts = gw.reserve_guest_in("state", {"state": "serving", "move": 3})
    host.run(acts)
    return [n for a in acts if a[0] == "notify" for n in a[1]]


def test_opening_while_the_probe_does_not_see_the_server_is_loud_and_mailed(gw, host):
    """Т-РА3: зонд шлюза сервер не видит — 🔴 «не отвечает» со звуком и почтой
    (Telegram агента идёт по аплинку, который сейчас может быть мёртв), момент — из взгляда."""
    _own_view(gw, "uns", since="2026-10-30T10:05:00+03:00")
    told = _move(gw, host)
    assert host.open == OPEN and len(told) == 1, told
    n = told[0]
    assert n.text.startswith("🔴 Сервер AWG не отвечает с ") and "переехали на резерв «Beta»" in n.text, n.text
    assert n.force_sound and n.critical, "Т-РА3 «сервер не отвечает» без звука или без почты"
    assert RECORD in n.text and RESERVE_IP in n.text, "в Т-РА3 нет записи и адреса резерва"


@pytest.mark.parametrize("w", ["see", "held"])
def test_opening_while_the_probe_sees_the_server_is_quiet(gw, host, w):
    """Т-РА3б: этому шлюзу сервер отвечает — переезд решило большинство; без
    звука и без почты, 🟡."""
    _own_view(gw, w)
    told = _move(gw, host)
    assert host.open == OPEN and len(told) == 1, told
    n = told[0]
    assert n.text.startswith("🟡 Пользователи переехали на резерв «Beta»") and "отвечает" in n.text, n.text
    assert not n.force_sound and n.silent and not n.critical, f"Т-РА3б со звуком или почтой: {n}"
    assert not n.text.endswith("."), n.text


def test_the_move_is_told_once_per_move_number(gw, host):
    _own_view(gw, "uns")
    first = _move(gw, host)
    assert len(first) == 1
    if first[0].on_sent:
        first[0].on_sent()                                  # доставлено — повтор отложенного не нужен
    again = [n for a in gw.reserve_guest_in("state", {"state": "serving", "move": 3}) if a[0] == "notify" for n in a[1]]
    again += [n for a in _watch(gw, 1000.0, 30) if a[0] == "notify" for n in a[1]]
    assert not [n for n in again if "переехали" in n.text], "Т-РА3 повторился на тот же переезд"


@pytest.mark.parametrize("w", ["", "new", "sus"])
def test_without_a_clear_unseen_the_notice_is_still_the_loud_one(gw, host, w):
    """Взгляда нет или он не ясен — шлюз не может сказать «мне сервер отвечает»:
    тревога громкая (лучше лишний звонок, чем тихий переезд при мёртвом сервере)."""
    if w:
        _own_view(gw, w)
    told = _move(gw, host)
    assert len(told) == 1 and told[0].text.startswith("🔴") and told[0].force_sound, told


# ── аплинк по умолчанию ─────────────────────────────────────────────────────

def test_the_default_uplink_is_never_the_reserve_link(monkeypatch):
    """Шлюз с линком к резерву и одним туннелем-аплинком: аплинк — туннель;
    без него — пусто, а не awgrsv."""
    monkeypatch.setattr(config, "GW_UPLINK_IF", "")
    monkeypatch.setattr(gwguard, "script_status", lambda: {})
    ifaces = {"v": "awglink awgrsv awg0"}
    monkeypatch.setattr(gwguard, "_awg", lambda args: ifaces["v"] if args == ["show", "interfaces"] else "")
    monkeypatch.setattr(gwguard, "_endpoint_host", lambda iface: RESERVE_IP if iface == "awgrsv" else "")
    assert gwguard.uplink_interface() == "awg0"
    ifaces["v"] = "awglink awgrsv"
    assert gwguard.uplink_interface() == "", "аплинком выбран линк к резерву"


# ── порт гостевого канала из раздела ────────────────────────────────────────

def test_the_guest_channel_goes_to_the_port_of_the_section_and_falls_back_to_the_setting(gw, host):
    gw.reserve_apply(_checked(gw.conf, chan=8791))
    addr, port = gw.reserve_guest_target()
    assert (addr, port) == ("10.99.97.1", 8791), "шлюз ходит не на порт гостевого канала резерва"
    gw.reserve_apply(_checked(gw.conf, chan=0))
    assert gw.reserve_guest_target()[1] == 8787


def test_a_changed_channel_port_is_a_change_of_the_section_and_of_its_print(gw, host):
    before = gw.reserve_print()
    res = gw.reserve_apply(_checked(gw.conf, chan=8791))
    assert res["changed"], "смена порта гостевого канала — не изменение раздела"
    assert gw.reserve_print() != before, "отпечаток раздела не заметил порт канала — сервер шлёт документ снова и снова"
    assert host.ups == [gw.conf], "смена порта канала переподняла линк"


# ── адреса админов и линк ───────────────────────────────────────────────────

def test_changed_admin_addresses_raise_the_link_again_and_the_same_ones_do_not(gw, host, monkeypatch):
    env = {"CLIENT_SUBNET": SUBNET, "ADMIN_IPS": "203.0.113.200"}
    monkeypatch.setattr(gwguard, "unit_env", lambda k: env.get(k, ""))
    gw.reserve_apply(_checked(gw.conf))
    assert len(host.ups) == 1, "те же адреса админов переподняли линк"
    env["ADMIN_IPS"] = "203.0.113.200,203.0.113.201"
    gw.reserve_apply(_checked(gw.conf))
    assert len(host.ups) == 2, "набор rsv_admin4 заполняет только --up: новые адреса админов не дошли бы"
    assert gw.reserve_section()["admins"] == ["203.0.113.200", "203.0.113.201"]


# ── момент открытия пересылки ───────────────────────────────────────────────

def test_the_moment_of_opening_is_kept_also_when_the_primary_says_away(gw, host):
    assert not gw.reserve_section().get("open_at")
    gw.reserve_apply(_checked(gw.conf, state="away", move=4))
    host.run(gw.reserve_forward_decide())
    assert host.open == OPEN and gw.reserve_section().get("open_at"), "момент открытия по слову основы не записан"


# ── проверка «резервный линк» — не авария обвязки ───────────────────────────

def test_a_silent_reserve_is_not_a_plumbing_alarm(gw, monkeypatch):
    """Резерв не отвечает — это не поломка шлюза: строка видна в мониторе,
    но «⚠️ Обвязка шлюза неисправна» (и её «снова в порядке») не приходят,
    сколько бы тиков ни длилось."""
    from awgbot.domain import gwchecks as gc
    from awgbot.domain.gateway import GwCheck
    from tests.unit.test_gateway import _quiet_status
    st = _quiet_status(checks=[GwCheck("MASQUERADE", True),
                               GwCheck(gc.CHECK_RSV_LINK, False, "«Gamma» не отвечает с 13:58", group="rsv")])
    monkeypatch.setattr(gw, "status", lambda: st)
    monkeypatch.setattr(gw, "uplink_policy_heal", lambda: [], raising=False)
    notes = []
    for _ in range(6):
        notes += gw.monitor_tick()
    assert not [n for n in notes if "Обвязка" in n.text], [n.text for n in notes]


def test_the_snapshot_keeps_the_suspect_view_of_the_reserve():
    """Взгляд «проверяю» (suspect) — штатное состояние пробы: снимок не
    превращает его в пустую строку (экран сервера показывал бы «нет данных»)."""
    from awgbot.domain import gwsnapshot
    assert gwsnapshot.sanitize({"rsv": {"seen": "suspect"}})["rsv"]["seen"] == "suspect"


def test_the_forward_lets_in_every_allowed_ip_of_the_zone_side(gw):
    """Адрес зоны (с него зонды живости бота основы) и все подсети туннеля
    основы — основная и окно переезда профилей."""
    conf = gw.conf.replace(f"AllowedIPs = 10.99.97.1/32, {SUBNET}\n",
                           f"AllowedIPs = 10.99.97.1/32 , {SUBNET},198.51.100.0/24\n")
    gw.reserve_apply(_checked(conf))
    assert gw._reserve_sources() == ["10.99.97.1/32", SUBNET, "198.51.100.0/24"]


def test_without_allowed_ips_the_forward_falls_back_to_the_client_subnet(gw):
    gw.reserve_apply(_checked(gw.conf.replace(f"AllowedIPs = 10.99.97.1/32, {SUBNET}\n", "")))
    assert gw._reserve_sources() == [SUBNET], "без AllowedIPs пересылка не знает, кого пускать"


def test_without_allowed_ips_and_without_the_client_subnet_nothing_is_let_in(gw, monkeypatch):
    monkeypatch.setattr(gwguard, "unit_env", lambda k: "")
    gw.reserve_apply(_checked(gw.conf.replace(f"AllowedIPs = 10.99.97.1/32, {SUBNET}\n", "")))
    assert gw._reserve_sources() == []


def test_the_snapshot_carries_no_matrix_view_of_the_primary(gw, host):
    """Поле `rsv` снимка — связь с резервом и пересылка; поколения доли и
    взгляда на основу (матрица) агент в нём не шлёт."""
    snap = gw.reserve_snapshot()
    assert set(snap) == {"seen", "since", "open", "state"}, f"поля rsv снимка: {sorted(snap)}"
