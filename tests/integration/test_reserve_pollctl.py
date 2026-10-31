"""Право отвечать в Telegram у основного бота (runtime/pollctl.py) на макете
Bot API (tests/lab/botapi.py): настоящий aiogram, настоящий диспетчер, макет —
на петле. Пока у бота задан вебхук, `getUpdates` отвечает 409 «webhook is
active» — так Telegram сам решает, кто отвечает.

Цена ошибки: поллинг, продолжающий стучаться после 409, — каждые несколько
секунд запрос с отказом и две стороны, отвечающие на одно обновление;
подтверждения последней пачки перед `released` нет — резерв обработает то, что
основа уже обработала (пользователь получает два ответа, оплата или выдача
конфигурации — дважды); `deleteWebhook` со сбросом очереди — обновления,
накопившиеся за время переезда, потеряны; чужой адрес вебхука на старте,
который не замечен, — два бота отвечают одному пользователю; close во время
проверки права, после которого поллинг всё равно стартует, — остановка юнита
висит до TimeoutStopSec.

«Ничего не случилось» здесь утверждается не сном, а фактом: цикл вошёл в
ожидание права (подменённый wait_for модуля), опрос остановлен (событие
заглушки start_polling) — на загруженном стенде сон в доли секунды не ловит
ни лишнего опроса, ни его отсутствия.
"""
from __future__ import annotations

import asyncio
import threading
import types

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import web

from awgbot.runtime import pollctl
from tests.integration import _reserve_common as rc
from tests.lab import botapi

pytestmark = pytest.mark.integration
rsv = rc.rsv
TOKEN = "123456:DUMMY-token"
# настоящий запрос сессии: общая подмена тестов закрывает сеть, а здесь ходим только на петлю макета
_REAL_MAKE_REQUEST = AiohttpSession.make_request


def _update(text: str = "привет") -> dict:
    return {"message": {"message_id": 1, "date": 0, "chat": {"id": 1, "type": "private"},
                        "from": {"id": 1, "is_bot": False, "first_name": "x"}, "text": text}}


async def until(pred, timeout: float = 2.0) -> bool:
    """Ждать условия опросом раз в 10 мс; False — не дождались за timeout."""
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return bool(pred())


class _Waits:
    """Подмена asyncio у pollctl: wait_for в pollctl — только ожидание права
    (wake или перепроверка) — и вход в него виден счётчиком. Остальное — как у
    настоящего модуля; цикл событий и макет не задеты."""

    def __init__(self):
        self.entered = self.left = 0
        self.mod = types.ModuleType("asyncio")
        self.mod.__dict__.update(vars(asyncio))
        real = asyncio.wait_for

        async def wait_for(aw, timeout):
            self.entered += 1
            try:
                return await real(aw, timeout)
            finally:
                self.left += 1
        self.mod.wait_for = wait_for

    def waiting(self) -> bool:
        return self.entered > self.left


class Lab:
    """Макет, бот aiogram, диспетчер и управляемый поллинг."""

    def __init__(self, tmp_path):
        self.api = botapi.Api(str(tmp_path / "botapi.jsonl"))
        self.calls: list[str] = []
        self.inflight: dict[str, int] = {}
        self.handled: list[str] = []
        self.waits = _Waits()
        real_call = self.api.call

        async def counted(bot, method, params, cert):
            self.calls.append(method)
            self.inflight[method] = self.inflight.get(method, 0) + 1
            try:
                return await real_call(bot, method, params, cert)
            finally:
                self.inflight[method] -= 1
        self.api.call = counted
        app = web.Application()
        app.router.add_route("*", "/bot{token}/{method}", self.api.handle)
        self.runner = web.AppRunner(app)

    async def start(self, services):
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.bot = Bot(TOKEN, session=AiohttpSession(api=TelegramAPIServer.from_base(f"http://127.0.0.1:{port}")))
        self.dp = Dispatcher()

        @self.dp.message()
        async def on_message(m):
            self.handled.append(m.text)
        self.starts = 0
        self.started, self.stopped = asyncio.Event(), asyncio.Event()
        real_start = self.dp.start_polling

        async def start_polling(*a, **kw):
            self.starts += 1
            self.stopped.clear()
            self.started.set()
            try:
                return await real_start(*a, **kw)
            finally:
                self.started.clear()
                self.stopped.set()
        self.dp.start_polling = start_polling
        self.ctl = pollctl.PollControl(self.dp, self.bot, services)
        self.mock = self.api.bot(TOKEN)
        self.task: asyncio.Task | None = None

    def run(self) -> asyncio.Task:
        self.task = asyncio.get_running_loop().create_task(self.ctl.run())
        return self.task

    def webhook(self, url: str = "https://backup.example.net:8443/hook") -> None:
        self.mock.webhook = {"url": url, "secret": "", "cert": None, "max_connections": 40, "set_at": 0}
        self.mock.wake.set()

    async def until(self, cond, timeout: float = 6.0):
        """Как until, с запасом под настоящий aiogram на петле."""
        return await until(cond, timeout)

    def waiting(self) -> bool:
        """Цикл ждёт права отвечать (wake или перепроверка), опроса нет."""
        return self.waits.waiting() and not self.ctl.polling()

    async def stop(self):
        await self.ctl.close()
        if self.task is not None:
            try:
                await asyncio.wait_for(self.task, 5)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):      # noqa: BLE001
                pass
        await self.bot.session.close()
        await self.runner.cleanup()


