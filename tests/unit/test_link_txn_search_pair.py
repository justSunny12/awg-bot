"""
Смена ключей линка: сквозной переход — сервер и шлюз в одном процессе, —
и контракт настоящего скрипта стадии с проверкой шлюза.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from awgbot.domain.gateway.cfg import rekey as gwrekey
from awgbot.runtime import linkclient
from awgbot.runtime import linkserver
from awgbot.runtime.linkserver import rekey as lsrekey
from awgbot.util import gwlink, wgconf
from tests.unit import _link_txn_common as _c
from tests.unit import test_gwlink_rekey as _vps
from tests.unit._link_txn_common import BACK_NOTE, _Client, gwcfg_render
from tests.unit.test_gwlink_rekey import gw_conf, keypair

pytestmark = pytest.mark.unit

conf_dir, svc, slot, vps, clock, host = _c.conf_dir, _c.svc, _c.slot, _c.vps, _c.clock, _c.host


# ── сквозной переход: сервер и шлюз в одном процессе ─────────────────────────

class _Pair:
    """Две половины канала без сокетов: слушатель ВПС (`LinkServer` над
    настоящими Services и моделью ВПС) и клиент шлюза (`LinkClient` над
    настоящим агентом). Сообщение доходит, только если линк жив (пара ключей
    сходится и сеть не порвана); `lost` — виды сообщений сервера, теряемые в
    пути после сдачи в сокет; `unsent` — не ушедшие вовсе (send вернул
    False). Сессия открывается заново по `session()`: hello на сервере
    (может закрыть её молча), первое слово сервера у клиента, доставка
    перехода. `drops` — сколько раз сервер сам порвал сессию."""

    def __init__(self, services, svc, host, vps, monkeypatch):
        self.services, self.svc, self.host, self.vps = services, svc, host, vps
        host.peer = vps
        # шлюз стоит ровно на том, что знает ВПС: /root/gw-awglink.conf
        host.link.write_text(gwcfg_render(vps.gw.read_text(encoding="utf-8")), encoding="utf-8")
        self.server = linkserver.LinkServer(services)
        self.c = _Client(svc, monkeypatch)
        self.lost: set[str] = set()
        self.unsent: set[str] = set()
        self.server_notes: list[str] = []
        self.to_gateway: list[str] = []
        self.session_keys: tuple[str, str] = ("", "")
        self.drops = 0
        self.holds = 0
        self.serve_ticks = True

        async def server_send(slot_id, kind, body=None, pad=gwlink.PAD_DELTA):
            self.to_gateway.append(kind)
            if kind in self.unsent or not host.link_up():
                return False
            if kind in self.lost:
                return True                               # ушёл в сокет и пропал в пути
            await self.c.client._handle({"t": kind, **dict(body or {})})
            return True
        self.server.send = server_send

        async def client_send(kind, body):
            if not host.link_up():
                return False
            if kind == "link_ack":
                await self.server.rekey_ack_in(1, {"t": kind, **body})
            elif kind == "link_done":
                await self.server.rekey_done_in(1, {"t": kind, **body})
            return True
        self.c.on_send = client_send

        async def drop(slot_id, *, record=False):
            self.drops += 1
            self.session_keys = ("", "")
            self.server._sessions.pop(slot_id, None)
        self.server._drop = drop

        async def on_notes(notes):
            self.server_notes += [n.text for n in notes]
        monkeypatch.setattr(linkserver, "_on_notes", on_notes)
        monkeypatch.setattr(lsrekey, "COMMIT_DELAY", 0)

    async def session(self) -> bool:
        """Переподключение клиента: удаётся, только если линк жив и сервер не
        закрыл сессию на hello."""
        if not self.host.link_up():
            return False
        self.server._sessions[1] = types.SimpleNamespace(hello=True, caps=["cfg1", "confirm1"], rekey_sent="")
        if not await self.server.rekey_hello(1):
            self.holds += 1
            self.server._sessions.pop(1, None)
            return False
        self.session_keys = self._keys()
        await self.c.client._rekey_answered()
        await self.server.deliver_rekey(1)
        return True

    def _keys(self) -> tuple[str, str]:
        mine = wgconf.parse(self.host.installed()).get("Interface", "PrivateKey")
        return mine, self.vps.peer_pub()

    async def tick(self) -> None:
        """Такт обеих сторон; линк жив на ключах, на которых сессии ещё не
        было, — клиент переподключается (как его бэкофф после разрыва)."""
        steps = ([self.server.rekey_tick] if self.serve_ticks else []) + [self.c.client.rekey_tick]
        for step in steps:
            if not self.host.link_up():
                self.session_keys = ("", "")              # линк лёг — сессия умерла
            await step()
        if not self.host.link_up():
            self.session_keys = ("", "")
        elif self._keys() != self.session_keys:
            await self.session()

    async def run_until(self, clock, until: float, step: float = 10) -> None:
        while clock.shift < until:
            clock.at(clock.shift + step)
            await self.tick()

    def on_same_keys(self) -> bool:
        return self.host.link_up()


async def _settle(cond, timeout: float = 3.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


async def _to_ready(pair: _Pair) -> str:
    """Старт слушателя (возврат на старте отработал до перехода), стадия на
    ВПС, link_next шлюзу, «Применить» в чате агента."""
    await pair.server.rekey_boot()
    st = pair.services.gateway_rekey_stage(1)
    assert await pair.session()
    assert pair.services.gateway_rekey(1)["state"] == "asked", "шлюз не ответил asked"
    assert pair.c.asks == ["link"]
    await linkclient.report_rekey_answer(pair.svc, True)
    return st["id"]


async def _to_switch(pair: _Pair) -> str:
    rid = await _to_ready(pair)
    assert await _settle(lambda: pair.services.gateway_rekey(1).get("committed")), "ВПС не переключился"
    return rid


async def test_a_clean_transition_ends_on_new_keys_on_both_sides(services, slot, vps, svc, host, clock, monkeypatch):
    """Норма: link_go дошёл, шлюз переключился и сам закрыл сессию, ВПС
    переключился и порвал свою; hello новым ключом фиксирует переход на ВПС,
    первое слово сервера — на шлюзе; по одному уведомлению с каждой стороны,
    хвостов на дисках нет."""
    pair = _Pair(services, svc, host, vps, monkeypatch)
    old_gw_pub = vps.gw_pub
    await _to_switch(pair)
    assert pair.c.closed == 1, "шлюз не закрыл сессию после link_go"
    assert await _settle(lambda: pair.drops == 1), "ВПС не порвал сессию после переключения"
    assert pair.on_same_keys(), "после link_go стороны на разных ключах"
    assert await pair.session()
    assert vps.peer_pub() != old_gw_pub and vps.extra_files() == set()
    assert pair.server_notes == [pair.server_notes[0]] and "ключи линка сменены" in pair.server_notes[0]
    assert pair.c.notes == ["✅ Линк на новых ключах"]
    assert services.gateway_rekey(1) == {} and host.txn() == {} and host.etc_files() == {"issued_at"}


async def test_a_link_go_that_did_not_leave_is_repeated_in_the_next_session(services, slot, vps, svc, host, clock,
                                                                           monkeypatch):
    """link_go не ушёл (сессия умерла в этот момент): ВПС не переключается.
    Новая сессия повторяет link_next с тем же id, шлюз отвечает ready без
    второго вопроса — и переход доходит до новых ключей."""
    pair = _Pair(services, svc, host, vps, monkeypatch)
    pair.unsent = {"link_go"}
    await _to_ready(pair)
    await asyncio.sleep(0.05)
    assert services.gateway_rekey(1)["state"] == "asked" and "--rekey-commit" not in vps.modes
    assert host.txn()["state"] == "ready" and pair.on_same_keys()
    pair.unsent = set()
    assert await pair.session()
    assert await _settle(lambda: services.gateway_rekey(1).get("committed")), "переход не дошёл в новой сессии"
    assert pair.c.asks == ["link"], "человека спросили второй раз"
    assert await pair.session()
    assert pair.c.notes == ["✅ Линк на новых ключах"] and services.gateway_rekey(1) == {}


async def test_a_lost_link_go_ends_on_the_old_keys_on_both_sides(services, slot, vps, svc, host, clock, monkeypatch):
    """link_go ушёл в сокет и пропал: ВПС переключился, шлюз — нет (ready).
    Линк лежит, пока ВПС по сроку (180 с) не вернёт прежние; шлюз из ready
    линк не трогает. Линк оживает сам, при первой же сессии ВПС шлёт
    link_cancel — согласие на шлюзе снято, приглашение поправлено «отозван»;
    обе стороны на прежних, админу — одно «вернул прежние», агенту — ни
    одного ложного «✅», хвостов нет ни на одной стороне."""
    pair = _Pair(services, svc, host, vps, monkeypatch)
    pair.lost = {"link_go"}
    old_gw_pub, installed = vps.gw_pub, host.installed()
    rid = await _to_switch(pair)
    assert not pair.on_same_keys(), "модель: link_go потерян — линк обязан лечь"
    await pair.run_until(clock, 170)
    assert not await pair.session() and pair.server_notes == []
    await pair.run_until(clock, _vps.rekeymod.SWITCH_SECONDS + 60)
    assert pair.on_same_keys(), "ВПС не вернул прежние — линк мёртв"
    assert vps.peer_pub() == old_gw_pub and host.installed() == installed
    assert host.link_runs == [], f"шлюз переставлял линк: {host.link_applies}"
    assert len(pair.server_notes) == 1 and "вернул прежние" in pair.server_notes[0], pair.server_notes
    assert pair.to_gateway.count("link_cancel") == 1, pair.to_gateway
    assert host.txn() == {} and host.etc_files() == set(), "согласие на шлюзе не снято отзывом"
    assert pair.c.notes == ["🔑 Новые ключи линка: запрос отозван сервером AWG"], f"агенту ушло: {pair.c.notes}"
    assert services.gateway_rekey(1) == {} and vps.extra_files() == set()
    assert services.gateway_rekey_cancel_take(1) == "", "отзыв остался в очереди после доставки"
    assert "link" not in svc.cfg_questions() and rid


async def test_new_keys_that_never_come_up_bring_both_sides_back(services, slot, vps, svc, host, clock, monkeypatch):
    """link_go дошёл, обе стороны переключились, но новый линк не поднялся
    (сеть рвётся): шлюз за 90 с вернулся на прежние, ВПС за 180 с — тоже;
    поиск находит их без перебора, первое слово сервера закрепляет выбор
    (link_done keys old); по одному уведомлению с каждой стороны."""
    pair = _Pair(services, svc, host, vps, monkeypatch)
    old_gw_pub = vps.gw_pub
    await _to_switch(pair)
    host.broken = True
    clock.at(10)
    await pair.tick()                                     # переподключиться не на чем — сеть рвётся
    assert services.gateway_rekey(1)["state"] == "switched"
    clock.at(gwrekey.SWITCH_SECONDS + 5)
    await pair.tick()
    assert host.txn()["state"] == "back" and pair.c.notes == [BACK_NOTE]
    clock.at(_vps.rekeymod.SWITCH_SECONDS + 5)
    await pair.tick()
    host.broken = False
    assert pair.on_same_keys() and vps.peer_pub() == old_gw_pub
    clock.at(_vps.rekeymod.SWITCH_SECONDS + 15)
    await pair.tick()                                     # сеть вернулась — клиент переподключился
    done = [b for k, b in pair.c.sent if k == "link_done"]
    assert done and done[-1]["keys"] == "old" and done[-1]["ok"] is False, pair.c.sent
    runs = len(host.link_runs)
    await pair.run_until(clock, clock.shift + 3600, step=60)
    assert len(host.link_runs) == runs, "живой линк переставили поиском"
    assert len(pair.server_notes) == 1 and len(pair.c.notes) == 1, (pair.server_notes, pair.c.notes)
    assert host.txn() == {} and services.gateway_rekey(1) == {}


async def test_a_gateway_that_cannot_raise_the_new_keys_brings_the_vps_back_at_once(
        services, slot, vps, svc, host, clock, monkeypatch):
    """Скрипт шлюза отказал новому конфигу на link_go: шлюз ответил declined,
    ВПС не остаётся на новых ключах до срока; обе стороны на прежних сразу,
    админу — одно уведомление с причиной, человеку — «вернул прежние»."""
    pair = _Pair(services, svc, host, vps, monkeypatch)
    old_gw_pub, installed = vps.gw_pub, host.installed()
    host.link_rc = 1
    await _to_ready(pair)
    await asyncio.sleep(0.05)
    assert vps.peer_pub() == old_gw_pub and host.installed() == installed and pair.on_same_keys()
    assert len(pair.server_notes) == 1 and "не прошёл проверку" in pair.server_notes[0], pair.server_notes
    assert pair.c.notes == [BACK_NOTE], pair.c.notes
    assert vps.extra_files() == set() and services.gateway_rekey(1).get("state") == "declined"
    assert host.etc_files() == {"link.txn"} and host.txn()["state"] == "declined"


async def test_a_lost_server_answer_is_found_by_the_search(services, slot, vps, svc, host, clock, monkeypatch):
    """hello новым ключом дошёл (ВПС зафиксировал), а ответ сервера шлюзу
    потерялся: шлюз за 90 с вернулся на прежние — линк мёртв; через 3 минуты
    и второй замер поиск ставит новые, линк оживает, ответ сервера
    закрепляет новые ключи."""
    pair = _Pair(services, svc, host, vps, monkeypatch)
    await _to_switch(pair)
    # сессия на новых ключах открылась на ВПС, первое слово до шлюза не дошло
    pair.session_keys = pair._keys()
    pair.server._sessions[1] = types.SimpleNamespace(hello=True, caps=["cfg1", "confirm1"], rekey_sent="")
    assert await pair.server.rekey_hello(1) is True
    assert services.gateway_rekey(1) == {} and "ключи линка сменены" in pair.server_notes[-1]
    new_gw_pub = vps.peer_pub()
    clock.at(gwrekey.SWITCH_SECONDS + 5)
    await pair.tick()
    assert not pair.on_same_keys(), "модель: шлюз вернулся на прежние, ВПС на новых"
    await pair.run_until(clock, gwrekey.SWITCH_SECONDS + 5 + gwrekey.SEARCH_FIRST + 2 * gwrekey.DEAD_CONFIRM_SECONDS)
    assert pair.on_same_keys(), "поиск не нашёл новые ключи"
    assert vps.peer_pub() == new_gw_pub
    assert pair.c.notes[-1] == "✅ Линк на новых ключах"
    assert host.txn() == {} and host.etc_files() == {"issued_at"}


# ── контракт: настоящий скрипт стадии → проверка шлюза ───────────────────────

class _RealScript:
    """Настоящий routing-link-setup.sh под Services: `id` и `awg` — подставные
    (awg genkey/pubkey дают настоящие X25519 через nacl: шлюз проверяет ключи
    по-настоящему), каталоги ВПС — временные. Конфиг шлюза — тот, что сервер
    шлёт документом (gwcfg_link_conf), как он и стоял бы на шлюзе."""

    def __init__(self, services, tmp_path, monkeypatch, server_host: str = ""):
        import os
        import sys
        from awgbot.core import config, settings
        from awgbot.domain.services.gwchannel import cfg as cfgmod
        from tests.conftest import fake_cmd
        from tests.unit.test_gwlink_rekey import vps_conf
        self.awg = tmp_path / "vps-awg"; self.awg.mkdir()
        self.root = tmp_path / "vps-root"; self.root.mkdir()
        bin_dir = tmp_path / "vps-bin"; bin_dir.mkdir()
        py = sys.executable
        fake_cmd(bin_dir / "id", "echo 0\n")
        fake_cmd(bin_dir / "awg",
                 'case "$1" in\n'
                 f'  genkey|genpsk) "{py}" -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())" ;;\n'
                 f'  pubkey) read k; "{py}" -c "import sys,base64;from nacl.public import PrivateKey;'
                 'print(base64.b64encode(bytes(PrivateKey(base64.b64decode(sys.argv[1])).public_key)).decode())" "$k" ;;\n'
                 'esac\n')
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}")
        monkeypatch.setenv("CONF_DIR", str(self.awg))
        monkeypatch.setenv("GW_CONF_OUT", str(self.root / "gw-awglink.conf"))
        monkeypatch.setenv("_APP_YAML", str(tmp_path / "нет.yaml"))
        monkeypatch.setenv("CLIENT_SUBNET", "10.8.1.0/24")
        monkeypatch.setattr(config, "AWG_DIR", str(self.awg))
        real_open = open

        def _open(path, *a, **kw):
            p = str(path)
            if p.startswith("/root/"):
                p = str(self.root / p[len("/root/"):])
            return real_open(p, *a, **kw)
        monkeypatch.setattr(_vps.rekeymod, "open", _open, raising=False)
        monkeypatch.setattr(cfgmod, "open", _open, raising=False)
        conf = {"app.network.server_host": server_host, "app.network.subnet_cidr": "10.8.1.0/24",
                "app.client_config.keepalive_seconds": "25-35"}
        real_get = settings.get
        monkeypatch.setattr(settings, "get", lambda k, d=None: conf[k] if k in conf else real_get(k, d))
        vpriv, vpub = keypair()
        gpriv, gpub = keypair()
        (self.awg / "awglink.conf").write_text(vps_conf(vpriv, gpub, "DUMMY-LINK-PSK="), encoding="utf-8")
        # источник конфига шлюзовой стороны — как его написал --apply (хост — тот, что был тогда)
        (self.root / "gw-awglink.conf").write_text(gw_conf(gpriv, vpub, "DUMMY-LINK-PSK="), encoding="utf-8")


def _gateway_stands_on_the_document(services, host) -> None:
    gw = services.db.gateway(1)
    text = services.gwcfg_link_conf(gw, services.gwcfg_env(gw))
    assert text, services.gwcfg_link_error(1)
    host.link.write_text(text, encoding="utf-8")


def test_keys_staged_by_the_real_script_pass_the_gateway_check(services, slot, svc, host, tmp_path, monkeypatch):
    """Сквозной контракт: настоящий скрипт стадии → тело link_next на ВПС →
    проверка шлюза. Шлюз принимает новые ключи и показывает тот же отпечаток,
    что карточка слота на ВПС."""
    _RealScript(services, tmp_path, monkeypatch)
    _gateway_stands_on_the_document(services, host)
    st = services.gateway_rekey_stage(1)
    body = services.gateway_rekey_next_body(1)
    res = svc.rekey_next_in({"t": "link_next", **body})
    assert res["reply"]["state"] == "asked", res
    assert svc.rekey_question()["fp"] == st["fp"], "отпечатки на ВПС и в чате агента разошлись"


def test_keys_staged_after_a_domain_change_pass_the_gateway_check(services, slot, svc, host, tmp_path, monkeypatch):
    """После «✏️ Домен» шлюз стоит на Endpoint с новым хостом — его довёз
    документ; `/root/gw-<линк>.conf` остался с прежним (его правит только
    --bundle). Новые ключи обязаны прийти с тем же Endpoint, что стоит, иначе
    шлюз откажет и смена ключей не пройдёт никогда."""
    _RealScript(services, tmp_path, monkeypatch, server_host="vps2.example.org")
    _gateway_stands_on_the_document(services, host)
    assert "Endpoint = vps2.example.org:51821" in host.installed()
    services.gateway_rekey_stage(1)
    res = svc.rekey_next_in({"t": "link_next", **services.gateway_rekey_next_body(1)})
    assert res["reply"]["state"] == "asked", f"шлюз отказал новым ключам: {res['reply']}"
