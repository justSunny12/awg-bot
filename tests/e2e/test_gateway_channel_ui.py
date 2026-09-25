"""
Экраны канала до шлюза (концепт «канал линка», §7): блок канала в карточке
слота — вместе со сверкой выданного с установленным, строка канала в панели
агента. Спрашивать шлюз по кнопке (свежий снимок, диагностика) карточка
больше не умеет — показывает то, что шлюз прислал сам.

Экрана «Конфигурация шлюза» перед выпуском файла больше нет (вычитка 3.1.0):
файл выпускается из карточки сразу. Всё, что человеку нужно для решения,
переехало в карточку — пункты расхождения при лежащем канале (с возрастом
снимка), обвязка старого образца или не развёрнутая, подсети соседей. Чего
экран показывал сверх этого — «совпадает с тем, что выдаст этот файл»,
строка «Контракт линка», подсказки «файл для них не нужен» / «дождись
канала» — не показывается больше нигде, и тесты на это сняты: карточка
говорит то же короче («✅ актуальна», «⏳ уходят каналом»).

Всё, что рисуется здесь, приехало с чужой машины: экран обязан говорить
«последнее известное, N назад», когда канал лежит, молчать, когда шлюз ещё
ничего не сообщал. Цена ошибки — человек чинит несуществующее расхождение или, наоборот,
уверен, что конфигурация доехала.
"""
from __future__ import annotations

import base64
import os

import pytest

from awgbot.bot import texts
from awgbot.bot.callbacks import GwSlotCB
from awgbot.bot.handlers import settings as sh
from awgbot.core import config, settings
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

async def test_a_live_channel_shows_four_lines_and_no_request_buttons(services, slot, fake_bot):
    """Вся видимая польза этапа: человек открывает карточку и видит не догадку,
    а ответ — канал жив, вот версия агента, установленное совпадает с выданным,
    наружу шлюз выходит. Кнопок «Обновить с шлюза» и «Диагностика» больше нет:
    сервер ничего не спрашивает у шлюза по нажатию."""
    _snap(services)
    text, labels = await _card(services, fake_bot)
    assert "🔗 Канал до шлюза: 🟢 на связи" in text
    assert "🤖 Версия агента: 3.1.0" in text
    assert "✅ Конфигурация шлюза актуальна" in text
    assert "🌐 Выход наружу: сервер — есть, шлюз — есть" in text
    assert not [b for b in labels if "Обновить с шлюза" in b or "Диагностика" in b], (
        f"снятые кнопки запроса к шлюзу вернулись в карточку: {labels}")
    assert "Пинг с " in text and services.pings == [1], (
        "пинг меряет путь ядро ↔ ядро, отклик канала — занятость процесса агента; "
        "подменить первое вторым значило бы врать в карточке")


async def test_a_gateway_that_never_spoke_says_so_and_offers_no_button(services, slot, fake_bot):
    """Канал ещё не поднимался: ни одной строки о конфигурации шлюза мы не
    выдумываем — «сказать нечего» честнее, чем «всё сошлось» без единого
    факта."""
    text, labels = await _card(services, fake_bot)
    assert "🔗 Канал до шлюза: ещё не поднимался" in text
    assert "перевыпусти конфигурацию шлюза" in text
    assert "Конфигурация" not in text and "🤖 Версия агента" not in text


async def test_a_dead_channel_shows_the_last_known_picture_with_its_age(services, slot, fake_bot):
    """Снимок при падении канала не выбрасываем — последнее известное полезнее
    пустого экрана. Но возраст обязан стоять рядом: иначе старое читается как
    настоящее."""
    _snap(services, online=False)
    services.gwlink_session_closed(1)
    services.db.set_state("gwlink_snap_at_1", "2026-09-22T18:00:00+03:00")
    text, labels = await _card(services, fake_bot)
    assert "🔗 Канал до шлюза: ⚪ нет связи" in text
    assert "🤖 Версия агента: 3.1.0" in text, "версию агента из последнего снимка показываем и при лежащем канале"
    assert "✅ Конфигурация шлюза актуальна (по снимку" in text and "назад)" in text, (
        "возраст снимка не показан — старое рисуется как настоящее")


def _snap_age(services, hours: int) -> None:
    """Снимок принят `hours` часов назад по часам ВПС."""
    import datetime
    from awgbot.util import timeutil
    at = timeutil.now() - datetime.timedelta(hours=hours, minutes=5)
    services.db.set_state("gwlink_snap_at_1", timeutil.to_iso(at))