@pytest.fixture()
async def lab(services, rsv, tmp_path, monkeypatch):
    monkeypatch.setattr(pollctl, "POLL_TIMEOUT", 1)
    monkeypatch.setattr(AiohttpSession, "make_request", _REAL_MAKE_REQUEST)
    lab = Lab(tmp_path)
    monkeypatch.setattr(pollctl, "asyncio", lab.waits.mod)
    await lab.start(services)
    yield lab
    await lab.stop()


@pytest.fixture()
def paired(services, rsv):
    rc.pair_row(services)
    return services


async def test_without_a_pair_it_polls_as_before_and_never_asks_about_the_webhook(lab, services):
    lab.mock.inject(_update("один"))
    lab.run()
    assert await lab.until(lambda: lab.handled == ["один"])
    assert "getWebhookInfo" not in lab.calls, "без пары бот лишний раз ходит к Telegram"
    assert services.reserve_poll() == "mine"


async def test_with_a_pair_and_no_webhook_it_polls_and_keeps_the_right(lab, paired):
    lab.mock.inject(_update("два"))
    lab.run()
    assert await lab.until(lambda: lab.handled == ["два"])
    assert "getWebhookInfo" in lab.calls and paired.reserve_poll() == "mine"


async def test_a_foreign_webhook_at_the_start_means_no_polling_and_away(lab, paired):
    """Бот перезапустился, пока пользователи у резерва: вебхук стоит — отвечать не мне."""
    lab.webhook()
    lab.mock.inject(_update("накопилось"))
    lab.run()
    assert await lab.until(lambda: paired.reserve_poll() == "away")
    assert await lab.until(lab.waiting), "цикл не ушёл ждать права отвечать"
    assert "getUpdates" not in lab.calls, "опрос при чужом вебхуке — 409 и гонка с ботом резерва"
    assert lab.handled == [] and lab.starts == 0
    assert paired.rsv_get("rsv_poll")["why"] == "webhook_info"


async def test_a_conflict_stops_the_polling_once_and_marks_away(lab, paired):
    lab.run()
    assert await lab.until(lambda: lab.ctl.polling())
    lab.webhook()
    assert await lab.until(lambda: paired.reserve_poll() == "away"), "409 не привёл к away"
    # опрос кончился (start_polling вернулся, а не повторяет 409 с отступом), цикл ждёт права
    assert await lab.until(lambda: lab.stopped.is_set() and lab.waiting()), \
        "после 409 опрос продолжается — повторы запросов"
    assert lab.inflight.get("getUpdates", 0) == 0, "в ожидании права висит запрос опроса"
    assert lab.starts == 1 and lab.waits.entered == 1
    assert paired.rsv_get("rsv_poll")["why"] == "webhook"


async def test_take_deletes_the_webhook_without_dropping_the_queue_and_polls_again(lab, paired):
    """Обновления, накопившиеся за время, пока вебхук был у резерва, не теряются."""
    lab.webhook()
    lab.mock.inject(_update("накопилось"))
    lab.run()
    assert await lab.until(lambda: paired.reserve_poll() == "away")
    assert await lab.ctl.take() is True
    assert lab.mock.webhook is None
    assert await lab.until(lambda: lab.handled == ["накопилось"]), "очередь сброшена при deleteWebhook"
    assert paired.reserve_poll() == "mine"


