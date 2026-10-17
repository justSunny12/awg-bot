"""
Копия настроек шлюза по каналу: сервер (gwchannel/keep) и агент
(domain/gateway/keep, runtime/linkclient/keep) через подставной канал — ответы
сервера на hello уходят прямо в разбор клиента, `keep` клиента — прямо в приём
сервера. Сокета нет; порядок и решения — настоящие.

Сценарии:
- правка тихих часов → `keep` → сервер хранит; в hello — отпечаток; traffic —
  не чаще раза в 6 часов и только при росте;
- «б» — новая установка того же устройства: своё не шлёт до ответа сервера,
  блок применён молча (firewall.env без SSH_PORT, реассерт, своя почта до
  раздела mail, счёт линка того же месяца продолжен), Т18, событие бэкапа;
- «в» — новое устройство в слоте: вопрос; «перенести» / «по умолчанию»;
  traffic того же месяца — в обоих ответах, прошлого месяца — нет;
- новая установка без блока — keep_none, шлёт своё; поправленная руками — вопрос;
- «г» — после восстановления сервера (offer): вопрос без traffic, счёт линка
  не тронут; «вернуть» / «оставить» снимают offer; совпадают — вопроса нет;
- обновлённый агент — не чистый, шлёт своё; агент без keep1 — сервер не
  отвечает keep_*, карточка «не сохраняются».
"""
from __future__ import annotations

import asyncio
import json
import shutil

import pytest

from awgbot.core import config, settings
from awgbot.util import timeutil

pytestmark = pytest.mark.integration


@pytest.fixture()
def conf(tmp_path):
    """Свои conf/*.yaml: агент пишет настройки — соседям по процессу не достаются."""
    from tests.conftest import _CONF_COPY, restore_settings
    d = tmp_path / "conf"
    shutil.copytree(_CONF_COPY, d)
    settings.init(d)
    yield d
    restore_settings()


class Agent:
    """Агент шлюза: своя база, firewall.env во временном каталоге, клиент канала."""

    def __init__(self, tmp, monkeypatch, *, fresh: bool = True, name="a"):
        from awgbot.domain.gateway import GatewayServices
        from awgbot.infra import gwguard
        from awgbot.infra.db import Database
        from awgbot.runtime.linkclient import LinkClient
        self.fw = tmp / f"firewall-{name}.env"
        monkeypatch.setattr(gwguard, "FW_ENV", str(self.fw))
        gwguard.write_env(SSH_PORT="2222", SSH_FILTER="0", SSH_ALLOW="")
        self.db = Database(str(tmp / f"gw-{name}.db")); self.db.init_schema()
        self.s = GatewayServices(self.db)
        self.reasserts = []
        self.s.reassert_guarded = lambda why, **kw: self.reasserts.append(why) or (True, "")
        if fresh:
            self.s.first_start_note(True)
        self.client = LinkClient(self.s)
        self.client._writer = object()
        self.sent: list[tuple[str, dict]] = []

    def close(self):
        self.db.close()


class Wire:
    """Подставной канал: сервер слота `slot` ↔ агент."""

    def __init__(self, server, slot: int, agent: Agent, monkeypatch, *, caps=("keep1",)):
        from awgbot.runtime import linkclient
        self.server, self.slot, self.agent, self.caps = server, slot, agent, caps
        self.notes, self.asks, self.silent = [], [], []

        async def notify(text, *, silent=False):
            self.notes.append(text)
            self.silent.append(silent)

        async def ask(section):
            self.asks.append(section)
        monkeypatch.setattr(linkclient, "_notify", notify)
        monkeypatch.setattr(linkclient, "_ask", ask)

        async def send(kind, body=None, pad=0):
            agent.sent.append((kind, body))
            if kind == "keep":
                digest = server.gwkeep_in(slot, body)
                await agent.client._keep_reply({"t": "keep_ok", "hash": digest})
            return True
        agent.client._send = send

    def hello(self) -> dict:
        """Новая сессия: hello агента, ответ сервера, разбор клиента."""
        self.agent.client._keep_ready = False
        h = self.agent.s.keep_hello()
        if "keep1" not in self.caps:
            return h
        reply, body = self.server.gwkeep_hello(self.slot, h["keep_hash"], h["keep_new"], h["keep_fresh"])
        asyncio.run(self.agent.client._keep_reply({"t": reply, **body}))
        return {**h, "reply": reply, "body": body}

    def answer(self, accept: bool) -> dict:
        from awgbot.runtime import linkclient
        monkey_client = linkclient._client
        linkclient._client = self.agent.client
        try:
            return asyncio.run(linkclient.report_keep_answer(self.agent.s, accept))
        finally:
            linkclient._client = monkey_client

    def tick(self):
        asyncio.run(self.agent.client.keep_tick())