async def test_a_drifted_configuration_is_listed_on_the_card_with_the_snapshot_age(
        services, slot, fake_bot):
    """Главный ответ канала: что реально стоит на шлюзе. Разошлось, а канал
    молчит — доставить сейчас некому, файл остаётся первым путём, и карточка
    (единственное место, где это видно) перечисляет пункты. Возраст снимка —
    в тех же скобках: выпустить файл по устаревшей сверке значит гадать."""
    _snap(services, bundle={**_installed(services), "home_subnets": "192.168.1.0/24",
                            "resolver": ""}, online=False)
    services.gwlink_session_closed(1)
    _snap_age(services, 2)
    text, _ = await _card(services, fake_bot)
    assert ("⚠️ Конфигурация на шлюзе расходится с выданной (2 пункта, по снимку 2 ч назад):\n"
            "   • локальные подсети: у сервера <code>192.168.68.0/24</code>, на шлюзе <code>192.168.1.0/24</code>\n"
            "   • резолвер: у сервера <code>10.9.1.1</code>, на шлюзе «—»") in text, text
    assert "уходят каналом" not in text, "канал лежит, а карточка обещает доставку"
    assert "подробности в «Конфигурация шлюза»" not in text, "ссылка на упразднённый экран"


async def test_the_drift_items_from_the_gateway_are_escaped_on_the_card(services, slot, fake_bot):
    """Значение «на шлюзе» приехало с чужой машины: разметка в нём обязана
    лечь текстом, иначе Telegram отвергнет карточку целиком."""
    _snap(services, bundle={**_installed(services), "home_subnets": "<b>x</b> & y"}, online=False)
    services.gwlink_session_closed(1)
    text, _ = await _card(services, fake_bot)
    assert "на шлюзе <code>&lt;b&gt;x&lt;/b&gt; &amp; y</code>" in text, text
    assert "<b>x</b>" not in text


async def test_a_drift_with_a_live_channel_is_shown_as_on_its_way(services, slot, fake_bot):
    """Канал жив — расхождение уедет само ближайшим сообщением. Гнать человека
    перевыпускать файл значит заставить его делать руками то, что канал сделает
    за секунды, а после — разбираться, какое из двух применений победило."""
    _snap(services, bundle={**_installed(services), "home_subnets": "192.168.1.0/24",
                            "resolver": ""})
    text, _ = await _card(services, fake_bot)
    assert ("⏳ Конфигурация на шлюзе расходится с выданной (2 пункта) — изменения уходят "
            "каналом и применятся сами") in text
    assert "⚠️ Конфигурация на шлюзе расходится" not in text


async def test_a_refused_delivery_is_said_plainly_on_the_card(services, slot, fake_bot):
    """Шлюз получил настройки и не смог применить — откатился. «Уходят каналом»
    здесь было бы ложью навсегда: этот набор в сессии больше не пошлют. Человек
    должен увидеть причину, а она пришла с чужой машины — только текстом."""
    _snap(services, bundle={**_installed(services), "home_subnets": "192.168.1.0/24"})
    services.gwlink_ack_in(1, {"ok": False, "error": "<b>LAN-интерфейс</b> не найден"})
    text, _ = await _card(services, fake_bot)
    assert ("⚠️ Шлюз не применил настройки с сервера: &lt;b&gt;LAN-интерфейс&lt;/b&gt; не найден "
            "— вернул прежние") in text
    assert "<b>LAN-интерфейс</b>" not in text, "разметка с малины легла в карточку как есть"
    assert "уходят каналом" not in text


async def test_an_old_refusal_does_not_hide_a_dead_channel(services, slot, fake_bot):
    """Отказ из прошлой сессии, а канал уже лежит: главное сейчас — что доставить
    нечем. Строка возвращается к «расходится» с пунктами и возрастом снимка."""
    _snap(services, bundle={**_installed(services), "home_subnets": "192.168.1.0/24"},
          online=False)
    services.gwlink_ack_in(1, {"ok": False, "error": "exit 1"})
    services.gwlink_session_closed(1)
    text, _ = await _card(services, fake_bot)
    assert "⚠️ Конфигурация на шлюзе расходится с выданной (1 пункт, по снимку " in text, text
    assert "не применил настройки" not in text


async def test_a_successful_delivery_leaves_no_trace_of_the_refusal(services, slot, fake_bot):
    """Следующее применение прошло — прежний отказ больше не висит в карточке."""
    _snap(services)
    services.gwlink_ack_in(1, {"ok": False, "error": "exit 1"})
    services.gwlink_ack_in(1, {"ok": True, "error": ""})
    text, _ = await _card(services, fake_bot)
    assert "✅ Конфигурация шлюза актуальна" in text
    assert "не применил" not in text