async def test_a_failing_delete_webhook_leaves_the_right_with_the_reserve(lab, paired, monkeypatch):
    lab.webhook()
    lab.run()
    assert await lab.until(lambda: paired.reserve_poll() == "away")

    async def boom(*a, **kw):
        raise RuntimeError("Telegram недоступен")
    monkeypatch.setattr(lab.bot, "delete_webhook", boom)
    assert await lab.ctl.take() is False
    assert paired.reserve_poll() == "away" and not lab.ctl.polling()


async def test_release_confirms_the_last_batch_so_the_reserve_does_not_see_it_again(lab, paired):
    """Последнее обработанное обновление подтверждено у Telegram `getUpdates(offset=last+1)`."""
    lab.mock.inject(_update("а"))
    lab.mock.inject(_update("б"))
    lab.run()
    assert await lab.until(lambda: lab.handled == ["а", "б"])
    assert await lab.ctl.release() is True
    assert not lab.ctl.polling()
    assert lab.mock.updates == [], f"подтверждения нет — резерв получит обработанное: {lab.mock.updates}"


async def test_the_last_update_id_follows_the_polled_updates(lab, paired):
    """Подтверждающий запрос `release` строится от последнего id полученного обновления: пока он
    нулевой, подтверждение — `offset=1`, то есть холостое, и последняя пачка не закрыта."""
    lab.mock.inject(_update("а"))
    lab.mock.inject(_update("б"))
    lab.run()
    assert await lab.until(lambda: lab.handled == ["а", "б"])
    assert lab.ctl.last_update_id == 2, "посредник запросов не видит ответ getUpdates — id остаётся нулевым"


async def test_no_update_is_handled_here_and_left_in_the_queue_for_the_reserve(lab, paired):
    """Главный инвариант передачи: обновление обработано либо здесь, либо у резерва, но не в обоих местах."""
    lab.run()
    assert await lab.until(lambda: lab.ctl.polling())
    # опрос ждёт в долгом getUpdates (запрос у макета в работе)
    assert await lab.until(lambda: lab.inflight.get("getUpdates", 0) > 0)
    lab.mock.inject(_update("в последнюю минуту"))
    assert await lab.ctl.release() is True
    left = {u["message"]["text"] for u in lab.mock.updates}
    assert not (set(lab.handled) & left), f"обработано здесь и осталось в очереди: {set(lab.handled) & left}"
    assert set(lab.handled) | left == {"в последнюю минуту"}, "обновление потеряно"


async def test_a_new_update_after_the_release_stays_in_the_queue_for_the_reserve(lab, paired):
    lab.mock.inject(_update("а"))
    lab.run()
    assert await lab.until(lambda: lab.handled == ["а"])
    await lab.ctl.release()
    # опрос остановлен и цикл ждёт права: следующий getUpdates возможен только после wake
    assert await lab.until(lambda: lab.stopped.is_set() and lab.waiting()), "поллинг продолжился после release"
    seen = lab.calls.count("getUpdates")
    lab.mock.inject(_update("после"))
    assert lab.handled == ["а"] and lab.starts == 1
    # прерванный долгий getUpdates у макета может ещё доживать свой таймаут — его
    # клиент уже снят; новых запросов нет
    assert lab.calls.count("getUpdates") == seen
    assert [u["message"]["text"] for u in lab.mock.updates] == ["после"]


async def test_release_reports_a_failed_confirmation(lab, paired, monkeypatch):
    lab.run()
    assert await lab.until(lambda: lab.ctl.polling())

    async def boom(*a, **kw):
        raise RuntimeError("нет сети")
    monkeypatch.setattr(lab.bot, "get_updates", boom)
    assert await lab.ctl.release() is False, "пачка не подтверждена, а released уйдёт — дубли у резерва"


async def test_the_right_returns_to_the_primary_through_wake_after_a_conflict(lab, paired):
    lab.run()
    assert await lab.until(lambda: lab.ctl.polling())
    lab.webhook()
    assert await lab.until(lambda: not lab.ctl.polling() and paired.reserve_poll() == "away")
    lab.mock.delete_webhook(False)
    lab.ctl.wake()
    assert await lab.until(lambda: lab.ctl.polling() and paired.reserve_poll() == "mine")


