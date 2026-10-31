"""Unit: кнопка «♻️ Восстановить» (bot/handlers/restore.run_restore) — двойное
нажатие и сброс диалога между вопросом и кнопкой.

  • флаг «восстановление идёт» ставится до первого await: два нажатия через
    gather запускают restore один раз, второе — «уже восстанавливаю»;
  • ранние выходы (другой файл, нет файла, замок установки) флаг снимают;
  • файл вопроса лежит на диске под отпечатком (restore_stash), в FSM — только
    отпечаток: state.clear() (другой диалог, /start) между вопросом и кнопкой
    восстановлению не мешает; отказ от вопроса и запуск файл убирают; файл
    старше часа — «нет файла».

Цена ошибки: aiogram ведёт апдейты параллельно — флаг после трёх await
пропускал оба нажатия, и `awg-bot restore` стартовал дважды поверх самого
себя; вопрос в данных FSM стирался любым другим диалогом, и кнопка отвечала
«нет файла» — админ присылал бэкап заново.
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from awgbot.bot import texts, ui
from awgbot.bot.handlers import restore as rs
from awgbot.core import config as cfg
from awgbot.domain.selfupdate import marks
from tests.conftest import FakeBot, FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.unit

FP = "aaaaaaaaaaaa"


@pytest.fixture()
def world(services, monkeypatch):
    monkeypatch.setattr(marks, "install_busy", lambda: False)
    launched = []
    services.prepare_restore_release = lambda plan: ""
    services.prepare_restore = lambda blob, plan=None: "/tmp/DUMMY"
    services.launch_restore = lambda path, release="": launched.append(path)
    services.restore_stash(FP, b"DUMMY", {"line": "same"}, "2026-10-05T14:00:00+03:00")
    return services, launched


def _cb():
    bot = FakeBot()
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=bot)
    return FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=bot), msg


async def test_two_taps_at_once_launch_the_restore_once(world):
    services, launched = world
    state = FakeState()
    await state.update_data(restore_fp=FP)
    slow = services.prepare_restore

    async def _press():
        cb, _ = _cb()
        await rs.run_restore(cb, services, state, FP)
        return cb

    def prepare(blob, plan=None):
        time.sleep(0.05)                                   # подготовка в потоке — второе нажатие успевает
        return slow(blob, plan)
    services.prepare_restore = prepare
    a, b = await asyncio.gather(_press(), _press())
    assert launched == ["/tmp/DUMMY"], f"восстановление запущено {len(launched)} раз(а)"
    toasts = [t[0] for cb in (a, b) for t in cb.answers if t[0]]
    assert toasts == [ui.toast(texts.RESTORE_ALREADY)], toasts


async def test_early_refusals_release_the_flag(world, monkeypatch):
    services, launched = world
    state = FakeState()
    await state.update_data(restore_fp=FP)
    cb, _ = _cb()
    await rs.run_restore(cb, services, state, "bbbbbbbbbbbb")        # другой файл
    assert cb.answers[-1][0] == ui.toast(texts.RESTORE_OTHER_FILE) and not services._restore_running
    monkeypatch.setattr(marks, "install_busy", lambda: True)
    await rs.run_restore(cb, services, state, FP)                    # замок установки
    assert not services._restore_running and launched == []
    monkeypatch.setattr(marks, "install_busy", lambda: False)
    services.restore_unstash(FP)
    await rs.run_restore(cb, services, state, FP)                    # файла нет
    assert cb.answers[-1][0] == ui.Toast.no_file and not services._restore_running and launched == []


async def test_a_cleared_dialog_between_the_question_and_the_button_does_not_lose_the_file(world):
    services, launched = world
    state = FakeState()
    await state.update_data(restore_fp=FP, restore_qmid=5)
    await state.clear()                                              # /start или другой диалог
    cb, msg = _cb()
    await rs.run_restore(cb, services, state, FP)                    # отпечаток — из кнопки
    assert launched == ["/tmp/DUMMY"], [t[0] for t in cb.answers]
    assert any(s[1] == texts.RESTORE_STARTED for s in msg.sent if s[0] == "answer")
    assert services.restore_stashed(FP) is None, "запущенный файл остался на диске"


async def test_the_stash_is_private_short_lived_and_dropped_with_the_question(world):
    services, _ = world
    d = services._restore_stash_dir()
    assert oct(os.stat(d / f"pending-{FP}.bin").st_mode & 0o777) == "0o600"
    got = services.restore_stashed(FP)
    assert got["blob"] == b"DUMMY" and got["plan"] == {"line": "same"}
    services.restore_stash("cccccccccccc", b"NEW", {}, "")
    assert services.restore_stashed(FP) is None, "прежний вопрос не убран при новом"
    old = time.time() - services.RESTORE_STASH_TTL - 5
    os.utime(d / "pending-cccccccccccc.bin", (old, old))
    assert services.restore_stashed("cccccccccccc") is None and not (d / "pending-cccccccccccc.bin").exists()

    services.restore_stash(FP, b"DUMMY", {}, "")
    state = FakeState()
    await state.update_data(restore_fp=FP)

    class _Hooks:
        async def main(self, services, chat_id):
            return None
    cb, _ = _cb()
    await rs.drop_restore(cb, services, state, _Hooks())
    assert services.restore_stashed(FP) is None and await state.get_data() == {}
