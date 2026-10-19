"""Сопряжение со стороны агента шлюза (domain/gateway/join.py, tools/join.py):
файл сопряжения, эфемерный ключ и код, секреты после кода, отказ сервера,
правило первого аплинка, конец сопряжения; терминал `awg-bot join` — рамка
кода и ход (Т-Т2…Т-Т8).

Цена ошибки: код на экране шлюза, который не совпадает с кодом сервера (не
подключиться никогда); ADMIN_ID чужого сервера в env агента; hello с полем
join после кода (сервер снова откроет сессию сопряжения на постоянных
ключах); отметка «установлен» раньше конца сопряжения.
"""
from __future__ import annotations

import base64
import json
import time

import pytest

from awgbot.domain.gateway.join import GatewayJoinMixin
from awgbot.util import joinsas, jointoken

pytestmark = pytest.mark.unit

JID = "0123456789abcdef"


class _Agent(GatewayJoinMixin):
    def __init__(self, tmp_path):
        self._join_file = str(tmp_path / "join.json")
        self._join_status_file = str(tmp_path / "join.status")
        self.env = tmp_path / "env"
        self.first = []

    def _cfg_env_path(self):
        return str(self.env)

    def first_start_note(self, fresh: bool) -> None:
        self.first.append(fresh)

    def status(self) -> dict:
        with open(self._join_status_file, encoding="utf-8") as f:
            return json.load(f)


@pytest.fixture()
def agent(tmp_path):
    a = _Agent(tmp_path)
    a._join_save({"id": JID, "slot": 2, "version": "3.4.0", "expires": int(time.time()) + 3600,
                  "channel_port": 8787, "state": "pairing", "name": ""})
    return a


def _server_side(agent_hello: dict) -> tuple[dict, str, bytes]:
    """Сервер на hello сопряжения: свой эфемерный ключ, код, ключ сопряжения."""
    g_pub = base64.urlsafe_b64decode(agent_hello["eph"] + "==")
    s_priv, s_pub = joinsas.new_pair()
    k = joinsas.shared(s_priv, g_pub)
    body = {"eph": base64.urlsafe_b64encode(s_pub).decode().rstrip("="), "slot_name": "Шлюз 2", "slot": 2}
    return body, joinsas.code(JID, 2, g_pub, s_pub, k), joinsas.pair_key(JID, k)


def test_both_sides_show_one_code(agent, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "raspberrypi.local")
    hello = agent.join_hello()
    assert hello["join"] == JID and hello["host"] == "raspberrypi"
    body, code, _kp = _server_side(hello)
    assert agent.join_eph_in(body) == code
    st = agent.status()
    assert st["state"] == "code" and st["code"] == joinsas.show(code)
    assert agent.join_state()["name"] == "Шлюз 2", "имя слота из join_eph не запомнено"
    second = agent.join_hello()
    assert second["eph"] != hello["eph"], "эфемерный ключ не новый на новую сессию"


def test_a_strange_hostname_is_not_sent(agent, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "bad host;name")
    assert "host" not in agent.join_hello()


def test_the_secrets_after_the_code_write_only_the_admin_id(agent):
    hello = agent.join_hello()
    body, _code, kp = _server_side(hello)
    agent.join_eph_in(body)
    agent.env.write_text("GW_BOT_TOKEN=x\nADMIN_ID=1\n", encoding="utf-8")
    box = joinsas.seal(kp, {"admin_id": 777000111, "server_bot": "awg_srv_bot", "slot": 2, "name": "Шлюз 2"})
    assert agent.join_ok_in({"box": box})
    lines = agent.env.read_text(encoding="utf-8").splitlines()
    assert lines.count("ADMIN_ID=777000111") == 1 and "ADMIN_ID=1" not in lines and "GW_BOT_TOKEN=x" in lines
    assert agent.join_state()["state"] == "accepted" and agent.status()["state"] == "accepted"
    assert agent.join_hello() == {}, "после кода hello снова с join"
    assert agent.join_fresh_uplink() and agent.join_active()


