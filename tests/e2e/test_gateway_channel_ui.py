"""
Экраны канала до шлюза (концепт «канал линка», §7): блок канала в карточке
слота, сверка выданного с установленным в «Конфигурации шлюза», кнопка
«Обновить с шлюза», строка канала в панели агента.

Всё, что рисуется здесь, приехало с чужой машины: экран обязан говорить
«последнее известное, N назад», когда канал лежит, молчать, когда шлюз ещё
ничего не сообщал, и не показывать кнопку, нажатие на которую заведомо ничего
не даст. Цена ошибки — человек чинит несуществующее расхождение или, наоборот,
уверен, что конфигурация доехала.
"""
from __future__ import annotations

import asyncio
import base64
import os

import pytest

from awgbot.bot import texts
from awgbot.bot.callbacks import GwSlotCB
from awgbot.bot.handlers import settings as sh
from awgbot.core import config, settings
from awgbot.runtime import linkserver
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID
PRIV = base64.b64encode(os.urandom(32)).decode()


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _screen(nav):
    s = next(x for x in reversed(nav.sent) if x[0] == "edit_text")
    labels = [b.text for row in s[2].inline_keyboard for b in row] if s[2] else []
    return s[1], labels


@pytest.fixture()
def slot(services, fake_awg, fake_routing, make_active_client, monkeypatch):
    """Слот шлюза с включённой маршрутизацией и пустым каналом."""
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.gateway_set_home_subnets(1, "192.168.68.0/24")
    services.gateway_set_lan_mode(1, True)
    monkeypatch.setattr(services, "gateway_resolver_addr", lambda g: "10.9.1.1" if g.lan_mode else "")
    monkeypatch.setattr(services, "_link_privkey", lambda g=None: PRIV)
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: None)
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "awglink")   # возраст хендшейка снимается
    monkeypatch.setattr(settings, "get_bool",
                        lambda k, d=False: True if k in ("app.routing.enabled",
                                                         "app.routing.failover.enabled") else d)
    monkeypatch.setattr(services, "routing_status", lambda: (True, "ок"))
    monkeypatch.setattr(services, "routing_link_ok", lambda: True)
    monkeypatch.setattr(services, "_probe_slot", lambda g, active=False: "ok")
    pings = []
    monkeypatch.setattr(services, "gateway_ping", lambda slot_id: pings.append(slot_id) or 61)
    services.pings = pings
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    return services.db.gateway(1)


def _installed(services) -> dict:
    """Что стоит на шлюзе, когда всё доехало: ровно те значения, из которых ВПС
    собирает бандл."""
    addrs = " ".join(sorted(set(services.db.admin_device_addresses(ADMIN))))
    return {"lan_mode": "1", "home_subnets": "192.168.68.0/24", "resolver": "10.9.1.1",
            "peer_home_nets": "", "admin_ips": addrs}


def _snap(services, *, bundle=None, online=True, **kw):
    """Положить снимок так, как его принял бы канал, и при желании открыть сессию."""
    snap = {"bundle": bundle if bundle is not None else _installed(services),
            "link_contract": "1", "plumbing_gen": "new", "mark_status": "confirmed",
            "agent_version": "3.1.0", "awg_generation": 1, "egress_ok": True,
            "boot_id": "b" * 36, "ts": "2026-09-22T20:00:00+03:00", "rev": 1}
    snap.update(kw)
    services.gwlink_snapshot_in(1, snap, 1, True)
    if online:
        services.gwlink_session_opened(1, "3.1.0", 1)
    return snap


async def _card(services, fake_bot):
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=1), services, FakeState())
    return _screen(nav)


# ── карточка слота ───────────────────────────────────────────────────────────

async def test_a_live_channel_shows_four_lines_and_the_refresh_button(services, slot, fake_bot):
    """Вся видимая польза этапа: человек открывает карточку и видит не догадку,
    а ответ — канал жив, вот версия агента, установленное совпадает с выданным,
    наружу шлюз выходит. Кнопка «Обновить» есть только тут, при живой сессии."""
    _snap(services)
    text, labels = await _card(services, fake_bot)
    assert "🔗 Канал до шлюза: 🟢 на связи" in text
    assert "🤖 Агент 3.1.0" in text
    assert "✅ Конфигурация на шлюзе совпадает с выданной" in text
    assert "🌐 Выход наружу: сервер — есть, шлюз — есть" in text
    assert "🔄 Обновить с шлюза" in labels
    assert "Пинг с " in text and services.pings == [1], (
        "пинг меряет путь ядро ↔ ядро, отклик канала — занятость процесса агента; "
        "подменить первое вторым значило бы врать в карточке")


