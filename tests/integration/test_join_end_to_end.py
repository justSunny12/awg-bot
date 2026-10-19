"""Подключение шлюза командой — сквозь обе стороны, от команды до join_done
(FIXES-uupd-4: блокер «токен бота не едет», join-1).

Сервер — настоящие Services поверх модели скрипта линка (_join_common.JoinVps):
выпуск команды, hello сопряжения, код, join_ok, смена ключей `auto=join`,
фиксация, снимок, НАСТОЯЩАЯ сборка документа (gwcfg_due) и join_done.
Агент — настоящий GatewayServices: файл сопряжения и конфиг линка — как их
кладёт `awg-bot join` (tools/join), эфемерная пара, код, секреты, приём
link_next и переключение (проверка конфига — настоящая wg_conf_check
routing-gw-setup.sh), закрепление по первому ответу сервера, НАСТОЯЩЕЕ
применение документа с разделом token, полный старт и join_done.

Подменено только то, чего в тесте нет: сеть Telegram (getMe), юнит обвязки и
её прогоны (--apply, транзакция env/link и аплинк — свои тесты), перезапуск
процесса агента (config.BOT_TOKEN выставляется, как его прочитал бы новый
процесс). Сообщения между сторонами — тела ровно те, что строит отправитель.

Цена ошибки: подключение, которое не доходит до конца ни у кого (дважды
ловилось только ревью): токен бота не едет — агент навсегда в режиме
сопряжения и join_stale через сутки; обрыв между join_ok и link_go — тупик
на час и откат.
"""
from __future__ import annotations

import json
import subprocess
import types
from pathlib import Path

import pytest

from awgbot.core import config
from awgbot.domain.gateway import GatewayServices
from awgbot.domain.services.gwjoin import state as statemod
from awgbot.infra.db import Database
from awgbot.util import joinsas, jointoken

from tests.integration import _join_common as jc
from tests.unit.test_join_rekey_e2e import _script_check

pytestmark = pytest.mark.integration
admin, vps = jc.admin, jc.vps                     # фикстуры сцены

TOKEN = "1234567890:DUMMY_gateway_bot_token_abcdef"
OTHER = "1234567891:DUMMY_another_gateway_bot_token"


class _Pi:
    """Шлюз: каталоги агента во временном, обвязка — подставная, проверка
    --link-apply — настоящая из скрипта."""

    def __init__(self, tmp_path, monkeypatch, *, bot_token: str = ""):
        from awgbot.infra import gwguard
        root = tmp_path / "pi"; root.mkdir()
        self.root = root
        self.link = root / "awglink.conf"
        self.env = root / "env"
        self.env.write_text(f"BOT_TOKEN={bot_token}\n" if bot_token else "", encoding="utf-8")
        monkeypatch.setattr(config, "GW_LINK_CONF", str(self.link))
        monkeypatch.setattr(config, "BOT_TOKEN", bot_token)
        monkeypatch.setattr(gwguard, "GW_ETC", str(root / "awg-gw"))
        monkeypatch.setattr(gwguard, "unit_env", lambda key: "")
        self.runs: list[str] = []

        class _P:
            def __init__(self, rc, out):
                self.returncode, self.stdout, self.stderr = rc, out, b""

        def run(argv, **kw):
            if len(argv) >= 3 and argv[1] == "--link-apply":
                r = _script_check(argv[2], kw.get("env") or {})
                self.runs.append(f"link-apply:{r.returncode}")
                if r.returncode:
                    return _P(1, ("ОШИБКА: " + r.stderr).encode())
                self.link.write_text(Path(argv[2]).read_text(encoding="utf-8"), encoding="utf-8")
                return _P(0, b"link: restarted\n")
            self.runs.append(" ".join(str(a) for a in argv[1:3]))
            return _P(0, b"")
        monkeypatch.setattr(gwguard, "subprocess", types.SimpleNamespace(
            run=run, TimeoutExpired=subprocess.TimeoutExpired, SubprocessError=subprocess.SubprocessError))
        self.db = Database(str(root / "pi.db")); self.db.init_schema()
        s = GatewayServices(self.db)
        s._join_file = str(root / "join.json")
        s._join_status_file = str(root / "join.status")
        monkeypatch.setattr(s, "_cfg_env_path", lambda: str(self.env))
        # сеть Telegram: getMe нового токена — тот же бот, что в токене
        monkeypatch.setattr(s, "_cfg_token_getme", staticmethod(lambda t: ("ok", int(t.split(":", 1)[0]))))
        # транзакция env/link и аплинк — свои тесты; здесь — что они «встали»
        monkeypatch.setattr(s, "cfg_apply_env_link", lambda env, link_text: {"ok": True, "changed": []})
        self.s = s

    def join(self, command: str) -> jointoken.JoinToken:
        """Что делает `awg-bot join <команда>`: конфиг линка по токену и файл
        сопряжения (tools/join)."""
        from tools import join as tjoin
        tok = jointoken.decode(jointoken.find(command))
        self.link.write_text(tjoin._link_conf(tok), encoding="utf-8")
        self.s._join_save({"id": tok.join_id, "slot": tok.slot, "version": tok.version, "expires": tok.expires,
                           "host": tok.host, "channel_port": tok.channel_port, "link_port": tok.link_port,
                           "state": "pairing", "name": ""})
        return tok

    def close(self):
        self.db.close()


