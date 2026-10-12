"""
Общее для test_link_txn_search_*.py: шлюз с моделью хендшейка линка (`_Host`
поверх `_Host` из теста документа), одни часы на агент и сервер, клиент
канала без сокета (`_Client`), заготовки приглашения и переключения.

Смена ключей линка на шлюзе: domain/gateway/cfg/rekey.py (приглашение,
согласие, переключение, отсчёт 90 с, поиск рабочего конфига сутки, закрепление
по первому подписанному ответу сервера) и runtime/linkclient/rekey.py
(link_next, link_go, link_cancel, link_done) — и сквозной переход двух половин
в одном процессе, включая потерю подтверждения в момент переключения.

Подтверждение может потеряться ровно в момент переключения — таймерами это
не решить, поэтому решает сервер, а шлюз сутки держит второй конфиг и ищет.
Цена ошибки: шлюз, который не вернулся сам и не нашёл рабочий конфиг, —
мёртвый линк, мёртвый канал и РФ-доступ у всех до визита к шлюзу; вопрос в
чате агента, заданный второй раз после разрыва сессии; поиск, который не
кончается и дёргает линк каждые три минуты.

Хост — `_Host` из теста документа (юнит, конфиг линка, /etc/awg-gw, скрипт
обвязки в режиме --link-apply ставит файл на место установленного) с одной
надстройкой: хендшейк линка есть, только если установленный конфиг шлюза
составляет пару с живым конфигом ВПС (модель ВПС — `_Vps` из
test_gwlink_rekey.py) или если тест сказал «линк жив». Часы — одни на обе
стороны: `time.time` агента и `timeutil.now` сервера.
"""
from __future__ import annotations

import datetime
import json
import time
import types

import pytest

from awgbot.runtime import linkclient
from awgbot.util import timeutil, wgconf
from tests.unit import test_gw_cfg_apply as _apply
from tests.unit import test_gwlink_rekey as _vps
from tests.unit.test_gwlink_rekey import gw_conf, keypair


conf_dir = _apply.conf_dir
svc = _apply.svc
slot = _vps.slot
vps = _vps.vps


class _Clock:
    """Одни часы на агент и сервер: `time.time` (агент: отсчёт, поиск,
    возраст хендшейка) и `timeutil.now` (сервер: сроки перехода)."""

    def __init__(self, monkeypatch):
        self.t0 = time.time()
        self.shift = 0.0
        monkeypatch.setattr(time, "time", lambda: self.t0 + self.shift)
        monkeypatch.setattr(timeutil, "now",
                            lambda: datetime.datetime.fromtimestamp(self.t0 + self.shift, timeutil.TZ))

    def at(self, seconds: float) -> None:
        self.shift = float(seconds)

    @property
    def now(self) -> float:
        return self.t0 + self.shift


class _Host(_apply._Host):
    """Шлюз: всё из теста документа, плюс модель хендшейка линка. `peer` —
    модель ВПС (`_Vps`): хендшейк свежий, когда установленный конфиг шлюза и
    живой конфиг ВПС — одна пара ключей и сеть не порвана (`broken`). Без
    `peer` — по флагу `alive`."""

    def __init__(self, tmp_path, monkeypatch, clock):
        super().__init__(tmp_path, monkeypatch)
        self.clock = clock
        self.peer = None
        self.alive = True
        self.broken = False

    def link_up(self) -> bool:
        if self.broken:
            return False
        if self.peer is None:
            return self.alive
        mine = wgconf.parse(self.link.read_text(encoding="utf-8"))
        return (wgconf.pubkey_of(mine.get("Interface", "PrivateKey")) == self.peer.peer_pub()
                and mine.get("Peer", "PublicKey") == self.peer.live_pub())

    def _run(self, argv, **kw):
        argv = list(argv)
        if argv[:2] == ["awg", "show"] and argv[-1] == "latest-handshakes":
            self.calls.append(argv)
            stamp = int(self.clock.now) - 5 if self.link_up() else 0
            return _apply._cp(0, f"DUMMYPUB=\t{stamp}\n".encode())
        return super()._run(argv, **kw)

    def installed(self) -> str:
        return self.link.read_text(encoding="utf-8")

    def txn(self) -> dict:
        p = self.etc / "link.txn"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def etc_files(self) -> set[str]:
        return {f.name for f in self.etc.iterdir()} if self.etc.exists() else set()


