"""
Токен бота шлюза с сервера AWG на агенте (domain/gateway/cfg/token.py,
раздел token документа конфигурации, возможность token1): тот же бот —
getMe новым токеном, запись BOT_TOKEN в env агента, ответ серверу, затем
перезапуск агента; другой бот или токен, которого Telegram не знает, —
отказ; Telegram не ответил — повтор.

Смена аварийная: прежний токен отозван в BotFather, бот шлюза из Telegram
пропал, и довезти новый может только канал. Цена ошибки: токен другого бота,
принятый агентом, уводит чат админа со шлюзом к чужому боту; env, переписанный
не целиком (потеряны ADMIN_ID и прочие строки) или с правами шире 0600, —
агент не стартует или секрет читает кто угодно; перезапуск агента до ответа
серверу — сервер не узнает, что токен применён, и повезёт его снова;
ответ «failed» на временный сбой сети — сервер больше не повторит, а шлюз
так и останется на мёртвом токене.

Хост подменён: env агента — настоящий файл во временном каталоге
(AWG_BOT_ENV), getMe — подмена `_cfg_token_getme` (а сам он — подменой
`urllib.request.urlopen`); юнит обвязки и конфиг линка — `_Host` из теста
документа конфигурации.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import urllib.error
import urllib.request

import pytest

from awgbot.core import config
from awgbot.domain import gwcfg
from awgbot.domain.gateway.cfg import token as tokenmod
from tests.unit import test_gw_cfg_apply as _apply
from tests.unit import _linkclient_session_common as _ls

pytestmark = pytest.mark.unit

# фиктивные токены: id до двоеточия — номер бота
CUR = "123456789:AA-dummy-token-for-tests"          # на чём агент работает сейчас
NEW = "123456789:AA-new-dummy-token-tests"          # новый токен того же бота
OTHER = "987654321:BB-other-bot-token-dummy"        # токен другого бота

# фикстуры теста документа конфигурации: копия conf, сервисы агента, хост
conf_dir, svc, host = _apply.conf_dir, _apply.svc, _apply.host
# фикстуры теста клиента канала
gateway_role, conf, cfg_hooks = _ls.gateway_role, _ls.conf, _ls.cfg_hooks

ENV_TEXT = ("# awg-bot\n"
            f"BOT_TOKEN={CUR}\n"
            "ADMIN_ID=42\n"
            "ROLE=gateway\n")


class _Tg:
    """getMe новым токеном: answer — что ответит Telegram ("ok", id) |
    ("bad", 0) | ("net", 0); calls — токены, по которым спрашивали."""

    def __init__(self, monkeypatch, svc):
        self.answer: tuple[str, int] = ("ok", 123456789)
        self.calls: list[str] = []
        monkeypatch.setattr(svc, "_cfg_token_getme", self._getme)

    def _getme(self, token):
        self.calls.append(token)
        return self.answer


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """env агента — настоящий файл с токеном и соседними строками; агент
    работает на CUR."""
    path = tmp_path / "etc" / "env"
    path.parent.mkdir()
    path.write_text(ENV_TEXT, encoding="utf-8")
    os.chmod(path, 0o600)
    monkeypatch.setenv("AWG_BOT_ENV", str(path))
    monkeypatch.setattr(config, "BOT_TOKEN", CUR)
    return path


@pytest.fixture()
def tg(monkeypatch, svc):
    return _Tg(monkeypatch, svc)


# ── cfg_token_apply ──────────────────────────────────────────────────────────

def test_the_same_token_is_same_and_asks_nobody(svc, env, tg):
    """Документ с тем же токеном (сервер просто вписал его в env ВПС) —
    «same»: ни getMe, ни перезаписи env, ни перезапуска, ни заметки."""
    res = svc.cfg_token_apply({"value": CUR})
    assert res == {"state": "same", "error": ""}, res
    assert tg.calls == [], "тот же токен проверяли в Telegram"
    assert env.read_text(encoding="utf-8") == ENV_TEXT, "env тронут тем же токеном"
    assert svc.cfg_token_note_take() is False, "заметка «на новом токене» без смены"


@pytest.mark.parametrize("state, want", [("accepted", "applied"), ("linked", "applied"), ("done", "same"),
                                         ("pairing", "same")])
def test_the_same_token_restarts_an_agent_finishing_a_pairing(svc, env, tg, monkeypatch, state, want):
    """flow-3: Т-Т11 с тем же ботом — конец сопряжения на старте полного агента: перезапуск, env цел."""
    monkeypatch.setattr(svc, "join_state", lambda: {"state": state})
    assert svc.cfg_token_apply({"value": CUR})["state"] == want
    assert tg.calls == [] and env.read_text(encoding="utf-8") == ENV_TEXT


def test_a_token_of_another_bot_is_taken_when_telegram_knows_it(svc, env, tg):
    """Токен нового бота (прежнего удалили вовсе) — принимается: годность
    бота шлюзу проверил сервер при вводе; агенту важно, что токен живой."""
    tg.answer = ("ok", 987654321)
    res = svc.cfg_token_apply({"value": OTHER})
    assert res == {"state": "applied", "error": ""}, res
    assert tg.calls == [OTHER], "новый бот не проверен в Telegram"
    lines = env.read_text(encoding="utf-8").splitlines()
    assert f"BOT_TOKEN={OTHER}" in lines and "ADMIN_ID=42" in lines, lines


def test_a_token_whose_id_telegram_denies_is_refused(svc, env, tg):
    """Новый бот, но getMe назвал другой id, чем в токене, — токен склеен или
    подделан: не пишем."""
    tg.answer = ("ok", 123456789)
    res = svc.cfg_token_apply({"value": OTHER})
    assert res == {"state": "failed", "error": "Telegram не принял новый токен"}, res
    assert env.read_text(encoding="utf-8") == ENV_TEXT


def test_telegram_silent_means_retry_and_env_untouched(svc, env, tg):
    """getMe не ответил (аплинк лежит, Telegram недоступен) — retry: сервер
    повторит; «failed» здесь оставил бы шлюз на мёртвом токене навсегда."""
    tg.answer = ("net", 0)
    res = svc.cfg_token_apply({"value": NEW})
    assert res["state"] == "retry" and res["error"], res
    assert tg.calls == [NEW]
    assert env.read_text(encoding="utf-8") == ENV_TEXT, "env переписан без ответа Telegram"
    assert svc.cfg_token_note_take() is False


def test_a_token_telegram_does_not_know_is_refused(svc, env, tg):
    """Telegram сказал 401/404 — токен не живой: записать его значило бы
    уронить агента на старте."""
    tg.answer = ("bad", 0)
    res = svc.cfg_token_apply({"value": NEW})
    assert res == {"state": "failed", "error": "Telegram не принял новый токен"}, res
    assert env.read_text(encoding="utf-8") == ENV_TEXT


def test_getme_naming_another_bot_is_refused(svc, env, tg):
    """Префикс тот же, а getMe вернул другой id — токен подделан или склеен
    из двух: не тот бот, не пишем."""
    tg.answer = ("ok", 555555555)
    res = svc.cfg_token_apply({"value": NEW})
    assert res == {"state": "failed", "error": "Telegram не принял новый токен"}, res
    assert env.read_text(encoding="utf-8") == ENV_TEXT


def test_the_same_bot_rewrites_only_bot_token_in_a_private_env(svc, env, tg):
    """Тот же бот — в env новый BOT_TOKEN, одной строкой; ADMIN_ID, ROLE и
    комментарий на месте (без них агент не стартует); права 0600 — в файле
    секрет; «applied» и заметка для чата после перезапуска."""
    res = svc.cfg_token_apply({"value": NEW})
    assert res == {"state": "applied", "error": ""}, res
    lines = env.read_text(encoding="utf-8").splitlines()
    assert [ln for ln in lines if ln.startswith("BOT_TOKEN=")] == [f"BOT_TOKEN={NEW}"], lines
    for keep in ("# awg-bot", "ADMIN_ID=42", "ROLE=gateway"):
        assert keep in lines, f"строка {keep!r} пропала из env при смене токена: {lines}"
    assert CUR not in env.read_text(encoding="utf-8"), "прежний токен остался в env"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600, oct(env.stat().st_mode)
    assert not [p for p in env.parent.iterdir() if p.name != "env"], "временный файл записи остался"


def test_the_note_is_said_once_after_the_restart(svc, env, tg):
    """Заметка «бот на новом токене» — одна: второй старт агента молчит."""
    svc.cfg_token_apply({"value": NEW})
    assert svc.cfg_token_note_take() is True
    assert svc.cfg_token_note_take() is False, "заметка о смене токена повторилась бы на каждом старте"


def test_the_note_stays_pending_until_cleared(svc, env, tg):
    """pending не снимает метку сам: сообщение не дошло — скажем при
    следующем старте; clear — после доставки."""
    svc.cfg_token_apply({"value": NEW})
    assert svc.cfg_token_note_pending() is True
    assert svc.cfg_token_note_pending() is True, "проверка сняла метку"
    svc.cfg_token_note_clear()
    assert svc.cfg_token_note_pending() is False


def test_a_rejected_token_with_markup_is_escaped_in_the_note(svc, host, env, tg, monkeypatch):
    """Причина отказа идёт в HTML-сообщение — экранируется."""
    monkeypatch.setattr(svc, "cfg_token_apply", lambda v: {"state": "failed", "error": "env не переписан (<b>RO</b>)"})
    res = svc.apply_cfg(_apply._doc(token={"value": NEW}))
    assert "&lt;b&gt;RO&lt;/b&gt;" in res["note"] and "<b>" not in res["note"], res["note"]


def test_a_missing_env_gets_just_the_token(svc, env, tg):
    """env нет (снесли руками) — создаётся с одной строкой токена и правами 0600."""
    env.unlink()
    res = svc.cfg_token_apply({"value": NEW})
    assert res["state"] == "applied", res
    assert env.read_text(encoding="utf-8") == f"BOT_TOKEN={NEW}\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_an_unwritable_env_fails_without_the_note(svc, env, tg, tmp_path, monkeypatch):
    """env не переписать (путь занят файлом вместо каталога, диск RO) —
    «failed» с причиной и без заметки: перезапуск на прежнем токене ничего
    бы не дал."""
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("AWG_BOT_ENV", str(blocker / "env"))
    res = svc.cfg_token_apply({"value": NEW})
    assert res["state"] == "failed" and res["error"].startswith("файл настроек агента не переписан"), res
    assert svc.cfg_token_note_take() is False


def test_a_repeated_document_leaves_one_token_line(svc, env, tg):
    """Документ пришёл ещё раз до перезапуска (сессия умерла до ответа): env
    тот же — одна строка BOT_TOKEN, соседние строки целы."""
    svc.cfg_token_apply({"value": NEW})
    first = env.read_text(encoding="utf-8")
    svc.cfg_token_apply({"value": NEW})
    assert env.read_text(encoding="utf-8") == first, "повтор документа испортил env"


# ── getMe новым токеном ──────────────────────────────────────────────────────

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _urlopen(monkeypatch, result):
    """urlopen: result — байты ответа или исключение."""
    seen = []

    def fake(url, timeout=None):
        seen.append((url, timeout))
        if isinstance(result, BaseException):
            raise result
        return _Resp(result)
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return seen


def _http(code):
    return urllib.error.HTTPError("https://api.telegram.org/bot…/getMe", code, "x", {}, None)


@pytest.mark.parametrize("result, want", [
    (json.dumps({"ok": True, "result": {"id": 123456789, "is_bot": True}}).encode(), ("ok", 123456789)),
    (json.dumps({"ok": False, "description": "Unauthorized"}).encode(), ("bad", 0)),
    (_http(401), ("bad", 0)),
    (_http(404), ("bad", 0)),
    (_http(502), ("net", 0)),
    (urllib.error.URLError("no route"), ("net", 0)),
    (TimeoutError("timed out"), ("net", 0)),
    (b"<html>captive portal</html>", ("net", 0)),
])
def test_getme_tells_a_dead_token_from_a_silent_telegram(svc, monkeypatch, result, want):
    """401/404 — токен мёртв (failed), всё прочее, включая 5xx, таймаут и не
    JSON, — Telegram не ответил (retry): спутай их — шлюз либо навсегда на
    мёртвом токене, либо сервер вечно возит токен, которого нет."""
    seen = _urlopen(monkeypatch, result)
    assert svc._cfg_token_getme(NEW) == want
    assert seen and seen[0][0] == f"https://api.telegram.org/bot{NEW}/getMe"
    assert seen[0][1] == tokenmod.GETME_TIMEOUT, "getMe без таймаута повесил бы применение документа"


# ── документ целиком ─────────────────────────────────────────────────────────

def test_the_document_applies_the_token_and_asks_for_a_restart(svc, host, env, tg):
    """Раздел token тот же бот — applied, в changed, restart=True (перезапуск
    сделает клиент канала после ответа); поколение записано; человеку —
    «применено: токен бота»."""
    res = svc.apply_cfg(_apply._doc(token={"value": NEW}))
    assert "token" in res["ack"]["applied"] and not res["ack"]["failed"], res["ack"]
    assert res["restart"] is True and "token" in res["changed"], res
    assert res["note"].endswith("применено: токен бота"), res["note"]
    assert f"BOT_TOKEN={NEW}" in env.read_text(encoding="utf-8")
    assert svc.db.get_state(svc._CFG_GEN_KEY) == "7"
    assert host.restarts == 0, "токен перевыставил обвязку"


def test_the_same_token_in_the_document_is_applied_silently(svc, host, env, tg):
    res = svc.apply_cfg(_apply._doc(token={"value": CUR}))
    assert "token" in res["ack"]["applied"] and res["restart"] is False, res
    assert res["note"] == "", "тот же токен — уведомление ни о чём"


def test_a_silent_telegram_makes_the_document_retry_without_saving_gen(svc, host, env, tg):
    """getMe не ответил — ack с retry и причиной token; поколение не
    записано (сервер повторит), перезапуска нет, человеку — тишина."""
    tg.answer = ("net", 0)
    res = svc.apply_cfg(_apply._doc(token={"value": NEW}))
    assert res["ack"].get("retry") is True and "token" in res["ack"]["failed"], res["ack"]
    assert "token" not in res["ack"]["applied"]
    assert res["restart"] is False and res["note"] == "", res
    assert not svc.db.get_state(svc._CFG_GEN_KEY), "поколение записано при временном отказе"


def test_a_token_of_another_bot_in_the_document_restarts_the_agent(svc, host, env, tg):
    """Документ с токеном нового бота — applied и перезапуск, как у того же."""
    tg.answer = ("ok", 987654321)
    res = svc.apply_cfg(_apply._doc(token={"value": OTHER}))
    assert "token" in res["ack"]["applied"] and res["restart"] is True, res
    assert f"BOT_TOKEN={OTHER}" in env.read_text(encoding="utf-8")


def test_a_dead_token_fails_the_section(svc, host, env, tg):
    """Telegram токен не знает — failed без retry: повтор того же документа
    дал бы тот же отказ; env и поколение не тронуты."""
    tg.answer = ("bad", 0)
    res = svc.apply_cfg(_apply._doc(token={"value": OTHER}))
    assert res["ack"]["failed"] == {"token": "Telegram не принял новый токен"}, res["ack"]
    # бот на прежнем токене может быть жив — человеку строка с причиной
    assert res["note"] == "⚠️ Токен бота с сервера AWG не применён: Telegram не принял новый токен", res["note"]
    assert not res["ack"].get("retry") and res["restart"] is False
    assert env.read_text(encoding="utf-8") == ENV_TEXT
    assert not svc.db.get_state(svc._CFG_GEN_KEY)


def test_a_document_without_token_does_not_touch_the_env(svc, host, env, tg):
    """Агенту без раздела token (у сервера токена слота нет) своё не трогают."""
    res = svc.apply_cfg(_apply._doc())
    assert res["restart"] is False and "token" not in res["ack"]["applied"]
    assert tg.calls == [] and env.read_text(encoding="utf-8") == ENV_TEXT


def test_the_snapshot_print_follows_the_running_token(svc, host, env, monkeypatch):
    """Отпечаток токена в снимке — того, на чём агент работает; нет токена —
    пусто (сервер по нему понимает «на шлюзе токена нет»)."""
    assert svc.cfg_fingerprints()["token"] == gwcfg.token_print(CUR)
    monkeypatch.setattr(config, "BOT_TOKEN", NEW)
    assert svc.cfg_fingerprints()["token"] == gwcfg.token_print(NEW) != gwcfg.token_print(CUR)
    monkeypatch.setattr(config, "BOT_TOKEN", "")
    assert svc.cfg_fingerprints()["token"] == ""


# ── клиент канала: перезапуск после ответа ───────────────────────────────────

class _RestartAgent(_ls._CfgAgent):
    """Агент клиента канала: события — когда ответ лёг в очередь, когда ушёл,
    когда звали перезапуск."""

    def __init__(self, result):
        super().__init__(result)
        self.events: list[str] = []

    def cfg_ack_pending_put(self, ack):
        self.events.append("queued")
        super().cfg_ack_pending_put(ack)

    def cfg_ack_pending_clear(self):
        self.events.append("sent")
        super().cfg_ack_pending_clear()

    def restart_bot(self):
        self.events.append("restart")


def _result(restart: bool, changed=None, note: str = "") -> dict:
    return {"ack": gwcfg.ack_build(7, "ab" * 8, applied=["env", "token"]), "note": note, "ask": [],
            "changed": list(changed) if changed is not None else (["token"] if restart else []),
            "restart": restart}


@pytest.fixture()
def no_sleep(monkeypatch):
    slept = []

    async def _sleep(s):
        slept.append(s)
    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return slept


async def test_the_agent_restarts_only_after_the_answer_went(gateway_role, conf, cfg_hooks, no_sleep):
    """restart — после того, как cfg_ack ушёл серверу: иначе процесс умер бы
    с неотправленным ответом, и сервер повёз бы токен снова."""
    agent = _RestartAgent(_result(True))
    client = _ls._live_client(agent)
    await client._apply_cfg({"t": "cfg", "gen": 7})
    assert agent.events == ["queued", "sent", "restart"], agent.events
    assert [m["t"] for m in _ls._sent(client)].count("cfg_ack") == 1


async def test_a_lost_answer_stays_queued_before_the_restart(gateway_role, conf, cfg_hooks, no_sleep):
    """Сессия умерла раньше ответа — ответ лежит в очереди (уйдёт первым
    словом новой сессии уже нового процесса), и только потом перезапуск."""
    agent = _RestartAgent(_result(True))
    client = _ls._live_client(agent)

    def broken(data):
        raise ConnectionResetError("линк перезапущен")
    client._writer.write = broken
    await client._apply_cfg({"t": "cfg", "gen": 7})
    assert agent.events == ["queued", "restart"], agent.events
    assert agent.pending.get("gen") == 7, "ответ потерян перед перезапуском"


async def test_no_restart_without_a_new_token(gateway_role, conf, cfg_hooks, no_sleep):
    """Документ без смены токена — агент не перезапускается."""
    agent = _RestartAgent(_result(False))
    client = _ls._live_client(agent)
    await client._apply_cfg({"t": "cfg", "gen": 7})
    assert "restart" not in agent.events, agent.events


def test_token1_is_offered_by_both_sides():
    """Возможность token1 — в hello агента и в role сервера: без неё сервер
    не везёт раздел token, агент не ждёт его."""
    from awgbot.runtime.linkclient.common import CAPS
    from awgbot.runtime.linkserver.common import SERVER_CAPS
    assert "token1" in CAPS and "token1" in SERVER_CAPS, (CAPS, SERVER_CAPS)


async def test_a_token_only_change_is_not_told_by_the_revoked_bot(gateway_role, conf, cfg_hooks, no_sleep):
    """Сменился только токен — итог «⚙️ Сервер AWG прислал конфигурацию…» в
    чат не уходит: слал бы его ещё прежний, отозванный бот; человеку скажет
    сообщение после перезапуска уже на новом токене."""
    note = "⚙️ Сервер AWG прислал конфигурацию шлюза — применено: токен бота"
    agent = _RestartAgent(_result(True, note=note))
    client = _ls._live_client(agent)
    await client._apply_cfg({"t": "cfg", "gen": 7})
    assert cfg_hooks["notes"] == [], f"итог о токене ушёл через отозванного бота: {cfg_hooks['notes']}"
    assert agent.events[-1] == "restart", agent.events


async def test_a_token_with_other_changes_tells_the_rest_then_restarts(gateway_role, conf, cfg_hooks,
                                                                      no_sleep, monkeypatch):
    """Вместе с токеном сменилось и другое (устройства админа) — итог уходит
    как всегда и ДО перезапуска: после него процесс другой, и итог потерялся бы."""
    note = "⚙️ Сервер AWG прислал конфигурацию шлюза — применено: устройства админа, токен бота"
    agent = _RestartAgent(_result(True, changed=["ADMIN_IPS", "token"], note=note))

    async def notify(text):
        agent.events.append("note")
        cfg_hooks["notes"].append(text)
    monkeypatch.setattr(_ls.linkclient, "_notify", notify)
    client = _ls._live_client(agent)
    await client._apply_cfg({"t": "cfg", "gen": 7})
    assert cfg_hooks["notes"] == [note], cfg_hooks["notes"]
    assert agent.events == ["queued", "sent", "note", "restart"], agent.events


async def test_a_note_without_a_restart_still_goes(gateway_role, conf, cfg_hooks, no_sleep):
    """Без перезапуска (токен тот же) итог уходит, как раньше: тишина только
    для смены токена."""
    note = "⚙️ Сервер AWG прислал конфигурацию шлюза — применено: имя сервера"
    agent = _RestartAgent(_result(False, changed=["server"], note=note))
    client = _ls._live_client(agent)
    await client._apply_cfg({"t": "cfg", "gen": 7})
    assert cfg_hooks["notes"] == [note] and "restart" not in agent.events, (cfg_hooks, agent.events)


async def test_questions_wait_for_the_new_bot_on_restart(gateway_role, conf, cfg_hooks, no_sleep):
    """Перезапуск на новом токене — вопросы о почте и фразе из того же
    документа не показываются: их показал бы прежний, отозванный бот (агент
    покажет их после старта)."""
    agent = _RestartAgent({**_result(True), "ask": ["mail"]})
    client = _ls._live_client(agent)
    await client._apply_cfg({"t": "cfg", "gen": 7})
    assert cfg_hooks["asks"] == [], cfg_hooks
    assert agent.events[-1] == "restart"


async def test_questions_show_without_a_restart(gateway_role, conf, cfg_hooks, no_sleep):
    agent = _RestartAgent({**_result(False), "ask": ["mail"]})
    client = _ls._live_client(agent)
    await client._apply_cfg({"t": "cfg", "gen": 7})
    assert cfg_hooks["asks"] == ["mail"], cfg_hooks


# ── журнал ВПС: токен из текста ошибки ───────────────────────────────────────

@pytest.mark.parametrize("text", [
    f"Cannot connect to host api.telegram.org:443 ssl:default [https://api.telegram.org/bot{NEW}/getMe]",
    f"Telegram server says - Unauthorized: bot{OTHER}",
])
def test_bot_tokens_are_cut_from_error_texts(text):
    """Текст исключения aiohttp/aiogram несёт URL запроса с токеном; в журнал
    (а с ним — в бэкапы и диагностику) он не попадает."""
    from awgbot.runtime import gwbotme
    out = gwbotme._no_token(text)
    for tok in (NEW, OTHER):
        assert tok.split(":", 1)[1] not in out, out
    assert "bot<токен>" in out, out
    assert out.split("bot<токен>")[0] == text.split("bot")[0], "вырезано больше, чем токен"


def test_texts_without_tokens_are_left_alone():
    from awgbot.runtime import gwbotme
    assert gwbotme._no_token("сеть не готова: timeout") == "сеть не готова: timeout"
