"""
Уведомление о настроенном шлюзе: о том, что установка удалась, говорит сам
агент. При старте он отмечает первый запуск (`first_start_note`); в первой же
сессии канала, если первый запуск был не больше часа назад, он шлёт вид
`installed` — после полного снимка (серверу нужен бот шлюза из него) — и
помечает отправку, второй раз не шлёт. Сервер на `installed` зовёт крючок
`linkserver.set_on_installed` (в бою — «✅ Шлюз … успешно настроен» админу).

Сокеты настоящие (петля), сервер — боевой LinkServer поверх временной БД
(сцена `link` из test_gwlink_channel); на том конце то подписывающий руками
`_Gw`, то боевой LinkClient поверх настоящего `GatewayServices` (сцена
`agent` из test_gwlink_applied).

Цена ошибки: уведомление не приходит — файл с ключами и токеном агента
висит в чате, а человек не знает, встал ли шлюз; приходит на каждое
переподключение — «успешно настроен» превращается в шум; приходит спустя
дни (канала в первый час не было) — пугает; упавший крючок рвёт сессию —
канал в петле переподключений из-за строки в чате.
"""
from __future__ import annotations

import asyncio
import datetime

import pytest

from awgbot.runtime import linkserver
from awgbot.util import timeutil
from tests.integration import test_gwlink_applied as _applied
from tests.integration import test_gwlink_channel as _chan
from tests.integration.test_gwlink_channel import SNAP, _connect, _until

pytestmark = pytest.mark.integration

# один слот на петле — сцена канала; настоящий агент с клиентом — сцена итога
link = _chan.link
agent = _applied.agent


@pytest.fixture()
def hook(monkeypatch):
    """Крючок сервера, записывающий слоты. Модульный: без отката остался бы
    на следующие тесты."""
    monkeypatch.setattr(linkserver, "_on_installed", None)
    calls: list[int] = []

    async def on_installed(slot_id):
        calls.append(slot_id)
    linkserver.set_on_installed(on_installed)
    return calls


async def _hello(srv) -> _chan._Gw:
    gw = await _connect(srv)
    await gw.send("hello", {"proto": _chan.gwlink.PROTO, "agent": "3.1.0"})
    assert await gw.role(), "сервер не ответил ролью на hello"
    return gw


def _first_start(pi, minutes_ago: int) -> None:
    """Первый запуск агента `minutes_ago` минут назад по часам шлюза."""
    at = timeutil.now() - datetime.timedelta(minutes=minutes_ago)
    pi.db.set_state(pi._FIRST_START_KEY, timeutil.to_iso(at))


async def _up(client, kinds) -> None:
    """Поднять сессию и дождаться, пока сервер примет её снимок."""
    n = kinds.count("snap")
    client.start()
    assert await _until(lambda: kinds.count("snap") > n and client._writer is not None, timeout=5), \
        f"сессия не поднялась: {kinds}"


# ── сервер: приём `installed` ────────────────────────────────────────────────

async def test_the_server_calls_the_hook_once_per_report(link, services, hook):
    """Главный путь на стороне ВПС: агент сказал «установлен» — крючок
    получает слот один раз; снимки и дельты крючок не зовут."""
    gw = await _hello(link)
    await gw.send("snap", {**SNAP, "rev": 1})
    assert await _until(lambda: services.gwlink_snapshot(1))
    await asyncio.sleep(0.1)
    assert hook == [], f"снимок сам по себе принят за установку: {hook}"
    await gw.send("installed", {})
    assert await _until(lambda: hook), "агент сказал «установлен», а крючок не вызван"
    await gw.send("delta", {"egress_ok": False, "rev": 2})
    assert await _until(lambda: services.db.get_state("gwlink_snap_rev_1") == "2")
    await asyncio.sleep(0.1)
    assert hook == [1], f"уведомление о настройке не одно: {hook}"
    await gw.close()


async def test_a_failing_hook_does_not_break_the_session(link, services, monkeypatch):
    """Крючок упал (Telegram не ответил) — сессия канала живёт, следующий
    снимок принимается как обычно."""
    monkeypatch.setattr(linkserver, "_on_installed", None)
    seen = []

    async def broken(slot_id):
        seen.append(slot_id)
        raise RuntimeError("Telegram не ответил")
    linkserver.set_on_installed(broken)
    gw = await _hello(link)
    await gw.send("installed", {})
    assert await _until(lambda: seen), "крючок не вызван"
    await gw.send("snap", {**SNAP, "rev": 1})
    assert await _until(lambda: services.gwlink_snapshot(1)), "после упавшего крючка снимок не принят"
    assert link.online(1), "упавший крючок порвал сессию"
    await gw.close()


