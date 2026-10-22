"""Агент шлюза о неудачной команде вне хода: команда `upd` несёт tries (число
прошлых неудач догона), агент хранит его и в уведомлении и строке панели
говорит, когда сервер AWG повторит — «через 30 минут / 2 часа / 6 часов» —
или что повторов больше не будет и где «🔁 Повторить». Команда хода (fleet)
— прежний текст «Подробности — в чате …».

Цена ошибки: после «не выполнено» в чате бота шлюза админ не знает, ждать
ли сервер или идти повторять руками.
"""
from __future__ import annotations

import pytest

from awgbot.bot import texts
from awgbot.core import config
from awgbot.domain.gateway import GatewayServices, GwStatus
from awgbot.domain.notices.fleet import agent_update_failed
from awgbot.infra.db import Database
from awgbot.runtime import linkclient

pytestmark = pytest.mark.e2e

SRV = {"username": "awg_srv_bot"}
SRV_LINK = '<a href="https://t.me/awg_srv_bot">бота сервера AWG</a>'


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROLE", "gateway")
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.4.0")
    monkeypatch.setattr(linkclient, "enabled", lambda: False)
    d = Database(str(tmp_path / "gw.db"))
    d.init_schema()
    yield GatewayServices(d)
    d.close()


def _cmd(**kw) -> dict:
    return {"id": "a1b2c3d4", "target": "v3.4.1", "why": "catchup", **kw}


# ── приём tries ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("given, kept", [(2, 2), ("мусор", 0), (50, 9), (None, 0), (-3, 0), ("1", 1)])
def test_upd_in_keeps_tries_within_zero_to_nine(agent, given, kept):
    assert agent.upd_in(_cmd(tries=given)) == "new"
    assert agent.upd_cmd()["tries"] == kept, f"tries={given!r} сохранён как {agent.upd_cmd()['tries']!r}"


def test_a_command_without_tries_is_the_first_attempt(agent):
    assert agent.upd_in(_cmd()) == "new"
    assert agent.upd_cmd()["tries"] == 0


def test_the_panel_hands_why_and_tries_over_after_a_failure(agent):
    agent.upd_in(_cmd(tries=2))
    agent._upd_state("failed", error="GitHub не ответил")
    panel = agent.upd_panel()
    assert panel["state"] == "failed" and panel["how"] == "failed", panel
    assert (panel["why"], panel["tries"]) == ("catchup", 2), panel
    agent._upd_save({**agent.upd_cmd(), "state": "applying"})
    assert "tries" not in agent.upd_panel(), "строка хода несёт лишнее"


# ── тексты ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("why", ["catchup", "follow"])
@pytest.mark.parametrize("tries, words", [(0, "30 минут"), (1, "2 часа"), (2, "6 часов")])
def test_outside_a_run_the_text_says_when_the_server_retries(why, tries, words):
    text = agent_update_failed("v3.4.1", "v3.4.0", "GitHub не ответил", SRV, state="failed", why=why, tries=tries)
    assert text == ("⚠️ Обновление до v3.4.1 не выполнено — агент остался на v3.4.0: GitHub не ответил. "
                    f"Сервер AWG повторит попытку через {words}. Подробности — в чате {SRV_LINK}"), text


@pytest.mark.parametrize("why", ["catchup", "follow"])
def test_after_three_failures_the_text_says_no_more_retries_and_where_the_button_is(why):
    text = agent_update_failed("v3.4.1", "v3.4.0", "GitHub не ответил", SRV, state="rolled_back", why=why, tries=3)
    assert text == ("⚠️ Обновление до v3.4.1 не удалось — агент вернулся на v3.4.0: GitHub не ответил. "
                    f"Повторов больше не будет — «🔁 Повторить» в разделе обновлений {SRV_LINK}"), text
    assert "повторит попытку" not in text
    plain = agent_update_failed("v3.4.1", "v3.4.0", "x", None, state="failed", why=why, tries=9)
    assert plain.endswith("«🔁 Повторить» в разделе обновлений бота сервера AWG"), plain


def test_a_run_command_keeps_the_old_text_whatever_tries_says():
    """Неудача шага хода — откат набора: о повторе говорит итог хода в чате
    сервера, агенту сказать нечего."""
    for tries in (0, 3):
        text = agent_update_failed("v3.4.1", "v3.4.0", "GitHub не ответил", SRV, state="failed", why="fleet",
                                   tries=tries)
        assert text == ("⚠️ Обновление до v3.4.1 не выполнено — агент остался на v3.4.0: GitHub не ответил. "
                        f"Подробности — в чате {SRV_LINK}"), text
    assert "повтор" not in agent_update_failed("v3.4.1", "v3.4.0", "x", SRV, state="failed", why="", tries=1)


# ── панель ───────────────────────────────────────────────────────────────────

def test_the_panel_line_after_a_failed_follow_says_the_server_retries_in_30_minutes(agent):
    st = GwStatus(link_up=True, handshake_age=5.0, hostname="gw")
    agent.upd_in(_cmd(why="follow", tries=0))
    agent._upd_state("failed", error="GitHub не ответил")
    upd = {**agent.upd_panel(), "server_bot": SRV}
    text = texts.gateway_panel(st, upd, {"online": True, "role": "active"}, ())
    assert ("⚠️ Обновление до v3.4.1 не выполнено — агент остался на v3.4.0: GitHub не ответил. "
            f"Сервер AWG повторит попытку через 30 минут. Подробности — в чате {SRV_LINK}") in text, text


def test_the_panel_line_counts_past_failures_and_the_end_of_retries(agent):
    st = GwStatus(link_up=True, handshake_age=5.0, hostname="gw")
    agent.upd_in(_cmd(why="catchup", tries=2))
    agent._upd_state("failed", error="GitHub не ответил")
    text = texts.gateway_panel(st, agent.upd_panel(), {"online": True, "role": "active"}, ())
    assert "Сервер AWG повторит попытку через 6 часов" in text, text
    agent._upd_save({})
    agent.upd_in(_cmd(id="b2c3d4e5", why="catchup", tries=3))
    agent._upd_state("failed", error="GitHub не ответил")
    text = texts.gateway_panel(st, agent.upd_panel(), {"online": True, "role": "active"}, ())
    assert "Повторов больше не будет — «🔁 Повторить» в разделе обновлений бота сервера AWG" in text, text
