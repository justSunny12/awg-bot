"""Настройки и надзор вебхука бота основы на резерве (runtime/webhook.py):
разбор файла настроек, пула адресов Telegram и тик надзора `getWebhookInfo`.

Слушатель на сокете и макет Bot API — задача этапа переезда кнопкой и
стенда; здесь то, что решается без сети.

Цена ошибки: порт вне четырёх, на которые Telegram доставляет вебхуки,
принятый из файла, — `setWebhook` молча не работает, бот резерва не отвечает;
пул с мусором вместо запасного — слушатель отвергает самого Telegram;
уведомление о застое на каждом тике — админ основы получает его каждые 10
минут; расхождение адреса вебхука, принятое за «всё в порядке», — два
процесса считают, что отвечают в Telegram.
"""
from __future__ import annotations

import json
import time
import types

import pytest

from awgbot.runtime import webhook

pytestmark = pytest.mark.unit

GOOD = {"port": 443, "path": "tok-en", "secret": "DUMMY-secret", "cert": "/run/x/c.crt", "key": "/run/x/c.key",
        "host": "203.0.113.20"}


def _conf(tmp_path, **over):
    p = tmp_path / "webhook.json"
    p.write_text(json.dumps({**GOOD, **over}))
    return p


def test_a_good_file_gives_the_conf_and_the_url(tmp_path):
    c = webhook.load_conf(_conf(tmp_path))
    assert c.url == "https://203.0.113.20:443/tok-en" and c.secret == "DUMMY-secret"


@pytest.mark.parametrize("port", [443, 80, 88, 8443])
def test_the_four_telegram_ports_are_accepted(tmp_path, port):
    assert webhook.load_conf(_conf(tmp_path, port=port)).port == port


@pytest.mark.parametrize("over", [{"port": 8080}, {"port": 0}, {"port": "x"}, {"path": ""}, {"secret": ""}, {"host": ""},
                                  {"cert": None}])
def test_a_conf_that_telegram_cannot_use_is_refused(tmp_path, over):
    if over.get("cert", 1) is None:
        p = tmp_path / "webhook.json"
        data = dict(GOOD)
        del data["cert"]
        p.write_text(json.dumps(data))
        assert webhook.load_conf(p) is None
        return
    assert webhook.load_conf(_conf(tmp_path, **over)) is None


def test_a_missing_or_broken_file_is_no_conf(tmp_path):
    assert webhook.load_conf(tmp_path / "nope.json") is None
    (tmp_path / "bad.json").write_text("{oops")
    assert webhook.load_conf(tmp_path / "bad.json") is None


def test_the_pool_keeps_valid_networks_and_normalizes_them(tmp_path):
    p = tmp_path / "pool.json"
    p.write_text(json.dumps({"pool": ["149.154.160.0/20", "198.51.100.5", "не сеть", "91.108.4.0/22"]}))
    assert webhook.load_pool(p) == ["149.154.160.0/20", "198.51.100.5/32", "91.108.4.0/22"]


@pytest.mark.parametrize("content", [None, "{oops", '{"pool": []}', '{"pool": ["мусор"]}', "[1]"])
def test_a_pool_that_is_missing_or_empty_falls_back_to_the_core_of_telegram(tmp_path, content):
    p = tmp_path / "pool.json"
    if content is not None:
        p.write_text(content)
    assert webhook.load_pool(p) == ["149.154.160.0/20", "91.108.4.0/22"]


# ── надзор ───────────────────────────────────────────────────────────────────

class _Bot:
    def __init__(self, url, pending=0, err_age=None, err="Connection timed out", fail=False):
        self.info = types.SimpleNamespace(url=url, pending_update_count=pending,
                                          last_error_date=(int(time.time()) - err_age) if err_age is not None else None,
                                          last_error_message=err)
        self.fail = fail

    async def get_webhook_info(self):
        if self.fail:
            raise RuntimeError("сеть")
        return self.info


