"""Запуск бота основы на резерве для стенда: как `python -m awgbot` в гостевом
режиме, но Bot API — макет `tests.lab.botapi` (адрес в LAB_BOTAPI), а не
настоящий Telegram.

Окружение — то же, что у юнита гостя: AWG_BOT_GUEST=1, AWG_BOT_CONF_DIR,
AWG_BOT_DATA_DIR, AWG_BOT_ENV, BOT_TOKEN и ADMIN_ID из env хранилища гостя.
LAB_MAKE_CLIENT=<имя> — до старта завести в базе гостя активный профиль (так
сценарий получает «запись, сделанную на резерве», не подключая настоящих
пользователей).
LAB_GUEST_CMD=<каталог> — правки в работающем боте (как правка админа на резерве):
файл `edit_one` с JSON {"name": …} — профиль с устройством, `set` с {"name",
"field", "value"} — поле профиля; итог — `<команда>.done` (отказ базы следующему —
полем error); раз в секунду — status.json (эпоха, роль, номера журнала).

    ip netns exec awg-guest env … python -m tests.lab.guest_main
"""
from __future__ import annotations

import asyncio
import os

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

import awgbot.runtime.main as main_pkg
from awgbot.core import config


class LabBot(main_pkg.Bot):
    def __init__(self, token: str, **kw):
        # таймаут как у продукта (≤10 с): первый запрос к макету может попасть в окно до перерисовки NAT зоны
        # (запись conntrack без NAT), и с умолчанием aiohttp (60 с) старт гостя стоял бы минуту
        kw["session"] = AiohttpSession(api=TelegramAPIServer.from_base(os.environ["LAB_BOTAPI"]), timeout=10)
        super().__init__(token, **kw)


def _make_client(name: str) -> None:
    from awgbot.infra.db import Database
    db = Database(config.DB_PATH)
    db.init_schema()
    cid = db.create_client(name, 3, "2026-01-01T00:00:00+03:00", "2099-01-01T00:00:00+03:00", f"LAB-{name}")
    db.activate_client(cid, 880001)
    db.close()


_svc: list = []


class _Capture:
    """Сервисы, которые собрал run_guest, — для правок по команде."""

    def __init__(self, real):
        self.real = real

    def __call__(self, db):
        svc = self.real(db)
        _svc[:] = [svc]
        return svc


def _do(svc, cmd: str, args: dict) -> dict:
    from tests.lab import pairnode
    try:
        if cmd == "edit_one":
            cid = pairnode.edit(svc.db, str(args.get("name") or "Правка-на-резерве"), int(args.get("host", 250)),
                                config.SUBNET_PREFIX)
        else:
            cid = pairnode.client_id(svc.db, str(args["name"]))
            svc.db.update_client_fields(cid, **{str(args["field"]): args["value"]})
        return {"id": cid, "last_i": svc.db.jrn_last_i()}
    except Exception as e:                                     # noqa: BLE001 — отказ базы следующему — тоже итог
        return {"error": f"{type(e).__name__}: {e}"}


async def _commands(path: str) -> None:
    import json
    from pathlib import Path
    d = Path(path)
    d.mkdir(parents=True, exist_ok=True)
    while True:
        await asyncio.sleep(0.5)
        if not _svc:
            continue
        svc = _svc[0]
        for cmd in ("edit_one", "set"):
            f = d / cmd
            if not f.exists():
                continue
            try:
                args = json.loads(f.read_text(encoding="utf-8") or "{}")
            except ValueError:
                args = {}
            f.unlink()
            res = await asyncio.to_thread(_do, svc, cmd, args)
            (d / f"{cmd}.done").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        try:
            meta = svc.db.jrn_meta()
            st = {k: meta[k] for k in ("epoch", "holder", "role", "capture", "applied", "acked", "fork_i")}
            st["last_i"] = svc.db.jrn_last_i()
            (d / "status.json.tmp").write_text(json.dumps(st), encoding="utf-8")
            os.replace(d / "status.json.tmp", d / "status.json")
        except Exception:                                      # noqa: BLE001
            pass


async def _run() -> None:
    task = asyncio.create_task(_commands(os.environ["LAB_GUEST_CMD"])) if os.environ.get("LAB_GUEST_CMD") else None
    try:
        await main_pkg.run_guest()
    finally:
        if task is not None:
            task.cancel()


def main() -> None:
    if not config.GUEST:
        raise SystemExit("AWG_BOT_GUEST не задан — это запуск гостевого режима")
    if os.environ.get("LAB_MAKE_CLIENT"):
        _make_client(os.environ["LAB_MAKE_CLIENT"])
    main_pkg.Bot = LabBot
    main_pkg.Services = _Capture(main_pkg.Services)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
