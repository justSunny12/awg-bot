"""Хендлеры агента шлюза: подтверждения и приём бандла."""
from __future__ import annotations

import base64
import os

import pytest

import awgbot.core.config as cfg
from awgbot.bot.callbacks import GwCB
from awgbot.bot.handlers import gateway as gh
from awgbot.domain.gateway import GatewayServices, GwStatus
from awgbot.infra.db import Database
from awgbot.util import bundlecrypt as bc
from tests.conftest import FakeBot, FakeCallback, FakeMessage, FakeState


class _Svc(GatewayServices):
    def __init__(self, db):
        super().__init__(db)
        self.restarted = 0
        self.applied: list[bytes] = []

    def status(self):
        return GwStatus(link_up=True, handshake_age=5.0)

    def restart_link(self):
        self.restarted += 1
        return True, "поднят"

    def apply_bundle(self, blob):
        self.applied.append(blob)
        return True, "Готово"


@pytest.fixture()
def svc(tmp_path):
    d = Database(tmp_path / "gw.db"); d.init_schema()
    return _Svc(d)


class _Doc:
    def __init__(self, size): self.file_size = size; self.file_id = "F"


class _DlBot(FakeBot):
    def __init__(self, blob): super().__init__(); self.blob = blob
    async def download(self, doc, destination=None):
        destination.write(self.blob)


async def test_restart_needs_confirmation(svc, fake_bot):
    """Кнопка «Рестарт линка» сама ничего не рвёт — только показывает цену."""
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_confirm(cb, GwCB(action="restart"), svc)
    assert svc.restarted == 0
    assert any("оборвётся" in t for kind, t, _ in msg.sent if kind == "edit_text")

    await gh.gw_execute(cb, GwCB(action="restart!"), svc)
    assert svc.restarted == 1
    edits = [t for kind, t, _ in msg.sent if kind == "edit_text"]
    assert "Рестарт линка: готово" in edits[-1]


async def test_foreign_document_is_refused_before_anything(svc):
    bot = _DlBot(b"#!/bin/sh\nrm -rf /\n")
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=bot)
    msg.document = _Doc(100)
    state = FakeState()
    await gh.gw_bundle_document(msg, svc, state)
    assert "не принят" in msg.sent[-1][1]
    assert "bundle" not in (await state.get_data())


async def test_our_bundle_waits_for_confirmation_then_applies(svc):
    priv = base64.b64encode(os.urandom(32)).decode()
    blob = bc.encrypt(b"#__GW_SETUP_BELOW__\n__LINK_CONF_EOF__\n", priv)
    bot = _DlBot(blob)
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=bot)
    msg.document = _Doc(len(blob))
    state = FakeState()
    await gh.gw_bundle_document(msg, svc, state)
    assert svc.applied == [], "применили без подтверждения"
    assert msg.sent[-1][2] is not None, "нет кнопок подтверждения"

    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=bot)
    await gh.gw_bundle_apply(cb, svc, state)
    assert svc.applied == [blob]
    assert (await state.get_data()) == {}, "бандл остался в памяти после применения"


async def test_oversized_document_is_not_downloaded(svc):
    class NoDl(FakeBot):
        async def download(self, *a, **k):
            raise AssertionError("скачали то, что заведомо не бандл")
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=NoDl())
    msg.document = _Doc(50 * 1024 * 1024)
    await gh.gw_bundle_document(msg, svc, FakeState())
    assert "не принят" in msg.sent[-1][1]


async def test_gateway_update_install_runs_the_shared_updater(svc, fake_bot, monkeypatch):
    """«Обновить» у агента: следующая ступень → «дождись» → apply_update. Та же
    механика, что у клиентской роли, — sha256 и запуск вне cgroup внутри."""
    import types
    from awgbot.bot.callbacks import UpdateCB
    nxt = types.SimpleNamespace(tag="v9.9.9", body="")
    applied = []
    monkeypatch.setattr(svc, "update_next", lambda: nxt)
    monkeypatch.setattr(svc, "apply_update", lambda r: applied.append(r.tag))
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_update_install(cb, svc)
    assert applied == ["v9.9.9"]
    assert svc.db.get_state("update_wait"), "«дождись» не запомнено для нового процесса"


async def test_start_removes_all_previous_menus(svc, fake_bot):
    """Повторный /start убирает ВСЕ прошлые меню, а не только снимает кнопки с
    последнего: /start — «начать заново», и стопка мёртвых панелей над живой
    заставляла бы листать историю."""
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_start(msg, svc, FakeState())
    await gh.gw_start(msg, svc, FakeState())
    first_ids = [m for k, chat, m in
                 [(r[0], r[1], r[2]) for r in fake_bot.records if r[0] == "delete_message"]]
    assert first_ids, "прошлое меню не удалено при повторном /start"
    await gh.gw_start(msg, svc, FakeState())
    deleted = [r[2] for r in fake_bot.records if r[0] == "delete_message"]
    assert len(deleted) >= 2, "удаляется не всё прошлое"
