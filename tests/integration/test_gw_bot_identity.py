"""Кто такой бот шлюза: кэш getMe по токену слота (services), запрос к
Telegram (runtime/gwbotme) и дозаполнение из такта живости шлюза.

Токены живут в настоящем env-файле во временном каталоге (AWG_BOT_ENV), а
aiogram.Bot подменён двойником `_Telegram`: он знает, какие токены «живые»,
и записывает каждый getMe — так видно, кто ходил в Telegram и сколько раз."""
from __future__ import annotations

import hashlib
import json
import types

import pytest

from awgbot.runtime import gwbotme

TOKEN1 = "111111111:AA-first-token-value-long-enough"
TOKEN1_NEW = "111111111:AA-replaced-token-value-long-enough"
TOKEN2 = "222222222:BB-second-token-value-long-enough"


class _Telegram:
    """Двойник Telegram для aiogram.Bot: bots — токен → (username, first_name);
    токена там нет — getMe падает, как на отозванном токене или без сети."""

    def __init__(self):
        self.bots = {}
        self.calls = []            # токены, по которым спрашивали getMe
        self.closed = 0            # сколько сессий закрыто

    def factory(self, token, *a, **k):
        tg = self

        class _Session:
            async def close(self):
                tg.closed += 1

        class _Bot:
            def __init__(self):
                self.session = _Session()

            async def get_me(self):
                tg.calls.append(token)
                if token not in tg.bots:
                    raise RuntimeError("Unauthorized")
                username, first = tg.bots[token]
                return types.SimpleNamespace(username=username, first_name=first, id=1, is_bot=True)

        return _Bot()


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """Часы паузы после неудачного getMe — свои и чистые на каждый тест:
    пауза хранится на уровне модуля, и неудача из чужого теста (любой ввод
    токена в e2e) иначе молча глушила бы здесь дозаполнение."""
    now = [1000.0]
    monkeypatch.setattr(gwbotme, "_next_try", {})
    # подменяем имя time в самом модуле, а не time.monotonic глобально —
    # на монотонных часах держится цикл asyncio
    monkeypatch.setattr(gwbotme, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.fixture()
def env(monkeypatch, tmp_path):
    path = tmp_path / "env"
    monkeypatch.setenv("AWG_BOT_ENV", str(path))
    return path


@pytest.fixture()
def tg(monkeypatch):
    t = _Telegram()
    import aiogram
    monkeypatch.setattr(aiogram, "Bot", t.factory)
    return t


@pytest.fixture()
def two_slots(services, make_active_client, env):
    admin = make_active_client(name="Админ", tg_id=1, device_limit=0)
    a = services.add_device(admin.id, "NASPi")
    b = services.add_device(admin.id, "Pi2")
    services.db.gateway_add(a.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.db.gateway_add(b.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    return services


# ── кэш в services ───────────────────────────────────────────────────────────

def test_cache_returns_what_was_written_for_the_current_token(two_slots):
    """Записали ответ getMe — карточка его и получает; ключ несёт отпечаток
    токена, а не сам токен (state попадает в резервные копии)."""
    s = two_slots
    s.set_gw_bot_token(TOKEN1, 1)
    s.set_gw_bot_identity(1, "naspi_gw_bot", "Шлюз квартиры")
    assert s.gw_bot_identity(1) == {"username": "naspi_gw_bot", "name": "Шлюз квартиры"}
    raw = s.db.get_state("gw_bot_me_1")
    assert TOKEN1 not in raw, "токен бота шлюза не должен лежать в state открытым"
    assert json.loads(raw)["token_id"] == hashlib.sha256(TOKEN1.encode()).hexdigest()[:12]
    assert s.gw_bot_identity_missing() == [], "ответ есть — спрашивать Telegram незачем"


def test_token_change_invalidates_the_cached_identity(two_slots):
    """Токен слота заменили (другой бот) — старое имя уже не о том боте:
    ссылка в карточке вела бы в чат чужого бота. Кэш пуст, слот снова в
    списке на getMe."""
    s = two_slots
    s.set_gw_bot_token(TOKEN1, 1)
    s.set_gw_bot_identity(1, "old_gw_bot", "Старый")
    s.set_gw_bot_token(TOKEN1_NEW, 1)
    assert s.gw_bot_identity(1) == {}, "имя старого бота пережило смену токена"
    assert s.gw_bot_identity_missing() == [1]


def test_slot_without_token_is_not_missing(two_slots):
    """Без токена спрашивать Telegram не о чем: такой слот в список на getMe
    не попадает (иначе такт живости дёргал бы пустой Bot каждый раз)."""
    s = two_slots
    assert s.gw_bot_identity_missing() == [], "слот без токена попал в список на getMe"
    s.set_gw_bot_token(TOKEN2, 2)
    assert s.gw_bot_identity_missing() == [2], "слот с токеном без ответа — должен быть в списке"
    assert s.gw_bot_identity(1) == {}


def test_garbage_in_state_reads_as_empty(two_slots):
    """Битая запись в state (ручная правка, старая схема) не роняет карточку —
    просто нет строки «Бот шлюза»."""
    s = two_slots
    s.set_gw_bot_token(TOKEN1, 1)
    for raw in ("{не json", "[1, 2]", '"строка"'):
        s.db.set_state("gw_bot_me_1", raw)
        assert s.gw_bot_identity(1) == {}, raw
    assert s.gw_bot_identity_missing() == [1]


def test_removing_the_slot_wipes_its_identity(two_slots, monkeypatch):
    """Слот сняли — его ответ getMe уходит вместе с ним: новый слот под тем же
    номером не должен унаследовать ссылку на чужого бота, даже если токен в
    env остался прежним."""
    s = two_slots
    monkeypatch.setattr(s, "_run_link_script", lambda mode, env=None: None)
    s.set_gw_bot_token(TOKEN2, 2)
    s.set_gw_bot_identity(2, "pi2_gw_bot", "Pi2")
    s.gateway_remove(2)
    assert not s.db.get_state("gw_bot_me_2"), "кэш снятого слота остался в state"
    assert s.gw_bot_identity(2) == {}


def test_slots_do_not_mix_up(two_slots):
    """Два слота — два бота: ответ одного не показывается в карточке другого,
    смена токена одного не трогает кэш другого."""
    s = two_slots
    s.set_gw_bot_token(TOKEN1, 1)
    s.set_gw_bot_token(TOKEN2, 2)
    s.set_gw_bot_identity(1, "one_bot", "Первый")
    assert s.gw_bot_identity(2) == {}, "ответ слота 1 оказался у слота 2"
    s.set_gw_bot_identity(2, "two_bot", "Второй")
    assert s.gw_bot_identity(1)["username"] == "one_bot"
    assert s.gw_bot_identity(2)["username"] == "two_bot"
    s.set_gw_bot_token(TOKEN1_NEW, 1)
    assert s.gw_bot_identity(1) == {} and s.gw_bot_identity(2)["username"] == "two_bot", \
        "смена токена слота 1 задела кэш слота 2"


def test_screen_state_carries_the_agent_bot(two_slots, monkeypatch):
    """Карточка слота строится из gateway_screen_state — там и должен быть бот
    шлюза; у слота без ответа — пусто."""
    s = two_slots
    monkeypatch.setattr(s, "gateway_ping", lambda slot: None)
    s.set_gw_bot_token(TOKEN1, 1)
    s.set_gw_bot_identity(1, "one_bot", "Первый")
    assert s.gateway_screen_state(1, lazy_ping=False)["agent_bot"] == {"username": "one_bot", "name": "Первый"}
    assert s.gateway_screen_state(2, lazy_ping=False)["agent_bot"] == {}


# ── gwbotme: запрос к Telegram ───────────────────────────────────────────────

async def test_refresh_stores_username_and_first_name(two_slots, tg):
    s = two_slots
    s.set_gw_bot_token(TOKEN1, 1)
    tg.bots[TOKEN1] = ("naspi_gw_bot", "Шлюз квартиры")
    assert await gwbotme.refresh(s, 1) is True
    assert tg.calls == [TOKEN1], "getMe должен идти токеном ЭТОГО слота"
    assert s.gw_bot_identity(1) == {"username": "naspi_gw_bot", "name": "Шлюз квартиры"}
    assert tg.closed == 1, "сессия временного Bot не закрыта — утечка соединений на каждый getMe"


async def test_refresh_failure_writes_nothing_and_does_not_raise(two_slots, tg):
    """Telegram не ответил (нет сети, отозванный токен): карточка просто без
    ссылки, а вызывающий — ввод токена — продолжает выдачу бандла."""
    s = two_slots
    s.set_gw_bot_token(TOKEN1, 1)
    assert await gwbotme.refresh(s, 1) is False
    assert tg.calls == [TOKEN1]
    assert not s.db.get_state("gw_bot_me_1"), "по неудаче в кэш что-то записали"
    assert s.gw_bot_identity_missing() == [1], "после неудачи слот должен ждать повтора"
    assert tg.closed == 1, "сессия не закрыта на пути отказа"


async def test_refresh_without_token_does_not_touch_telegram(two_slots, tg):
    s = two_slots
    assert await gwbotme.refresh(s, 1) is False
    assert tg.calls == [], "без токена Bot создавать и getMe звать не из чего"


async def test_refresh_falls_back_to_username_when_first_name_is_empty(two_slots, tg):
    s = two_slots
    s.set_gw_bot_token(TOKEN1, 1)
    tg.bots[TOKEN1] = ("naspi_gw_bot", "")
    assert await gwbotme.refresh(s, 1) is True
    assert s.gw_bot_identity(1) == {"username": "naspi_gw_bot", "name": "naspi_gw_bot"}


async def test_ensure_all_asks_only_slots_without_an_answer(two_slots, tg):
    """Дозаполнение ходит в Telegram только за тем, чего нет: слот с ответом
    не трогается, слот без токена тоже; повторный вызов после ответа — ни
    одного getMe."""
    s = two_slots
    s.set_gw_bot_token(TOKEN1, 1)
    s.set_gw_bot_identity(1, "one_bot", "Первый")
    s.set_gw_bot_token(TOKEN2, 2)
    tg.bots[TOKEN2] = ("two_bot", "Второй")
    await gwbotme.ensure_all(s)
    assert tg.calls == [TOKEN2], "getMe ушёл по слоту, у которого ответ уже есть"
    assert s.gw_bot_identity(2)["username"] == "two_bot"
    await gwbotme.ensure_all(s)
    assert tg.calls == [TOKEN2], "повторный вызов снова пошёл в Telegram"


async def test_ensure_all_waits_after_a_failure_then_retries_until_answered(two_slots, tg, clock):
    """Telegram не ответил — слот не спрашивается каждый такт (стук в Telegram
    раз в полминуты и строка в журнале на каждый), а ждёт паузу; после паузы
    спрашивается снова, после ответа — больше никогда."""
    s = two_slots
    s.set_gw_bot_token(TOKEN1, 1)
    await gwbotme.ensure_all(s)
    assert tg.calls == [TOKEN1] and s.gw_bot_identity(1) == {}
    tg.bots[TOKEN1] = ("one_bot", "Первый")
    clock[0] += gwbotme.RETRY_SECONDS - 1
    await gwbotme.ensure_all(s)
    assert tg.calls == [TOKEN1], "в паузе после неудачи getMe ушёл снова"
    clock[0] += 2
    await gwbotme.ensure_all(s)
    assert tg.calls == [TOKEN1, TOKEN1], "после паузы слот без ответа не спросили"
    assert s.gw_bot_identity(1)["username"] == "one_bot"
    clock[0] += gwbotme.RETRY_SECONDS * 10
    await gwbotme.ensure_all(s)
    assert tg.calls == [TOKEN1, TOKEN1], "после ответа Telegram спрашивать незачем"


async def test_pause_of_one_slot_does_not_hold_the_other(two_slots, tg):
    """Неудача слота 1 — пауза только ему: слот 2 спрашивается в тот же такт."""
    s = two_slots
    s.set_gw_bot_token(TOKEN1, 1)
    s.set_gw_bot_token(TOKEN2, 2)
    tg.bots[TOKEN2] = ("two_bot", "Второй")
    await gwbotme.refresh(s, 1)                      # неудача — пауза слоту 1
    await gwbotme.ensure_all(s)
    assert tg.calls == [TOKEN1, TOKEN2], "пауза слота 1 задела слот 2 (или слот 1 спросили в паузе)"
    assert s.gw_bot_identity(2)["username"] == "two_bot"


async def test_explicit_refresh_ignores_the_pause(two_slots, tg):
    """Человек только что ввёл токен — ответ нужен сейчас, а не через десять
    минут после чьей-то неудачи."""
    s = two_slots
    s.set_gw_bot_token(TOKEN1, 1)
    await gwbotme.ensure_all(s)                      # неудача — пауза
    tg.bots[TOKEN1] = ("one_bot", "Первый")
    assert await gwbotme.refresh(s, 1) is True, "явный вызов упёрся в паузу"
    assert s.gw_bot_identity(1)["username"] == "one_bot"


# ── такт живости шлюза ───────────────────────────────────────────────────────

async def test_liveness_job_fills_the_missing_identity(two_slots, tg, fake_bot, monkeypatch):
    """На старте сети могло не быть: такт живости дозаполняет имя бота шлюза,
    и карточка получает ссылку без перезапуска основного бота."""
    from awgbot.core import config
    from awgbot.runtime import linkserver
    from awgbot.runtime.scheduler import setup_scheduler
    s = two_slots
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    monkeypatch.setattr(s, "routing_liveness_tick", lambda: [])

    async def _ensure(services):
        return None
    monkeypatch.setattr(linkserver, "ensure", _ensure)
    s.set_gw_bot_token(TOKEN2, 2)
    tg.bots[TOKEN2] = ("two_bot", "Второй")
    job = setup_scheduler(s, fake_bot, s.db).get_job("routing_liveness").func
    await job()
    assert s.gw_bot_identity(2) == {"username": "two_bot", "name": "Второй"}
    await job()
    assert tg.calls == [TOKEN2], "такт живости спрашивает getMe только у слотов без ответа"


async def test_liveness_job_survives_a_broken_getme(two_slots, fake_bot, monkeypatch):
    """Сбой в дозаполнении не валит такт живости: иначе автомат переключения
    шлюзов замолчал бы из-за строчки в карточке."""
    from awgbot.core import config
    from awgbot.runtime import linkserver
    from awgbot.runtime.scheduler import setup_scheduler
    s = two_slots
    ticks = []
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    monkeypatch.setattr(s, "routing_liveness_tick", lambda: ticks.append(1) or [])

    async def _ensure(services):
        return None

    async def _boom(services):
        raise RuntimeError("boom")
    monkeypatch.setattr(linkserver, "ensure", _ensure)
    monkeypatch.setattr(gwbotme, "ensure_all", _boom)
    await setup_scheduler(s, fake_bot, s.db).get_job("routing_liveness").func()
    assert ticks == [1], "такт живости не отработал"
