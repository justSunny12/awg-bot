"""Общее для тестов набора обновлений (domain/fleet): сервер AWG на временной
базе со слотами шлюзов, выпуски с метаданными и стенд хода.

Сети нет: список выпусков кладётся в базу так же, как его сохраняет проверка
обновлений; сессия шлюза — тем же путём, каким её открывает канал
(gwlink_session_opened). Сервер «обновляется» подменой установленной версии
— так новый процесс видит себя после перезапуска; действия сервера над
собой (апдейтер, __update_commit / __update_revert) записываются, а не
исполняются.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import threading
import types

import pytest

from awgbot.core import config
from awgbot.domain.fleet import order as fleet_order
from awgbot.domain.fleet import server as fleet_server
from awgbot.domain.selfupdate import marks
from awgbot.infra import awglock, updates
from awgbot.runtime.linkserver import upd as ls_upd
from awgbot.util import timeutil


def rel(tag: str, *, main: dict | None = None, gw: dict | None = None, both: bool = False,
        gen: int = 1) -> updates.Release:
    """Выпуск с метаданными: main / gw — поля роли (requires, needs_gw,
    needs_main); both — обеим ролям с минимумом 3.4.0."""
    roles: dict = {}
    if both:
        main = main or {"requires": "3.4.0"}
        gw = gw or {"requires": "3.4.0"}
    if main is not None:
        roles["main"] = main
    if gw is not None:
        roles["gw"] = gw
    meta = {"schema": 1, "version": tag[1:], "awg_gen": gen, "roles": roles}
    return updates.Release(tag=tag, version=updates.parse_version(tag), body=f"- изменения {tag}",
                           asset_url=f"https://api/assets/{tag}", sha256=hashlib.sha256(tag.encode()).hexdigest(),
                           meta=updates.meta.check(meta, tag))


class Fleet:
    """Стенд хода: сервер, слоты, выпуски, такты."""

    def __init__(self, services, make_active_client, monkeypatch, tmp_path):
        self.s = services
        self.mp = monkeypatch
        self.tmp = tmp_path
        self.applied: list[str] = []
        self.spawned: list[list[str]] = []
        self.revert_rc = 0
        monkeypatch.setattr(config, "ROLE", "client")
        monkeypatch.setattr(config, "INSTALLED_VERSION", "3.4.0")
        monkeypatch.setattr(config, "DB_PATH", tmp_path / "bot.db")
        monkeypatch.setattr(awglock, "generation", lambda: 1)
        self.hold_ids: list[str] = []

        def apply(r, hold=""):
            self.applied.append((r.tag, bool(hold)))
            self.hold_ids.append(hold if isinstance(hold, str) else "")
        monkeypatch.setattr(services, "apply_update", apply)
        monkeypatch.setattr(fleet_order, "_spawn", lambda argv, unit: self.spawned.append(argv[1:]))
        monkeypatch.setattr(marks, "revert_check", lambda: self.revert_rc)
        self.admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID, device_limit=0)
        self.names: dict[int, str] = {}
        self.drop: set[int] = set()        # слоты, которым отправка срывается (сессия падает на записи)

    # ── мир ─────────────────────────────────────────────────────────────────

    def releases(self, *items) -> None:
        self.s._update_store(list(items), {})

    def slot(self, n: int, version: str = "3.4.0", *, online: bool = True, caps=("cfg1", "upd1"),
             name: str = "") -> None:
        name = name or f"Шлюз {n}"
        dev = self.s.add_device(self.admin.id, name)
        self.s.db.gateway_add(dev.device_id, f"awglink{n if n > 1 else ''}", 443 + n, f"10.99.99.{(n - 1) * 4}/30",
                              slot_id=n)
        self.names[n] = name
        self.s.gwlink_snapshot_in(n, {"agent_version": version}, 1, True)
        if online:
            self.s.gwlink_session_opened(n, version, 2, list(caps))
        else:
            self.s.db.set_state(f"gwlink_caps_{n}", ",".join(caps))
            self.s.db.set_state(f"gwlink_seen_{n}", "2026-10-12T09:00:00+05:00")

    def active(self, n: int) -> None:
        self.s.db.set_state(self.s._RT_ACTIVE_KEY, str(n))

    def online(self, n: int, version: str, caps=("cfg1", "upd1")) -> None:
        """Шлюз вышел на связь (тем же путём, что hello канала)."""
        self.s.gwlink_session_closed(n)
        self.s.gwlink_snapshot_in(n, {"agent_version": version}, 1, True)
        self.s.gwlink_session_opened(n, version, 2, list(caps))

    def offline(self, n: int) -> None:
        self.s.gwlink_session_closed(n)

    def server(self, version: str) -> None:
        """Новый процесс сервера после перезапуска: версия установки сменилась
        (и процесс другой — шаг, начатый прежним, ждёт факта уже не его)."""
        self.mp.setattr(config, "INSTALLED_VERSION", version)
        self.mp.setattr(fleet_server, "PROC", fleet_server.new_id())

    def guard(self, armed: bool) -> None:
        path = self.tmp / "update-guard"
        if armed:
            path.write_text("VERSION=x\n", encoding="utf-8")
        elif path.exists():
            path.unlink()

    # ── ход ─────────────────────────────────────────────────────────────────

    def start(self, tag: str | None = None) -> str:
        target = self.s.fleet_target()
        return self.s.fleet_start(tag or (target.tag if target else ""))

    def deliver(self, out):
        """Отправка — настоящим слушателем набора (linkserver/upd._run): сданное
        в сокет подтверждается домену; слотам из drop запись в сокет срывается
        (send → False, сессия падает). Живое сообщение, уведомления и действие
        сервера над собой тест проверяет по out сам — в _run они не уходят."""
        stand = self

        class _Srv:
            async def send(self, slot, kind, body):
                if slot in stand.drop:
                    stand.offline(slot)
                    return False
                return True
        quiet = dataclasses.replace(out, server="", live=False, notes=[])
        coro = ls_upd._run(self.s, quiet, _Srv())
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
            return out
        failed: list = []                              # внутри цикла событий (e2e) — своим потоком

        def _go():
            try:
                asyncio.run(coro)
            except BaseException as e:                 # noqa: BLE001
                failed.append(e)
        t = threading.Thread(target=_go)
        t.start()
        t.join(30)
        if failed:
            raise failed[0]
        return out

    def tick(self):
        return self.deliver(self.s.fleet_tick())

    def ack(self, n: int, state: str, error: str = "", step: str = ""):
        st = self.s.fleet_slot(n)
        return self.deliver(self.s.fleet_ack(n, {"id": st["id"], "state": state, "step": step or st.get("target"),
                                                 "error": error}))

    def ack_id(self, n: int, rid: str, state: str, step: str = "", error: str = ""):
        return self.deliver(self.s.fleet_ack(n, {"id": rid, "state": state, "step": step, "error": error}))

    def hello(self, n: int, version: str):
        self.online(n, version)
        return self.deliver(self.s.fleet_hello(n))

    def st(self, n: int) -> dict:
        return self.s.fleet_slot(n)

    def run(self) -> dict:
        return self.s.fleet_run()

    def sent(self, out, kind: str = "upd") -> list:
        return [(slot, body) for slot, k, body in out.send if k == kind]

    def finish_server(self, out, version: str):
        """Ступень сервера ушла апдейтеру — новый процесс на version вышел на
        связь (сторож снят), такт."""
        assert out.server == "apply", out
        self.s.fleet_server_act(out)
        self.server(version)
        return self.tick()

    def gw_done(self, n: int, version: str):
        """Шлюз принял команду, обновился, вышел на связь новой версией, ответил done."""
        self.ack(n, "accepted")
        self.online(n, version)
        return self.ack(n, "done", step=f"v{version}")

    def now_shift(self, seconds: int) -> None:
        real = timeutil.now()
        self.mp.setattr(timeutil, "now", lambda: real + __import__("datetime").timedelta(seconds=seconds))


@pytest.fixture()
def fleet(services, make_active_client, monkeypatch, tmp_path):
    return Fleet(services, make_active_client, monkeypatch, tmp_path)


def ns(**kw):
    return types.SimpleNamespace(**kw)