async def test_a_failing_webhook_info_falls_back_to_the_stored_right(lab, paired, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("нет сети")
    monkeypatch.setattr(lab.bot, "get_webhook_info", boom)
    paired.reserve_set_poll("away")
    lab.mock.inject(_update("x"))
    lab.run()
    assert await lab.until(lab.waiting), "цикл не ушёл ждать права отвечать"
    assert lab.handled == [] and lab.starts == 0, "информации нет, а записано «право у резерва» — поллить нельзя"
    assert "getUpdates" not in lab.calls
    paired.reserve_set_poll("mine")
    lab.ctl.wake()
    assert await lab.until(lambda: lab.handled == ["x"])


class _StubDp:
    """Диспетчер-заглушка: опрос заканчивается сам, падает или (hold) ждёт
    stop_polling; started/stopped — события «запущен/остановлен»."""

    def __init__(self, error=None, hold=False):
        self.error = error
        self.hold = hold
        self.starts = 0
        self.started, self.stopped = asyncio.Event(), asyncio.Event()
        self._stop = asyncio.Event()

    async def start_polling(self, *a, **kw):
        self.starts += 1
        self.started.set()
        try:
            if self.error:
                raise self.error
            if self.hold:
                await self._stop.wait()
        finally:
            self.stopped.set()

    def resolve_used_update_types(self):
        return []

    async def stop_polling(self):
        self._stop.set()


class _GateSvc:
    """reserve_paired держит поток до gate: «run внутри _allowed» — факт (entered), а не сон."""

    def __init__(self):
        self.entered, self.gate = threading.Event(), threading.Event()

    def reserve_paired(self):
        self.entered.set()
        self.gate.wait(5)
        return False


async def test_a_polling_that_ends_by_itself_ends_the_run(services, rsv):
    dp = _StubDp()
    ctl = pollctl.PollControl(dp, object(), services)
    await asyncio.wait_for(ctl.run(), 3)
    assert dp.starts == 1, "run() обязан завершиться, а не ждать пробуждения, которого никто не пришлёт"


async def test_a_polling_failure_goes_up_to_the_caller(services, rsv):
    dp = _StubDp(RuntimeError("токен отозван"))
    ctl = pollctl.PollControl(dp, object(), services)
    with pytest.raises(RuntimeError, match="токен отозван"):
        await asyncio.wait_for(ctl.run(), 3)
    assert dp.started.is_set() and dp.stopped.is_set() and ctl._task is None


async def test_close_stops_a_holding_polling_and_ends_the_run(services, rsv):
    dp = _StubDp(hold=True)
    ctl = pollctl.PollControl(dp, object(), services)
    run = asyncio.get_running_loop().create_task(ctl.run())
    await asyncio.wait_for(dp.started.wait(), 2)
    assert ctl.polling()
    await ctl.close()
    await asyncio.wait_for(run, 2)
    assert dp.stopped.is_set() and dp.starts == 1 and not ctl.polling()


@pytest.mark.xfail(strict=True, reason="RT-9: _closing не перепроверяется после _allowed()")
async def test_close_during_the_right_check_does_not_start_the_polling():
    """SIGTERM (close) пришёл, пока run() ждёт ответа проверки права: опрос не
    стартует, run() завершается — иначе остановка юнита висит до TimeoutStopSec
    и finally main не исполняется (при правке RT-9 снять отметку xfail)."""
    dp = _StubDp(hold=True)
    svc = _GateSvc()
    ctl = pollctl.PollControl(dp, object(), svc)
    run = asyncio.get_running_loop().create_task(ctl.run())
    try:
        assert await until(svc.entered.is_set), "run() не дошёл до проверки права"
        await ctl.close()
        svc.gate.set()
        done, _ = await asyncio.wait({run}, timeout=2)
        assert dp.starts == 0, "опрос стартовал после close"
        assert done, "run() не завершился после close"
    finally:
        svc.gate.set()
        await dp.stop_polling()
        await asyncio.wait({run}, timeout=2)
        if not run.done():
            run.cancel()