@pytest.fixture()
def clock(monkeypatch):
    return _Clock(monkeypatch)


@pytest.fixture()
def host(tmp_path, monkeypatch, clock):
    """Шлюз со своей парой ключей линка (настоящий X25519 — по ней модель
    решает, жив ли линк)."""
    h = _Host(tmp_path, monkeypatch, clock)
    h.priv, h.pub = keypair()
    h.vps_priv, h.vps_pub = keypair()
    h.standing = gw_conf(h.priv, h.vps_pub, "DUMMY-LINK-PSK=")
    h.link.write_text(h.standing, encoding="utf-8")
    return h


def _next_conf(host, **over) -> tuple[str, str, str]:
    """Новые ключи шлюзовой стороны так, как их пришлёт сервер: (текст, свой
    публичный, публичный ВПС). `endpoint` — другой Endpoint (для отказа)."""
    priv, pub = keypair()
    _, vpub = keypair()
    return gw_conf(priv, vpub, "DUMMY-NEW-PSK=", **over), pub, vpub


def _msg(conf: str, rid: str = "a1b2c3d4e5f6") -> dict:
    return {"t": "link_next", "id": rid, "conf": conf, "fp": "ignored"}


def _ready(svc, host, rid: str = "a1b2c3d4e5f6") -> str:
    conf, _, _ = _next_conf(host)
    svc.rekey_next_in(_msg(conf, rid))
    svc.rekey_answer(True)
    return conf


def _switched(svc, host, clock, rid: str = "a1b2c3d4e5f6") -> str:
    conf = _ready(svc, host, rid)
    assert svc.rekey_go({"id": rid})["ok"] is True
    return conf


QUIET = {"note": "", "msg": None}
NONE_ANSWERED = {"done": None, "note": "", "msg": None}
BACK_NOTE = "⚠️ Новые ключи линка не поднялись — вернул прежние"


def gwcfg_render(text: str) -> str:
    """Конфиг так, как его кладёт агент: канонический текст со штампом."""
    return wgconf.render(wgconf.parse(text), "link", stamp="1")


def _tries(svc, host, clock, until: float, step: float = 30) -> list[float]:
    """Тикать каждые `step` с до `until`; вернуть моменты (от начала часов),
    когда агент переставлял конфиг линка."""
    out = []
    while clock.shift < until:
        clock.at(clock.shift + step)
        before = len(host.link_runs)
        svc.rekey_tick()
        if len(host.link_runs) > before:
            out.append(clock.shift)
    return out


class _Client:
    """Клиент канала без сокета: `_send` записывает, что ушло серверу;
    крючки вопроса, уведомления и правки сообщения — пакета; `closed` —
    сколько раз клиент сам закрыл свой writer; `kicks` — запуски частого такта."""

    def __init__(self, svc, monkeypatch):
        self.client = linkclient.LinkClient(svc)
        self.closed = 0

        def close():
            self.closed += 1
        self.client._writer = types.SimpleNamespace(close=close)
        self.sent: list[tuple[str, dict]] = []
        self.asks: list[str] = []
        self.notes: list[str] = []
        self.edits: list[tuple[int, int, str]] = []
        self.edit_ok = True
        self.kicks = 0
        self.on_send = None

        async def send(kind, body=None, pad=0):
            self.sent.append((kind, dict(body or {})))
            if self.on_send is not None:
                return await self.on_send(kind, dict(body or {}))
            return True
        self.client._send = send

        async def ask(section):
            self.asks.append(section)

        async def notify(text):
            self.notes.append(text)

        async def edit(chat, mid, text):
            self.edits.append((chat, mid, text))
            return self.edit_ok

        def kick():
            self.kicks += 1
        self.client.rekey_kick = kick
        monkeypatch.setattr(linkclient, "_ask", ask)
        monkeypatch.setattr(linkclient, "_notify", notify)
        monkeypatch.setattr(linkclient, "_edit", edit)
        monkeypatch.setattr(linkclient, "_client", self.client)
