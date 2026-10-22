"""Набор обновлений на стороне шлюза: исход команды — строкой в журнале агента.

Инцидент: команда набора пришла на шлюз, агент её не выполнил, а в журнале
шлюза об этом ни слова — картину восстанавливали по серверу. Теперь каждый
исход `_upd_run` — одна строка в журнале клиента линка: отказ по занятости,
«уже на цели», отказ проверки или GitHub, запуск ступени, успех запуска
апдейтера, неудача запуска. Уровни: то, что надо смотреть, — WARNING,
нормальный ход — INFO.

Агент, команда и подставной GitHub — из tests/integration/test_fleet_channel.py.
"""
from __future__ import annotations

import logging

import pytest

from awgbot.infra import updates
from awgbot.runtime import linkclient
from awgbot.runtime.linkclient import upd as lc_upd
from tests.integration import test_fleet_channel as _ch

pytestmark = pytest.mark.integration

agent, _cmd, _run = _ch.agent, _ch._cmd, _ch._run        # агент и команда — из test_fleet_channel

LOGGER = lc_upd.log.name


def _lines(caplog, level: int) -> list[str]:
    """Строки журнала агента об исходе команды набора заданного уровня."""
    return [r.getMessage() for r in caplog.records
            if r.name == LOGGER and r.levelno == level and r.getMessage().startswith("набор: ")]


def _acceptance_only(lines: list[str]) -> list[str]:
    return [x for x in lines if not x.startswith("набор: команда сервера — обновиться")]


async def test_a_refusal_for_being_busy_is_a_warning_with_the_reason(agent, monkeypatch, caplog):
    """Смена ключей линка не закончилась за svc_busy_wait — агент отвечает
    серверу refused и пишет WARNING с той же причиной: по журналу шлюза
    видно, почему набор его обошёл, без похода на сервер."""
    monkeypatch.setattr(agent, "rekey_state", lambda: {"state": "switched"})
    monkeypatch.setattr(lc_upd, "BUSY_POLL_S", 0)
    monkeypatch.setattr(lc_upd, "svc_busy_wait", lambda: 0)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        sent = await _run(agent, _cmd("v4.0.1"))
    assert sent[-1]["state"] == "refused", sent
    assert _lines(caplog, logging.WARNING) == \
        ["набор: команда обновления не принята — занят: идёт смена ключей линка"], caplog.text
    assert not [x for x in _lines(caplog, logging.INFO) if "ступень" in x or "на цели" in x], \
        "отказ по занятости — а в журнале строки нормального хода"


async def test_a_busy_revert_names_the_revert_command(agent, monkeypatch, caplog):
    """Откат набора при занятости — та же строка со словом «отката», не
    «обновления»: иначе по журналу не отличить, что именно не приняли."""
    await _run(agent, _cmd("v4.0.1"))
    monkeypatch.setattr(agent, "rekey_state", lambda: {"state": "switched"})
    monkeypatch.setattr(lc_upd, "BUSY_POLL_S", 0)
    monkeypatch.setattr(lc_upd, "svc_busy_wait", lambda: 0)
    client = linkclient.LinkClient(agent)

    async def send(kind, body=None, pad=0):
        return True
    client._send, client._writer = send, object()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await client._upd_run(revert=True)
    assert _lines(caplog, logging.WARNING) == \
        ["набор: команда отката не принята — занят: идёт смена ключей линка"], caplog.text


async def test_a_target_already_installed_is_an_info_line_and_nothing_is_applied(agent, caplog):
    """Сервер велел обновиться до версии, которая уже стоит: апдейтер не
    запускается, серверу — done, в журнале — INFO «уже на цели»; без строки
    тишина выглядит как потерянная команда."""
    with caplog.at_level(logging.INFO, logger=LOGGER):
        sent = await _run(agent, _cmd("v4.0.0"))
    assert sent[-1]["state"] == "done", sent
    assert agent.applied == [], "на цели — а апдейтер запущен"
    assert _acceptance_only(_lines(caplog, logging.INFO)) == \
        ["набор: обновление — уже на цели, делать нечего"], caplog.text
    assert _lines(caplog, logging.WARNING) == [], "нормальный исход — а в журнале предупреждение"


