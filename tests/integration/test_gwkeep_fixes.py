"""
Копия настроек шлюза по каналу — частные случаи: висящий вопрос, который
сервер снял (keep_ok / keep_none); повторный keep_back того же блока;
счёт линка нового месяца; сбой применения (ничего не теряется: своё не
уходит, вопрос остаётся, conf откатывается); файервол, ждущий перезапуска
обвязки; тот же шлюз заново и поправленный до первой связи; переезд
профилей (двойник — та же машина); ответ, данный на вопрос любого вида;
настройки, вставшие молча, — без звука.

Подставной канал и агент — из test_gwkeep_channel: сокета нет, решения
настоящие.
"""
from __future__ import annotations

import asyncio
import json
import shutil

import pytest

from awgbot.core import config, settings
from awgbot.util import timeutil
from tests.integration.test_gwkeep_channel import Agent, Wire, _gw_traffic, _keep, _offer, _replace_device

pytestmark = pytest.mark.integration


@pytest.fixture()
def conf(tmp_path):
    from tests.conftest import _CONF_COPY, restore_settings
    d = tmp_path / "conf"
    shutil.copytree(_CONF_COPY, d)
    settings.init(d)
    yield d
    restore_settings()


@pytest.fixture()
def server(services, make_active_client, fake_awg, monkeypatch):
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID, device_limit=0)
    services.slot_dev = services.add_device(admin.id, "NASPi").device_id
    services.db.gateway_add(services.slot_dev, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.backup_set_passphrase("correct horse battery")
    services.admin_id_ = admin.id
    return services


def _asked_restore(server, a, monkeypatch, w=None):
    settings.set_value("quiet_hours.quiet_hours_start", 23)
    _offer(server, {"conf": {"quiet_hours.quiet_hours_start": 18}, "fw": {}})
    w = w or Wire(server, 1, a, monkeypatch)
    w.hello()
    assert a.s.keep_section() == "keep"
    return w


def test_a_question_the_server_dropped_is_dropped_and_own_goes(server, conf, tmp_path, monkeypatch):
    """Вопрос висит, а сервер на hello ответил keep_ok (предложения больше нет):
    вопрос снят — «вернуть» не применит снятое — и своё ушло."""
    a = Agent(tmp_path, monkeypatch, fresh=False)
    w = _asked_restore(server, a, monkeypatch)
    blk = _keep(server)
    blk.pop("offer"); blk.pop("offer_from", None); blk.pop("asked", None)
    blk["hash"] = "0" * 16
    server.db.set_state("gwkeep_1", json.dumps(blk))
    a.sent.clear()
    assert w.hello()["reply"] == "keep_ok"
    assert a.s.keep_section() == "" and a.s.cfg_questions() == []
    assert [k for k, _ in a.sent] == ["keep"], "после снятого вопроса своё не ушло"
    a.close()


def test_keep_none_drops_a_stale_question_too(server, conf, tmp_path, monkeypatch):
    a = Agent(tmp_path, monkeypatch, fresh=True)
    a.db.set_state(a.s._KEEP_ASK_KEY, json.dumps({"why": "device", "hash": "ab" * 8, "items": {}}))
    w = Wire(server, 1, a, monkeypatch)
    assert w.hello()["reply"] == "keep_none"
    assert a.s.keep_section() == "" and [k for k, _ in a.sent] == ["keep"]
    a.close()


def test_the_same_block_again_does_not_ask_again(server, conf, tmp_path, monkeypatch):
    """keep_back того же блока на каждом переподключении — вопрос один:
    сохранённое молча обновлено, нового меню нет, своё не уходит."""
    a = Agent(tmp_path, monkeypatch, fresh=False)
    w = _asked_restore(server, a, monkeypatch)
    assert w.asks == ["keep"]
    blk = _keep(server)
    blk["at"] = "2026-10-06T10:00:00+03:00"                 # тот же отпечаток, другая дата
    server.db.set_state("gwkeep_1", json.dumps(blk))
    a.sent.clear()
    w.hello()
    assert w.asks == ["keep"], "тот же вопрос показан снова"
    assert a.sent == [] and a.s.keep_question()["at"].startswith("2026-10-06"), "сохранённое не обновлено"
    a.close()


def test_the_traffic_of_a_new_month_goes_at_once(server, conf, tmp_path, monkeypatch):
    """Счёт нового месяца начинается с нуля: ждать, пока он перерастёт прежний, нельзя."""
    a = Agent(tmp_path, monkeypatch, fresh=False)
    w = Wire(server, 1, a, monkeypatch)
    _gw_traffic(a, month="2026-09", rx=9000, tx=9000)
    w.hello()
    last = json.loads(a.db.get_state(a.s._KEEP_TRAFFIC_KEY))
    assert last["month"] == "2026-09" and last["sum"] == 18000
    a.sent.clear()
    _gw_traffic(a, rx=5, tx=5)                               # текущий месяц, сумма меньше прежней
    w.tick()
    sends = [b for k, b in a.sent if k == "keep"]
    assert len(sends) == 1 and sends[0]["traffic"]["rx"] == 5, "счёт нового месяца не ушёл"
    a.close()


def test_a_failed_accept_keeps_the_question_and_the_server_block(server, conf, tmp_path, monkeypatch):
    """«Вернуть» не применилось: своё серверу не уходит (единственная копия
    блока — там), вопрос остаётся, записанное в conf откатывается."""
    from awgbot.infra import gwguard
    a = Agent(tmp_path, monkeypatch, fresh=False)
    settings.set_value("quiet_hours.quiet_hours_start", 23)
    _offer(server, {"conf": {"quiet_hours.quiet_hours_start": 18}, "fw": {"SSH_FILTER": "1"}})
    w = Wire(server, 1, a, monkeypatch)
    w.hello()
    before = _keep(server)
    a.sent.clear()

    def boom(**kw):
        raise OSError("диск только для чтения")
    monkeypatch.setattr(gwguard, "write_env", boom)
    res = w.answer(True)
    assert res["ok"] and res["error"] == "диск только для чтения"
    assert settings.get("quiet_hours.quiet_hours_start") == 23, "conf не откатился"
    assert a.s.keep_section() == "keep" and not a.db.get_state(a.s._KEEP_ANSWERED_KEY)
    assert a.sent == [] and _keep(server) == before, "блок сервера затёрт своим"
    from awgbot.bot.texts.gateway.ask import keep_answer_text
    assert keep_answer_text(res) == ("⚠️ Настройки из бэкапа не применены: диск только для чтения — остались "
                                     "текущие, вопрос остаётся")
    a.close()


def test_a_bad_firewall_value_changes_nothing(server, conf, tmp_path, monkeypatch):
    """Значение файервола не проходит проверку — до первой записи: conf не тронут."""
    a = Agent(tmp_path, monkeypatch, fresh=False)
    settings.set_value("quiet_hours.quiet_hours_start", 23)
    res = a.s.keep_apply({"conf": {"quiet_hours.quiet_hours_start": 18}, "fw": {"ADMIN_IPS_EXTRA": "not-an-address"}})
    assert not res["ok"] and settings.get("quiet_hours.quiet_hours_start") == 23
    a.close()


def test_a_failed_silent_apply_sends_nothing_and_says_why(server, conf, tmp_path, monkeypatch):
    """Тот же шлюз заново, чистый: блок не встал — своё (умолчания) не уходит,
    в чате — почему; копия на сервере цела."""
    from awgbot.infra import gwguard
    old = Agent(tmp_path, monkeypatch, fresh=False, name="old")
    Wire(server, 1, old, monkeypatch).hello()
    settings.set_value("quiet_hours.quiet_hours_start", 20)
    gwguard.write_env(SSH_FILTER="1")
    asyncio.run(old.client.keep_tick())
    old.close()
    settings.set_value("quiet_hours.quiet_hours_start", 23)
    new = Agent(tmp_path, monkeypatch, fresh=True, name="new")
    before = _keep(server)

    def boom(**kw):
        raise OSError("нет места")
    monkeypatch.setattr(gwguard, "write_env", boom)
    w = Wire(server, 1, new, monkeypatch)
    w.hello()
    assert new.sent == [] and _keep(server) == before
    assert settings.get("quiet_hours.quiet_hours_start") == 23
    assert w.notes == ["⚠️ Настройки шлюза с сервера AWG не применены: нет места — остались текущие, копия на "
                       "сервере цела; повторю при следующем подключении к серверу"]
    new.close()


def test_the_firewall_waits_for_the_unit_and_the_tick_finishes(server, conf, tmp_path, monkeypatch):
    """Обвязка занята: файервол записан, отметка «ждёт перезапуска»; такт
    монитора добивает через общий троттлинг."""
    a = Agent(tmp_path, monkeypatch, fresh=False)
    a.s.reassert_guarded = lambda why, **kw: (False, "обвязка сейчас применяется")
    res = a.s.keep_apply({"conf": {}, "fw": {"SSH_FILTER": "1"}})
    assert res["ok"] and a.db.get_state(a.s._KEEP_FW_PENDING_KEY) == "1"
    calls = []
    a.s._reassert_throttled = lambda why: calls.append(why) or True
    assert a.s.keep_fw_finish() is True and calls == ["настройки файервола с сервера AWG"]
    assert not a.db.get_state(a.s._KEEP_FW_PENDING_KEY)
    assert a.s.keep_fw_finish() is False, "второй такт перезапустил снова"
    a.close()


def test_the_same_gateway_reinstalled_and_touched_gets_its_own_question(server, conf, tmp_path, monkeypatch):
    """Тот же шлюз заново, а до первой связи в нём поправили настройки: не
    «прежнее устройство до замены», а свой вопрос; ответ — своими словами."""
    old = Agent(tmp_path, monkeypatch, fresh=False, name="old")
    settings.set_value("quiet_hours.quiet_hours_start", 20)
    Wire(server, 1, old, monkeypatch).hello()
    old.close()
    settings.set_value("quiet_hours.quiet_hours_start", 23)
    new = Agent(tmp_path, monkeypatch, fresh=True, name="new")
    settings.set_value("quiet_hours.quiet_hours_start", 19)
    w = Wire(server, 1, new, monkeypatch)
    h = w.hello()
    assert h["body"]["why"] == "same" and new.s.cfg_questions() == ["keep_same"]
    from awgbot.bot.texts.gateway.ask import cfg_question_text, cfg_pending_line, keep_answer_text
    from awgbot.bot.keyboards.gateway import gateway_cfg_question_kb
    text = cfg_question_text(new.s.cfg_question("keep_same"))
    assert text.startswith("💾 <b>Настройки этого шлюза на сервере AWG</b>") and "до замены" not in text, text
    assert "отличаются от текущих: тихие часы" in text
    labels = [b.text for r in gateway_cfg_question_kb("keep_same").inline_keyboard for b in r]
    assert labels[:2] == ["⬅️ Оставить текущие", "↩️ Применить с сервера"], labels
    assert cfg_pending_line(["keep_same"]) == "⏳ Ждут решения: настройки шлюза с сервера AWG"
    assert server.gwkeep_card(1, agent_caps=["keep1"])["asked"] == "same"
    res = w.answer(True)
    assert settings.get("quiet_hours.quiet_hours_start") == 20
    assert keep_answer_text(res).startswith("✅ Настройки шлюза вернулись к сохранённым на сервере AWG от ")
    new.close()


def test_keep_ok_on_a_new_install_marks_it_synced(server, conf, tmp_path, monkeypatch):
    a = Agent(tmp_path, monkeypatch, fresh=True)
    w = Wire(server, 1, a, monkeypatch)
    w.hello()                                               # keep_none → своё → keep_ok
    assert a.db.get_state(a.s._KEEP_SYNCED_KEY) == "1" and not a.s.keep_new()
    a.close()


def test_a_migrated_slot_is_still_the_same_device(server, conf, tmp_path, monkeypatch):
    """Переезд профилей: у машины два ключа, на финале слот переходит к
    двойнику — переустановка той же машины не «другое устройство»."""
    old = Agent(tmp_path, monkeypatch, fresh=False, name="old")
    settings.set_value("quiet_hours.quiet_hours_start", 20)
    Wire(server, 1, old, monkeypatch).hello()
    old.close()
    dev = server.db.get_device(server.slot_dev)
    twin = server.add_device(server.admin_id_, "NASPi-двойник").device_id
    server.db.update_device_fields(twin, twin_of=dev.id)
    assert server._slot_same_device(1, server.db.get_device(twin).public_key), "в окне переезда двойник — чужой"
    server.db.gateway_update(1, device_id=twin)              # финал переезда
    server.db.update_device_fields(twin, twin_of=None)
    server.gwkeep_device_moved(1, dev.public_key, server.db.get_device(twin).public_key)
    assert _keep(server)["dev"] == server.db.get_device(twin).public_key
    settings.set_value("quiet_hours.quiet_hours_start", 23)
    new = Agent(tmp_path, monkeypatch, fresh=True, name="new")
    h = Wire(server, 1, new, monkeypatch).hello()
    assert h["body"]["why"] == "same", "переезд выдан за другое устройство"
    new.close()


def test_an_answered_device_block_is_not_asked_again(server, conf, tmp_path, monkeypatch):
    """Ответ запоминается для вопроса любого вида: тот же блок нового
    устройства на следующем hello — своё серверу, без вопроса."""
    old = Agent(tmp_path, monkeypatch, fresh=False, name="old")
    settings.set_value("quiet_hours.quiet_hours_start", 20)
    Wire(server, 1, old, monkeypatch).hello()
    old.close()
    _replace_device(server)
    settings.set_value("quiet_hours.quiet_hours_start", 23)
    new = Agent(tmp_path, monkeypatch, fresh=True, name="new")
    w = Wire(server, 1, new, monkeypatch)
    w.hello()
    new.client._writer = None                               # ответили без сессии
    assert w.answer(False)["ok"]
    new.client._writer = object()
    new.db.set_state(new.s._KEEP_SYNCED_KEY, "")             # ещё «новая» установка
    w.asks.clear(); new.sent.clear()
    w.hello()
    assert w.asks == [] and [k for k, _ in new.sent] == ["keep"]
    new.close()


def test_the_silent_install_note_goes_without_sound(server, conf, tmp_path, monkeypatch):
    old = Agent(tmp_path, monkeypatch, fresh=False, name="old")
    Wire(server, 1, old, monkeypatch).hello()
    settings.set_value("quiet_hours.quiet_hours_start", 20)
    asyncio.run(old.client.keep_tick())
    old.close()
    settings.set_value("quiet_hours.quiet_hours_start", 23)
    new = Agent(tmp_path, monkeypatch, fresh=True, name="new")
    w = Wire(server, 1, new, monkeypatch)
    w.hello()
    assert len(w.notes) == 1 and w.silent == [True], (w.notes, w.silent)
    new.close()


def test_the_traffic_mark_keeps_its_month(server, conf, tmp_path, monkeypatch):
    a = Agent(tmp_path, monkeypatch, fresh=False)
    _gw_traffic(a, rx=1, tx=2)
    a.s.keep_sent(a.s.keep_block())
    assert json.loads(a.db.get_state(a.s._KEEP_TRAFFIC_KEY))["month"] == timeutil.now().strftime("%Y-%m")
    a.close()