def test_secrets_under_another_key_are_refused(agent):
    agent.join_eph_in(_server_side(agent.join_hello())[0])
    box = joinsas.seal(bytes(32), {"admin_id": 777000111})
    assert agent.join_ok_in({"box": box}) is False
    assert not agent.env.exists() and agent.join_state()["state"] == "pairing"
    assert not agent.join_fresh_uplink(), "правило fresh до кода"


def test_wrong_and_refused(agent):
    agent.join_eph_in(_server_side(agent.join_hello())[0])
    code = agent.status()["code"]
    agent.join_wrong_in({"left": 2})
    st = agent.status()
    assert (st["state"], st["left"], st["code"]) == ("wrong", 2, code), "код пропал с экрана после неверного"
    agent.join_refused_in({"why": "code; rm"})
    assert agent.join_state()["state"] == "refused" and agent.status()["why"] == "coderm"
    assert not agent.join_active()


def test_the_end_of_pairing_marks_the_first_start_and_removes_the_file(agent, tmp_path):
    st = agent.join_state()
    st["state"] = "linked"
    agent._join_save(st)
    assert agent.join_finish_start("pi2_gw_bot") is True
    assert agent.first == [True], "отметка первого старта не от конца сопряжения"
    assert not (tmp_path / "join.json").exists()
    assert agent.status()["state"] == "done" and agent.status()["bot"] == "pi2_gw_bot"
    assert agent.join_finish_start("pi2_gw_bot") is False, "второй старт — снова «установлен»"


# ── терминал: awg-bot join ───────────────────────────────────────────────────

def _token(**kw) -> str:
    base = dict(slot=2, version="3.4.0", join_id=JID, expires=int(time.time()) + 3600,
                host="vpn-server.example.org", link_port=8443, channel_port=8787, gw_addr="10.99.99.6",
                vps_pub=bytes(32), gw_priv=bytes(range(32)), jc=4, jmin=40, jmax=70, s=(10, 20, 0, 0),
                h=((100, 200), (300, 400), (500, 600), (700, 800)))
    base.update(kw)
    return jointoken.encode(jointoken.JoinToken(**base))


def test_a_broken_or_expired_command_says_so(capsys):
    from tools import join as tjoin
    assert tjoin.cmd_join("curl … --join " + _token()[:-6]) == 1
    assert "команда подключения повреждена" in capsys.readouterr().err
    assert tjoin.cmd_join(_token(expires=int(time.time()) - 5)) == 1
    assert "выпусти новую" in capsys.readouterr().err


def test_the_terminal_follows_the_pairing(tmp_path, monkeypatch, capsys):
    """Т-Т3 рамка кода, Т-Т4 неверный, Т-Т5 после кода, Т-Т6/Т-Т8 — отказ."""
    from tools import join as tjoin
    status = {}
    monkeypatch.setattr(tjoin, "_status", lambda: dict(status))
    status.update(state="code", code="K7F-2QX")
    assert tjoin._follow(int(time.time()) + 3600, once=True) == 0
    out = capsys.readouterr().out
    assert "Код подтверждения:   K7F-2QX" in out and "«✏️ Ввести код»" in out and "awg-bot join --status" in out
    status.update(state="wrong", left=2)
    tjoin._follow(int(time.time()) + 3600, once=True)
    assert "осталось попыток: 2" in capsys.readouterr().out
    status.clear()
    status.update(state="refused", why="code")
    assert tjoin._follow(int(time.time()) + 3600, once=True) == 1
    assert "код не подтверждён трижды" in capsys.readouterr().err
    status.clear()
    status.update(state="done", bot="pi2_gw_bot", name="Шлюз 2")
    assert tjoin._follow(int(time.time()) + 3600) == 0
    assert "Готово: шлюз «Шлюз 2» подключён. Открой бота шлюза @pi2_gw_bot" in capsys.readouterr().out