async def test_a_gateway_that_never_spoke_says_so_and_offers_no_button(services, slot, fake_bot):
    """Канал ещё не поднимался: ни одной строки о конфигурации шлюза мы не
    выдумываем — «сказать нечего» честнее, чем «всё сошлось» без единого
    факта. Кнопка, которой некого спросить, не рисуется."""
    text, labels = await _card(services, fake_bot)
    assert "🔗 Канал до шлюза: ещё не поднимался" in text
    assert "перевыпусти конфигурацию шлюза" in text
    assert "Конфигурация на шлюзе" not in text and "🤖 Агент" not in text
    assert "🔄 Обновить с шлюза" not in labels


async def test_a_dead_channel_shows_the_last_known_picture_with_its_age(services, slot, fake_bot):
    """Снимок при падении канала не выбрасываем — последнее известное полезнее
    пустого экрана. Но возраст обязан стоять рядом: иначе старое читается как
    настоящее."""
    _snap(services, online=False)
    services.gwlink_session_closed(1)
    services.db.set_state("gwlink_snap_at_1", "2026-09-22T18:00:00+03:00")
    text, labels = await _card(services, fake_bot)
    assert "🔗 Канал до шлюза: ⚪ нет связи" in text
    assert "🤖 Агент 3.1.0" in text, "версию агента из последнего снимка показываем и при лежащем канале"
    assert "назад)" in text, "возраст снимка не показан — старое рисуется как настоящее"
    assert "🔄 Обновить с шлюза" not in labels, "кнопка при лежащем канале только обманет"


async def test_a_drifted_configuration_is_counted_on_the_card_and_listed_where_it_is_reissued(
        services, slot, fake_bot):
    """Главный ответ канала: что реально стоит на шлюзе. Разошлось — карточка
    говорит сколько, а подробности ждут там, где человек выпускает файл."""
    _snap(services, bundle={**_installed(services), "home_subnets": "192.168.1.0/24",
                            "resolver": ""})
    text, _ = await _card(services, fake_bot)
    assert "⚠️ Конфигурация на шлюзе расходится с выданной: 2" in text

    head, _labels = await sh._screen("rt_bundle", services, "1")
    assert "Что стоит на шлюзе" in head
    assert "локальные подсети: у сервера «192.168.68.0/24», на шлюзе «192.168.1.0/24»" in head
    assert "резолвер: у сервера «10.9.1.1», на шлюзе «—»" in head
    assert "Перевыпусти файл и примени его на шлюзе." in head


async def test_old_plumbing_on_the_gateway_is_called_out_where_the_file_is_issued(services, slot):
    """Обвязка старого образца или снятая таблица — ответ на вопрос, который ВПС
    до канала задавал наугад. Человек читает его там, где выпускает файл."""
    _snap(services, plumbing_gen="old", link_contract="")
    head, _ = await sh._screen("rt_bundle", services, "1")
    assert "Контракт линка: не указан (конфиг старого образца)" in head
    assert "обвязка ⚠️ старого образца — перевыпусти файл" in head

    _snap(services, plumbing_gen="none")
    head, _ = await sh._screen("rt_bundle", services, "1")
    assert "обвязка ⚠️ не развёрнута — примени файл на шлюзе" in head


async def test_a_snapshot_without_the_installed_configuration_is_not_a_green_tick(
        services, slot, fake_bot):
    """Снимок пришёл, а блока «что применено из конфигурации» в нём нет — так
    выглядит и старый агент, и урезанный чужой снимок. Пустой список расхождений
    здесь значит «шлюз не сообщал, что у него стоит», а не «всё сошлось»: ровно
    об этом говорит докстринг gwlink_config_drift и вариант строки из концепта
    §7.1 «⚙️ Конфигурация: шлюз ещё не сообщал, что у него стоит».

    Цена ошибки — зелёная галочка там, где сверки не было: человек видит
    «конфигурация совпадает» и не перевыпускает файл, хотя на шлюзе может стоять
    что угодно. Напоминание о перевыпуске при этом продолжает приходить, то есть
    экраны бота противоречат друг другу."""
    services.gwlink_snapshot_in(1, {"agent_version": "3.0.0", "egress_ok": True,
                                    "ts": "2026-09-22T20:00:00+03:00", "rev": 1}, 1, True)
    services.gwlink_session_opened(1, "3.0.0", 1)
    text, _ = await _card(services, fake_bot)
    assert "🤖 Агент 3.0.0" in text, "версию агента из такого снимка показать можно"
    assert "Конфигурация на шлюзе совпадает с выданной" not in text, (
        "сверки не было — зелёная галочка выдумана")

    head, _labels = await sh._screen("rt_bundle", services, "1")
    assert "совпадает с тем, что выдаст этот файл" not in head, (
        "экран выпуска подтверждает доставку, которой никто не подтверждал")


