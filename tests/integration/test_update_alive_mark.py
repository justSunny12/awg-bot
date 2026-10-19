"""Отметка жизни для сторожа обновления (update-ok): новый процесс бота пишет
её, когда вышел на связь, — и не раньше.

Основной бот — после первого ответа Telegram на getUpdates (опрос пошёл, база
уже открыта); агент шлюза — после него же или первого подписанного слова
сервера AWG в сессии канала, что раньше. getMe на старте, отказ getUpdates,
сессия канала, закрытая без слова или подписанная чужим ключом, — не в счёт:
по ним сторож сохранил бы версию, которая не работает. Сторож не взведён —
отметка не пишется вовсе.

Telegram — настоящий aiogram.Bot с подставной сессией (посредники запросов
работают как в бою); канал — настоящий клиент на шлюзе с подставным ВПС.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetMe, GetUpdates
from aiogram.types import User

from awgbot.core import config
from awgbot.domain.selfupdate import marks
from awgbot.runtime import linkclient
from awgbot.runtime.main import alive
from tests.unit import _linkclient_session_common as _c
from tests.unit._linkclient_session_common import _Agent

pytestmark = pytest.mark.integration

conf, vps, gateway_role = _c.conf, _c.vps, _c.gateway_role


class _Session(BaseSession):
    """Bot API без сети: getMe — бот, getUpdates — пусто или отказ сети."""

    def __init__(self, fail_updates: bool = False):
        super().__init__()
        self.fail_updates = fail_updates
        self.calls: list[str] = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(type(method).__name__)
        if isinstance(method, GetMe):
            return User(id=42, is_bot=True, first_name="DUMMY")
        if isinstance(method, GetUpdates):
            if self.fail_updates:
                raise TelegramNetworkError(method=method, message="DUMMY: нет сети")
            return []
        raise AssertionError(f"неожиданный запрос {method!r}")

    async def stream_content(self, *a, **k):          # pragma: no cover — не нужен
        raise NotImplementedError

    async def close(self) -> None:
        pass


_COMMITS: list = []          # вызовы фиксации сразу по отметке (`awg-bot __update_guard --alive`)


@pytest.fixture()
def data(tmp_path, monkeypatch) -> Path:
    """Каталог базы со взведённым сторожем; отметка процесса — заново."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "bot.db")
    monkeypatch.setattr(marks, "_alive_done", False)
    _COMMITS.clear()
    monkeypatch.setattr(marks, "_commit_now", lambda: _COMMITS.append(1))   # без systemd-run
    (tmp_path / "update-guard").write_text("VERSION=3.4.1\nPREV=3.4.0\n", encoding="utf-8")
    return tmp_path


def _mark(data: Path) -> str | None:
    f = data / "update-ok"
    return f.read_text(encoding="utf-8") if f.exists() else None


def _bot(session: _Session) -> Bot:
    bot = Bot("12345:DUMMY", session=session)
    alive.watch(bot)
    return bot


async def test_main_bot_marks_after_the_first_get_updates_answer(data):
    session = _Session()
    bot = _bot(session)
    await bot.get_me()
    assert _mark(data) is None, "getMe — ещё не опрос: отметка раньше времени"
    assert _COMMITS == []
    await bot(GetUpdates(timeout=50))
    assert _COMMITS == [1], "отметка есть, а фиксация сразу не позвана"
    text = _mark(data)
    assert text is not None and text.split()[0] == config.INSTALLED_VERSION, text
    assert len(text.split()) == 2 and "T" in text.split()[1], "вторым полем — время отметки"
    assert session.calls == ["GetMe", "GetUpdates"]


async def test_a_failing_get_updates_is_not_a_sign_of_life(data):
    session = _Session(fail_updates=True)
    bot = _bot(session)
    with pytest.raises(TelegramNetworkError):
        await bot(GetUpdates(timeout=50))
    assert _mark(data) is None
    session.fail_updates = False
    await bot(GetUpdates(timeout=50))
    assert _mark(data) is not None, "опрос пошёл после сбоя — отметка должна появиться"


async def test_no_guard_no_mark(data):
    (data / "update-guard").unlink()
    await _bot(_Session())(GetUpdates(timeout=50))
    assert _mark(data) is None, "сторожа нет — отметка ни к чему"
    assert _COMMITS == []


async def test_one_mark_per_process(data):
    bot = _bot(_Session())
    await bot(GetUpdates(timeout=50))
    first = _mark(data)
    (data / "update-ok").unlink()
    await bot(GetUpdates(timeout=50))
    assert first is not None and _mark(data) is None, "отметка пишется на каждом опросе"
    assert _COMMITS == [1]


# ── агент: подписанное слово сервера в канале ───────────────────────────────

async def _session_once(vps_conn) -> None:
    for _ in range(100):
        if vps_conn.closed or linkclient.online():
            return
        await asyncio.sleep(0.01)


async def test_agent_marks_after_the_first_signed_word_of_the_server(data, vps, gateway_role):
    conn = vps.answers(hang=True)
    linkclient.ensure(_Agent())
    await _session_once(conn)
    assert linkclient.online(), "сессия не открылась — сценарий собран не так"
    text = _mark(data)
    assert text is not None and text.split()[0] == config.INSTALLED_VERSION
    assert _COMMITS == [1]


def test_the_commit_runs_the_guard_outside_the_bot_cgroup(monkeypatch):
    """Фиксация — `awg-bot __update_guard --alive` транзиентным юнитом, как апдейтер."""
    from awgbot.infra import detach
    got = []
    monkeypatch.setattr(detach, "spawn_detached", lambda argv, **kw: got.append((argv, kw)))
    marks._commit_now()
    assert got == [(["/usr/local/bin/awg-bot", "__update_guard", "--alive"],
                    {"unit_prefix": "awg-bot-update-commit"})]


@pytest.mark.parametrize("kind", ["hangs_up", "forged"])
async def test_agent_does_not_mark_on_a_silent_or_forged_server(data, vps, gateway_role, kind, monkeypatch):
    """Сервер закрыл сессию молча или подписал чужим ключом — не на связи."""
    getattr(vps, kind)()
    stop = asyncio.Event()

    async def fake_sleep(seconds):                  # бэкофф после неудачи — конец сценария
        stop.set()
        raise asyncio.CancelledError

    # asyncio у клиента — общий модуль: дальше в тесте только ожидание события
    monkeypatch.setattr(linkclient.asyncio, "sleep", fake_sleep)
    linkclient.ensure(_Agent())
    await asyncio.wait_for(stop.wait(), 1)
    assert vps.connects == 1
    assert _mark(data) is None and _COMMITS == []
