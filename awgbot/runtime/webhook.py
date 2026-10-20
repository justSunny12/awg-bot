"""
webhook.py — право отвечать в Telegram у бота основы на резерве (гостевой
режим): слушатель вебхука и его постановка.

У бота один адрес вебхука; пока он задан, `getUpdates` тем же токеном Bot
API отвергает (409 «webhook is active») — так Telegram сам решает, кто
отвечает: запись DNS — арбитр VPN, вебхук — арбитр Telegram. Бот основы на
резерве поднимает HTTPS-слушатель aiohttp внутри своего процесса (штатный
`SimpleRequestHandler` aiogram, `handle_in_background=False`: ответ 200 —
только после обработки, иначе обновление, на которое бот ответил и упал,
потерялось бы; `secret_token` — проверка заголовка) и по факту записи ставит
`setWebhook` с самоподписанным сертификатом пары. Порт — один из четырёх,
на которые Telegram доставляет вебхуки (443, 80, 88, 8443); его выбирает
резерв при открытии. Адреса — только из пула Telegram, тем же пулом, что в
файерволе хоста резерва (DNAT): фильтр aiogram с умолчанием библиотеки
отверг бы адрес из нового префикса, который файервол уже пропустил.

Надзор: раз в 10 мин ± 40 % `getWebhookInfo` — растёт очередь и свежая
ошибка доставки → уведомление админу основы (Т-РУ19) и строка боту резерва;
починилось — «доставка восстановлена», один раз на расхождение.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import ssl
import time
from dataclasses import dataclass

from awgbot.util import timeutil

log = logging.getLogger(__name__)

WEBHOOK_PORTS = (443, 80, 88, 8443)
MAX_CONNECTIONS = 4                    # одно ядро на резерве
WATCH_SECONDS = 600
STALL_FRESH_SECONDS = 15 * 60          # ошибка доставки свежее — считаем застоем


@dataclass
class WebhookConf:
    port: int
    path: str
    secret: str
    cert: str
    key: str
    host: str

    @property
    def url(self) -> str:
        return f"https://{self.host}:{self.port}/{self.path}"


def load_conf(path=None) -> WebhookConf | None:
    from awgbot.infra import rsvzone
    data = rsvzone.read_json(path or rsvzone.WEBHOOK_FILE)
    try:
        conf = WebhookConf(port=int(data["port"]), path=str(data["path"]), secret=str(data["secret"]),
                           cert=str(data["cert"]), key=str(data["key"]), host=str(data.get("host") or ""))
    except (KeyError, ValueError, TypeError):
        return None
    if conf.port not in WEBHOOK_PORTS or not conf.path or not conf.secret or not conf.host:
        return None
    return conf


def load_pool(path=None) -> list[str]:
    from awgbot.infra import rsvzone
    data = rsvzone.read_json(path or rsvzone.TG_POOL_FILE)
    out = []
    for item in data.get("pool") or []:
        try:
            out.append(str(ipaddress.ip_network(str(item), strict=False)))
        except ValueError:
            continue
    return out or ["149.154.160.0/20", "91.108.4.0/22"]


class WebhookServer:
    """Слушатель вебхука в процессе бота основы на резерве."""

    def __init__(self, conf: WebhookConf, pool: list[str]):
        self.conf = conf
        self.pool = pool
        self._runner = None
        self.set_at: str = ""
        self._stalled_since: float = 0.0
        self._stalled_notified = False

    async def start(self, dp, bot, host: str = "0.0.0.0") -> None:
        from aiohttp import web
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler, ip_filter_middleware, setup_application
        from aiogram.webhook.security import IPFilter
        app = web.Application(middlewares=[ip_filter_middleware(IPFilter(self.pool))])
        SimpleRequestHandler(dispatcher=dp, bot=bot, handle_in_background=False,
                             secret_token=self.conf.secret).register(app, path="/" + self.conf.path)
        setup_application(app, dp, bot=bot)
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(self.conf.cert, self.conf.key)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host=host, port=self.conf.port, ssl_context=ctx, backlog=MAX_CONNECTIONS * 4)
        await site.start()
        log.info("гость: слушатель вебхука поднят на :%d (адреса — пул Telegram, %d префиксов)",
                 self.conf.port, len(self.pool))

    async def stop(self) -> None:
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception as e:                           # noqa: BLE001
                log.warning("гость: слушатель вебхука не закрылся чисто: %s", e)
            self._runner = None

    async def set_webhook(self, bot, dp) -> bool:
        """`setWebhook` с сертификатом пары; обновления, накопленные у
        Telegram, не сбрасываются — их отдадут сюда."""
        from aiogram.types import FSInputFile
        try:
            ok = await bot.set_webhook(url=self.conf.url, certificate=FSInputFile(self.conf.cert),
                                       secret_token=self.conf.secret,
                                       allowed_updates=dp.resolve_used_update_types(),
                                       max_connections=MAX_CONNECTIONS, drop_pending_updates=False)
        except Exception as e:                               # noqa: BLE001
            log.warning("гость: setWebhook не прошёл: %s", e)
            return False
        if ok:
            self.set_at = timeutil.now_iso()
            log.info("гость: вебхук поставлен — право отвечать в Telegram у бота основы на резерве")
        return bool(ok)

    async def watch(self, bot) -> dict | None:
        """Один тик надзора: {"stalled": bool, "since": iso, "error": str,
        "restored": bool} — только по смене; None — без изменений."""
        try:
            info = await bot.get_webhook_info()
        except Exception as e:                               # noqa: BLE001
            log.warning("гость: getWebhookInfo: %s", e)
            return None
        if info.url != self.conf.url:
            return {"foreign": True, "url": info.url or ""}
        err_at = int(getattr(info, "last_error_date", None) or 0)
        fresh = err_at and time.time() - err_at < STALL_FRESH_SECONDS
        pending = int(getattr(info, "pending_update_count", 0) or 0)
        stalled = bool(fresh and pending > 0)
        if stalled and not self._stalled_notified:
            self._stalled_since = self._stalled_since or time.time()
            self._stalled_notified = True
            return {"stalled": True, "since": timeutil.now_iso(), "error": str(info.last_error_message or "")}
        if not stalled and self._stalled_notified:
            self._stalled_notified = False
            self._stalled_since = 0.0
            return {"stalled": False, "restored": True}
        return None


def watch_delay() -> float:
    import random
    return random.uniform(WATCH_SECONDS * 0.6, WATCH_SECONDS * 1.4)


def dump_conf(conf: WebhookConf) -> str:
    return json.dumps({"port": conf.port, "path": conf.path, "secret": conf.secret, "cert": conf.cert,
                       "key": conf.key, "host": conf.host})


async def sleep_jitter(seconds: float) -> None:
    await asyncio.sleep(seconds)
