"""
Кнопки снятого раздела «💾 Бэкапы» агента в сообщениях прежних версий —
через настоящий диспетчер агента: ни один экран их не берёт, отвечает
обработчик устаревшей кнопки («Кнопка устарела — открыл меню») с панелью;
бэкап в чате бота шлюза — один ответ, сообщение убрано.

Цена ошибки: кнопка, которую не взял никто, висит со спиннером; кнопка,
которую взял общий раздел, открыла бы у шлюза раздел копий, которых нет.
"""
from __future__ import annotations

import pytest

from awgbot.bot import texts
from awgbot.bot.callbacks import GwCB
from awgbot.bot.handlers.stale import STALE_BUTTON
from tests.screens import harness
from tests.screens.shots_gateway_agent import _shot

pytestmark = pytest.mark.e2e


def _stale(rec):
    return [c.head for c in rec.everything if STALE_BUTTON in (c.toast or "") or STALE_BUTTON in (c.body or "")]


@pytest.mark.parametrize("cb", [GwCB(action="backup"), GwCB(action="enc"), GwCB(action="enc_set"),
                                GwCB(action="backup!"), GwCB(action="restore!"), GwCB(action="restore_drop"),
                                GwCB(action="tgl", val="app.scheduler.backup_enabled"),
                                GwCB(action="cyc", val="app.scheduler.backup_channel"),
                                GwCB(action="edit", val="backup_when"), GwCB(action="backup/open")],
                         ids=lambda c: c.pack())
async def test_old_backup_buttons_are_stale(tmp_path, cb):
    rec = await harness.take(_shot(f"gw.retired.{cb.action.replace('/', '_')}", press=[cb]), tmp_path)
    assert _stale(rec), [c.head for c in rec.everything]


async def test_the_settings_root_has_no_backups(tmp_path):
    rec = await harness.take(_shot("gw.retired.root", press=[GwCB(action="settings")]), tmp_path)
    labels = [b.text for c in rec.calls if c.markup is not None for r in c.markup.inline_keyboard for b in r]
    assert labels and not [x for x in labels if "Бэкап" in x], labels


async def test_a_backup_file_is_refused_and_removed(tmp_path):
    shot = _shot("gw.retired.file", steps=[("file", "awg-bot-backup-gw-20260914_030000.tgz.enc", b"DUMMY")])
    rec = await harness.take(shot, tmp_path)
    bodies = [c.body for c in rec.calls if c.body]
    assert texts.GW_OLD_BACKUP_REFUSED in bodies, bodies