OLD_PLUMBING = "⚠️ Обвязка шлюза старого образца — перевыпусти файл конфигурации и примени его на шлюзе"
NO_PLUMBING = "⚠️ Обвязка шлюза не развёрнута — примени файл конфигурации на шлюзе"


async def test_old_plumbing_on_the_gateway_is_called_out_on_the_card(services, slot, fake_bot):
    """Обвязка старого образца или снятая таблица — ответ на вопрос, который ВПС
    до канала задавал наугад. Экрана выпуска больше нет — человек читает это в
    карточке, откуда и выпускает файл; строка идёт после строки конфигурации."""
    _snap(services, plumbing_gen="old", link_contract="")
    text, _ = await _card(services, fake_bot)
    assert "✅ Конфигурация шлюза актуальна\n" + OLD_PLUMBING in text, text
    assert NO_PLUMBING not in text

    _snap(services, plumbing_gen="none")
    text, _ = await _card(services, fake_bot)
    assert NO_PLUMBING in text and OLD_PLUMBING not in text, text


async def test_new_plumbing_draws_no_plumbing_line(services, slot, fake_bot):
    """Обвязка нового образца — строки нет: предупреждение без повода учит
    пропускать предупреждения."""
    for gen in ("new", ""):
        _snap(services, plumbing_gen=gen)
        text, _ = await _card(services, fake_bot)
        assert "Обвязка шлюза" not in text, (gen, text)


async def test_a_snapshot_without_the_installed_configuration_is_not_a_green_tick(
        services, slot, fake_bot):
    """Снимок пришёл, а блока «что применено из конфигурации» в нём нет — так
    выглядит и старый агент, и урезанный чужой снимок. Пустой список расхождений
    здесь значит «шлюз не сообщал, что у него стоит», а не «всё сошлось»: ровно
    об этом говорит докстринг gwlink_config_drift и вариант строки из концепта
    §7.1 «⚙️ Конфигурация: шлюз ещё не сообщал».

    Цена ошибки — зелёная галочка там, где сверки не было: человек видит
    «конфигурация совпадает» и не перевыпускает файл, хотя на шлюзе может стоять
    что угодно. Напоминание о перевыпуске при этом продолжает приходить, то есть
    экраны бота противоречат друг другу."""
    services.gwlink_snapshot_in(1, {"agent_version": "3.0.0", "egress_ok": True,
                                    "ts": "2026-09-22T20:00:00+03:00", "rev": 1}, 1, True)
    services.gwlink_session_opened(1, "3.0.0", 1)
    text, _ = await _card(services, fake_bot)
    assert "🤖 Версия агента: 3.0.0" in text, "версию агента из такого снимка показать можно"
    assert "Конфигурация шлюза актуальна" not in text, "сверки не было — зелёная галочка выдумана"
    assert "⚙️ Конфигурация: шлюз ещё не сообщал" in text, "не сказано, почему сверки нет"

    services.gwlink_snapshot_in(1, {"plumbing_gen": "old", "rev": 2}, 2, False)
    text, _ = await _card(services, fake_bot)
    assert "Обвязка шлюза" not in text, (
        "снимок без сведений об установленном — и строку об обвязке рисовать не на чем")


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
    """Обратная сторона: снимок есть, установленное не совпадает, а канал молчит
    дольше суток — ждать его больше нечего. Возвращается прежний путь: одно
    тихое напоминание перевыпустить файл, и не больше одного на расхождение."""
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_DEPS_KEY, 1), "lan=0;nets=;resolver=;peer=")
    _snap(services, bundle={**_installed(services), "home_subnets": "192.168.1.0/24"}, online=False)
    services.gwlink_session_closed(1)
    services.db.set_state("gwlink_seen_1", "2026-01-01T00:00:00+03:00")
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1 and "Перевыпусти" in notes[0].text
    assert services.gw_bundle_drift_notes() == [], "напоминание повторилось на то же расхождение"


def test_a_live_channel_silences_the_reissue_reminder_it_will_deliver_itself(services, slot):
    """Канал жив — он довезёт расхождение сам. Напоминание «перевыпусти» тогда
    шум: человек перевыпустит, а канал довёз бы то же самое."""
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_DEPS_KEY, 1), "lan=0;nets=;resolver=;peer=")
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_SSH_KEY, 1), "")
    _snap(services, bundle={**_installed(services), "home_subnets": "192.168.1.0/24"})
    assert services.gw_bundle_drift_notes() == []


