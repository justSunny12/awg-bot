"""Снимки админа: переезд профилей на второй интерфейс."""
from __future__ import annotations

from awgbot.bot.callbacks import Menu, SetCB

from tests.screens.base import Shot
from tests.screens.shots_admin_common import ADM, _mig_ready, _mig_running, _mig_spread, _people


def _mig_finishing(services, mp):
    """Переезд идёт, и его можно завершить: смена основного интерфейса на
    хосте (гашение старого, файл поколения) — заглушками."""
    from awgbot.infra import awglock
    who = _mig_running(services, mp)
    services._retire_interface = lambda *a, **k: None
    mp.setattr(awglock, "write_state", lambda **k: None)
    mp.setattr(awglock, "needs_migration", lambda: False)
    return who


def _mig_cancelled(services, mp):
    """Переезд отменён, а один двойник уже успел переехать."""
    who = _mig_running(services, mp)
    services.migration_cancel()
    return who


MIGRATION = [
    # ── переезд профилей (⚙️ → 🔧 Сервис) ───────────────────────────────────
    Shot("adm.set.svc.mig", role="admin", press=[SetCB(sec="svc")], data=_mig_ready,
         title="сервис: переезд настроен"),
    Shot("adm.set.mig.start", role="admin", press=[SetCB(sec="mig", act="do", key="start")], data=_mig_ready,
         title="начать переезд?"),
    Shot("adm.set.mig.start.yes", role="admin", press=[SetCB(sec="mig", act="do", key="start!")],
         data=_mig_ready, title="переезд начат — след и раздел"),
    Shot("adm.set.svc.running", role="admin", press=[SetCB(sec="svc")], data=_mig_running,
         title="сервис: переезд идёт"),
    Shot("adm.set.mig.pending", role="admin", press=[SetCB(sec="mig", act="do", key="pending")],
         data=_mig_running, title="кто не переехал"),
    Shot("adm.set.mig.finish", role="admin", press=[SetCB(sec="mig", act="do", key="finish")],
         data=_mig_running, title="завершить переезд?"),
    Shot("adm.set.mig.cancel", role="admin", press=[SetCB(sec="mig", act="do", key="cancel")],
         data=_mig_running, title="отменить переезд?"),
    Shot("adm.set.mig.cancel.yes", role="admin", press=[SetCB(sec="mig", act="do", key="cancel!")],
         data=_mig_running, title="переезд отменён — след и раздел"),
    Shot("adm.set.mig.finish.yes", role="admin", press=[SetCB(sec="mig", act="do", key="finish!")],
         data=_mig_finishing, title="переезд завершён — след, раздел, перезапуск"),
    Shot("adm.set.mig.orphans", role="admin", press=[SetCB(sec="mig", act="do", key="orphans")],
         data=_mig_cancelled, title="переехавшие после отмены"),
    Shot("adm.migration", role="admin", press=[Menu(action="migration")], data=_mig_running,
         title="обзор переезда"),
    Shot("adm.main.migration", role="admin", start="", data=_mig_running, title="главная во время переезда"),
]


# ── шаг 5: ветки, которые держали e2e — переезд ─────────────────────────────
OVERVIEW = [
    Shot("adm.migration.params", role="admin", press=[Menu(action="migration")], data=_mig_spread,
         title="обзор переезда: порт и ядро меняются, отстающие по убыванию остатка"),
    Shot("adm.migration.empty", role="admin", press=[Menu(action="migration")], data=_people,
         title="обзор переезда: переезд не настроен — переезжать некому"),
    Shot("adm.link.migration_cl.spread", role="admin", start=f"migration-{ADM}", data=_mig_spread,
         title="переезд профиля: последнее подключение прежней конфигурации, ещё не подключалось"),
    Shot("adm.link.migration_cl.missing", role="admin", start="migration-99", data=_mig_running,
         title="переезд профиля, которого нет — главная"),
]
