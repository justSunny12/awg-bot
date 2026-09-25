"""
Уведомление о настроенном шлюзе: файл первого применения выдан — сервер
ждёт слот (`gw_install_wait_set`); первый принятый ПОЛНЫЙ снимок канала
этого слота значит «установщик отработал, агент стоит и на связи» — сервер
зовёт крючок `linkserver.set_on_installed` (в бою — «✅ Шлюз … успешно
настроен» админу) ровно один раз.

Сокеты настоящие (петля), сервер — боевой LinkServer поверх временной БД
(сцена `link` из test_gwlink_channel); на том конце подписывающий руками `_Gw`.

Цена ошибки: уведомление не приходит — файл с ключами и токеном агента
висит в чате, а человек не знает, встал ли шлюз; приходит на каждое
переподключение или дельту — «успешно настроен» превращается в шум; упавший
крючок рвёт сессию — канал в петле переподключений из-за строки в чате;
ожидание, пережившее снятие слота или месяц простоя, выстреливает на чужом
или давно работающем шлюзе.
"""
from __future__ import annotations

import asyncio
import datetime

import pytest

from awgbot.runtime import linkserver
from awgbot.util import timeutil
from tests.integration import test_gwlink_channel as _chan
from tests.integration.test_gwlink_channel import SNAP, _connect, _until

pytestmark = pytest.mark.integration

# один слот на петле, ключ линка слота подставлен — та же сцена, что у канала
link = _chan.link


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


async def _snap(services, gw, rev: int) -> None:
    """Полный снимок — и дождаться, пока сервер его примет."""
    await gw.send("snap", {**SNAP, "rev": rev})
    assert await _until(lambda: services.db.get_state("gwlink_snap_rev_1") == str(rev)), \
        f"снимок rev={rev} не принят"


async def _delta(services, gw, rev: int) -> None:
    await gw.send("delta", {"egress_ok": False, "rev": rev})
    assert await _until(lambda: services.db.get_state("gwlink_snap_rev_1") == str(rev)), \
        f"дельта rev={rev} не принята"


def _wait_raw(services, slot: int = 1) -> str:
    return services.db.get_state(services._gwlink_key(services._GWLINK_INSTALL_WAIT_KEY, slot)) or ""


# ── сервер: первый полный снимок ─────────────────────────────────────────────

async def test_the_first_full_snapshot_after_the_file_calls_the_hook_once(link, services, hook):
    """Главный путь: файл выдан, шлюз встал и прислал полный снимок — крючок
    получает слот один раз; дальнейшие дельта и полный снимок той же сессии
    уведомления не дают, ожидание снято."""
    services.gw_install_wait_set(1)
    gw = await _hello(link)
    await _snap(services, gw, 1)
    assert await _until(lambda: hook), "шлюз на связи, а уведомления о настройке нет"
    assert hook == [1], hook
    assert _wait_raw(services) == "", "ожидание не снято — следующий снимок повторит уведомление"
    await _delta(services, gw, 2)
    await _snap(services, gw, 3)
    await asyncio.sleep(0.1)
    assert hook == [1], f"уведомление о настройке повторилось: {hook}"
    await gw.close()


async def test_a_reconnect_after_the_notice_does_not_repeat_it(link, services, hook):
    """Шлюз переподключился (ребут малины, перезапуск линка) — новый полный
    снимок уже не «установка»: второго «успешно настроен» нет."""
    services.gw_install_wait_set(1)
    gw = await _hello(link)
    await _snap(services, gw, 1)
    assert await _until(lambda: hook)
    await gw.close()
    assert await _until(lambda: not link.online(1)), "сессия не закрылась"
    gw = await _hello(link)
    await _snap(services, gw, 5)
    await asyncio.sleep(0.1)
    assert hook == [1], f"переподключение принесло второе уведомление: {hook}"
    await gw.close()


async def test_a_delta_does_not_count_as_the_install(link, services, hook):
    """Ожидание поставили, пока сессия уже жила (перевыпуск файла первого
    применения при живом канале): дельта — не «установщик отработал»,
    уведомления нет, ожидание остаётся до полного снимка."""
    gw = await _hello(link)
    await _snap(services, gw, 1)
    services.gw_install_wait_set(1)
    await _delta(services, gw, 2)
    await asyncio.sleep(0.1)
    assert hook == [], f"дельта засчитана как установка: {hook}"
    assert _wait_raw(services), "дельта сняла ожидание — полный снимок уже ничего не скажет"
    await _snap(services, gw, 3)
    assert await _until(lambda: hook == [1]), f"полный снимок после дельты не дал уведомления: {hook}"
    await gw.close()