@pytest.fixture()
def server(services, vps, tmp_path, monkeypatch):
    """Сервер AWG: env с токеном бота слота 1 (введён при «Подключить шлюз»),
    сборка документа читает /root/gw-<линк>.conf модели."""
    from awgbot.domain.services.gwchannel import cfg as cfgmod
    env = tmp_path / "srv.env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setenv("AWG_BOT_ENV", str(env))
    real_open = open

    def _open(path, *a, **kw):
        p = str(path)
        if p.startswith("/root/"):
            p = str(vps.root / p[len("/root/"):])
        return real_open(p, *a, **kw)
    monkeypatch.setattr(cfgmod, "open", _open, raising=False)
    services.set_gw_bot_token(TOKEN, 1)
    return services


def _pair(srv, pi) -> str:
    """hello сопряжения → join_eph → код у агента."""
    res = srv.gwjoin_hello(1, pi.s.join_hello())
    assert res["ok"], res
    return pi.s.join_eph_in(res["body"])


def _keys(srv, pi):
    """join_ok, link_next → ready → link_go, переключение обеих сторон и
    первый ответ сервера на новых ключах."""
    assert pi.s.join_ok_in(srv.gwjoin_ok_body(1))
    nxt = srv.gwjoin_next_body(1)
    assert nxt and nxt.get("auto") == "join", nxt
    res = pi.s.rekey_next_in({"t": "link_next", **nxt})
    assert res["reply"]["state"] == "ready" and not res["ask"], res
    action, _ = srv.gateway_rekey_ack_in(1, res["reply"])
    assert action == "go"
    srv.gateway_rekey_go(1)
    out = pi.s.rekey_go({"id": nxt["id"]})
    assert out["ok"], (out, pi.runs)
    assert srv.gateway_rekey_commit(1)[0] is True
    assert srv.gateway_rekey_hello(1)["rekeyed"]        # hello агента новым ключом
    answered = pi.s.rekey_server_answered()            # первое слово сервера в этой сессии
    assert answered["done"] and answered["done"]["ok"], answered


def _doc(srv, pi) -> dict | None:
    """Снимок агента (поле cfg — его отпечатки) и документ, как его отправил бы
    сервер."""
    snap = srv.gwlink_snapshot(1) or {}
    assert srv.gwlink_snapshot_in(1, {**snap, "cfg": pi.s.cfg_fingerprints(), "agent_version": "3.4.0"},
                                  int(snap.get("rev") or 0) + 1, True)
    return srv.gwcfg_due(srv.db.gateway(1))


def _done(srv, pi) -> None:
    """Полный старт агента (новый процесс прочёл BOT_TOKEN из env) и join_done."""
    body = pi.s.join_done_pending()
    assert body, pi.s.join_state()
    st = srv.gwjoin_installed(1, body["id"])
    assert st and st.get("id") == body["id"]
    pi.s.join_done_sent()
    assert srv.gwjoin(1) == {} and pi.s.join_state() == {}


def _env_token(pi) -> str:
    return next((ln.split("=", 1)[1] for ln in pi.env.read_text(encoding="utf-8").splitlines()
                 if ln.startswith("BOT_TOKEN=")), "")


@pytest.fixture()
def pi(tmp_path, monkeypatch):
    p = _Pi(tmp_path, monkeypatch)
    yield p
    p.close()


def test_a_new_gateway_gets_its_bot_token_and_finishes(server, vps, pi, monkeypatch):
    """Блокер fix4: новый шлюз (агент без токена, режим сопряжения) — от кода
    до join_done. Раздел token едет в документе (токен, введённый при
    «Подключить шлюз», помечен для доставки), агент его применяет, полный
    старт закрывает сопряжение у обеих сторон."""
    pi.join(server.gwjoin_issue()["command"])
    code = _pair(server, pi)
    assert server.gwjoin_code(1, server.gwjoin(1)["id"], joinsas.show(code))["result"] == "ok"
    _keys(server, pi)
    assert server.gwjoin(1)["state"] == "linked" and pi.s.join_state()["state"] == "linked"
    msg = _doc(server, pi)
    assert msg is not None, "сервер не прислал документ шлюзу на постоянных ключах"
    assert (msg.get("sections") or msg).get("token", {}).get("value") == TOKEN, \
        f"раздела token в документе нет — агент не получит токен своего бота: {sorted(msg.get('sections') or msg)}"
    res = pi.s.apply_cfg(msg)
    assert "token" in res["ack"]["applied"] and res["restart"], res["ack"]
    assert _env_token(pi) == TOKEN, "токен бота не записан в env агента"
    monkeypatch.setattr(config, "BOT_TOKEN", _env_token(pi))      # новый процесс агента прочёл env
    assert pi.s.join_finish_start("naspi_gw_bot") is True
    _done(server, pi)


