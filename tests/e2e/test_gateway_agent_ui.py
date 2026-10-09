"""E2E: агент шлюза — то, чего нет в эталонах экранов (tests/screens/gateway.txt):
живая проба здоровья, реассерт только после подтверждения, порог линка в
секундах, «никогда» из старого конфига при старте агента, найденная версия
переживает сбой проверки, обрезка всплывашки «➖». Тексты и раскладки экранов
(панель в редких состояниях, подтверждения без угрозы РФ-доступу, переспрос
порога, рецепт роутера, огромный ввод доменов) — в эталоне.

Цена ошибки: порог линка, записанный минутами в секундный ключ, — монитор
кричит «линк мёртв» через 5 секунд тишины; «никогда» из старого конфига —
строка «⬆️ Доступна vX» на панели умирает навсегда; перезапуск без
подтверждения — РФ-доступ у всех рвётся от промаха пальцем.
"""
from __future__ import annotations

import types

import pytest

import awgbot.core.config as cfg
from awgbot.bot import texts
from awgbot.bot.callbacks import GwCB
from awgbot.bot.handlers import gateway as gh
from awgbot.core import settings
from awgbot.domain.gateway import GatewayServices, GwCheck, GwStatus
from awgbot.infra.db import Database
from awgbot.runtime import linkclient
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e
ADMIN = cfg.ADMIN_ID


class _Svc(GatewayServices):
    """Агент без хоста: живой статус — подставной, пробы считаются."""

    def __init__(self, db):
        super().__init__(db)
        self.live = GwStatus(link_up=True, handshake_age=40.0, hostname="naspi")
        self.probes = 0

    def status(self):
        self.probes += 1
        return self.live

    def invalidate_static(self):
        pass


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(linkclient, "enabled", lambda: False)
    d = Database(tmp_path / "gw.db"); d.init_schema()
    yield _Svc(d)
    d.close()


@pytest.fixture()
def store(monkeypatch):
    """Настройки в памяти: запись видна чтению, как у настоящего кэша."""
    data: dict = {}
    real_get, real_int, real_bool = settings.get, settings.get_int, settings.get_bool
    monkeypatch.setattr(settings, "set_value", lambda k, v: data.__setitem__(k, v) or [k])
    monkeypatch.setattr(settings, "get", lambda k, d=None: data[k] if k in data else real_get(k, d))
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(data[k]) if k in data else real_int(k, d))
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: bool(data[k]) if k in data else real_bool(k, d))
    return data


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _msg(bot, text):
    return FakeMessage(text=text, chat_id=ADMIN, user_id=ADMIN, bot=bot)


# ── здоровье и восстановление ────────────────────────────────────────────────

async def test_health_is_a_live_probe_not_the_tick_snapshot(svc, fake_bot):
    """«🩺 Здоровье» — живой замер на каждое нажатие, а не снимок тика: человек
    жмёт его, когда что-то сломалось только что."""
    svc.cached_status = lambda max_age: pytest.fail("здоровье взято из снимка тика")
    svc.live = GwStatus(link_up=True, hostname="naspi",
                        checks=[GwCheck("линк", True, "хендшейк 40 с"), GwCheck("обвязка", False, "нет цепочки")])
    cb, nav = _acb(fake_bot)
    await gh.gw_health(cb, svc)
    assert svc.probes == 1, "живой замер не снят"
    await gh.gw_health(cb, svc)
    assert svc.probes == 2, "второе нажатие не сняло замер заново"


async def test_recovery_touches_nothing_until_confirmed(svc, fake_bot, monkeypatch):
    """«🔧 Восстановить» сам ничего не трогает — только вопрос; реассерт —
    после подтверждения и ровно один раз: промах пальцем не рвёт РФ-доступ."""
    calls = []
    monkeypatch.setattr(svc, "reassert", lambda: calls.append(1) or (True, ""))

    async def _poke(services):
        return None
    monkeypatch.setattr(linkclient, "poke", _poke)
    _carrying(svc)
    cb, nav = _acb(fake_bot)
    await gh.gw_confirm(cb, GwCB(action="reassert"), svc)
    assert calls == [], "реассерт до подтверждения"
    await gh.gw_execute(cb, GwCB(action="reassert!"), svc)
    assert calls == [1], calls