async def test_without_waiting_a_snapshot_says_nothing(link, services, hook):
    """Файла первого применения не выдавали — каждый снимок работающего
    шлюза остаётся просто снимком."""
    gw = await _hello(link)
    await _snap(services, gw, 1)
    await asyncio.sleep(0.1)
    assert hook == [], f"уведомление о настройке без выданного файла: {hook}"
    await gw.close()


async def test_a_refused_snapshot_does_not_take_the_wait(link, services, hook):
    """Дельта не по порядку отвергнута (сервер просит полный снимок) —
    ожидание не тронуто, уведомление придёт со следующим полным снимком."""
    gw = await _hello(link)
    await _snap(services, gw, 1)
    services.gw_install_wait_set(1)
    await gw.send("delta", {"egress_ok": False, "rev": 9})
    await asyncio.sleep(0.2)
    assert hook == [] and _wait_raw(services), (hook, _wait_raw(services))
    await gw.close()


async def test_a_failing_hook_does_not_break_the_session(link, services, monkeypatch):
    """Крючок упал (Telegram не ответил) — сессия канала живёт, следующая
    дельта принимается как обычно, ожидание снято (второй попытки не будет —
    лучше промолчать, чем повторять на каждое переподключение)."""
    monkeypatch.setattr(linkserver, "_on_installed", None)
    seen = []

    async def broken(slot_id):
        seen.append(slot_id)
        raise RuntimeError("Telegram не ответил")
    linkserver.set_on_installed(broken)
    services.gw_install_wait_set(1)
    gw = await _hello(link)
    await _snap(services, gw, 1)
    assert await _until(lambda: seen), "крючок не вызван"
    await _delta(services, gw, 2)
    assert link.online(1), "упавший крючок порвал сессию"
    assert _wait_raw(services) == ""
    await gw.close()


async def test_without_a_hook_the_wait_is_only_taken(link, services, monkeypatch):
    """Крючок не поставлен (слушатель поднят раньше, чем main его повесил) —
    снимок принимается, сессия не рвётся."""
    monkeypatch.setattr(linkserver, "_on_installed", None)
    services.gw_install_wait_set(1)
    gw = await _hello(link)
    await _snap(services, gw, 1)
    assert link.online(1)
    await gw.close()


async def test_a_month_old_wait_is_dropped_silently(link, services, hook):
    """Файл выдали месяц назад, а канал поднялся только сейчас (включили
    позже, шлюз ставили руками) — «успешно настроен» здесь было бы новостью
    месячной давности: ожидание снимается молча."""
    old = timeutil.now() - datetime.timedelta(seconds=services._GWLINK_INSTALL_WAIT_TTL_S + 3600)
    services.db.set_state(services._gwlink_key(services._GWLINK_INSTALL_WAIT_KEY, 1), timeutil.to_iso(old))
    gw = await _hello(link)
    await _snap(services, gw, 1)
    await asyncio.sleep(0.1)
    assert hook == [], f"уведомление о настройке по месячному ожиданию: {hook}"
    assert _wait_raw(services) == "", "просроченное ожидание осталось висеть"
    await gw.close()


async def test_forgetting_the_slot_drops_its_wait(link, services, hook):
    """Слот сняли, пока ждали: новый слот с тем же номером не получит чужое
    «успешно настроен» на первом же снимке."""
    services.gw_install_wait_set(1)
    services.gwlink_forget(1)
    assert _wait_raw(services) == "", "ожидание пережило снятие слота"
    gw = await _hello(link)
    await _snap(services, gw, 1)
    await asyncio.sleep(0.1)
    assert hook == [], hook
    await gw.close()


# ── ожидание само по себе ────────────────────────────────────────────────────

@pytest.mark.parametrize("age_days, want", [(0, True), (29, True), (31, False)])
def test_the_wait_is_taken_once_and_expires_after_a_month(link, services, age_days, want):
    """Ожидание снимается ровно один раз; моложе месяца — уведомить,
    старше — молча. Слоты не смешиваются."""
    at = timeutil.now() - datetime.timedelta(days=age_days)
    services.db.set_state(services._gwlink_key(services._GWLINK_INSTALL_WAIT_KEY, 2), timeutil.to_iso(at))
    assert services.gw_install_wait_take(1) is False, "ожидание слота 2 снято за слот 1"
    assert services.gw_install_wait_take(2) is want
    assert services.gw_install_wait_take(2) is False, "ожидание снимается не один раз"
