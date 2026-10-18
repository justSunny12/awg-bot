"""E2E: агент шлюза — то, чего нет в эталонах экранов (tests/screens/gateway.txt):
живая проба здоровья, реассерт только после подтверждения, порог линка в
секундах, «никогда» из старого конфига при старте агента, найденная версия
переживает сбой проверки, обрезка всплывашки «➖», адреса в туннеле из `role`
на экране «🔀 VPN-транзит» и отбрасывание мусора в этом поле. Тексты и раскладки экранов
(панель в редких состояниях, подтверждения без угрозы РФ-доступу, переспрос
порога, рецепт роутера, огромный ввод доменов) — в эталоне.

Цена ошибки: порог линка, записанный минутами в секундный ключ, — монитор
кричит «линк мёртв» через 5 секунд тишины; «никогда» из старого конфига —
строка «⬆️ Доступна vX» на панели умирает навсегда; перезапуск без
подтверждения — РФ-доступ у всех рвётся от промаха пальцем.
"""
from __future__ import annotations


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
    """«🚑 Восстановить» сам ничего не трогает — только вопрос; реассерт —
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


# ── «🔀 VPN-транзит»: адреса в туннеле своей сети из role ───────────────────

def _transit_status():
    return GwStatus(link_up=True, handshake_age=5.0, hostname="naspi",
                    lan={"iface": "end0", "addr": "192.168.1.2", "resolver": "10.9.1.1", "domains": 3,
                         "nets": 4, "updated_at": "", "own_vpn": 0, "own_ru": 0, "lan_pkts": 9})


async def _role(svc, monkeypatch, **fields) -> None:
    """`role` от сервера через настоящий разбор канала агента."""
    await linkclient.LinkClient(svc)._dispatch({"t": "role", "active": True, "standby": False,
                                                "name": "NASPi", **fields})


async def _transit_text(svc, fake_bot, monkeypatch) -> str:
    monkeypatch.setattr(svc, "cached_status", lambda max_age: _transit_status())
    monkeypatch.setattr(svc, "lan_own_lists", lambda: [])
    cb, nav = _acb(fake_bot)
    await gh.gw_transit(cb, svc, FakeState())
    return nav.sent[-1][1]


async def test_the_transit_screen_shows_the_tunnel_addresses_from_the_role(svc, fake_bot, monkeypatch):
    """Сервер 3.3.0 называет в `role` адреса в туннеле сети слота — строка
    «в туннеле — …» под строкой интерфейса; сервер старше (поля нет) — строки нет."""
    await _role(svc, monkeypatch, server="3.3.0", lan_virtual=["10.240.1.0/24", "10.240.2.0/24"])
    text = await _transit_text(svc, fake_bot, monkeypatch)
    lines = text.split("\n")
    at = next(i for i, ln in enumerate(lines) if ln.startswith("<code>end0</code>"))
    assert lines[at + 1] == "Адреса в туннеле — <code>10.240.1.0/24</code>, <code>10.240.2.0/24</code>", lines
    await _role(svc, monkeypatch)                                  # сервер старше 3.3.0
    assert svc.lan_virtual() == []
    assert "в туннеле —" not in await _transit_text(svc, fake_bot, monkeypatch)


async def test_tunnel_addresses_may_live_in_any_private_space(svc, monkeypatch):
    """Пространство — любая частная /12 (routing.lan_space): 172.16/12 так же
    годится, как 10/8; до 64 записей — столько подсетей у слота бывает."""
    many = [f"172.17.{i}.0/24" for i in range(64)]
    await _role(svc, monkeypatch, lan_virtual=["172.17.1.0/24"])
    assert svc.lan_virtual() == ["172.17.1.0/24"]
    await _role(svc, monkeypatch, lan_virtual=many)
    assert svc.lan_virtual() == many


@pytest.mark.parametrize("junk", [
    "10.240.1.0/24", ["10.240.1.0/24", 7], ["192.168.1.0/24"], ["10.240.1.5/24"], ["10.0.0.0/8"],
    ["10.240.1.0/31"], ["10.240.1.0/24<b>"], [f"10.240.{i}.0/24" for i in range(65)], [None], {"x": 1},
    ["172.32.0.0/16"],
], ids=["str", "not-str", "192.168", "host-bits", "wider-16", "narrower-30", "markup", "65",
        "none", "dict", "outside-172.16/12"])
async def test_the_dispatch_drops_junk_in_the_tunnel_addresses(svc, fake_bot, monkeypatch, junk):
    """`role` — данные с чужой машины: не список, больше 64 записей, запись не
    сеть с префиксом /16–/30 внутри 10/8 или 172.16/12 — поле отбрасывается
    целиком, строки нет; прежнее значение не остаётся висеть."""
    await _role(svc, monkeypatch, lan_virtual=["10.240.1.0/24"])
    assert svc.lan_virtual() == ["10.240.1.0/24"]
    await _role(svc, monkeypatch, lan_virtual=junk)
    assert svc.lan_virtual() == []
    assert "в туннеле —" not in await _transit_text(svc, fake_bot, monkeypatch)


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

# ── ⬆️ обновления: у агента их нет — его обновляет сервер AWG ───────────────

async def test_agent_start_wipes_the_keys_of_its_former_updates(tmp_path, monkeypatch):
    """Первый старт 3.4.0: ключи прежних «⬆️ Обновлений» агента (цель,
    объявленное, ожидание, список и метаданные, мьют) — прочь (Р3)."""
    from awgbot.domain.gateway.upd import LEGACY_KEYS
    from awgbot.runtime import main as rt

    class _Stop(Exception):
        pass

    def _no_bot(*a, **k):
        raise _Stop()
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "gw.db")
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    for key in LEGACY_KEYS:
        d.set_state(key, "1")
    d.close()
    monkeypatch.setattr(rt, "Bot", _no_bot)
    with pytest.raises(_Stop):
        await rt.run_gateway()
    d = Database(tmp_path / "gw.db")
    try:
        assert [k for k in LEGACY_KEYS if d.get_state(k)] == []
    finally:
        d.close()


# ── мелочи, которые люди видят каждый день ───────────────────────────────────

@pytest.mark.parametrize("sync", ["online", "offline"])
def test_a_long_domain_does_not_eat_the_sync_tail_of_the_toast(sync):
    """Домен длиной в сотню знаков обрезается сам, а хвост синхронизации
    остаётся целиком: всплывашка — до 200 знаков, и Telegram режет хвост
    первым — как раз ту строку, ради которой её читают."""
    dom = "a" * 150 + ".example.com"
    toast = texts.gateway_transit_removed_toast(dom, sync=sync)
    assert len(toast) <= 200, len(toast)
    assert toast.endswith("\n" + texts.SYNC_TAILS[sync]), toast
    assert "Убран на всех шлюзах" not in toast, "первая строка «Убран на всех шлюзах» снята"
    assert texts.gateway_transit_removed_toast("sber.ru") == "✅ sber.ru: убран"
    assert texts.gateway_transit_removed_toast("sber.ru", True, "") == "✅ sber.ru: убран", (
        "shared больше ничего не добавляет — хвост решает только sync")


