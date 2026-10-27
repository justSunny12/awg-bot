"""Старт основного бота (runtime/main/client.py: main) — настоящей сборкой,
а не по исходнику: порядок шагов и крючки канала линка.

Подменены: Telegram (двойник Bot), диспетчер (поллинг ждёт конца стартовых
фоновых задач, проигрывает сценарий и останавливает сборку), планировщик,
вотчдоги, слушатель канала и всё, что на старте ходит в Telegram или
на хост. База и сервисы — настоящие, во временном каталоге; хранилище
диалогов — настоящего aiogram.Dispatcher.

- отметка жизни для сторожа обновления — после открытия базы и до поллинга
  (обе роли): иначе отметка без базы или сторож без отметки;
- крючки канала (installed, набор обновлений) — раньше планировщика и
  слушателя; слушатель — раньше старта набора;
- бэкап сервера из чата бота шлюза: крючок main задаёт вопрос в чате админа
  и кладёт данные в то же хранилище диалогов, из которого их возьмёт
  «♻️ Восстановить» админа, — восстановление запускается;
- такт живости: «на связи» живых сессий каждые 4 такта, такта набора в нём
  нет; набор — своим заданием планировщика без условия маршрутизации.

Цена ошибки: «шлюз настроен», потерянное на старте; бэкап из чата шлюза,
который нельзя восстановить (кнопка не видит файла); ход сервера без шлюзов,
застывший на «running».
"""
from __future__ import annotations

import asyncio
import types

import pytest
from aiogram import Dispatcher

from awgbot.core import config
from awgbot.domain.services import Services
from awgbot.infra.db import Database
from awgbot.runtime import main
from tests.conftest import FakeBot, FakeCallback, FakeMessage

pytestmark = pytest.mark.unit

ADMIN = config.ADMIN_ID


class _Stop(Exception):
    """Сценарий поллинга проигран — сборка дошла до конца."""


async def _noop(*a, **k):
    return None


