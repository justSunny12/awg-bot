"""
Экраны канала до шлюза: строка «🔗 Упр. канал …» в карточке слота и
предупреждения открытыми строками под ней — вместе со сверкой выданного с
установленным. Спрашивать шлюз по кнопке (свежий снимок, диагностика) карточка
больше не умеет — показывает то, что шлюз прислал сам. Карточка слота во всех
состояниях канала (жив, лежит с возрастом снимка, расхождения, отказ шлюза,
устаревший агент, поколение, связь подсетей, часы, выход наружу, подсети соседей) и
диалоги тумблеров при живом и лежащем канале — в эталоне
tests/screens/admin.txt (adm.gw.card.ch.*, adm.gw.lan_*.ch.*,
adm.gw.lan_ask.peers), строка канала в панели агента — в gateway.txt; здесь —
что меняется в слоте и настройках, уведомления о расхождении и строки канала
на входе функций.

Экрана «Конфигурация шлюза» перед выпуском файла больше нет (вычитка 3.1.0):
файл выпускается из карточки сразу. Всё, что человеку нужно для решения,
переехало в карточку — пункты расхождения при лежащем канале (с возрастом
снимка), подсети соседей; что обвязка на шлюзе не развёрнута, говорит сам
агент в «🩺 Здоровье». Чего
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
    services.gateway_set_lan_subnets(1, "192.168.68.0/24")
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
    return {"lan_mode": "1", "lan_subnets": "192.168.68.0/24", "resolver": "10.9.1.1",
            "peer_lan_nets": "", "admin_ips": addrs}


CAPS = ["cfg1", "confirm1", "token1"]


def _snap(services, *, bundle=None, online=True, caps=CAPS, **kw):
    """Положить снимок так, как его принял бы канал, и при желании открыть
    сессию (по умолчанию — агента не ниже 3.3.0)."""
    snap = {"bundle": bundle if bundle is not None else _installed(services),
            "link_contract": "1", "mark_status": "confirmed",
            "agent_version": "3.3.0", "awg_generation": 1, "egress_ok": True,
            "boot_id": "b" * 36, "ts": "2026-09-22T20:00:00+03:00", "rev": 1}
    snap.update(kw)
    services.gwlink_snapshot_in(1, snap, 1, True)
    if online:
        services.gwlink_session_opened(1, snap["agent_version"], 2, list(caps))
    return snap


async def _card(services, fake_bot):
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=1), services, FakeState())
    return _screen(nav)


# ── карточка слота ───────────────────────────────────────────────────────────

async def test_a_live_channel_does_not_replace_the_link_ping(services, slot, fake_bot):
    """Канал жив, а пинг в карточке меряется по линку, как и без канала. Сама
    карточка с живым каналом (одна строка, выход наружу под «подробнее», без
    кнопок запроса к шлюзу) — эталон adm.gw.card.ch.live."""
    _snap(services)
    await _card(services, fake_bot)
    assert services.pings == [1], (
        "пинг меряет путь ядро ↔ ядро, отклик канала — занятость процесса агента; "
        "подменить первое вторым значило бы врать в карточке")


# ── уведомления о расхождении ────────────────────────────────────────────────

def _refuse(services):
    """Шлюз на связи отверг раздел env документа, который ВПС хочет сейчас."""
    from awgbot.domain import gwcfg
    doc = services.gwcfg_doc(services.db.gateway(1))
    services.gwcfg_ack_in(1, gwcfg.ack_build(doc["gen"], doc["hash"], failed={"env": "exit 1"}))


def test_a_matching_installed_configuration_notifies_nothing(services, slot):
    """На шлюзе стоит ровно выдаваемое — даже отказ шлюза (на тот же набор,
    который уже стоит) не повод для уведомления: расходиться нечему."""
    services.add_device(services.db.get_client_by_tg(ADMIN).id, "Ноутбук")
    _snap(services)
    _refuse(services)
    assert services.gw_bundle_drift_notes() == [], (
        "на шлюзе стоит ровно выдаваемое, а бот всё равно шлёт уведомление")


def test_a_live_channel_silences_what_it_will_deliver_itself(services, slot):
    """Канал жив, ответа на доставку ещё нет — он довезёт расхождение сам.
    Уведомление тогда шум: человек полезет делать то, что уже едет."""
    _snap(services, bundle={**_installed(services), "lan_subnets": "192.168.1.0/24"})
    assert services.gw_bundle_drift_notes() == []


def test_a_silent_gateway_is_not_notified_however_long_it_is_silent(services, slot):
    """Шлюз лёг пару часов, сутки, месяц назад — уведомления нет ни в один из
    моментов: канал обязателен и довезёт, а возраст молчания видно в карточке.
    Даже после отказа шлюза: шлюза нет — и сказать ему нечего."""
    import datetime
    from awgbot.util import timeutil
    _snap(services, bundle={**_installed(services), "lan_subnets": "192.168.1.0/24",
                            "peer_lan_nets": "192.168.2.0/24"}, online=False)
    _refuse(services)
    for hours in (2, 25, 24 * 30):
        services.db.set_state("gwlink_seen_1",
                              timeutil.to_iso(timeutil.now() - datetime.timedelta(hours=hours)))
        assert services.gw_bundle_drift_notes() == [], f"шлюз молчит {hours} ч — и бот прислал уведомление"


def test_a_channel_that_never_came_up_notifies_nothing(services, slot):
    """Канал не поднимался ни разу — снимка нет, судить не по чему. Об этом
    говорит карточка («ещё не поднимался»), а не уведомление по догадке."""
    services.add_device(services.db.get_client_by_tg(ADMIN).id, "Ноутбук")
    assert services.gw_bundle_drift_notes() == []


# ── этап 4: тумблеры при живом канале ────────────────────────────────────────

async def test_the_lan_mode_turns_off_with_a_live_channel(
        services, slot, fake_bot):
    """Канал доставит смену режима сам — гнать человека перевыпускать файл значит
    заставить его делать руками то, что уже едет (вопрос и всплывашка —
    эталоны adm.gw.lan_ask.ch.live, adm.gw.lan_yes.ch.live). Режим снят."""
    _snap(services)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_ask(cb, GwSlotCB(action="lan_ask", slot=1), services)
    assert services.db.gateway(1).lan_mode == 1, "вопрос уже выключил режим"
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1), services)
    assert services.db.gateway(1).lan_mode == 0, "режим не выключен"


async def test_the_lan_mode_turns_off_without_a_channel(
        services, slot, fake_bot):
    """Канала нет — режим всё равно снимается, а вопрос и всплывашка говорят
    о перевыпуске (эталоны adm.gw.lan_ask.ch.dead, adm.gw.lan_yes.ch.dead)."""
    _snap(services, online=False)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_ask(cb, GwSlotCB(action="lan_ask", slot=1), services)
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1), services)
    assert services.db.gateway(1).lan_mode == 0, "режим не выключен"


def _peer_store(monkeypatch, on=False):
    store = {"app.routing.enabled": True, "app.routing.failover.enabled": True,
             "app.routing.peer_nets.enabled": on}
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: bool(store.get(k, d)))
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v) or [k])
    return store


async def test_transit_on_names_the_networks_it_will_be_linked_with(services, slot, fake_bot, monkeypatch):
    """Связь подсетей — часть VPN-транзита: включая его, человек соглашается и
    на связь с сетями других шлюзов с транзитом — диалог называет их (эталон
    adm.gw.lan_ask.peers); других нет — строки нет."""
    _peer_store(monkeypatch)
    admin = services.db.get_client_by_tg(ADMIN)
    pi2 = services.add_device(admin.id, "Pi2")
    services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    services.gateway_set_lan_subnets(2, "192.168.70.0/24")
    line = ("🔗 Связь подсетей: устройства локальных подсетей шлюзов станут доступны из подсетей других шлюзов "
            "с включённым VPN-транзитом (NASPi)")
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_ask(cb, GwSlotCB(action="lan_ask", slot=2), services)
    assert line in _screen(nav)[0], _screen(nav)[0]
    services.gateway_set_lan_mode(1, False)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_ask(cb, GwSlotCB(action="lan_ask", slot=2), services)
    assert "🔗 Связь подсетей:" not in _screen(nav)[0], _screen(nav)[0]


def _neighbour(services, monkeypatch):
    """Второй шлюз с VPN-транзитом — связь подсетей его часть: у слота 1
    появляется префикс соседа в туннеле, которого на нём ещё нет."""
    _peer_store(monkeypatch)
    admin = services.db.get_client_by_tg(ADMIN)
    pi2 = services.add_device(admin.id, "Pi2")
    services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    services.gateway_set_lan_subnets(2, "192.168.70.0/24")
    services.gateway_set_lan_mode(2, True)
    assert services.gateway_peer_nets(1) == ["10.241.0.0/16"], "сценарий собран не так"


# ── строки канала на входе функций: экранирование и пределы ──────────────────

def test_the_neighbour_subnets_line_escapes_what_the_gateway_sent():
    """Имена подсетей приехали с чужой машины: разметка в них ломает сообщение
    целиком, и карточка не рисуется вовсе."""
    ch = {"ever": True, "online": True, "has_snap": True, "has_bundle": True,
          "peer_nets": {"ok": False, "missing": ["<b>1.2.3.0/24</b>", "&x"]}}
    out = _channel_block(ch, True)
    assert "<b>1.2.3.0/24</b>" not in out
    assert "<code>&lt;b&gt;1.2.3.0/24&lt;/b&gt;</code>, <code>&amp;x</code>" in out


def test_the_neighbour_subnets_line_names_at_most_eight_and_survives_an_empty_list():
    many = [f"192.168.{i}.0/24" for i in range(12)]
    ch = {"ever": True, "online": True, "has_snap": True, "has_bundle": True,
          "peer_nets": {"ok": False, "missing": many}}
    out = _channel_block(ch, True)
    assert "192.168.7.0/24" in out and "192.168.8.0/24" not in out, "список не ограничен восемью"
    ch["peer_nets"] = {"ok": False, "missing": ["1" * 40]}
    assert "1" * 18 in _channel_block(ch, True) and "1" * 19 not in _channel_block(ch, True), \
        "элемент списка со шлюза не ограничен"
    ch["peer_nets"] = {"ok": False, "missing": []}
    assert "⚠️ Связь подсетей: на шлюзе нет подсетей — " in _channel_block(ch, True), (
        "пустой список отказа — строка без предмета")


@pytest.mark.parametrize("skew, side", [(600, "спешат"), (-3600, "отстают")])
def test_the_clock_line_warns_about_the_clock_not_about_the_channel(skew, side):
    """Канал от часов больше не зависит: строка, обещающая, что «канал
    перестанет принимать сообщения», послала бы человека чинить то, что не
    сломается, и промолчала бы о том, что сломается — TLS и расписания."""
    ch = {"ever": True, "online": True, "has_snap": True, "has_bundle": True, "clock_skew": skew}
    out = _channel_block(ch, True)
    line = next((x for x in out.splitlines() if x.startswith("⚠️ Часы шлюза")), "")
    assert line, f"расхождение {skew} с не показано"
    assert f"{side} на {abs(skew) // 60} мин" in line
    assert "синхронизацию" in line, "не сказано, что делать"
    assert "перестанет принимать" not in line and "канал" not in line, (
        f"строка по-прежнему пугает отказом канала: {line}")


def test_a_small_clock_drift_draws_nothing():
    ch = {"ever": True, "online": True, "has_snap": True, "has_bundle": True, "clock_skew": 119}
    assert "Часы шлюза" not in _channel_block(ch, True), "дрожь в пару минут подана как проблема"


def _channel_block(ch, server_ok):
    """Строка канала и предупреждения одним блоком — как их склеивает карточка."""
    from awgbot.bot.texts.routing import channel_lines
    head, warns, _egress = channel_lines(ch, server_ok)
    return "\n".join([head] + warns) if head else ""


def test_the_egress_line_names_the_snapshot_age_when_the_channel_is_down():
    """Шлюз мёртв вторую неделю, а карточка хранит последний снимок: «выход
    наружу» без оговорки читался как живой статус."""
    from awgbot.bot.texts.routing import channel_lines
    ch = {"ever": True, "online": False, "seen": "", "age": 13 * 86400, "has_snap": True, "has_bundle": True,
          "egress_gw": True}
    _, _, egress = channel_lines(ch, False)
    # момент снимка — абсолютным временем: сообщение само не перерисовывается
    import re
    assert re.search(r"\(по снимку от \d\d\.\d\d \d\d:\d\d\)$", egress), egress
    ch["online"] = True
    _, _, egress = channel_lines(ch, False)
    assert "по снимку" not in egress, egress


# ── агент с документом конфигурации (cfg1) ───────────────────────────────────
# Карточка такого агента говорит о документе: актуален, уходит каналом,
# расходится по разделам, отказан, ждёт решения человека. Сами экраны — в
# эталоне (adm.gw.card.cfg.*); здесь — какие строки и кнопки появляются в
# каком состоянии. Конфиг линка не собирается (источника на тестовой машине
# нет), почта и фраза ВПС — подстановкой.

MAIL = {"login": "box@example.org", "password": "DUMMY-pass", "imap_host": "imap.example.org",
        "imap_port": 993, "smtp_host": "smtp.example.org", "smtp_port": 465, "backup": "telegram"}


@pytest.fixture()
def cfg1(services, slot, monkeypatch):
    monkeypatch.setattr(services, "gwcfg_link_conf", lambda gw, env: "")
    monkeypatch.setattr(services, "gwcfg_mail", lambda: dict(MAIL))
    monkeypatch.setattr(services, "gwcfg_backup", lambda: {"phrase": "DUMMY phrase long enough"})
    return slot


def _doc(services):
    return services.gwcfg_doc(services.db.gateway(1))


def _cfg_snap(services, *, online=True, caps=("cfg1",), snap=None, **over):
    """Снимок агента с полем cfg (отпечатки желаемого с правками over, прочие
    поля снимка — snap) и сессия с возможностями caps; online=False — сессия
    была и закрылась."""
    doc = _doc(services)
    _snap(services, online=False, **{"agent_version": "3.3.0", **(snap or {})},
          cfg={"gen": doc["gen"], **doc["prints"], **over})
    services.gwlink_session_opened(1, "3.3.0", 2, list(caps))
    if not online:
        services.gwlink_session_closed(1)


def _cfg_ack(services, **body):
    doc = _doc(services)
    services.gwcfg_ack_in(1, {"gen": doc["gen"], "hash": doc["hash"], **body})


async def test_an_up_to_date_cfg1_card_has_no_file_button(services, cfg1, fake_bot):
    """Всё совпадает — строка канала без «актуальна» (только негативные
    исходы), ни строки про старого агента, ни кнопки файла: агенту с документом
    файл не нужен, а кнопка звала бы перевыпускать то, что везёт канал."""
    _cfg_snap(services)
    text, labels = await _card(services, fake_bot)
    assert "🔧 Канал конфигурации 🟢 · v3.3.0\n" in text and "актуальн" not in text, text
    assert "устарел" not in text and "перевыпусти" not in text, text
    assert "📤 Конфигурация" not in labels, labels


async def test_a_cfg1_drift_on_a_live_channel_goes_by_itself(services, cfg1, fake_bot):
    """Шлюз на связи, документ расходится — уходит каналом (К6): число
    пунктов, без хвоста про перевыпуск и без галочки «актуален»."""
    _cfg_snap(services, env="0" * 16, server="0" * 16)
    text, _ = await _card(services, fake_bot)
    assert ("🟡 Конфигурация на шлюзе расходится с выданной (2 пункта) — изменения уходят по каналу "
            "конфигурации и применятся сами") in text.splitlines(), text
    assert "актуальн" not in text and "перевыпусти" not in text, text


async def test_a_cfg1_drift_off_the_channel_is_listed_by_section(services, cfg1, fake_bot):
    """Шлюз не на связи — расходящиеся разделы по именам, с возрастом снимка
    (К9): человек видит, что доедет, когда шлюз вернётся."""
    _cfg_snap(services, online=False, env="0" * 16, server="0" * 16)
    text, labels = await _card(services, fake_bot)
    assert "⚠️ Конфигурация на шлюзе расходится с выданной (2 пункта, по снимку" in text, text
    assert "   • имя сервера\n   • настройки обвязки" in text, text
    assert "📤 Конфигурация" not in labels, "агент с документом не на связи выглядит агентом 3.2"


async def test_a_cfg1_refusal_says_what_and_that_the_old_one_stays(services, cfg1, fake_bot):
    """Шлюз не применил документ (К5): причина текстом, экранирована, и что
    стоит прежнее; «уходят каналом» при этом не обещаем."""
    _cfg_snap(services, env="0" * 16)
    _cfg_ack(services, failed={"env": "<b>dnsmasq</b> не поднялся"})
    text, _ = await _card(services, fake_bot)
    assert ("⚠️ Шлюз не применил конфигурацию: &lt;b&gt;dnsmasq&lt;/b&gt; не поднялся — оставил прежнюю; "
            "подробности в чате бота шлюза" in text.splitlines()), text
    assert "уходят по каналу" not in text and "актуальн" not in text, text


async def test_waiting_mail_and_phrase_are_named_on_the_card(services, cfg1, fake_bot):
    """Вопрос о почте и фразе висит в чате бота шлюза (К14): карточка говорит,
    где решать, и не обещает доставку того, что ждёт человека."""
    doc = _doc(services)
    _cfg_snap(services, mail="ab" * 8, backup="cd" * 8)
    _cfg_ack(services, applied=["env", "server"], wait={"mail": doc["prints"]["mail"],
                                                        "backup": doc["prints"]["backup"]})
    text, _ = await _card(services, fake_bot)
    lines = text.splitlines()
    assert "⏳ Почта ждёт решения в чате бота шлюза: на шлюзе свой ящик" in lines, text
    assert "⏳ Ключевая фраза бэкапов ждёт решения в чате бота шлюза: на шлюзе своя" in lines, text
    assert "расходится с выданной" not in text, "ожидание решения выдано за доставку"


@pytest.mark.parametrize("declined, line", [
    (("mail",), "почта на шлюзе своя — так решено в чате бота шлюза"),
    (("backup",), "ключевая фраза бэкапов на шлюзе своя — так решено в чате бота шлюза"),
    (("mail", "backup"), "почта и ключевая фраза бэкапов на шлюзе свои — так решено в чате бота шлюза"),
])
async def test_details_name_the_applied_generation_and_what_was_kept(services, cfg1, fake_bot, declined, line):
    """«подробнее» (К13): номер применённого документа и дата; что человек
    оставил своим — без вопроса и без «расходится»."""
    doc = _doc(services)
    _cfg_snap(services, **{s: "ab" * 8 for s in declined})
    _cfg_ack(services, applied=["server", "env"], declined={s: doc["prints"][s] for s in declined})
    text, _ = await _card(services, fake_bot)
    details = text.split("<blockquote expandable>", 1)[1]
    assert f"конфигурация №{doc['gen']} применена " in details, details
    assert line in details, details
    assert "расходится" not in text, "оставленное своим выдано за расхождение"


async def test_no_applied_line_before_an_answer_to_the_current_document(services, cfg1, fake_bot):
    """Ответа на текущий документ нет (или он об отказе) — «применена» не пишем."""
    _cfg_snap(services)
    text, _ = await _card(services, fake_bot)
    assert "применена" not in text, text
    _cfg_ack(services, failed={"env": "exit 1"})
    text, _ = await _card(services, fake_bot)
    assert "применена" not in text, text


async def test_a_link_conf_the_server_could_not_build_is_said(services, cfg1, fake_bot, monkeypatch):
    monkeypatch.setattr(services, "gwcfg_link_error",
                        lambda slot_id: "конфиг линка: AllowedIPs: 172.16.0.0/12 шире /16")
    _cfg_snap(services)
    text, _ = await _card(services, fake_bot)
    assert "⚠️ Конфиг линка для шлюза не собран: конфиг линка: AllowedIPs: 172.16.0.0/12 шире /16" in text, text


async def test_cfg1_hints_point_to_restore_not_to_reissue(services, cfg1, fake_bot):
    """Не применившаяся связь подсетей лечится повтором доставки и «🚑
    Восстановить» в чате бота шлюза, а не новым файлом; строк об образце
    обвязки в карточке больше нет (поле снимка снято)."""
    _cfg_snap(services, snap={"plumbing_gen": "old", "peer_nets": {"ok": False, "missing": ["192.168.70.0/24"]}})
    text, _ = await _card(services, fake_bot)
    assert "Обвязка шлюза" not in text, text
    assert ("⚠️ Связь подсетей: на шлюзе нет <code>192.168.70.0/24</code> — сервер повторит доставку; "
            "не поможет — «🚑 Восстановить» в чате бота шлюза") in text, text
    assert "перевыпусти" not in text, text


async def test_an_agent_32_card_asks_to_update_and_has_no_file_button(services, slot, fake_bot):
    """Агент ниже 3.3.0 вернулся после молчания (К7): документа он не
    принимает, файла перевыпуска больше нет — только «обнови агента шлюза»."""
    _snap(services, agent_version="3.2.1", caps=[])
    text, labels = await _card(services, fake_bot)
    assert "⚠️ Агент v3.2.1 устарел: настройки по каналу конфигурации не принимает — обнови агента шлюза" in text, text
    assert "📤 Конфигурация" not in labels, labels


async def test_without_a_snapshot_there_is_no_agent_32_line(services, slot, fake_bot):
    """Снимка ещё нет — о версии агента судить не по чему, строки К7 нет."""
    text, _ = await _card(services, fake_bot)
    assert "устарел" not in text, text


@pytest.mark.parametrize("online", [True, False])
async def test_the_lan_mode_toast_for_cfg1_says_it_applies_by_itself(services, cfg1, fake_bot, online):
    """VPN-транзит у агента с документом доезжает каналом — и не на связи
    тоже, как только шлюз вернётся: всплывашка не зовёт перевыпускать."""
    _cfg_snap(services, online=online)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1, val="0"), services)
    assert services.db.gateway(1).lan_mode == 0
    assert ("VPN-транзит выключен: применится автоматически по каналу конфигурации", True) in cb.answers, cb.answers


@pytest.mark.parametrize("caps2", [[], CAPS])
@pytest.mark.parametrize("on", ["1", "0"])
async def test_transit_never_asks_for_a_reissue(services, cfg1, fake_bot, monkeypatch, caps2, on):
    """Подсети соседей меняются у этого слота и у других слотов с VPN-транзитом
    — их везёт документ: ни диалог, ни всплывашка не зовут перевыпускать,
    какой бы ни был агент соседа."""
    _peer_store(monkeypatch)
    admin = services.db.get_client_by_tg(ADMIN)
    pi2 = services.add_device(admin.id, "Pi2")
    services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    services.db.gateway_update(2, lan_subnets=["192.168.70.0/24"], lan_mode=1)
    services.gwlink_session_opened(2, "3.3.0" if caps2 else "3.2.1", 2, caps2)
    services.gateway_set_lan_mode(1, on == "0")
    _cfg_snap(services)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_ask(cb, GwSlotCB(action="lan_ask", slot=1), services)
    assert "перевыпуск" not in _screen(nav)[0]
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1, val=on), services)
    word = "включён" if on == "1" else "выключен"
    assert cb.answers == [(f"VPN-транзит {word}: применится автоматически по каналу конфигурации", True)], \
        cb.answers


async def test_the_card_names_linked_neighbours_as_links(services, cfg1, fake_bot, monkeypatch):
    """Карточка говорит, с кем связана сеть, — именами-ссылками на карточки
    соседей; без второго слота с VPN-транзитом строки нет."""
    _peer_store(monkeypatch)
    text, _ = await _card(services, fake_bot)
    assert "🔗 Связь подсетей" not in text, text
    admin = services.db.get_client_by_tg(ADMIN)
    pi2 = services.add_device(admin.id, "Pi2")
    services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    services.db.gateway_update(2, lan_subnets=["192.168.70.0/24"], lan_mode=1)
    text, _ = await _card(services, fake_bot)
    # без username бота имя — текстом; ссылкой (gw-<слот>) — в снимке экранов
    assert "🔗 Связь подсетей: с Pi2\n" in text, text
