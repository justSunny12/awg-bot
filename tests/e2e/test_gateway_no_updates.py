"""Агент шлюза без своих «⬆️ Обновлений» (Р3): в «⚙️ Настройки» раздела нет,
в панели — ни кнопки «⬆️ vX», ни строки «⬆️ Доступна»; прежние кнопки
обновлений — «Кнопка устарела — обновления теперь в боте сервера AWG». Пока
идёт обновление по команде сервера — строка Т-А1, после неудачи — Т-А3.
"""
from __future__ import annotations

import pytest

from awgbot.bot import keyboards as kb
from awgbot.bot import roles, sections, texts
from awgbot.bot.callbacks import GwCB, UpdateCB
from awgbot.core import config
from awgbot.domain.gateway import GatewayServices, GwStatus
from awgbot.infra.db import Database
from awgbot.runtime import linkclient

pytestmark = pytest.mark.e2e


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROLE", "gateway")
    monkeypatch.setattr(linkclient, "enabled", lambda: False)
    d = Database(str(tmp_path / "gw.db"))
    d.init_schema()
    yield GatewayServices(d)
    d.close()


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def test_settings_have_no_updates_and_the_panel_no_update_button():
    assert "upd" not in roles.GATEWAY.settings_root
    labels = _labels(kb.gateway_panel_kb(lan=True, pending=1, broken=True))
    assert not any(label.startswith("⬆️") for label in labels), labels


async def test_old_update_buttons_say_where_updates_live_now(agent, fake_bot):
    """Прежние «⬆️ vX», «⬆️ Обновления» и кнопки уведомления агента —
    устаревшие: всплывашка называет, где теперь обновления; роутер разделов
    агента ловит их раньше общей «устаревшей кнопки»."""
    from aiogram import Router
    from tests.conftest import FakeCallback, FakeMessage
    router = Router()
    sections.updates.register_update_buttons(router, roles.GATEWAY)
    handlers = router.callback_query.handlers
    assert len(handlers) == 3, "у агента — «В меню» на итоге и «устаревшая кнопка» обновлений"
    for h in handlers[1:]:
        msg = FakeMessage(chat_id=config.ADMIN_ID, user_id=config.ADMIN_ID, bot=fake_bot)
        cb = FakeCallback(message=msg, user_id=config.ADMIN_ID, bot=fake_bot)
        await h.callback(cb, agent)
        assert cb.answers == [(sections.updates.AGENT_STALE, True)]
    assert sections.updates.AGENT_STALE == "Кнопка устарела — обновления теперь в боте сервера AWG"
    assert sections.resolve(roles.GATEWAY, GwCB(action="updates").pack()) is None
    assert UpdateCB(action="install", tag="v3.4.1").pack().startswith("upd:")


def test_the_panel_shows_the_command_while_it_runs_and_its_failure_after(agent):
    st = GwStatus(link_up=True, handshake_age=5.0, hostname="naspi")
    agent._upd_save({"id": "a1b2c3d4", "target": "v3.5.0", "prev": "v3.4.0", "state": "applying"})
    text = texts.gateway_panel(st, agent.upd_panel(), {"online": True, "role": "active"}, ())
    assert "⬆️ Обновляюсь до v3.5.0 по команде сервера AWG" in text
    agent._upd_save({"id": "a1b2c3d4", "target": "v3.5.0", "prev": "v3.4.0", "state": "rolled_back",
                     "error": "обновленный агент не вышел на связь за 10 минут"})
    upd = {**agent.upd_panel(), "server_bot": {"username": "awg_srv_bot"}}
    text = texts.gateway_panel(st, upd, {"online": True, "role": "active"}, ())
    assert ("⚠️ Обновление до v3.5.0 не удалось — агент вернулся на v3.4.0: обновленный агент не вышел на "
            'связь за 10 минут. Подробности — в чате <a href="https://t.me/awg_srv_bot">бота сервера AWG</a>') in text
    agent._upd_save({"id": "a1b2c3d4", "target": "v3.5.0", "prev": "v3.4.0", "state": "done"})
    text = texts.gateway_panel(st, agent.upd_panel(), {"online": True, "role": "active"}, ())
    assert "Обновл" not in text, "удачное обновление оставило строку в панели"


def test_a_command_that_never_started_says_the_agent_stayed(agent):
    """refused / failed — агент не обновлялся: «не выполнено, остался на»;
    «вернулся на» — только когда обновлённый агент не вышел на связь."""
    st = GwStatus(link_up=True, handshake_age=5.0, hostname="naspi")
    for state in ("refused", "failed"):
        agent._upd_save({"id": "a1b2c3d4", "target": "v3.5.0", "prev": "v3.4.0", "state": state,
                         "error": "GitHub не ответил"})
        text = texts.gateway_panel(st, agent.upd_panel(), {"online": True, "role": "active"}, ())
        assert ("⚠️ Обновление до v3.5.0 не выполнено — агент остался на v3.4.0: GitHub не ответил. "
                "Подробности — в чате бота сервера AWG") in text, state
        assert "вернулся" not in text
