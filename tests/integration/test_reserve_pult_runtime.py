"""Пульт у основы — слой runtime и запасной путь из терминала:

- исполнитель действий (runtime/rsvactions): `pult_probe` спрашивает getWebhookInfo (без
  снятия вебхука, дата ошибки — секунды эпохи) и отдаёт ответ домену; `pult_take` —
  `deleteWebhook` поллинга и отметка пульта, не прошёл — отметки нет;
- поллинг (runtime/pollctl, макет Bot API): 409 при пульте снимает отметку с итогом
  админу — по факту Telegram, не по нажатию;
- канал пары: `pult_back` доходит до сервисов резерва и их действие — гостю;
- гость (runtime/main/guest, guestctl): `tg_retake` снимает запрет и ставит вебхук снова,
  решения снова пишет, рассылки «переподключи VPN» второй раз нет;
- `awg-bot reserve home --force` (runtime/rsvhome, tools/rsvhome.py, awg-bot.sh): запрос
  забирает бот и делает то же, что рычаг из меню, — те же отказы; пульт не забран —
  сначала он; просроченный запрос не исполняется; CLI — коды 0/3/4/5/2, незабранный
  запрос убирается.

Цена ошибки: проба, снимающая вебхук, — Telegram отнят у живого бота резерва; отметка
пульта без снятого вебхука — пользователям «бот на техработах», а отвечает им резерв;
отметка, пережившая 409, — основа считает Telegram своим, а он у гостя; гость, не
поставивший вебхук по «отдаю», — Telegram у основы молчит по кнопке, бот у
пользователей «бот на техработах» навсегда; запрос из терминала, исполненный позже
отказа по сроку, — админ ушёл чинить руками, а бот тем временем вернул пользователей;
CLI с другим кодом, чем итог, — скрипт админа решает неправильно.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import subprocess
import threading
import time
import types
from pathlib import Path

import pytest

from awgbot.core import config
from awgbot.runtime import pollctl, rsvactions, rsvhome
from awgbot.runtime.main import guest as guest_main
from awgbot.runtime.pairlink import guestctl
from awgbot.runtime.pairlink.inbound import InboundMixin
from awgbot.util import pairproto, timeutil
from tests.conftest import FakeBot
from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration.test_reserve_pollctl import Lab, _REAL_MAKE_REQUEST
from tools import rsvhome as rsvhome_cli

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
ROOT = Path(__file__).resolve().parents[2]
HOOK = "https://203.0.113.9:8443/DUMMY"


def _ago(minutes: float) -> str:
    return timeutil.to_iso(timeutil.shift(timeutil.now(), -minutes * 60))


class _Bot(FakeBot):
    """Сессия Bot API: getWebhookInfo отвечает так, как скажет тест."""

    def __init__(self, url: str = HOOK, pending: int = 0, err_ago: float | None = None, fail: bool = False):
        super().__init__()
        self.url, self.pending, self.err_ago, self.fail = url, pending, err_ago, fail
        self.asked = 0

    async def get_webhook_info(self):
        self.asked += 1
        if self.fail:
            raise RuntimeError("Telegram не ответил")
        err = None if self.err_ago is None else datetime.datetime.fromtimestamp(time.time() - self.err_ago,
                                                                               tz=datetime.timezone.utc)
        return types.SimpleNamespace(url=self.url, pending_update_count=self.pending, last_error_date=err)


class _Poll:
    """Управление поллингом: take() — deleteWebhook без сброса очереди."""

    def __init__(self, svc=None, ok: bool = True):
        self.svc, self.ok, self.takes = svc, ok, 0

    async def take(self) -> bool:
        self.takes += 1
        if self.ok and self.svc is not None:
            await asyncio.to_thread(self.svc.reserve_set_poll, "mine")
        return self.ok

    def wake(self) -> None:
        pass


@pytest.fixture()
def ctx(monkeypatch):
    box: dict = {}
    monkeypatch.setattr(rsvactions, "_ctx", box)
    return box


@pytest.fixture()
def gone(mv):
    """Мои пользователи у резерва, он не на связи 11 минут, запись меняет бот."""
    svc = mv.svc
    rm.go_away(svc, kind="dead")
    rm.automatic(svc)
    svc.db.set_state("last_server_ok", "0")
    svc.rsv_update("rsv_peer_state", session=False, down_since=_ago(11))
    svc._lv.state = "down"
    return mv


# ── исполнитель действий ────────────────────────────────────────────────────

async def test_the_probe_asks_telegram_without_removing_the_webhook_and_answers_the_domain(ctx):
    got = []
    svc = types.SimpleNamespace(reserve_pult_probe_in=lambda info: got.append(info) or [])
    bot = _Bot(pending=7, err_ago=30)
    ctx.update(bot=bot, services=svc, pollctl=_Poll())
    await rsvactions.run([("pult_probe",)])
    assert bot.asked == 1 and len(got) == 1
    assert got[0]["url"] == HOOK and got[0]["pending_update_count"] == 7
    assert abs(got[0]["last_error_date"] - (time.time() - 30)) < 5, "дата ошибки — не секунды эпохи"
    assert ctx["pollctl"].takes == 0, "проба сняла вебхук"
    bot.fail = True
    await rsvactions.run([("pult_probe",)])
    assert got[-1] is None, "Telegram не ответил — домену None, а не исключение"


@pytest.mark.parametrize("ok", [True, False])
async def test_taking_the_pult_removes_the_webhook_first(ctx, ok):
    marks = []
    svc = types.SimpleNamespace(reserve_pult_taken=lambda: marks.append(1) or [])
    ctx.update(bot=_Bot(), services=svc, pollctl=_Poll(ok=ok))
    await rsvactions.run([("pult_take",)])
    assert ctx["pollctl"].takes == 1
    assert marks == ([1] if ok else []), "отметка пульта без снятого вебхука"


# ── поллинг: 409 снимает отметку ────────────────────────────────────────────

@pytest.fixture()
async def lab(services, rsv, tmp_path, monkeypatch, ctx):
    from aiogram.client.session.aiohttp import AiohttpSession
    monkeypatch.setattr(pollctl, "POLL_TIMEOUT", 1)
    monkeypatch.setattr(AiohttpSession, "make_request", _REAL_MAKE_REQUEST)
    lab = Lab(tmp_path)
    monkeypatch.setattr(pollctl, "asyncio", lab.waits.mod)
    await lab.start(services)
    sent = []

    async def notify(bot, notes):
        sent.extend(notes)
    import awgbot.bot.notifier as notifier
    monkeypatch.setattr(notifier, "send_notifications", notify)
    lab.sent = sent
    ctx.update(bot=lab.bot, services=services)              # исполнитель действий уведомит этим ботом
    yield lab
    await lab.stop()


async def test_a_conflict_while_the_pult_is_here_drops_the_mark(lab, services):
    rc.pair_row(services)
    services.rsv_update("rsv_out", state="away", pult_at=timeutil.now_iso())
    lab.run()
    assert await lab.until(lambda: lab.ctl.polling())
    assert services.reserve_pult_on()
    lab.webhook()                                            # гость поставил вебхук снова
    assert await lab.until(lambda: not services.reserve_pult_on()), "409 не снял отметку пульта"
    assert await lab.until(lambda: services.reserve_poll() == "away")
    assert await lab.until(lambda: [n.text for n in lab.sent] == ["📤 Управление ботом передано на «Beta»"])


async def test_a_webhook_found_at_the_start_drops_the_mark_too(lab, services):
    rc.pair_row(services)
    services.rsv_update("rsv_out", state="away", pult_at=timeutil.now_iso())
    lab.webhook()
    lab.run()
    assert await lab.until(lambda: services.reserve_poll() == "away")
    assert await lab.until(lambda: not services.reserve_pult_on())


# ── канал пары и гость ──────────────────────────────────────────────────────

async def test_the_word_reaches_the_reserve_services_and_their_action_runs():
    calls, acts = [], []

    class _In(InboundMixin):
        services = types.SimpleNamespace(reserve_pult_back_in=lambda at: calls.append(at) or [("guest", "tg_retake", {})])

        async def _actions(self, a):
            acts.extend(a)

    sess = types.SimpleNamespace(listener=True, faults={})
    await _In()._handle(sess, {"t": "pult_back", "at": "2026-11-01T10:00:00+05:00"})
    assert calls == ["2026-11-01T10:00:00+05:00"] and acts == [("guest", "tg_retake", {})]


async def test_the_guest_retake_lifts_the_hold_and_sets_the_webhook_again(services):
    services.guest_state_in({"name": "Beta", "tg_hold": "Telegram бота основы у неё — жду, пока отдаст"})
    services.guest_webhook_foreign()                          # надзор видел чужой адрес — решений не пишу
    assert services.reserve_following()
    ctl = guestctl.GuestCtlClient(services, path="/nonexistent")
    await ctl._handle({"kind": "tg_retake"})
    assert ctl.retake.is_set() and ctl.take_tg.is_set() and services.guest_can_take_tg()
    tg_set = asyncio.Event()
    tg_set.set()                                              # вебхук этот гость уже ставил
    assert guest_main._retake_begin(ctl, tg_set) is True and not tg_set.is_set(), "повторной постановки не будет"
    await guest_main._retake_done(ctl, services)
    assert not ctl.retake.is_set() and not services.reserve_following(), "вебхук снова мой, а решения не пишу"
    assert guest_main._retake_begin(ctl, tg_set) is False


def test_the_guest_without_the_word_does_not_set_again():
    tg_set = asyncio.Event()
    tg_set.set()
    assert guest_main._retake_begin(types.SimpleNamespace(), tg_set) is False and tg_set.is_set()


def test_the_retake_branch_skips_the_second_move_notice():
    """Ветка повторной постановки в такте гостя — без «tg_taken» и рассылки «переподключи VPN»."""
    src = (ROOT / "awgbot" / "runtime" / "main" / "guest.py").read_text(encoding="utf-8")
    i = src.index("if again:")
    branch = src[i:src.index("else:", i)]
    assert "_retake_done" in branch and "notify_at" not in branch and "tg_taken" not in branch


# ── awg-bot reserve home --force: бот ───────────────────────────────────────

def _request(until_s: float = 30) -> str:
    rid = "abc123"
    rsvhome.req_path().parent.mkdir(parents=True, exist_ok=True)
    rsvhome.req_path().write_text(json.dumps({"id": rid, "until": time.time() + until_s}), encoding="utf-8")
    return rid


def _result() -> dict:
    return json.loads(rsvhome.res_path().read_text(encoding="utf-8"))


async def test_the_bot_takes_the_pult_first_then_runs_the_lever(gone, ctx):
    svc = gone.svc
    pc = _Poll(svc)
    ctx.update(bot=_Bot(pending=12, err_ago=60), services=svc, pollctl=pc)
    rid = _request()
    await rsvhome.tick()
    assert not rsvhome.req_path().exists(), "запрос не убран — CLI решит, что бот его не взял"
    res = _result()
    assert res["id"] == rid and res["rc"] == 0, res
    assert res["text"] == (f"пользователи возвращаются сюда: запись {rc.HOST_A} → {rm.OWN_IP}, Telegram у этого "
                           "сервера; изменения с «Beta» придут, когда он выйдет на связь"), res["text"]
    assert pc.takes == 2, "пульт не забран отдельно перед рычагом"
    out = svc.rsv_get("rsv_out")
    assert out["await_merge"] is True and svc.reserve_out() == "home" and gone.dns.sets == [rm.OWN_IP]
    texts = [r[2] for r in ctx["bot"].records if r[0] == "send_message"]
    assert not any("🎛" in t for t in texts), "из терминала — уведомление о пульте поверх итога рычага"


async def test_with_the_pult_already_here_the_lever_goes_without_a_second_pult(gone, ctx):
    svc = gone.svc
    svc.reserve_pult_taken()
    pc = _Poll(svc)
    ctx.update(bot=_Bot(url=""), services=svc, pollctl=pc)
    _request()
    await rsvhome.tick()
    assert _result()["rc"] == 0 and pc.takes == 1


@pytest.mark.parametrize("prep, bot, word", [
    (lambda s: s.reserve_set_out("home"), _Bot(), "не у резерва"),
    (lambda s: s.rsv_update("rsv_peer_state", down_since=_ago(5)), _Bot(), "меньше 10 мин"),
    (lambda s: None, _Bot(pending=1), "отвечает в Telegram"),        # Q4-09: бот на резерве жив
], ids=["not-away", "too-early", "reserve-answers"])
async def test_the_bot_refuses_with_the_lever_reasons(gone, ctx, prep, bot, word):
    svc = gone.svc
    prep(svc)
    pc = _Poll(svc)
    ctx.update(bot=bot, services=svc, pollctl=pc)
    _request()
    await rsvhome.tick()
    res = _result()
    assert res["rc"] == 3 and word in res["text"], res
    assert pc.takes == 0 and not svc.rsv_get("rsv_out").get("await_merge") and not svc.reserve_pult_on()


async def test_a_failing_delete_webhook_refuses_and_moves_nothing(gone, ctx):
    svc = gone.svc
    ctx.update(bot=_Bot(pending=12, err_ago=60), services=svc, pollctl=_Poll(svc, ok=False))
    _request()
    await rsvhome.tick()
    res = _result()
    assert res["rc"] == 3 and "deleteWebhook" in res["text"]
    assert svc.reserve_out() == "away" and not svc.reserve_pult_on() and gone.dns.sets == []


async def test_an_expired_request_is_dropped_unexecuted(gone, ctx):
    svc = gone.svc
    ctx.update(bot=_Bot(url=""), services=svc, pollctl=_Poll(svc))
    _request(until_s=-1)
    await rsvhome.tick()
    assert not rsvhome.req_path().exists() and not rsvhome.res_path().exists()
    assert svc.reserve_out() == "away", "просроченный запрос исполнен — CLI уже сказал «не взят»"


async def test_a_garbage_request_is_dropped(gone, ctx):
    ctx.update(bot=_Bot(url=""), services=gone.svc, pollctl=_Poll(gone.svc))
    rsvhome.req_path().parent.mkdir(parents=True, exist_ok=True)
    rsvhome.req_path().write_text("{не json", encoding="utf-8")
    await rsvhome.tick()
    assert not rsvhome.req_path().exists() and not rsvhome.res_path().exists()


# ── awg-bot reserve home --force: CLI ───────────────────────────────────────

@pytest.fixture()
def cli(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rsvhome_cli, "PICK_S", 0.6)
    monkeypatch.setattr(rsvhome_cli, "DONE_S", 0.6)
    monkeypatch.setattr(rsvhome_cli, "POLL_S", 0.02)
    return tmp_path


def _bot_thread(answer: dict | None, *, take: bool = True) -> threading.Thread:
    """Бот: дождаться запроса, взять его и (если answer) ответить итогом с его номером."""
    def run():
        end = time.monotonic() + 3
        while time.monotonic() < end and not rsvhome.req_path().exists():
            time.sleep(0.01)
        if not take:
            return
        req = json.loads(rsvhome.req_path().read_text(encoding="utf-8"))
        assert req["until"] > time.time(), "срок запроса уже вышел"
        rsvhome.req_path().unlink()
        if answer is not None:
            rsvhome.res_path().write_text(json.dumps({"id": req["id"], **answer}), encoding="utf-8")
    t = threading.Thread(target=run)
    t.start()
    return t


@pytest.mark.parametrize("answer, code", [({"rc": 0, "text": "пользователи возвращаются сюда"}, 0),
                                          ({"rc": 3, "text": "«Beta» на связи — верни пользователей обычным путём"}, 3)])
def test_the_cli_returns_the_bot_verdict(cli, capsys, answer, code):
    rsvhome.res_path().parent.mkdir(parents=True, exist_ok=True)
    rsvhome.res_path().write_text(json.dumps({"id": "old", "rc": 0, "text": "прошлый"}), encoding="utf-8")
    t = _bot_thread(answer)
    rc_ = rsvhome_cli.main(["--force"])
    t.join()
    out = capsys.readouterr()
    assert rc_ == code and answer["text"] in (out.out if code == 0 else out.err), out
    assert "прошлый" not in out.out + out.err, "итог прошлого запроса выдан за этот"
    assert not rsvhome.res_path().exists()


def test_the_cli_removes_a_request_the_bot_did_not_take(cli, capsys):
    t = _bot_thread(None, take=False)
    assert rsvhome_cli.main(["--force"]) == 4
    t.join()
    assert not rsvhome.req_path().exists(), "незабранный запрос сработает позже, когда админ уже чинит руками"
    assert "не взял запрос" in capsys.readouterr().err


def test_the_cli_tells_a_taken_request_without_a_result(cli, capsys):
    t = _bot_thread(None)
    assert rsvhome_cli.main(["--force"]) == 5
    t.join()
    assert "итога нет" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["--yes"], ["--force", "x"]])
def test_the_cli_wants_exactly_force(cli, capsys, argv):
    assert rsvhome_cli.main(argv) == 2 and not rsvhome.req_path().exists()
    assert "--force" in capsys.readouterr().err


def _shell(tmp_path: Path, *args: str, active: bool = True) -> subprocess.CompletedProcess:
    from tests.unit.test_reserve_fixes_shell import SCRIPT, _func
    log = tmp_path / "tool.log"
    harness = "\n".join(["set -euo pipefail", f'INSTALL_DIR="{tmp_path}"', f'DATA_DIR="{tmp_path / "data"}"',
                         'SERVICE=awg-bot', "die() { echo \"DIE: $*\"; exit 1; }", "require_root() { :; }",
                         f"systemctl() {{ {'return 0' if active else 'return 3'}; }}",
                         f'bot_tool() {{ echo "$*" >> {log}; return 3; }}',
                         _func(SCRIPT, "cmd_reserve"), f"cmd_reserve {' '.join(args)}"])
    return subprocess.run(["bash", "-c", harness], capture_output=True, text=True, env={**os.environ})


def test_the_shell_verb_runs_the_tool_and_passes_its_code(tmp_path):
    r = _shell(tmp_path, "home", "--force")
    assert r.returncode == 3, r.stdout + r.stderr
    assert (tmp_path / "tool.log").read_text(encoding="utf-8").split() == ["tools.rsvhome", "--force"]


@pytest.mark.parametrize("args, active, word", [(("home",), True, "--force"), (("home", "--force", "x"), True, "--force"),
                                                (("home", "--force"), False, "не запущен")])
def test_the_shell_verb_refuses_without_force_or_a_running_bot(tmp_path, args, active, word):
    r = _shell(tmp_path, *args, active=active)
    assert r.returncode != 0 and word in r.stdout and not (tmp_path / "tool.log").exists(), r.stdout


def test_the_word_is_in_the_protocol_and_the_handler_exists():
    assert "pult_back" in pairproto.PAIR_KINDS and hasattr(InboundMixin, "_in_pult_back")