def _carrying(svc, role: str = "active", link_up: bool = True) -> None:
    """Сервер сказал роль, последний тик монитора видел линк живым или нет."""
    svc.set_link_role(role == "active", standby=False, name="NASPi")
    svc.cached_status = lambda max_age: GwStatus(link_up=link_up, handshake_age=40.0 if link_up else None,
                                                 hostname="naspi")


# ── настройки ────────────────────────────────────────────────────────────────

async def test_monitoring_link_threshold_is_minutes_on_screen_and_seconds_in_the_config(svc, fake_bot, store):
    """Ввод «7» в минутах пишет в секундный ключ 420 и закрывает ввод: записанные
    минутами 7 секунд — монитор кричит «линк мёртв» на каждой паузе."""
    store["app.gateway.handshake_max_age"] = 300
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await gh.gw_edit(cb, GwCB(action="edit", val="app.gateway.handshake_max_age"), svc, st)
    msg = _msg(fake_bot, "7")
    await gh.gw_receive_value(msg, st, svc)
    assert store["app.gateway.handshake_max_age"] == 420, "ввод в минутах записан не секундами"
    assert len([s for s in msg.sent if s[0] == "answer"]) == 1, msg.sent
    assert await st.get_state() is None, "ввод остался открытым"


@pytest.mark.parametrize("raw", ["0", "1441", "abc", "5.5", ""])
async def test_monitoring_link_threshold_out_of_bounds_is_asked_again(svc, fake_bot, store, raw):
    """Ввод вне 1–1440 минут — переспрос, ничего не записано, ввод открыт:
    «0» записался бы порогом в 0 секунд — линк мёртв всегда."""
    st = FakeState()
    await st.set_state("SettingsInput:value")
    await st.update_data(key="app.gateway.handshake_max_age", sec="mon")
    msg = _msg(fake_bot, raw)
    await gh.gw_receive_value(msg, st, svc)
    assert "app.gateway.handshake_max_age" not in store
    assert await st.get_state() is not None, "переспрос закрыл ввод"


async def test_link_threshold_absent_in_the_config_is_written_in_seconds(svc, fake_bot, store):
    """Ключа в конфиге агента нет (секция закомментирована) — первый ввод
    «10» всё равно пишет секунды (600), а не минуты поверх умолчания."""
    assert settings.get("app.gateway.handshake_max_age") is None, "в образце конфига ключ появился — тест устарел"
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await gh.gw_edit(cb, GwCB(action="edit", val="app.gateway.handshake_max_age"), svc, st)
    msg = _msg(fake_bot, "10")
    await gh.gw_receive_value(msg, st, svc)
    assert store["app.gateway.handshake_max_age"] == 600


# ── 💾 бэкапы ────────────────────────────────────────────────────────────────

# ── ⬆️ обновления ────────────────────────────────────────────────────────────

@pytest.fixture()
def upd(svc, monkeypatch, store):
    """Проверка обновлений без сети: счётчик походов к списку релизов."""
    from awgbot.infra import updates
    # поколение ядра 0 — блокировка обновления (update_block_reason) не срабатывает
    scene = {"next": types.SimpleNamespace(tag="v3.2.1", body="- пункт", title="", awg_generation=lambda: 0),
             "fail": False, "calls": 0}

    def _next(max_generation=None):
        scene["calls"] += 1
        if scene["fail"]:
            raise updates.UpdateError("GitHub не ответил")
        return scene["next"]
    monkeypatch.setattr(updates, "next_release", _next)
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.2.0")
    store["updates.poll_schedule"] = "day"
    return scene