@pytest.fixture()
def server(services, make_active_client, fake_awg, monkeypatch):
    """Сервер AWG со слотом 1; фраза задана — события бэкапа отмечаются."""
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID, device_limit=0)
    services.slot_dev = services.add_device(admin.id, "NASPi").device_id
    services.db.gateway_add(services.slot_dev, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.backup_set_passphrase("correct horse battery")
    services.admin_id_ = admin.id
    return services


def _keep(server) -> dict:
    return server.gwkeep(1)


def _replace_device(server):
    other = server.add_device(server.admin_id_, "Pi-новый").device_id
    server.db.gateway_update(1, device_id=other)


def _gw_traffic(agent, month=None, rx=0, tx=0):
    month = month or timeutil.now().strftime("%Y-%m")
    agent.db.set_state("gw_traffic", json.dumps({"month": month, "rx": rx, "tx": tx, "last_rx": 7, "last_tx": 7}))


# ── обычная жизнь: правка → keep → сервер хранит ─────────────────────────────

def test_a_change_goes_to_the_server_and_traffic_at_most_every_six_hours(server, conf, tmp_path, monkeypatch):
    a = Agent(tmp_path, monkeypatch, fresh=False)
    w = Wire(server, 1, a, monkeypatch)
    h = w.hello()
    assert h["reply"] == "keep_ok" and [k for k, _ in a.sent] == ["keep"], "обновлённый агент не прислал своё"
    first = _keep(server)
    assert first["hash"] == a.s.keep_print() and first["dev"] == server.db.get_device(server.slot_dev).public_key
    a.sent.clear()
    settings.set_value("quiet_hours.quiet_hours_start", 21)
    w.tick()
    assert [k for k, _ in a.sent] == ["keep"] and _keep(server)["items"]["conf"]["quiet_hours.quiet_hours_start"] == 21
    assert w.hello()["keep_hash"] == _keep(server)["hash"]
    a.sent.clear()
    w.tick()
    assert a.sent == [], "без изменений ушёл блок"
    _gw_traffic(a, rx=1000, tx=1000)
    w.tick()
    assert a.sent == [], "счёт линка ушёл раньше срока"
    last = json.loads(a.db.get_state(a.s._KEEP_TRAFFIC_KEY))
    last["at"] = timeutil.to_iso(timeutil.shift(timeutil.now(), -9 * 3600))
    a.db.set_state(a.s._KEEP_TRAFFIC_KEY, json.dumps(last))
    w.tick()
    w.tick()
    sends = [b for k, b in a.sent if k == "keep"]
    assert len(sends) == 1 and sends[0]["traffic"]["rx"] == 1000, "счёт линка ушёл чаще раза в 6 часов"
    assert _keep(server)["traffic"]["rx"] == 1000
    a.sent.clear()
    last = json.loads(a.db.get_state(a.s._KEEP_TRAFFIC_KEY))
    last["at"] = timeutil.to_iso(timeutil.shift(timeutil.now(), -9 * 3600))
    a.db.set_state(a.s._KEEP_TRAFFIC_KEY, json.dumps(last))
    w.tick()
    assert a.sent == [], "счёт не вырос, а блок ушёл"
    a.close()


# ── «б»: новая установка того же устройства ──────────────────────────────────

def test_a_fresh_install_of_the_same_device_gets_its_settings_silently(server, conf, tmp_path, monkeypatch):
    old = Agent(tmp_path, monkeypatch, fresh=False, name="old")
    Wire(server, 1, old, monkeypatch).hello()
    settings.set_value("quiet_hours.quiet_hours_start", 20)
    from awgbot.infra import gwguard
    gwguard.write_env(SSH_FILTER="1", SSH_ALLOW="203.0.113.5")
    from awgbot.domain import gwcfg
    server_mail = {"login": "srv@example.org", "password": "DUMMY-srv", "imap_host": "imap.example.org",
                   "imap_port": 993, "smtp_host": "smtp.example.org", "smtp_port": 587}
    old.db.set_state(old.s._CFG_KEEP_KEY, json.dumps({"mail": gwcfg.mail_print(server_mail)}))
    old.s.email_save("own@example.org", "DUMMY", "imap.example.org", 993, "smtp.example.org", 587)
    _gw_traffic(old, rx=500, tx=600)
    asyncio.run(old.client.keep_tick())
    old.close()
    server.db.set_state("backup_event_due", "")
    # SD-карту переставили: база и firewall.env чистые, настройки — по умолчанию
    settings.set_value("quiet_hours.quiet_hours_start", 23)
    new = Agent(tmp_path, monkeypatch, fresh=True, name="new")
    _gw_traffic(new, rx=10, tx=20)
    w = Wire(server, 1, new, monkeypatch)
    h = w.hello()
    assert h["keep_new"] and h["keep_fresh"] and h["reply"] == "keep_back" and h["body"]["why"] == "same"
    assert w.asks == [], "тот же шлюз спросили"
    assert settings.get("quiet_hours.quiet_hours_start") == 20
    env = gwguard.read_env()
    assert env["SSH_FILTER"] == "1" and env["SSH_ALLOW"] == "203.0.113.5" and env["SSH_PORT"] == "2222"
    assert new.reasserts, "обвязка не перевыставлена после firewall.env"
    assert json.loads(new.db.get_state("gw_traffic"))["rx"] == 510, "счёт линка не продолжил цифры блока"
    # своя почта легла раньше раздела mail документа: тот видит памятку и отвечает declined
    assert new.s.email_account().login == "own@example.org"
    assert new.s.cfg_decide("mail", server_mail)[0] == "declined"
    assert len(w.notes) == 1 and w.notes[0].startswith("💾 Настройки шлюза получены с сервера AWG: ")
    assert [k for k, _ in new.sent] == ["keep"], "после применения своё не ушло"
    assert not server.db.get_state("backup_event_due"), "переустановка того же устройства — не новое устройство"
    new.close()


def test_a_fresh_agent_does_not_send_before_the_server_answers(server, conf, tmp_path, monkeypatch):
    a = Agent(tmp_path, monkeypatch, fresh=True)
    w = Wire(server, 1, a, monkeypatch)
    asyncio.run(a.client.keep_push(force=True))
    assert a.sent == [], "чистый агент прислал умолчания до ответа сервера"
    w.hello()
    assert [k for k, _ in a.sent] == ["keep"]
    a.close()


# ── «в»: новое устройство в слоте ────────────────────────────────────────────

@pytest.mark.parametrize("accept", [True, False])
def test_a_new_device_in_the_slot_is_asked_and_traffic_moves_either_way(server, conf, tmp_path, monkeypatch, accept):
    old = Agent(tmp_path, monkeypatch, fresh=False, name="old")
    settings.set_value("quiet_hours.quiet_hours_start", 20)
    _gw_traffic(old, rx=500, tx=600)
    Wire(server, 1, old, monkeypatch).hello()
    old.close()
    _replace_device(server)
    settings.set_value("quiet_hours.quiet_hours_start", 23)
    new = Agent(tmp_path, monkeypatch, fresh=True, name="new")
    w = Wire(server, 1, new, monkeypatch)
    h = w.hello()
    assert h["body"]["why"] == "device" and w.asks == ["keep"] and new.sent == []
    assert new.s.cfg_questions() == ["keep_dev"]
    q = new.s.cfg_question("keep_dev")
    assert q["groups"] == ["тихие часы"]
    assert server.gwkeep_card(1, agent_caps=["keep1"])["state"] == "asked"
    assert json.loads(new.db.get_state("gw_traffic"))["rx"] == 500, "счёт линка не перенесён сразу"
    res = w.answer(accept)
    assert res["ok"] and res["why"] == "device"
    assert settings.get("quiet_hours.quiet_hours_start") == (20 if accept else 23)
    assert _keep(server)["items"]["conf"]["quiet_hours.quiet_hours_start"] == (20 if accept else 23)
    assert _keep(server)["dev"] == server.db.get_device(server.db.gateway(1).device_id).public_key
    assert "keep" in json.loads(server.db.get_state("backup_event_due"))["why"][-1]["kind"], "первый блок нового устройства — без бэкапа"
    assert server.gwkeep_card(1, agent_caps=["keep1"])["state"] == "saved"
    new.close()


def test_traffic_of_the_previous_month_does_not_move(server, conf, tmp_path, monkeypatch):
    old = Agent(tmp_path, monkeypatch, fresh=False, name="old")
    _gw_traffic(old, month="2000-01", rx=500, tx=600)
    Wire(server, 1, old, monkeypatch).hello()
    old.close()
    _replace_device(server)
    new = Agent(tmp_path, monkeypatch, fresh=True, name="new")
    _gw_traffic(new, rx=1, tx=1)
    Wire(server, 1, new, monkeypatch).hello()
    assert json.loads(new.db.get_state("gw_traffic"))["rx"] == 1
    new.close()


def test_a_fresh_agent_without_a_block_gets_keep_none_and_a_touched_one_is_asked(server, conf, tmp_path, monkeypatch):
    a = Agent(tmp_path, monkeypatch, fresh=True, name="a")
    w = Wire(server, 1, a, monkeypatch)
    assert w.hello()["reply"] == "keep_none" and [k for k, _ in a.sent] == ["keep"]
    a.close()
    # тот же слот, переустановка, а человек успел поправить тихие часы до первой сессии
    settings.set_value("quiet_hours.quiet_hours_start", 23)
    b = Agent(tmp_path, monkeypatch, fresh=True, name="b")
    settings.set_value("quiet_hours.quiet_hours_start", 19)
    w = Wire(server, 1, b, monkeypatch)
    h = w.hello()
    assert h["keep_new"] and not h["keep_fresh"] and w.asks == ["keep"] and b.sent == []
    b.close()


# ── «г»: после восстановления сервера ────────────────────────────────────────

def _offer(server, items, at="2026-10-05T14:00:00+03:00"):
    from awgbot.domain import gwkeep
    block = {"v": 1, "hash": gwkeep.print_(items), "at": at, "items": gwkeep.clean(items),
             "dev": server.db.get_device(server.slot_dev).public_key, "traffic": {"month": "2026-10", "rx": 9, "tx": 9},
             "offer": "restore", "offer_from": "2026-10-05T14:00:00+03:00"}
    server.db.set_state("gwkeep_1", json.dumps(block))


@pytest.mark.parametrize("accept", [True, False])
def test_after_a_restore_different_settings_are_asked_without_traffic(server, conf, tmp_path, monkeypatch, accept):
    a = Agent(tmp_path, monkeypatch, fresh=False)
    _gw_traffic(a, rx=77, tx=77)
    settings.set_value("quiet_hours.quiet_hours_start", 23)
    _offer(server, {"conf": {"quiet_hours.quiet_hours_start": 18}, "fw": {}})
    w = Wire(server, 1, a, monkeypatch)
    h = w.hello()
    assert h["body"]["why"] == "restore" and "traffic" not in h["body"] and w.asks == ["keep"]
    assert json.loads(a.db.get_state("gw_traffic"))["rx"] == 77, "счёт живого шлюза тронут"
    q = a.s.cfg_question("keep")
    assert q["groups"] == ["тихие часы"] and q["from"].startswith("2026-10-05")
    res = w.answer(accept)
    assert res["ok"] and res["why"] == "restore"
    assert settings.get("quiet_hours.quiet_hours_start") == (18 if accept else 23)
    k = _keep(server)
    assert "offer" not in k and "asked" not in k and k["items"]["conf"]["quiet_hours.quiet_hours_start"] == (
        18 if accept else 23)
    a.close()


def test_after_a_restore_equal_settings_ask_nothing(server, conf, tmp_path, monkeypatch):
    a = Agent(tmp_path, monkeypatch, fresh=False)
    _offer(server, a.s.keep_collect())
    w = Wire(server, 1, a, monkeypatch)
    assert w.hello()["reply"] == "keep_ok" and w.asks == []
    assert "offer" not in _keep(server)
    a.close()


def test_an_answer_given_offline_is_not_asked_again(server, conf, tmp_path, monkeypatch):
    a = Agent(tmp_path, monkeypatch, fresh=False)
    settings.set_value("quiet_hours.quiet_hours_start", 23)
    _offer(server, {"conf": {"quiet_hours.quiet_hours_start": 18}, "fw": {}})
    w = Wire(server, 1, a, monkeypatch)
    w.hello()
    a.client._writer = None                         # сессия умерла, человек ответил «оставить»
    assert w.answer(False)["ok"]
    a.client._writer = object()
    w.asks.clear()
    w.hello()
    assert w.asks == [] and "offer" not in _keep(server)
    a.close()


# ── версии ───────────────────────────────────────────────────────────────────

def test_an_agent_without_keep1_gets_no_keep_answer_and_the_card_says_so(server, conf, tmp_path, monkeypatch):
    a = Agent(tmp_path, monkeypatch, fresh=True)
    w = Wire(server, 1, a, monkeypatch, caps=())
    w.hello()
    asyncio.run(a.client.keep_push(force=True))
    assert a.sent == [] and not server.db.get_state("gwkeep_1")
    assert server.gwkeep_card(1, agent_caps=["cfg1"], online=True)["state"] == "outdated"
    assert server.gwkeep_card(1, agent_caps=["cfg1"], online=False)["state"] == "", "не на связи — не судим"
    from awgbot.bot.texts.routing.card import keep_line
    assert keep_line({"state": "outdated"}) == ("⚠️ Настройки бота шлюза не сохраняются в бэкап: агент шлюза "
                                                 "старше 3.4.0 — обнови его")
    a.close()