class _Client:
    """Весь старт main() до поллинга; events — шаги по порядку."""

    def __init__(self, monkeypatch, tmp_path):
        self.events: list[str] = []
        self.services = None
        self.bot = None
        self.during = None                                # async fn(start) — сценарий внутри поллинга
        st = self
        ev = self.events.append

        class _Bot(FakeBot):
            def __init__(self, token, *a, **k):
                super().__init__()
                self.id = 4242
                self.session = types.SimpleNamespace(close=_noop)
                st.bot = self

            async def get_me(self):
                return types.SimpleNamespace(username="awg_test_bot", first_name="AWG", id=4242)

        class _Db(Database):
            def init_schema(self):
                super().init_schema()
                ev("db.init_schema")

        class _Services(Services):
            def __init__(self, db):
                super().__init__(db)
                st.services = self

            def detect_and_handle_restart(self):          # awg на машине теста нет
                return None

            def server_ok(self):
                return True

            def bootstrap_admin_device(self):
                return None

        class _Dp:
            def __init__(self):
                self.real = Dispatcher()                  # хранилище диалогов — настоящее, как у бота
                self.storage = self.real.storage

            def resolve_used_update_types(self):
                return []

            async def start_polling(self, *a, **k):
                ev("start_polling")
                for _ in range(500):
                    if "after_start_done" in st.events:
                        break
                    await asyncio.sleep(0.01)
                assert "after_start_done" in st.events, f"стартовые задачи не дошли до конца: {st.events}"
                if st.during is not None:
                    await st.during(st)
                raise _Stop

        class _Sched:
            def start(self):
                ev("scheduler.start")

            def shutdown(self, wait=False):
                pass

        class _Watcher:
            def __init__(self, *a, **k):
                pass

            start = stop = ensure_watching = lambda self: None

        self.dp = _Dp()
        from awgbot.bot import notifier
        from awgbot.bot.handlers import admin as admin_h
        from awgbot.bot.handlers import restore as restore_h
        from awgbot.runtime import gwbotme, linkserver, poolprobe, preflight
        from awgbot.runtime.linkserver import upd as fleet_upd
        from awgbot.runtime.main import alive
        monkeypatch.setattr(config, "ROLE", "client")
        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "bot.db"))
        monkeypatch.setattr(config, "BOT_TOKEN", "42:DUMMY")
        monkeypatch.setattr(config, "validate", lambda: None)
        from awgbot.core import settings
        monkeypatch.setattr(settings, "init", lambda conf_dir: None)   # кэш настроек — копия conf прогона
        monkeypatch.setattr(preflight, "check_fatal", lambda: None)
        monkeypatch.setattr(preflight, "collect_warnings", lambda s, ok: [])
        for name, value in (("join_pending", lambda: False), ("Bot", _Bot), ("Database", _Db),
                            ("Services", _Services), ("make_dispatcher", lambda *a, **k: self.dp),
                            ("AwgWatcher", _Watcher), ("ConfWatcher", _Watcher),
                            ("setup_scheduler", lambda *a, **k: _Sched()), ("_menu_command", _noop),
                            ("do_reconcile", _noop), ("report_update_result", _noop),
                            ("_notify_migration_needed", _noop), ("_notify_private_dns_offer", _noop),
                            ("_sync_bot_identity", _noop), ("notify_one", _noop)):
            monkeypatch.setattr(main, name, value)

        async def _reboot(*a, **k):
            ev("after_start_done")
        monkeypatch.setattr(main, "_announce_reboot", _reboot)
        monkeypatch.setattr(alive, "watch", lambda bot: ev("alive.watch"))
        monkeypatch.setattr(notifier, "set_email_fallback", lambda fn: None)
        monkeypatch.setattr(admin_h, "restore_panel_after_restart", _noop)
        monkeypatch.setattr(restore_h, "report_restore_result", _noop)
        monkeypatch.setattr(poolprobe, "run", _noop)
        monkeypatch.setattr(gwbotme, "ensure_all", _noop)

        async def _ensure(services):
            ev("linkserver.ensure")
        monkeypatch.setattr(linkserver, "ensure", _ensure)
        monkeypatch.setattr(linkserver, "shutdown", _noop)

        async def _fleet_start(services):
            ev("fleet.on_start")
        monkeypatch.setattr(fleet_upd, "on_start", _fleet_start)
        for hook in ("set_on_installed", "set_on_fleet", "set_on_restore"):
            real = getattr(linkserver, hook)
            monkeypatch.setattr(linkserver, hook, lambda fn, _h=hook, _r=real: (ev(_h), _r(fn))[1])
        real_fleet = fleet_upd.set_on_fleet
        monkeypatch.setattr(fleet_upd, "set_on_fleet", lambda fn: (ev("set_on_fleet"), real_fleet(fn))[1])

    async def run(self):
        with pytest.raises(_Stop):
            await main.main()


@pytest.fixture()
def start(monkeypatch, tmp_path, fake_awg, fake_routing):
    return _Client(monkeypatch, tmp_path)


def _before(events: list[str], *names: str) -> None:
    idx = [events.index(n) for n in names]
    assert idx == sorted(idx), f"порядок {names} нарушен: {events}"


async def test_the_life_mark_hooks_and_the_channel_come_in_order(start):
    await start.run()
    ev = start.events
    _before(ev, "db.init_schema", "alive.watch", "start_polling")
    _before(ev, "set_on_installed", "scheduler.start", "linkserver.ensure", "fleet.on_start")
    _before(ev, "set_on_fleet", "scheduler.start")
    assert ev.count("set_on_installed") == 1 and ev.count("set_on_fleet") == 1, ev


