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
который не замечен, — два бота отвечают одному пользователю.
"""
from __future__ import annotations

import asyncio

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


class Lab:
    """Макет, бот aiogram, диспетчер и управляемый поллинг."""

    def __init__(self, tmp_path):
        self.api = botapi.Api(str(tmp_path / "botapi.jsonl"))
        self.calls: list[str] = []
        self.handled: list[str] = []
        real_call = self.api.call

        async def counted(bot, method, params, cert):
            self.calls.append(method)
            return await real_call(bot, method, params, cert)
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
        end = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < end:
            if cond():
                return True
            await asyncio.sleep(0.05)
        return bool(cond())

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
    await asyncio.sleep(0.5)
    assert "getUpdates" not in lab.calls, "опрос при чужом вебхуке — 409 и гонка с ботом резерва"
    assert lab.handled == [] and not lab.ctl.polling()
    assert paired.rsv_get("rsv_poll")["why"] == "webhook_info"


async def test_a_conflict_stops_the_polling_once_and_marks_away(lab, paired):
    lab.run()
    assert await lab.until(lambda: lab.ctl.polling())
    lab.webhook()
    assert await lab.until(lambda: paired.reserve_poll() == "away"), "409 не привёл к away"
    assert await lab.until(lambda: not lab.ctl.polling())
    seen = lab.calls.count("getUpdates")
    await asyncio.sleep(1.5)
    assert lab.calls.count("getUpdates") == seen, "после 409 опрос продолжается — повторы запросов"
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
    await asyncio.sleep(0.3)                              # опрос ждёт в долгом getUpdates
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
    lab.mock.inject(_update("после"))
    await asyncio.sleep(1.2)
    assert lab.handled == ["а"], "поллинг продолжился после release"
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
    await asyncio.sleep(0.6)
    assert lab.handled == [], "информации нет, а записано «право у резерва» — поллить нельзя"
    paired.reserve_set_poll("mine")
    lab.ctl.wake()
    assert await lab.until(lambda: lab.handled == ["x"])


class _StubDp:
    """Диспетчер-заглушка: опрос заканчивается сам или падает."""

    def __init__(self, error=None):
        self.error = error
        self.starts = 0

    async def start_polling(self, *a, **kw):
        self.starts += 1
        if self.error:
            raise self.error

    def resolve_used_update_types(self):
        return []

    async def stop_polling(self):
        pass


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