async def test_the_bundle_screen_confirms_a_configuration_that_did_arrive(services, slot):
    """Человек применил файл на малине и вернулся на экран выпуска: подтверждение
    «совпадает» — это то, ради чего канал и затевался."""
    _snap(services)
    head, _ = await sh._screen("rt_bundle", services, "1")
    assert "<b>Что стоит на шлюзе</b>: совпадает с тем, что выдаст этот файл." in head
    assert "Контракт линка: 1 · обвязка нового образца" in head, (
        "подробности сверки живут там, где человек выпускает файл")


async def test_the_bundle_screen_stays_silent_without_a_snapshot(services, slot):
    """Снимка нет — блок сверки не рисуется вовсе: пустой блок честнее, чем
    «всё сошлось» без единого факта с той стороны."""
    head, _ = await sh._screen("rt_bundle", services, "1")
    assert "Что стоит на шлюзе" not in head


async def test_a_stale_snapshot_is_marked_as_stale_on_the_bundle_screen(services, slot):
    """Тот же блок при лежащем канале обязан сказать, что смотрит в прошлое:
    выпустить файл по устаревшей сверке — значит гадать."""
    _snap(services, online=False)
    services.gwlink_session_closed(1)
    services.db.set_state("gwlink_snap_at_1", "2026-09-22T18:00:00+03:00")
    head, _ = await sh._screen("rt_bundle", services, "1")
    assert "канал сейчас не на связи" in head and "назад" in head


async def test_a_mismatched_awg_generation_is_called_out(services, slot, fake_bot):
    """Поколение ядра разъехалось между концами линка — это про «что стоит на
    той стороне», и заметить это должен человек, а не пользователь, у которого
    перестал ходить трафик."""
    from awgbot.infra import awglock
    _snap(services, awg_generation=7)
    text, _ = await _card(services, fake_bot)
    assert f"поколение AWG 7, у сервера {awglock.generation()}" in text


async def test_html_from_the_gateway_is_escaped_on_the_screen(services, slot, fake_bot):
    """Снимок приходит с малины, а она может быть скомпрометирована. Разметка в
    значении обязана доехать до экрана текстом, иначе карточка ломается или
    показывает чужую ссылку как свою."""
    _snap(services, agent_version="<b>3.1.0</b>")
    text, _ = await _card(services, fake_bot)
    assert "&lt;b&gt;3.1.0&lt;/b&gt;" in text and "<b>3.1.0</b>" not in text


# ── кнопка «Обновить с шлюза» ────────────────────────────────────────────────

def _no_waiting(monkeypatch):
    """Ожидание ответа шлюза — без настоящих секунд: тик тот же, часы наши."""
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: real_sleep(0))


async def test_the_refresh_button_asks_the_gateway_and_redraws_the_card(
        services, slot, fake_bot, monkeypatch):
    """Кнопка нужна не ради свежести (снимок и так свеж), а ради доверия:
    человек, который только что применил бандл, хочет увидеть результат сейчас.
    Автообновления по таймеру нет и не будет — это был бы тот же период, только
    с человеческим лицом."""
    _snap(services, bundle={**_installed(services), "home_subnets": "192.168.1.0/24"})
    services.db.set_state("gwlink_snap_at_1", "2026-09-22T18:00:00+03:00")
    sent = []

    class _Srv:
        # Счётчик принятых снимков — по нему кнопка и понимает, что ответ
        # пришёл: строка времени в state с точностью до секунды для этого не годится.
        snaps_in: dict = {}

        async def send(self, slot_id, kind, body=None):
            """Шлюз на том конце: получил `ask snap` — прислал полный снимок."""
            sent.append((slot_id, kind, dict(body or {})))
            services.gwlink_snapshot_in(slot_id, {"bundle": _installed(services),
                                                  "agent_version": "3.1.0", "rev": 1}, 1, True)
            self.snaps_in[slot_id] = self.snaps_in.get(slot_id, 0) + 1
            return True

    monkeypatch.setattr(linkserver, "current", lambda: _Srv())
    _no_waiting(monkeypatch)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_snap(cb, GwSlotCB(action="snap", slot=1), services)

    assert sent == [(1, "ask", {"what": "snap"})], "по кнопке уходит ровно один запрос"
    text, _ = _screen(nav)
    assert "✅ Конфигурация на шлюзе совпадает с выданной" in text, (
        "карточка перерисована из старого снимка")
    assert cb.answers[-1] == ("Снимок обновлён", False)


