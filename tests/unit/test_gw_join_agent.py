"""Сопряжение со стороны агента шлюза (domain/gateway/join.py, tools/join.py):
файл сопряжения, эфемерный ключ и код, секреты после кода, отказ сервера,
правило первого аплинка, конец сопряжения; терминал `awg-bot join` — рамка
кода и ход.

Цена ошибки: код на экране шлюза, который не совпадает с кодом сервера (не
подключиться никогда); ADMIN_ID чужого сервера в env агента; hello с полем
join после кода (сервер снова откроет сессию сопряжения на постоянных
ключах); отметка «установлен» раньше конца сопряжения.
"""
from __future__ import annotations

import base64
import json
import os
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

    def installed_report_done(self) -> None:
        self.first.append("installed-sent")

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
    assert second["eph"] == hello["eph"], "пара сменилась до join_ok — сервер после кода её не примет"
    assert len(bytes.fromhex(agent.join_state()["eph_priv"])) == 32
    import os
    assert os.stat(agent._join_file).st_mode & 0o777 == 0o600, "закрытый эфемерный ключ — не 0600"


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
    # join-1: пока линк на ключах токена, hello — с join и тем же эфемерным
    # ключом (сервер повторит join_ok и link_next); повтор join_ok — не отказ
    again = agent.join_hello()
    assert again.get("join") == JID and again["eph"] == hello["eph"], again
    assert agent.join_ok_in({"box": box}) and lines == agent.env.read_text(encoding="utf-8").splitlines()
    agent.rekey_state = lambda: {"state": "switched"}
    assert agent.join_hello() == {}, "на новых ключах hello с join — сервер не зафиксировал бы смену"
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
    assert agent.first == [True, "installed-sent"], \
        "отметка первого старта не от конца сопряжения или ушло бы второе «установлен»"
    assert agent.status()["state"] == "done" and agent.status()["bot"] == "pi2_gw_bot"
    assert agent.join_done_pending() == {"id": JID}, "итог сопряжения серверу не уйдёт"
    assert not agent.join_active() and agent.join_hello() == {}
    assert agent.join_finish_start("pi2_gw_bot") is False, "второй старт — снова конец сопряжения"
    agent.join_done_sent()
    assert not (tmp_path / "join.json").exists() and agent.join_done_pending() == {}


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
    """Рамка кода, неверный код, ход после кода, отказ сервера."""
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


def test_status_before_the_first_answer_is_not_silent(monkeypatch, capsys):
    """flow-7: `join --status` до ответа сервера и без хода вовсе — строкой."""
    from tools import join as tjoin
    monkeypatch.setattr(tjoin, "_status", lambda: {"state": "waiting"})
    assert tjoin._follow(int(time.time()) + 3600, once=True) == 0
    assert "жду ответа до" in capsys.readouterr().out
    monkeypatch.setattr(tjoin, "_status", lambda: {})
    monkeypatch.setattr("awgbot.domain.gateway.join.JOIN_FILE", "/nonexistent/join.json")
    assert tjoin.cmd_status() == 1
    assert "подключения командой нет" in capsys.readouterr().out


@pytest.fixture()
def joinrun(tmp_path, monkeypatch):
    """cmd_join без хоста: файл сопряжения во временном каталоге, скрипт
    обвязки и systemctl — подставные."""
    from awgbot.domain.gateway import join as jmod
    from awgbot.infra import gwguard
    from tools import join as tjoin
    jf = tmp_path / "awg-gw" / "join.json"
    monkeypatch.setattr(jmod, "JOIN_FILE", str(jf))
    monkeypatch.setattr(jmod, "STATUS_FILE", str(tmp_path / "join.status"))
    calls = []
    monkeypatch.setattr(gwguard, "join_link", lambda conf, port, script="": (calls.append("link"), (True, ""))[1])
    rc = {"restart": 0}

    def run(argv, **kw):
        calls.append(" ".join(argv))
        return type("P", (), {"returncode": rc["restart"], "stdout": b"", "stderr": b""})()
    monkeypatch.setattr(tjoin.subprocess, "run", run)
    monkeypatch.setattr(tjoin, "_follow", lambda expires, once=False: (calls.append("follow"), 0)[1])
    return tjoin, jf, calls, rc


def test_a_failed_agent_restart_is_said(joinrun, capsys):
    """flow-10: агент не перезапустился — не «жду ответа» вечно, а отказ."""
    tjoin, _jf, calls, rc = joinrun
    rc["restart"] = 1
    assert tjoin.cmd_join(_token(expires=int(time.time()) + 3600)) == 1
    assert "агент awg-bot не перезапустился" in capsys.readouterr().err and "follow" not in calls


def test_the_same_command_again_only_follows(joinrun):
    """flow-11: та же команда ещё идёт (код на экране, или уже после кода) —
    линк и агент не трогаются, только ход; новая — заново."""
    tjoin, jf, calls, _rc = joinrun
    tok = _token(expires=int(time.time()) + 3600)
    jf.parent.mkdir(parents=True)
    from awgbot.domain.gateway import join as jmod
    for state, status in (("pairing", {"state": "code"}), ("done", {}), ("accepted", {})):
        jf.write_text(json.dumps({"id": JID, "state": state}), encoding="utf-8")
        with open(jmod.STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status, f)
        calls.clear()
        assert tjoin.cmd_join(tok) == 0
        assert calls == ["follow"], (state, calls)
    jf.write_text(json.dumps({"id": "ffffffffffffffff", "state": "pairing"}), encoding="utf-8")
    calls.clear()
    assert tjoin.cmd_join(tok) == 0
    assert calls[0] == "link" and "systemctl restart awg-bot" in calls


