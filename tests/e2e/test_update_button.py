"""«⬆️ Обновить» несёт показанный тег (UpdateCB.tag): ставится только он.
Цель пересчитана другой или кнопка прежнего образца без тега — всплывашка
«цель изменилась», установки нет; допустимой цели не осталось, а выше ждёт
другая сторона — «Нельзя: … ждёт …»; сеть не ответила — своя всплывашка, не
«обновлять не на что»; окно после старта — «жду связи». Кнопка в живом меню
после отказа перерисовывает раздел из кэша."""
from __future__ import annotations

import types

import pytest

import awgbot.core.config as cfg
from awgbot.bot import texts
from awgbot.bot.callbacks import UpdateCB
from awgbot.bot.handlers import gateway as gh
from awgbot.domain.gateway import GatewayServices
from awgbot.infra import updates
from awgbot.infra.db import Database
from tests.conftest import FakeCallback, FakeMessage


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "ROLE", "gateway")
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.3.0")
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    s = GatewayServices(d)
    s.applied = []
    monkeypatch.setattr(s, "apply_update", lambda r: s.applied.append(r.tag))
    yield s
    d.close()


def _rel(tag):
    return types.SimpleNamespace(tag=tag, body="", awg_generation=lambda: 0)


def _press(fake_bot, svc, tag: str | None, *, live: bool = False):
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    if live:
        svc.db.nav_touch(cfg.ADMIN_ID, msg.message_id)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    data = UpdateCB(action="install", tag=tag) if tag is not None else UpdateCB.unpack("upd:install")
    return cb, msg, gh.gw_update_install(cb, svc, data)


async def test_the_shown_target_is_installed(svc, fake_bot, monkeypatch):
    monkeypatch.setattr(svc, "update_next", lambda: _rel("v3.3.1"))
    cb, _, run = _press(fake_bot, svc, "v3.3.1")
    await run
    assert svc.applied == ["v3.3.1"] and cb.answers[0][0] == texts.UPDATE_STARTING


async def test_another_target_is_not_installed(svc, fake_bot, monkeypatch):
    monkeypatch.setattr(svc, "update_next", lambda: _rel("v3.3.2"))
    cb, msg, run = _press(fake_bot, svc, "v3.3.1")
    await run
    assert svc.applied == [] and cb.answers == [(texts.UPDATE_TARGET_CHANGED_TOAST, True)]
    assert not msg.sent, "кнопка не в живом меню — раздел не рисуется поверх уведомления"


async def test_an_old_button_without_a_tag_installs_nothing(svc, fake_bot, monkeypatch):
    assert UpdateCB.unpack("upd:install") == UpdateCB(action="install"), "кнопки прежних сообщений разбираются"
    assert UpdateCB(action="menu").pack() == "upd:menu", "данные кнопок без тега — прежние"
    monkeypatch.setattr(svc, "update_next", lambda: _rel("v3.3.1"))
    cb, _, run = _press(fake_bot, svc, None)
    await run
    assert svc.applied == [] and cb.answers == [(texts.UPDATE_TARGET_CHANGED_TOAST, True)]


async def test_a_closed_condition_says_what_waits(svc, fake_bot, monkeypatch):
    from awgbot.domain.selfupdate import UpdatePeer
    monkeypatch.setattr(svc, "update_next", lambda: None)
    monkeypatch.setattr(svc, "update_waiting", lambda: {
        "tag": "v3.3.2", "need": "3.3.1", "role": "gateway",
        "peers": [UpdatePeer(slot=0, name="", version="3.3.0", fresh=True)]})
    cb, msg, run = _press(fake_bot, svc, "v3.3.2", live=True)
    await run
    assert svc.applied == [] and cb.answers[0] == ("⛔ Обновление сейчас недоступно: v3.3.2 ждёт сервер AWG v3.3.1", True)
    assert msg.sent and msg.sent[-1][0] == "edit_text" and "Обновления" in msg.sent[-1][1], \
        "в живом меню раздел перерисован из кэша"


async def test_a_network_failure_is_not_nothing_to_update(svc, fake_bot, monkeypatch):
    def boom(**_):
        raise updates.UpdateError("GitHub не ответил")
    monkeypatch.setattr(updates, "list_releases", boom)
    cb, _, run = _press(fake_bot, svc, "v3.3.1")
    await run
    assert svc.applied == [] and cb.answers == [(texts.UPDATE_CHECK_FAILED_TOAST, True)]


async def test_the_window_after_start_answers_with_the_wait(svc, fake_bot, monkeypatch):
    svc.update_hold_start()
    calls = []
    monkeypatch.setattr(svc, "update_next", lambda: calls.append(1))
    cb, _, run = _press(fake_bot, svc, "v3.3.1")
    await run
    assert calls == [] and svc.applied == []
    text, alert = cb.answers[0]
    assert alert and text.startswith("⏳ Жду связи с сервером AWG после перезапуска — до ")


async def test_a_changed_target_in_the_live_menu_redraws_the_section(svc, fake_bot, monkeypatch):
    """«Цель изменилась» на кнопке раздела (живое меню): раздел тут же
    перерисован из кэша — с той целью, что теперь правда."""
    monkeypatch.setattr(svc, "update_next", lambda: _rel("v3.3.2"))
    svc.db.set_state(svc._AVAILABLE_KEY, "v3.3.2")
    cb, msg, run = _press(fake_bot, svc, "v3.3.1", live=True)
    await run
    assert svc.applied == [] and cb.answers == [(texts.UPDATE_TARGET_CHANGED_TOAST, True)]
    kind, text, markup = msg.sent[-1]
    assert kind == "edit_text" and "v3.3.0 → v3.3.2" in text
    assert [b.callback_data for row in markup.inline_keyboard for b in row][0] == "upd:install:v3.3.2"


async def test_the_cached_section_still_says_a_migration_blocks(svc, fake_bot, monkeypatch):
    """Ветка «cached» (тумблер, цикл — без сети) не прячет блок переездом:
    причина — по метаданным из сохранённого списка."""
    from awgbot.bot import roles
    from awgbot.bot.sections import updates as sec
    rel = updates.Release(tag="v3.3.1", version=(3, 3, 1), body="", asset_url="u", sha256="x",
                          meta={"schema": 1, "version": "3.3.1", "awg_gen": 2, "roles": {"gw": {"requires": "3.3.0"}}})
    svc._update_store([rel], {})
    svc.db.set_state(svc._AVAILABLE_KEY, "v3.3.1")
    monkeypatch.setattr(svc, "update_block_reason", lambda r=None: "идёт переезд" if r is not None else "")
    text, markup = await sec.screen(roles.GATEWAY, svc, "cached")
    assert "⛔ Обновление до v3.3.1 сейчас недоступно: идёт переезд" in text
    assert not any("upd:install" in (b.callback_data or "") for row in markup.inline_keyboard for b in row)
