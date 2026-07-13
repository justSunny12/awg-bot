"""E2E: остаток задач scheduler — job_monitor (telegram/tunnel + swallow),
job_backup (реальная отправка), job_purge_history.
"""

import pytest

from awgbot.runtime.scheduler import setup_scheduler
from awgbot.core import config
from awgbot.util import timeutil

pytestmark = pytest.mark.e2e


def _jobs(services, bot):
    sched = setup_scheduler(services, bot, services.db)
    return {jid: sched.get_job(jid).func
            for jid in ("poll", "expiry", "monthly", "backup", "monitor", "purge_history")}


class _Watcher:
    def __init__(self, alive=True):
        self._alive = alive
        self.rebound = False

    def alive(self):
        return self._alive

    def ensure_watching(self):
        self.rebound = True


# ── job_monitor: статус + ребайнд вотчдога + локальные метрики ───────────────
async def test_monitor_tunnel_status_transition_and_rebind(services, fake_bot, monkeypatch):
    monkeypatch.setattr(services, "detect_and_handle_restart", lambda: False)
    monkeypatch.setattr(services, "server_ok", lambda: False)
    services.db.set_state("last_server_ok", "1")            # был 🟢 → станет 🔴 (переход)
    watcher = _Watcher(alive=False)                        # мёртв → монитор ребайндит
    sched = setup_scheduler(services, fake_bot, services.db, watcher=watcher)
    await sched.get_job("monitor").func()
    assert watcher.rebound is True
    assert any(r[0] == "send_message" for r in fake_bot.records)   # уведомление о падении


async def test_monitor_swallows_errors(services, fake_bot, monkeypatch):
    monkeypatch.setattr(services, "detect_and_handle_restart",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    await _jobs(services, fake_bot)["monitor"]()           # не должно поднять исключение


# ── job_backup: реальная отправка файлов ─────────────────────────────────────
async def test_job_backup_sends_documents(services, fake_bot, monkeypatch, tmp_path):
    ym = timeutil.now().strftime("%Y-%m")
    services.db.set_state("last_backup", "2020-01")        # прошлый месяц → бэкап нужен
    f = tmp_path / "b.db"
    f.write_bytes(b"x")
    monkeypatch.setattr(services, "make_backup", lambda: [str(f)])
    sent_docs = []

    async def _send_doc(chat_id, document, **kw):
        sent_docs.append(chat_id)
    fake_bot.send_document = _send_doc                     # FakeBot не имеет метода — добавим
    await _jobs(services, fake_bot)["backup"]()
    assert sent_docs == [config.ADMIN_ID]
    assert services.db.get_state("last_backup") == ym      # метка обновлена


# ── job_purge_history ────────────────────────────────────────────────────────
async def test_job_purge_history_runs(services, fake_bot, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(services, "purge_old_history",
                        lambda: called.__setitem__("n", called["n"] + 1) or {"clients": 0})
    await _jobs(services, fake_bot)["purge_history"]()
    assert called["n"] == 1


async def test_job_purge_history_swallows(services, fake_bot, monkeypatch):
    monkeypatch.setattr(services, "purge_old_history",
                        lambda: (_ for _ in ()).throw(RuntimeError("db locked")))
    await _jobs(services, fake_bot)["purge_history"]()      # не поднимает