async def test_a_failed_check_keeps_the_found_version(svc, fake_bot, upd):
    """Сбой проверки не стирает найденную раньше версию: иначе строка
    «⬆️ Доступна vX» на панели гаснет от каждого таймаута GitHub."""
    cb, nav = _acb(fake_bot)
    await gh.gw_updates_screen(cb, svc)
    upd["fail"] = True
    await gh.gw_updates_screen(cb, svc)
    assert upd["calls"] == 2, "проверка при открытии не сходила к списку релизов"
    assert svc.update_available_tag() == "v3.2.1", "сбой проверки стёр найденную версию"


async def test_never_from_an_old_config_becomes_month_and_mute_at_agent_start(tmp_path, monkeypatch):
    """То же при старте агента — до первого открытия раздела: иначе до него
    периодическая проверка спит, и строка «⬆️ Доступна vX» молчит."""
    from awgbot.runtime import main as rt
    from tests.conftest import restore_settings
    (tmp_path / "updates.yaml").write_text('poll_schedule: "never"\npoll_hour: 10\npoll_minute: 0\n',
                                           encoding="utf-8")

    class _Stop(Exception):
        pass

    def _no_bot(*a, **k):
        raise _Stop()
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "gw.db")
    monkeypatch.setattr(rt, "Bot", _no_bot)
    settings.init(tmp_path)
    try:
        with pytest.raises(_Stop):
            await rt.run_gateway()
        assert settings.get("updates.poll_schedule") == "month"
        assert "never" not in (tmp_path / "updates.yaml").read_text(encoding="utf-8")
        d = Database(tmp_path / "gw.db")
        try:
            assert GatewayServices(d).updates_muted(), "уведомления при «никогда» не выключены"
        finally:
            d.close()
    finally:
        restore_settings()


async def test_an_ordinary_schedule_is_left_alone_at_agent_start(tmp_path, monkeypatch):
    """«день» при старте не трогается и уведомления не глушатся — починка
    только для «никогда»."""
    from awgbot.runtime import main as rt
    from tests.conftest import restore_settings
    (tmp_path / "updates.yaml").write_text('poll_schedule: "day"\npoll_hour: 10\npoll_minute: 0\n',
                                           encoding="utf-8")

    class _Stop(Exception):
        pass

    def _no_bot(*a, **k):
        raise _Stop()
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "gw.db")
    monkeypatch.setattr(rt, "Bot", _no_bot)
    settings.init(tmp_path)
    try:
        with pytest.raises(_Stop):
            await rt.run_gateway()
        assert settings.get("updates.poll_schedule") == "day"
        d = Database(tmp_path / "gw.db")
        try:
            assert not GatewayServices(d).updates_muted()
        finally:
            d.close()
    finally:
        restore_settings()


# ── мелочи, которые люди видят каждый день ───────────────────────────────────

@pytest.mark.parametrize("sync", ["online", "offline", "no_channel"])
def test_a_long_domain_does_not_eat_the_sync_tail_of_the_toast(sync):
    """Домен длиной в сотню знаков обрезается сам, а хвост синхронизации
    остаётся целиком: всплывашка — до 200 знаков, и Telegram режет хвост
    первым — как раз ту строку, ради которой её читают."""
    dom = "a" * 150 + ".example.com"
    toast = texts.gateway_transit_removed_toast(dom, sync=sync)
    assert len(toast) <= 200, len(toast)
    assert toast.endswith("\n" + texts.SYNC_TAILS[sync]), toast
    assert "Убран на всех шлюзах" not in toast, "первая строка «Убран на всех шлюзах» снята"
    assert texts.gateway_transit_removed_toast("sber.ru") == "sber.ru: убран"
    assert texts.gateway_transit_removed_toast("sber.ru", True, "") == "sber.ru: убран", (
        "shared больше ничего не добавляет — хвост решает только sync")