async def test_github_silence_is_a_warning_with_the_outcome_and_the_reason(agent, monkeypatch, caplog):
    """GitHub не ответил: исход failed с причиной — WARNING «не выполнено
    (failed): GitHub не ответил: …», чтобы по журналу шлюза было видно,
    что упало именно скачивание списка выпусков, а не команда сервера."""
    def boom(**_):
        raise updates.UpdateError("сеть недоступна (api.github.com): timed out")
    monkeypatch.setattr(updates, "list_releases", boom)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        sent = await _run(agent, _cmd("v4.0.1"))
    assert sent[-1]["state"] == "failed", sent
    assert _lines(caplog, logging.WARNING) == \
        ["набор: обновление не выполнено (failed): GitHub не ответил: сеть недоступна (api.github.com): timed out"], \
        caplog.text
    assert agent.applied == []


async def test_a_failed_check_is_a_warning_with_the_refused_outcome(agent, caplog):
    """Проверка не прошла (digest у GitHub не тот, что назвал сервер): исход
    refused — WARNING с «(refused)» и причиной проверки; слово исхода в скобках
    отличает отказ агента от неудачи сети."""
    with caplog.at_level(logging.INFO, logger=LOGGER):
        sent = await _run(agent, _cmd("v4.0.1", digest="sha256:" + "0" * 64))
    assert sent[-1]["state"] == "refused", sent
    warn = _lines(caplog, logging.WARNING)
    assert len(warn) == 1 and warn[0].startswith("набор: обновление не выполнено (refused): "), caplog.text
    assert "digest v4.0.1 у GitHub не совпал" in warn[0], warn


async def test_a_step_is_announced_before_and_after_the_updater_starts(agent, caplog):
    """Норма: две INFO-строки — «ступень vX — скачиваю и ставлю» до апдейтера
    и «ступень vX запущена — апдейтер перезапустит агент» после; по ним
    видно, что агент ушёл в перезапуск по команде, а не упал."""
    with caplog.at_level(logging.INFO, logger=LOGGER):
        sent = await _run(agent, _cmd("v4.0.1"))
    assert sent[-1]["state"] == "accepted" and agent.applied == [("v4.0.1", True)], sent
    assert _acceptance_only(_lines(caplog, logging.INFO)) == [
        "набор: ступень v4.0.1 — скачиваю и ставлю",
        "набор: ступень v4.0.1 запущена — апдейтер перезапустит агент",
    ], caplog.text
    assert _lines(caplog, logging.WARNING) == [], "успешный запуск — а в журнале предупреждение"


async def test_a_step_that_did_not_start_is_a_warning_with_the_error(agent, monkeypatch, caplog):
    """Апдейтер не запустился (sha256 поставки не совпал): WARNING «ступень vX
    не запустилась: <ошибка>», строки «запущена» нет, серверу — failed;
    строка «скачиваю и ставлю» перед этим остаётся — по ней видно, где именно
    оборвалось."""
    def bad_apply(release, *, hold=""):
        raise updates.UpdateError("sha256 не совпал")
    monkeypatch.setattr(agent, "apply_update", bad_apply)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        sent = await _run(agent, _cmd("v4.0.1"))
    assert sent[-1]["state"] == "failed" and sent[-1]["error"] == "sha256 не совпал", sent
    assert _lines(caplog, logging.WARNING) == \
        ["набор: ступень v4.0.1 не запустилась: sha256 не совпал"], caplog.text
    info = _acceptance_only(_lines(caplog, logging.INFO))
    assert info == ["набор: ступень v4.0.1 — скачиваю и ставлю"], caplog.text
    assert not any("запущена" in x for x in info), "запуск сорвался — а журнал говорит «запущена»"