async def test_a_gateway_that_did_not_answer_in_time_is_not_reported_as_refreshed(
        services, slot, fake_bot, monkeypatch):
    """Агент мог быть занят тиком. «Обновлено» поверх старой карточки — прямая
    ложь: человек решит, что видит результат своего последнего действия."""
    _snap(services)
    services.db.set_state("gwlink_snap_at_1", "2026-09-22T18:00:00+03:00")

    class _Mute:
        snaps_in: dict = {}                  # снимков не приходило и не придёт

        async def send(self, slot_id, kind, body=None):
            return True                      # запрос ушёл, ответа не будет

    monkeypatch.setattr(linkserver, "current", lambda: _Mute())
    _no_waiting(monkeypatch)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_snap(cb, GwSlotCB(action="snap", slot=1), services)

    text, _ = _screen(nav)
    assert "Канал до шлюза" in text, "карточку всё равно показываем — с прежним снимком"
    assert cb.answers[-1][0] == "Шлюз не ответил за 3 секунды — показан прежний снимок"
    assert cb.answers[-1][1] is True, "молчание шлюза человек должен заметить"


async def test_the_refresh_button_says_plainly_that_the_channel_is_down(
        services, slot, fake_bot, monkeypatch):
    """Кнопку могли нажать из старого сообщения, когда канал уже лёг. Ответ —
    прямой алерт, а не молчание и не перерисованная карточка с тем же старым."""
    _snap(services, online=False)
    monkeypatch.setattr(linkserver, "current", lambda: None)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_snap(cb, GwSlotCB(action="snap", slot=1), services)
    assert cb.answers == [("Канал до шлюза сейчас не на связи", True)]
    assert not [x for x in nav.sent if x[0] == "edit_text"], "экран перерисован впустую"


# ── напоминания о перевыпуске ────────────────────────────────────────────────

def test_a_matching_installed_configuration_silences_the_reissue_reminder(services, slot):
    """Раньше ВПС сравнивал своё со своим — что выдал тогда с тем, что выдал бы
    сейчас, — и напоминал о перевыпуске даже тогда, когда человек уже применил
    файл другим путём. С каналом напоминание перестаёт быть догадкой: на шлюзе
    стоит то же самое — молчим."""
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_DEPS_KEY, 1), "lan=0;nets=;resolver=;peer=")
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_SSH_KEY, 1), "")
    assert services.gw_bundle_drift_notes(), "без снимка напоминание работает как раньше"

    _snap(services)
    assert services.gw_bundle_drift_notes() == [], (
        "на шлюзе стоит ровно выдаваемое, а бот всё равно гонит перевыпускать")


def test_a_drifted_installed_configuration_still_asks_for_a_reissue(services, slot):
    """Обратная сторона: снимок есть, но установленное не совпадает — значит
    прошлый файл не доехал, и напоминание обязано остаться."""
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_DEPS_KEY, 1), "lan=0;nets=;resolver=;peer=")
    _snap(services, bundle={**_installed(services), "home_subnets": "192.168.1.0/24"})
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1 and "Перевыпусти" in notes[0].text


# ── панель агента ────────────────────────────────────────────────────────────

def test_the_agent_panel_shows_the_channel_only_when_the_bundle_turned_it_on(monkeypatch):
    """Канал не включён бандлом — строки нет вовсе: сказать о нём нечего, а
    «выключено» читалось бы как поломка на машине, где функции просто нет."""
    from awgbot.bot.texts.gateway import channel_panel_line
    from awgbot.domain.gateway import GwStatus
    from awgbot.runtime import linkclient

    monkeypatch.setattr(linkclient, "enabled", lambda: False)
    assert channel_panel_line() == ""
    assert "Канал до ВПС" not in texts.gateway_panel(GwStatus(link_up=True, handshake_age=5.0))

    monkeypatch.setattr(linkclient, "enabled", lambda: True)
    monkeypatch.setattr(linkclient, "online", lambda: False)
    assert channel_panel_line() == "🔗 Канал до ВПС: ⚪ нет связи"
    monkeypatch.setattr(linkclient, "online", lambda: True)
    assert channel_panel_line() == "🔗 Канал до ВПС: 🟢 на связи"
    panel = texts.gateway_panel(GwStatus(link_up=True, handshake_age=5.0, server_name="awg-srv"))
    assert "🔗 Канал до ВПС: 🟢 на связи" in panel
    assert panel.index("📡 Линк до awg-srv") < panel.index("🔗 Канал до ВПС"), (
        "строка канала стоит рядом со строкой линка — они про один и тот же путь")