def test_a_gateway_that_had_the_same_bot_finishes_without_a_token_section(server, vps, tmp_path, monkeypatch):
    """Агент уже стоял / «🔁 Настроить заново»: бот тот же — отпечатки
    токена совпадают, раздел token ничего не меняет и не перезапускает:
    сопряжение заканчивается на линке."""
    jc.slot_with_device(server, vps, 1)
    pi = _Pi(tmp_path, monkeypatch, bot_token=TOKEN)
    try:
        pi.join(server.gwjoin_issue(slot_id=1, same=True)["command"])
        code = _pair(server, pi)
        assert server.gwjoin_code(1, server.gwjoin(1)["id"], joinsas.show(code))["result"] == "ok"
        _keys(server, pi)
        assert pi.s.join_state()["state"] == "done", "полный агент не закончил сопряжение на линке"
        msg = _doc(server, pi)
        if msg is not None:
            res = pi.s.apply_cfg(msg)                    # тот же бот — без перезапуска
            assert not res["restart"] and "token" not in res["changed"], res
        _done(server, pi)
    finally:
        pi.close()


def test_a_reinstalled_gateway_with_another_bot_gets_the_new_token(server, vps, tmp_path, monkeypatch):
    """Замена устройства, на новом шлюзе стоял агент с другим ботом: токен
    слота едет разделом, агент переходит на него."""
    jc.slot_with_device(server, vps, 1)
    pi = _Pi(tmp_path, monkeypatch, bot_token=OTHER)
    try:
        pi.join(server.gwjoin_issue(slot_id=1)["command"])
        code = _pair(server, pi)
        assert server.gwjoin_code(1, server.gwjoin(1)["id"], joinsas.show(code))["result"] == "ok"
        _keys(server, pi)
        msg = _doc(server, pi)
        assert msg is not None and (msg.get("sections") or msg).get("token", {}).get("value") == TOKEN
        assert "token" in pi.s.apply_cfg(msg)["ack"]["applied"] and _env_token(pi) == TOKEN
    finally:
        pi.close()


@pytest.mark.parametrize("dropped", [False, True], ids=["stage-kept", "stage-dropped"])
def test_a_server_restart_between_join_ok_and_link_go_still_gets_through(server, vps, pi, monkeypatch, dropped):
    """join-1: обрыв (перезапуск сервера) после join_ok, до link_go. Агент
    приходит снова hello с join и тем же эфемерным ключом — сервер повторяет
    join_ok и link_next (стадии нет — заводит заново), подключение доходит."""
    pi.join(server.gwjoin_issue()["command"])
    code = _pair(server, pi)
    assert server.gwjoin_code(1, server.gwjoin(1)["id"], joinsas.show(code))["result"] == "ok"
    assert pi.s.join_ok_in(server.gwjoin_ok_body(1))
    first = server.gwjoin_next_body(1)
    pi.s.rekey_next_in({"t": "link_next", **first})     # агент готов, link_go не пришёл
    # перезапуск сервера: память процесса (код, ключ сопряжения) — прочь
    statemod._LIVE.clear()
    if dropped:
        server.gateway_rekey_abort(server.db.gateway(1))  # переход снят (срок, старт посреди)
    again = pi.s.join_hello()
    assert again.get("join") and again["eph"], "после кода агент не приходит сессией сопряжения"
    res = server.gwjoin_hello(1, again)
    assert res["ok"] and res.get("accepted"), res
    pi.s.join_eph_in(res["body"])
    _keys(server, pi)
    assert server.gwjoin(1)["state"] == "linked"
    msg = _doc(server, pi)
    assert "token" in pi.s.apply_cfg(msg)["ack"]["applied"]
    monkeypatch.setattr(config, "BOT_TOKEN", _env_token(pi))
    assert pi.s.join_finish_start("naspi_gw_bot")
    _done(server, pi)


def test_the_token_mark_is_set_by_the_code(server, vps, pi):
    """Метка «доставить токен» ставится верным кодом — до кода раздела нет
    (документа шлюзу в карантине и так не шлют)."""
    gw_print = server.db.get_state(server._gw_slot_key(server._TOKEN_PUSH_KEY, 1)) or ""
    assert gw_print == ""
    pi.join(server.gwjoin_issue()["command"])
    code = _pair(server, pi)
    server.gwjoin_code(1, server.gwjoin(1)["id"], joinsas.show(code))
    assert server.gateway_token_pushed(1, TOKEN)
    assert json.loads(json.dumps(server.gwcfg_token(server.db.gateway(1)))) == {"value": TOKEN}