def test_the_channel_port_while_pairing_comes_from_the_token(tmp_path, monkeypatch):
    """flow-9: у шлюза, подключённого раньше, юнит обвязки ещё прежний — порт
    канала сопряжения из файла сопряжения (токена), не из юнита."""
    from awgbot.domain.gateway import join as jmod
    from awgbot.infra import gwguard
    from awgbot.runtime import linkclient
    jf = tmp_path / "join.json"
    monkeypatch.setattr(jmod, "JOIN_FILE", str(jf))
    monkeypatch.setattr(gwguard, "unit_env", lambda key: "9999")
    jf.write_text(json.dumps({"id": JID, "channel_port": 8787, "state": "pairing"}), encoding="utf-8")
    assert linkclient.server_port() == 8787
    # join-4: файл закрытого сопряжения — не про порт
    jf.write_text(json.dumps({"id": JID, "channel_port": 8787, "state": "refused"}), encoding="utf-8")
    assert linkclient.server_port() == 9999
    jf.unlink()
    assert linkclient.server_port() == 9999


def test_the_status_file_with_the_code_is_private(agent):
    """Вне оси (axis-pairing): файл хода с кодом — 0600, не 0644."""
    import os
    agent.join_status(state="code", code="K7F-2QX")
    assert os.stat(agent._join_status_file).st_mode & 0o777 == 0o600


@pytest.mark.parametrize("token, finished", [("", False), ("123456789:AA-token-of-the-gateway-bot", True)])
def test_linked_drops_the_pair_and_a_full_agent_finishes_at_once(agent, monkeypatch, token, finished):
    """join-1: эфемерная пара живёт до линка на постоянных ключах. Блокер
    fix4: агент, стоявший раньше (токен бота уже есть), заканчивает
    сопряжение сразу — раздела token с тем же ботом нет (отпечатки равны), и
    ждать его значило бы join_stale через сутки; агент в режиме сопряжения
    (токена нет) ждёт раздела token."""
    from awgbot.core import config
    from awgbot.infra import gwguard
    monkeypatch.setattr(gwguard, "join_finish", lambda port: (True, ""))
    monkeypatch.setattr(config, "BOT_TOKEN", token)
    agent.join_hello()
    st = agent.join_state()
    assert st.get("eph_priv")
    st["state"] = "accepted"
    agent._join_save(st)
    agent.join_linked()
    st = agent.join_state()
    assert "eph_priv" not in st
    assert st["state"] == ("done" if finished else "linked")
    assert (agent.join_done_pending() == {"id": JID}) is finished


def test_a_link_that_did_not_come_up_leaves_no_pairing_file(joinrun, monkeypatch, capsys):
    """Вне оси: --join отказал — файла сопряжения нет: агент на следующем
    перезапуске не уйдёт в режим сопряжения без линка."""
    from awgbot.domain.gateway import join as jmod
    from awgbot.infra import gwguard
    tjoin, jf, calls, _rc = joinrun
    monkeypatch.setattr(gwguard, "join_link", lambda conf, port, script="": (False, "awg-quick: нет"))
    assert tjoin.cmd_join(_token(expires=int(time.time()) + 3600)) == 1
    assert not jf.exists() and not os.path.exists(jmod.STATUS_FILE)
    assert "линк по команде подключения не поднялся" in capsys.readouterr().err


def test_the_keys_after_the_code_open_only_with_the_pairing_key(agent, monkeypatch):
    """sec-1: link_next сессии сопряжения несёт конфиг под ключом сопряжения;
    агент расшифровывает своим Kp, чужой или отсутствующий — отказ без
    перехода; после join_refused ключей сопряжения у агента нет."""
    hello = agent.join_hello()
    body, _code, kp = _server_side(hello)
    agent.join_eph_in(body)
    box = joinsas.seal(kp, {"conf": "[Interface]\nPrivateKey = DUMMY\n"})
    assert agent.join_next_open({"t": "link_next", "id": "a1", "box": box})["conf"].startswith("[Interface]")
    bad = joinsas.seal(bytes(32), {"conf": "x"})
    assert "conf" not in agent.join_next_open({"t": "link_next", "id": "a1", "box": bad})
    monkeypatch.setattr(agent, "rekey_state", lambda: {}, raising=False)
    monkeypatch.setattr(agent, "cfg_link_text", lambda: "", raising=False)
    from awgbot.domain.gateway.cfg.rekey import CfgRekeyMixin
    res = CfgRekeyMixin.rekey_next_in(agent, {"t": "link_next", "id": "a1", "box": bad, "auto": "join"})
    assert res["reply"]["state"] == "declined" and "ключом сопряжения" in res["reply"]["error"]
    agent.join_refused_in({"why": "code"})
    assert "eph_priv" not in agent.join_state() and "_join_kp" not in agent.__dict__