def test_a_briefly_silent_channel_still_covers_the_delivery(services, slot):
    """Канал лёг пару часов назад (ребут малины, линк моргнул) — он поднимется и
    довезёт. Напоминать о перевыпуске после каждого моргания линка — шум."""
    import datetime
    from awgbot.util import timeutil
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_DEPS_KEY, 1), "lan=0;nets=;resolver=;peer=")
    _snap(services, bundle={**_installed(services), "home_subnets": "192.168.1.0/24"}, online=False)
    services.gwlink_session_closed(1)
    services.db.set_state("gwlink_seen_1", timeutil.to_iso(timeutil.now() - datetime.timedelta(hours=2)))
    assert services.gw_bundle_drift_notes() == [], "канал молчит два часа, а бот уже гонит перевыпускать"
    services.db.set_state("gwlink_seen_1", timeutil.to_iso(timeutil.now() - datetime.timedelta(hours=25)))
    assert len(services.gw_bundle_drift_notes()) == 1, "канал молчит сутки — напоминание обязано вернуться"


def test_a_channel_that_never_came_up_does_not_silence_the_reminder(services, slot):
    """Канал не поднимался ни разу (старый агент, выключен бандлом) — доставлять
    нечем, и напоминание работает как до канала."""
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_DEPS_KEY, 1), "lan=0;nets=;resolver=;peer=")
    assert len(services.gw_bundle_drift_notes()) == 1


# ── панель агента ────────────────────────────────────────────────────────────

def test_the_agent_panel_shows_the_channel_only_when_the_bundle_turned_it_on(monkeypatch):
    """Канал не включён бандлом — строки нет вовсе: сказать о нём нечего, а
    «выключено» читалось бы как поломка на машине, где функции просто нет."""
    from awgbot.bot.texts.gateway import channel_panel_line
    from awgbot.domain.gateway import GwStatus
    from awgbot.runtime import linkclient

    monkeypatch.setattr(linkclient, "enabled", lambda: False)
    assert channel_panel_line() == ""
    assert "Канал до сервера AWG" not in texts.gateway_panel(GwStatus(link_up=True, handshake_age=5.0))

    monkeypatch.setattr(linkclient, "enabled", lambda: True)
    monkeypatch.setattr(linkclient, "online", lambda: False)
    assert channel_panel_line() == "🔗 Канал до сервера AWG: ⚪ нет связи"
    monkeypatch.setattr(linkclient, "online", lambda: True)
    assert channel_panel_line() == "🔗 Канал до сервера AWG: 🟢 на связи"
    panel = texts.gateway_panel(GwStatus(link_up=True, handshake_age=5.0, server_name="awg-srv"))
    assert "🔗 Канал до сервера AWG: 🟢 на связи" in panel
    assert panel.index("📡 Линк до awg-srv") < panel.index("🔗 Канал до сервера AWG"), (
        "строка канала стоит рядом со строкой линка — они про один и тот же путь")


# ── этап 4: тумблеры при живом канале ────────────────────────────────────────

async def test_the_lan_mode_dialog_does_not_ask_for_a_reissue_while_the_channel_is_up(
        services, slot, fake_bot):
    """Канал доставит смену режима сам — гнать человека перевыпускать файл значит
    заставить его делать руками то, что уже едет."""
    _snap(services)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_ask(cb, GwSlotCB(action="lan_ask", slot=1), services)
    text, _ = _screen(nav)
    assert "шлюз получит по каналу и применит сам" in text
    assert "потребует перевыпустить" not in text
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1), services)
    assert services.db.gateway(1).lan_mode == 0
    assert cb.answers[-1] == ("Выключено: шлюз применит сам по каналу", True)


async def test_the_lan_mode_dialog_asks_for_a_reissue_when_the_channel_is_down(
        services, slot, fake_bot):
    _snap(services, online=False)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_ask(cb, GwSlotCB(action="lan_ask", slot=1), services)
    text, _ = _screen(nav)
    assert "Выключение потребует перевыпустить конфигурацию шлюза" in text
    assert "по каналу" not in text
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1), services)
    assert cb.answers[-1] == ("Выключено: перевыпусти конфигурацию шлюза", True)


def _peer_store(monkeypatch, on=False):
    store = {"app.routing.enabled": True, "app.routing.failover.enabled": True,
             "app.routing.peer_nets.enabled": on}
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: bool(store.get(k, d)))
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v) or [k])
    return store