@pytest.fixture()
def srv(tmp_path):
    return webhook.WebhookServer(webhook.load_conf(_conf(tmp_path)), ["149.154.160.0/20"])


async def test_a_healthy_webhook_says_nothing(srv):
    assert await srv.watch(_Bot(srv.conf.url)) is None


async def test_a_stall_is_reported_once_and_the_recovery_once(srv):
    stuck = _Bot(srv.conf.url, pending=5, err_age=60)
    first = await srv.watch(stuck)
    assert first["stalled"] is True and first["error"] == "Connection timed out" and first["since"]
    assert await srv.watch(stuck) is None, "тот же застой сообщён дважды"
    assert await srv.watch(stuck) is None
    healed = await srv.watch(_Bot(srv.conf.url))
    assert healed == {"stalled": False, "restored": True}
    assert await srv.watch(_Bot(srv.conf.url)) is None, "восстановление сообщено дважды"
    again = await srv.watch(stuck)
    assert again and again["stalled"] is True, "новый застой после восстановления сообщается снова"


@pytest.mark.parametrize("kw", [dict(pending=0, err_age=60), dict(pending=5, err_age=3600), dict(pending=5, err_age=None)])
async def test_an_old_error_or_an_empty_queue_is_not_a_stall(srv, kw):
    assert await srv.watch(_Bot(srv.conf.url, **kw)) is None


async def test_a_webhook_that_points_elsewhere_is_reported_as_foreign_every_time(srv):
    other = _Bot("https://198.51.100.9:443/other")
    assert await srv.watch(other) == {"foreign": True, "url": "https://198.51.100.9:443/other"}
    assert await srv.watch(_Bot("")) == {"foreign": True, "url": ""}, "вебхука нет вовсе — тоже не наш"


async def test_a_failing_get_webhook_info_is_a_quiet_no_change(srv):
    assert await srv.watch(_Bot(srv.conf.url, fail=True)) is None


def test_the_watch_delay_stays_within_forty_percent_of_ten_minutes():
    ds = [webhook.watch_delay() for _ in range(300)]
    assert min(ds) >= 0.6 * 600 - 1e-6 and max(ds) <= 1.4 * 600 + 1e-6 and max(ds) - min(ds) > 60


# ── setWebhook ───────────────────────────────────────────────────────────────

class _SetBot:
    def __init__(self, ok=True, exc=None):
        self.args = None
        self.ok, self.exc = ok, exc

    async def set_webhook(self, **kw):
        self.args = kw
        if self.exc:
            raise self.exc
        return self.ok


class _Dp:
    def resolve_used_update_types(self):
        return ["message", "callback_query"]


async def test_set_webhook_carries_the_pair_certificate_secret_and_does_not_drop_pending_updates(srv, tmp_path):
    cert = tmp_path / "c.crt"
    cert.write_text("DUMMY-cert")
    srv.conf.cert = str(cert)
    bot = _SetBot()
    assert await srv.set_webhook(bot, _Dp()) is True
    a = bot.args
    assert a["url"] == srv.conf.url and a["secret_token"] == "DUMMY-secret"
    assert a["drop_pending_updates"] is False, "накопленные у Telegram обновления нельзя сбрасывать"
    assert a["allowed_updates"] == ["message", "callback_query"] and a["max_connections"] == webhook.MAX_CONNECTIONS
    assert srv.set_at, "время постановки не записано"


@pytest.mark.parametrize("bot", [_SetBot(ok=False), _SetBot(exc=RuntimeError("409"))])
async def test_a_refused_set_webhook_is_false_and_leaves_no_timestamp(srv, tmp_path, bot):
    cert = tmp_path / "c.crt"
    cert.write_text("DUMMY-cert")
    srv.conf.cert = str(cert)
    assert await srv.set_webhook(bot, _Dp()) is False and srv.set_at == ""