async def test_a_backup_from_the_gateway_chat_is_restored_by_the_admin_button(start, monkeypatch):
    """Крючок restore из настоящего main: вопрос в чате админа, данные — в
    хранилище диалогов диспетчера; «♻️ Восстановить» админа (контекст
    диалога — так, как его строит aiogram для админа в своём чате) находит
    файл и запускает восстановление."""
    from awgbot.bot.handlers import restore as rs
    from awgbot.domain.selfupdate import marks
    from awgbot.runtime import linkserver
    blob = b"AWGBK1P" + b"DUMMY" * 600
    launched = []

    async def scenario(st):
        s = st.services
        s.inspect_backup = lambda b, name="": {"ok": True, "created_at": "2026-10-05T14:00:00+03:00",
                                               "plan": {"line": "same", "slots": [], "created_at": ""}}
        s.prepare_restore_release = lambda plan: ""
        s.prepare_restore = lambda b, plan: launched.append(("blob", b)) or "/tmp/restore-pending.tgz.enc"
        s.launch_restore = lambda path, release="": launched.append(("launch", path, release))
        assert await linkserver._on_restore(0, blob, "awg-bot-backup-main-x.tgz.enc") == ""
        asked = [r for r in st.bot.records if r[0] == "send_message"]
        assert asked and asked[-1][1] == ADMIN and asked[-1][2].startswith("♻️ Восстановить бэкап от "), asked
        state = st.dp.real.fsm.resolve_context(bot=st.bot, chat_id=ADMIN, user_id=ADMIN)
        msg = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=st.bot)
        cb = FakeCallback(message=msg, user_id=ADMIN, bot=st.bot)
        await rs.run_restore(cb, s, state, rs._fingerprint(blob))
        assert ("blob", blob) in launched and ("launch", "/tmp/restore-pending.tgz.enc", "") in launched, \
            f"кнопка админа не нашла бэкап из чата шлюза: {cb.answers}"
    monkeypatch.setattr(marks, "install_busy", lambda: False)
    start.during = scenario
    await start.run()


# ── вторая роль: отметка жизни агента — тоже после базы ─────────────────────

async def test_the_agent_watches_telegram_once_its_base_is_open(monkeypatch, tmp_path):
    from awgbot.runtime.main import alive
    from tests.unit.test_gateway_token_start import _Start, _Stop as _GwStop
    gw = _Start(monkeypatch, tmp_path)

    class _Db(Database):
        def init_schema(self):
            super().init_schema()
            gw.events.append("db.init_schema")
    monkeypatch.setattr(main, "Database", _Db)
    monkeypatch.setattr(alive, "watch", lambda bot: gw.events.append("alive.watch"))
    with pytest.raises(_GwStop):
        await main.run_gateway()
    _before(gw.events, "db.init_schema", "alive.watch", "polling")


# ── такт живости и задание набора ───────────────────────────────────────────

async def test_the_liveness_tick_touches_seen_every_fourth_tick_and_never_ticks_the_fleet(monkeypatch):
    from awgbot.runtime import linkserver
    from awgbot.runtime.linkserver import upd as fleet_upd
    calls: list = []

    class _Srv:
        def __init__(self):
            self._sessions = {1: types.SimpleNamespace(hello=True), 2: types.SimpleNamespace(hello=False)}

        def __getattr__(self, name):
            async def step(*a, **k):
                calls.append(name)
            return step

        def deliver_soon(self):
            calls.append("deliver_soon")
    monkeypatch.setattr(config, "ROLE", "client")
    monkeypatch.setattr(linkserver, "_server", _Srv())

    async def fleet_tick(services):
        calls.append("fleet_tick")
    monkeypatch.setattr(fleet_upd, "fleet_run_tick", fleet_tick)
    seen: list = []
    services = types.SimpleNamespace(gwlink_seen_touch=lambda live: seen.append(list(live)))
    for _ in range(8):
        await linkserver.liveness_tick(services)
    assert calls.count("deliver_soon") == 2 and seen == [[1], [1]], (calls, seen)
    for step in ("ensure", "sweep_dead", "send_roles"):
        assert calls.count(step) == 8, step
    assert "fleet_tick" not in calls and "deliver_all" not in calls, "такт живости тянет набор или полную доставку"


async def test_the_fleet_has_its_own_job_without_the_routing_condition(services, monkeypatch):
    from awgbot.runtime import scheduler as sch
    from awgbot.runtime.linkserver import upd as fleet_upd
    monkeypatch.setattr(config, "ROUTING_ENABLED", False)
    jobs: list = []

    async def job(s):
        jobs.append(s)
    monkeypatch.setattr(fleet_upd, "fleet_job", job)
    sched = sch.setup_scheduler(services, object(), services.db, watcher=None)
    fleet = sched.get_job("fleet")
    assert fleet is not None, "у набора нет своего задания"
    await fleet.func()
    assert jobs == [services], "задание набора не зовёт fleet_job при выключенной маршрутизации"