async def test_the_peer_toggle_always_asks_for_a_reissue_even_with_a_live_channel(
        services, slot, fake_bot, monkeypatch):
    """Подсети соседей живут на шлюзе ещё и в конфиге линка, а его везёт только
    файл. Пообещай диалог «применят сами» — человек не перевыпустит, и доступ
    между подсетями не заработает при зелёном канале."""
    store = _peer_store(monkeypatch)
    _snap(services)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_peer_ask(cb, services)
    text = _screen(nav)[0]
    assert "перевыпусти" in text and "по каналу" not in text
    await sh.gw_slot_peer_yes(cb, services)
    assert store["app.routing.peer_nets.enabled"] is True
    assert cb.answers[-1] == ("Доступ между подсетями включён: перевыпусти конфигурации шлюзов", True)


async def test_the_lan_mode_answer_still_asks_for_a_reissue_for_the_neighbour_subnets(
        services, slot, fake_bot, monkeypatch):
    """Режим доедет каналом, а подсети соседей при включённом доступе между
    подсетями меняются у обоих шлюзов и живут в конфиге линка — это файлом."""
    _peer_store(monkeypatch, on=True)
    _snap(services)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_ask(cb, GwSlotCB(action="lan_ask", slot=1), services)
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1), services)
    assert cb.answers[-1] == ("Выключено: шлюз применит сам по каналу; для доступа между "
                              "подсетями перевыпусти конфигурации шлюзов", True)


def _neighbour(services, monkeypatch):
    """Второй шлюз с режимом без VPN и включённый доступ между подсетями: у
    слота 1 появляются подсети соседа, которых на нём ещё нет."""
    _peer_store(monkeypatch, on=True)
    admin = services.db.get_client_by_tg(ADMIN)
    pi2 = services.add_device(admin.id, "Pi2")
    services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    services.gateway_set_home_subnets(2, "192.168.70.0/24")
    services.gateway_set_lan_mode(2, True)
    assert services.gateway_peer_nets(1) == ["192.168.70.0/24"], "сценарий собран не так"


async def test_neighbour_subnets_out_of_date_are_not_promised_to_the_channel(
        services, slot, fake_bot, monkeypatch):
    """Включили доступ между подсетями, канал жив, конфигурацию не перевыпустили.
    Канал подсети соседей не везёт (они живут и в конфиге линка), значит
    карточка не вправе говорить «уходят каналом и применятся
    сами» — иначе человек ждёт того, что не приедет никогда."""
    _neighbour(services, monkeypatch)
    _snap(services)                                     # на шлюзе подсетей соседей ещё нет
    assert services.gwlink_settings_due(services.db.gateway(1)) is None, (
        "канал их и не везёт — сценарий про экран")
    text, _ = await _card(services, fake_bot)
    assert "уходят каналом и применятся сами" not in text, (
        "карточка обещает доставку каналом того, что канал не везёт")
    assert ("⚠️ Локальные подсети других шлюзов неактуальны — они едут только файлом: "
            "перевыпусти конфигурацию шлюза") in text


async def test_neighbour_subnets_out_of_date_are_listed_when_the_channel_is_down(
        services, slot, fake_bot, monkeypatch):
    """Канал лежит, подсетей соседа на шлюзе нет — карточка называет пункт
    (подробности экрана выпуска переехали сюда)."""
    _neighbour(services, monkeypatch)
    _snap(services, online=False)
    services.gwlink_session_closed(1)
    text, _ = await _card(services, fake_bot)
    assert ("⚠️ Конфигурация на шлюзе расходится с выданной (1 пункт, по снимку только что):\n"
            "   • локальные подсети других шлюзов: у сервера <code>192.168.70.0/24</code>, на шлюзе «—»"
            ) in text, text


async def test_a_mixed_drift_says_what_the_channel_brings_and_what_needs_the_file(
        services, slot, fake_bot, monkeypatch):
    _neighbour(services, monkeypatch)
    _snap(services, bundle={**_installed(services), "home_subnets": "192.168.1.0/24"})
    text, _ = await _card(services, fake_bot)
    assert ("⏳ Конфигурация на шлюзе расходится с выданной (2 пункта) — изменения уходят каналом "
            "и применятся сами; локальные подсети других шлюзов — только перевыпуском файла") in text


