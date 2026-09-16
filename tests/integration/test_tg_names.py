"""Имена Telegram-аккаунтов на профилях (docs/guest-role.md): пишет middleware
по сообщениям, молчунов раз в сутки и на старте обновляет фоновая задача; в
текстах человек — ссылкой с именем аккаунта, а не профильным."""
from __future__ import annotations

import datetime

import pytest

from awgbot.bot import texts
from awgbot.util import timeutil

pytestmark = pytest.mark.integration


def test_stale_and_empty_names_are_candidates(services, make_active_client):
    fresh = make_active_client(tg_id=3100, name="Свежий")
    stale = make_active_client(tg_id=3101, name="Старый")
    empty = make_active_client(tg_id=3102, name="Пустой")
    now = timeutil.now()
    services.db.update_client_fields(fresh.id, tg_name="F", tg_name_at=timeutil.to_iso(now))
    services.db.update_client_fields(stale.id, tg_name="S",
                                     tg_name_at=timeutil.to_iso(now - datetime.timedelta(days=9)))
    cutoff = timeutil.to_iso(now - datetime.timedelta(days=7))
    ids = [c.id for c in services.db.clients_needing_tg_name(cutoff)]
    assert stale.id in ids and empty.id in ids and fresh.id not in ids
    # гость — тоже кандидат: его имя видит владелец
    dc = services.add_device(fresh.id, "d")
    res = services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=93100)
    assert res.holder.id in [c.id for c in services.db.clients_needing_tg_name(cutoff)]


async def test_refresh_job_fills_names_and_survives_api_refusals(services, make_active_client):
    from awgbot.runtime.scheduler import refresh_tg_names
    a = make_active_client(tg_id=3110, name="А")
    b = make_active_client(tg_id=3111, name="Б")

    class Chat:
        def __init__(self, first, last="", username=""):
            self.first_name, self.last_name, self.username = first, last, username
            self.full_name = (first + " " + last).strip()

    class Bot:
        async def get_chat(self, tg_id):
            if tg_id == 3111:
                raise RuntimeError("Forbidden: bot was blocked by the user")
            return Chat("Артём", "П.", "artem")

    n = await refresh_tg_names(Bot(), services.db)
    assert n == 1
    fa = services.db.get_client(a.id)
    assert fa.tg_name == "Артём П." and fa.tg_name_at and fa.tg_username == "artem"
    assert services.db.get_client(b.id).tg_name == "" and services.db.get_client(b.id).tg_name_at is None
    # свежее не переспрашиваем
    calls = []

    class Bot2:
        async def get_chat(self, tg_id):
            calls.append(tg_id); return Chat("X")
    await refresh_tg_names(Bot2(), services.db)
    assert calls == [3111], "свежее имя переспросили"


async def test_middleware_stamps_account_name_on_every_role(services, make_active_client):
    from awgbot.bot.middleware import AccessMiddleware
    from tests.e2e.test_middleware import _run
    c = make_active_client(tg_id=3120, name="Профиль")
    mw = AccessMiddleware(services.db)
    await _run(mw, uid=3120, text="привет")
    fresh = services.db.get_client(c.id)
    assert fresh.tg_name == "U" and fresh.tg_name_at and fresh.tg_username == ""
    assert texts.client_link(fresh) == '<a href="tg://user?id=3120">U</a>'
    from awgbot.core import access_cache
    access_cache.invalidate_all()                       # иначе — кэш ролей, БД не читается
    await _run(mw, uid=3120, text="ещё", username="serg")
    fresh = services.db.get_client(c.id)
    assert fresh.tg_username == "serg"
    assert texts.client_link(fresh) == '<a href="https://t.me/serg">U</a>', \
        "по юзернейму — t.me/: ссылка tg://user у посторонних не отрисовывается"


def test_links_prefer_account_name_and_fall_back_to_profile(services, make_active_client):
    owner = make_active_client(tg_id=3130, name="Тестовый клиент")
    dc = services.add_device(owner.id, "Тел")
    res = services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=93130)
    dev = services.db.get_device(dc.device_id)
    assert 'Получено от <a href="tg://user?id=3130">Тестовый клиент</a>' in texts.held_device_card(dev, 0)
    services.db.update_client_fields(owner.id, tg_name="Вася Пупкин", tg_username="vasya")
    services.db.update_client_fields(res.holder.id, tg_name="Артём")
    dev = services.db.get_device(dc.device_id)
    assert 'Получено от <a href="https://t.me/vasya">Вася Пупкин</a>' in texts.held_device_card(dev, 0)
    assert texts.lent_out_marker(dev) == '👤 Передано <a href="tg://user?id=93130">Артём</a> и управляется им'
    donor = services.db.get_client(owner.id)
    assert 'владелец: <a href="https://t.me/vasya">Вася Пупкин</a>' in texts.greeting_guest("Артём", True, donor, 1)


def test_scheduler_registers_daily_and_startup_refresh(services, db):
    from awgbot.runtime import scheduler as sch
    sched = sch.setup_scheduler(services, object(), db, watcher=None)
    try:
        assert sched.get_job("tg_names") is not None and sched.get_job("tg_names").trigger.jitter == 1800
        assert sched.get_job("tg_names_startup") is not None, "разовый проход на старте"
    finally:
        from awgbot.core import settings
        settings._on_change.clear()