async def test_without_a_hook_the_report_is_ignored(link, services, monkeypatch):
    """Крючок не поставлен (слушатель поднят раньше, чем main его повесил) —
    `installed` ничего не ломает, сессия живёт."""
    monkeypatch.setattr(linkserver, "_on_installed", None)
    gw = await _hello(link)
    await gw.send("installed", {})
    await gw.send("snap", {**SNAP, "rev": 1})
    assert await _until(lambda: services.gwlink_snapshot(1)) and link.online(1)
    await gw.close()


# ── агент: когда говорить «установлен» ───────────────────────────────────────

async def test_a_fresh_agent_reports_once_after_its_snapshot(link, services, hook, agent):
    """Агент впервые запущен 10 минут назад: в первой сессии «installed»
    уходит сразу за полным снимком (серверу нужен бот шлюза из него), крючок
    зовётся один раз; следующее подключение уже ничего не говорит."""
    pi, client, kinds = agent
    _first_start(pi, 10)
    await _up(client, kinds)
    assert await _until(lambda: hook, timeout=5), f"«установлен» не дошёл; сервер получил {kinds}"
    assert hook == [1], hook
    assert kinds.index("installed") > kinds.index("snap"), f"«установлен» ушёл раньше снимка: {kinds}"
    assert await _until(lambda: pi.installed_report_pending() is False), "отправка не помечена"
    await client.stop()
    await _up(client, kinds)
    await asyncio.sleep(0.3)
    assert kinds.count("installed") == 1, f"«установлен» ушёл и во второй сессии: {kinds}"
    assert hook == [1], hook


async def test_an_agent_started_two_hours_ago_says_nothing(link, services, hook, agent):
    """Первый запуск два часа назад (канала в первый час не было) —
    «шлюз настроен» спустя часы только пугает: не шлётся."""
    pi, client, kinds = agent
    _first_start(pi, 120)
    await _up(client, kinds)
    await asyncio.sleep(0.3)
    assert "installed" not in kinds and hook == [], kinds


async def test_an_agent_without_a_first_start_mark_says_nothing(link, services, hook, agent):
    """Отметки первого запуска нет (агент обновлён со старой версии, а не
    поставлен) — работающий давно шлюз «установленным» не объявляется."""
    _pi, client, kinds = agent
    await _up(client, kinds)
    await asyncio.sleep(0.3)
    assert "installed" not in kinds and hook == [], kinds


# ── агент: отметка первого запуска и окно ────────────────────────────────────

def test_first_start_note_is_written_only_for_a_fresh_database_and_not_moved(agent):
    """Отметку ставит только первый запуск на свежей базе (её создал
    установщик). Повторный запуск (ребут, обновление) отметку не двигает —
    иначе каждый рестарт открывал бы новый час для «установлен»."""
    pi, _client, _kinds = agent
    pi.first_start_note(True)
    assert pi.db.get_state(pi._FIRST_START_KEY), "первый запуск на свежей базе не отмечен"
    assert pi.installed_report_pending() is True
    _first_start(pi, 300)
    moved = pi.db.get_state(pi._FIRST_START_KEY)
    pi.first_start_note(True)
    pi.first_start_note(False)
    assert pi.db.get_state(pi._FIRST_START_KEY) == moved, "повторный запуск сдвинул отметку"


def test_an_agent_updated_from_an_old_version_is_never_announced(agent):
    """База была до этого запуска, а отметки в ней нет — агент обновлён со
    старой версии, а не поставлен: давно работающий шлюз «установленным» не
    объявляется ни сейчас, ни потом."""
    pi, _client, _kinds = agent
    pi.first_start_note(False)
    assert pi.db.get_state(pi._FIRST_START_KEY) in (None, ""), "обновлённый агент получил отметку первого запуска"
    assert pi.installed_report_pending() is False


def test_the_report_is_pending_only_within_the_hour_and_until_done(agent):
    """Окно — час с первого запуска; после отметки об отправке — больше
    никогда; мусор в отметке — не слать."""
    pi, _client, _kinds = agent
    _first_start(pi, 59)
    assert pi.installed_report_pending() is True
    assert pi.installed_report_pending() is True, "проверка сама по себе сняла отчёт — канал мог не отправить"
    pi.installed_report_done()
    assert pi.installed_report_pending() is False, "после отправки отчёт снова ждёт"
    pi.db.set_state(pi._INSTALLED_SENT_KEY, "")
    _first_start(pi, 61)
    assert pi.installed_report_pending() is False, "окно в час не соблюдено"
    pi.db.set_state(pi._FIRST_START_KEY, "мусор")
    assert pi.installed_report_pending() is False