def test_a_live_channel_does_not_silence_the_reminder_about_neighbour_subnets(
        services, slot, monkeypatch):
    """Напоминание перевыпустить файл молчит, пока канал довезёт всё сам. Подсети
    соседей он не довезёт — значит напоминание о них обязано прийти, один раз."""
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_DEPS_KEY, 1),
                          services._gw_bundle_deps(services.db.gateway(1)))
    _snap(services)
    _neighbour(services, monkeypatch)
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1 and "Перевыпусти" in notes[0].text, "канал жив — и о подсетях соседей молчок"
    assert services.gw_bundle_drift_notes() == [], "одно напоминание на расхождение"


# ── вердикт подсетей соседей на карточке ─────────────────────────────────────

async def test_missing_neighbour_subnets_on_the_gateway_are_named_on_the_card(services, slot, fake_bot):
    """Доступ между подсетями включают тумблером на ВПС. Шлюз сообщил, что в
    его таблице нет подсетей соседа, — это единственный вердикт, который едет с
    малины, и едет ради этой строки: без неё отказ функции беспричинен."""
    _snap(services, peer_nets={"ok": False, "missing": ["192.168.70.0/24", "192.168.71.0/24"]})
    text, _ = await _card(services, fake_bot)
    assert ("⚠️ Доступ между подсетями: на шлюзе в таблице нет 192.168.70.0/24, 192.168.71.0/24"
            " — перевыпусти конфигурацию шлюза и примени её") in text


async def test_a_healthy_neighbour_subnets_verdict_draws_nothing(services, slot, fake_bot):
    _snap(services, peer_nets={"ok": True, "missing": []})
    text, _ = await _card(services, fake_bot)
    assert "Доступ между подсетями" not in text, "исправная функция нарисована как отказ"
    _snap(services)                                  # функции на шлюзе нет — поля нет
    text, _ = await _card(services, fake_bot)
    assert "Доступ между подсетями" not in text


def test_the_neighbour_subnets_line_escapes_what_the_gateway_sent():
    """Имена подсетей приехали с чужой машины: разметка в них ломает сообщение
    целиком, и карточка не рисуется вовсе."""
    from awgbot.bot.texts.routing import channel_block
    ch = {"ever": True, "online": True, "has_snap": True, "has_bundle": True,
          "peer_nets": {"ok": False, "missing": ["<b>1.2.3.0/24</b>", "&x"]}}
    out = channel_block(ch, True)
    assert "<b>1.2.3.0/24</b>" not in out
    assert "&lt;b&gt;1.2.3.0/24&lt;/b&gt;, &amp;x" in out


def test_the_neighbour_subnets_line_names_at_most_eight_and_survives_an_empty_list():
    from awgbot.bot.texts.routing import channel_block
    many = [f"192.168.{i}.0/24" for i in range(12)]
    ch = {"ever": True, "online": True, "has_snap": True, "has_bundle": True,
          "peer_nets": {"ok": False, "missing": many}}
    out = channel_block(ch, True)
    assert "192.168.7.0/24" in out and "192.168.8.0/24" not in out, "список не ограничен восемью"
    ch["peer_nets"] = {"ok": False, "missing": []}
    assert "на шлюзе в таблице нет подсетей" in channel_block(ch, True), (
        "пустой список отказа — строка без предмета")


@pytest.mark.parametrize("skew, side", [(600, "спешат"), (-3600, "отстают")])
def test_the_clock_line_warns_about_the_clock_not_about_the_channel(skew, side):
    """Канал от часов больше не зависит: строка, обещающая, что «канал
    перестанет принимать сообщения», послала бы человека чинить то, что не
    сломается, и промолчала бы о том, что сломается — TLS и расписания."""
    from awgbot.bot.texts.routing import channel_block
    ch = {"ever": True, "online": True, "has_snap": True, "has_bundle": True, "clock_skew": skew}
    out = channel_block(ch, True)
    line = next((x for x in out.splitlines() if x.startswith("⏱ Часы шлюза")), "")
    assert line, f"расхождение {skew} с не показано"
    assert f"{side} на {abs(skew) // 60} мин" in line
    assert "синхронизацию" in line, "не сказано, что делать"
    assert "перестанет принимать" not in line and "канал" not in line, (
        f"строка по-прежнему пугает отказом канала: {line}")


def test_a_small_clock_drift_draws_nothing():
    from awgbot.bot.texts.routing import channel_block
    ch = {"ever": True, "online": True, "has_snap": True, "has_bundle": True, "clock_skew": 119}
    assert "Часы шлюза" not in channel_block(ch, True), "дрожь в пару минут подана как проблема"
