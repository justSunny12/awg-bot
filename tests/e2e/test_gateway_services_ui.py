"""Сервисы соседних сетей на экранах:
строка в карточке слота основного бота — «🗂 SMB: свои — N, извне — M»
(нулевая часть не выводится), а состояние на шлюзе — строкой сразу под ней;
строка про SMB в диалоге «↔️ Связь подсетей»; у агента
— одна строка SMB в панели и на экране «🔀 VPN-транзит». Новых кнопок нет.

Цена ошибки: имя с малины на экране ВПС — чужой текст в разметке сервера;
неэкранированная ошибка шлюза ломает всю карточку (Telegram отвергает
сообщение с битой разметкой); «доступны», когда шлюз отверг записи, —
человек ищет серверы в Finder, которых там нет.
"""
from __future__ import annotations

import pytest

import awgbot.core.config as cfg
from awgbot.bot.callbacks import GwSlotCB
from awgbot.bot.handlers import gateway as gh
from awgbot.bot.handlers import settings as sh
from awgbot.domain import gwservices
from awgbot.domain.gateway import GatewayServices, GwStatus
from awgbot.infra import gwguard
from awgbot.infra.db import Database
from tests.conftest import FakeCallback, FakeMessage, FakeState
from tests.e2e import test_gateway_slots_ui as _slots_ui
from tests.e2e.test_gateway_router_peers_ui import _two_lan_slots
from tests.e2e.test_gateway_slots_ui import _acb, _peer_conf, _screen

pytestmark = pytest.mark.e2e
slots = _slots_ui.slots
NAS = {"t": "_smb._tcp", "n": "NASPi5", "h": "naspi5", "p": 445, "a": "192.168.1.10"}
H_NAS = gwservices.feed_hash([NAS])


def _snap(peers: str, version: str = "3.1.0") -> dict:
    return {"bundle": {"lan_mode": "1", "home_subnets": "192.168.68.0/24", "resolver": "10.9.1.1",
                       "peer_home_nets": peers, "admin_ips": "10.8.1.2"},
            "agent_version": version, "link_contract": "1", "rev": 1}


@pytest.fixture()
def peers(services, slots, monkeypatch):
    """Два слота с доступом между подсетями: 1 — 192.168.1.0/24, 2 — 192.168.68.0/24."""
    store = _peer_conf(monkeypatch)
    _two_lan_slots(services, slots)
    store["app.routing.peer_nets.enabled"] = True
    return store


def _svc_line(text: str) -> str | None:
    return next((ln for ln in text.splitlines() if ln.startswith("🗂")), None)


async def _card(services, fake_bot, slot: int):
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=slot), services, FakeState())
    return _screen(nav)


def _publish(services, peers: str = "192.168.1.0/24", version: str = "3.1.0") -> None:
    """Слот 1 назвал сервер, был на связи; слот 2 — снимок с тем, что у него стоит."""
    services.gwlink_services_in(1, [NAS])
    services.gwlink_session_closed(1)
    services.gwlink_snapshot_in(2, _snap(peers, version), 1, True)


HEAD = "🗂 SMB: извне — 1"


def _svc_note(text: str) -> str | None:
    """Строка о судьбе записей на шлюзе: сразу под строкой 🗂, без пустой
    (вычитка 3.1.0), с ⏳ или ⚠️ в начале; дальше в карточке другие блоки —
    они не в счёт."""
    lines = text.splitlines()
    i = next((k for k, ln in enumerate(lines) if ln.startswith("🗂")), None)
    if i is None or i + 1 >= len(lines):
        return None
    return lines[i + 1] if lines[i + 1].startswith(("⏳", "⚠️")) else None


async def test_the_slot_card_counts_services_and_follows_their_fate(services, peers, fake_bot):
    """Карточка получателя: сколько SMB у него и сколько пришло от соседей, и
    что с ними — отправлены, доступны, не приняты. Имена на экран ВПС не идут."""
    _, labels_before = await _card(services, fake_bot, 2)
    _publish(services)
    text, labels = await _card(services, fake_bot, 2)
    assert _svc_line(text) == HEAD, text
    assert _svc_note(text) == "⏳ Синхронизация с другими шлюзами…", (
        f"записи ушли на шлюз, а карточка молчит, где они: {text}")
    assert "NASPi5" not in text and "naspi5" not in text, "имя с малины на экране ВПС"
    assert labels == labels_before, "строка сервисов добавила или убрала кнопки"
    services.gwlink_peer_services_ack_in(2, {"ok": True, "hash": H_NAS, "n": 1})
    text, _ = await _card(services, fake_bot, 2)
    assert _svc_line(text) == HEAD + " 🟢 доступны", _svc_line(text)
    assert "⏳ Синхронизация с другими шлюзами" not in text and "⚠️ Шлюз" not in text, (
        f"записи на шлюзе, а карточка всё ещё пишет про их путь: {text}")
    services.gwlink_peer_services_ack_in(2, {"ok": False, "hash": H_NAS, "error": "<b>dnsmasq</b> & rc=1"})
    text, _ = await _card(services, fake_bot, 2)
    assert _svc_line(text) == HEAD, _svc_line(text)
    assert _svc_note(text) == "⚠️ Шлюз Pi2 не смог принять записи: &lt;b&gt;dnsmasq&lt;/b&gt; &amp; rc=1", (
        f"ошибка шлюза не экранирована или потерялась: {_svc_note(text)}")
    text1, _ = await _card(services, fake_bot, 1)
    assert _svc_line(text1) == "🗂 SMB: свои — 1", "нулевая часть «извне» не выводится"
    assert _svc_note(text1) is None, f"от соседей ничего — сообщать о судьбе нечего: {text1}"


async def test_a_refusal_without_details_has_no_dangling_colon(services, peers, fake_bot):
    """Шлюз отказал, не объяснив, — «не смог принять записи» без двоеточия в
    конце: висящее «: » читалось бы как обрезанное сообщение."""
    _publish(services)
    services.gwlink_peer_services_ack_in(2, {"ok": False, "hash": H_NAS, "error": ""})
    text, _ = await _card(services, fake_bot, 2)
    assert _svc_note(text) == "⚠️ Шлюз Pi2 не смог принять записи", text


@pytest.mark.parametrize("peers_applied,version,note", [
    ("", "3.1.0", "⚠️ Необходим перевыпуск конфигурации шлюза Pi2"),
    ("192.168.1.0/24", "3.0.2", "⚠️ Необходимо обновить шлюз Pi2"),
])
async def test_the_slot_card_says_why_services_are_not_there_yet(services, peers, fake_bot,
                                                                 peers_applied, version, note):
    """Подсети соседей на шлюзе ещё не применены — «необходим перевыпуск»;
    агент старый — «необходимо обновить». Не «отправлены», которые не дойдут
    никогда. Бот шлюза неизвестен — ссылки в скобках нет."""
    _publish(services, peers=peers_applied, version=version)
    text, _ = await _card(services, fake_bot, 2)
    assert _svc_line(text) == HEAD, text
    assert _svc_note(text) == note, (
        f"у соседа сервер есть, а карточка не говорит, почему его нет на шлюзе: {text}")


async def test_the_update_advice_links_the_gateway_bot_when_it_is_known(services, peers, fake_bot):
    """Обновлять агент — в чате его бота: известен бот — ссылка на него прямо
    в совете, имя профиля экранировано (оно из Telegram, не наше)."""
    _publish(services, version="3.0.2")
    services.set_gw_bot_identity(2, "pi2_gw_bot", "Шлюз <2> & co")
    text, _ = await _card(services, fake_bot, 2)
    assert _svc_note(text) == ('⚠️ Необходимо обновить шлюз Pi2 (бот: '
                               '<a href="https://t.me/pi2_gw_bot">Шлюз &lt;2&gt; &amp; co</a>)'), text


async def test_the_gateway_name_in_the_note_is_escaped_once(services, peers, fake_bot, slots):
    """Имя устройства шлюза — свободный текст админа. В заголовке карточки оно
    экранировано; в строке о судьбе записей должно быть тем же, а не
    «&amp;amp;» — иначе человек видит в чате мусор вместо имени."""
    _, _, pi2 = slots
    services.rename_device(pi2.id, "Pi & <2>")
    _publish(services)
    # «в пути» имени больше не называет — имя остаётся в отказе шлюза
    services.gwlink_peer_services_ack_in(2, {"ok": False, "hash": H_NAS, "error": ""})
    text, _ = await _card(services, fake_bot, 2)
    assert text.splitlines()[0].startswith("🛰 <b>Pi &amp; &lt;2&gt;</b> — "), text.splitlines()[0]
    assert _svc_note(text) == "⚠️ Шлюз Pi &amp; &lt;2&gt; не смог принять записи", (
        f"имя шлюза экранировано дважды: {_svc_note(text)}")


async def test_a_breakage_on_the_gateway_is_not_called_a_refusal(services, peers, fake_bot):
    """Файл записей не записался на малине (диск полон) — это поломка на
    шлюзе, а не отказ принять записи: карточка пишет «Шлюз «X»: ошибка записи
    файла: …», без «не смог принять», иначе человек ищет, что не так с
    записями, а чинить нужно диск. Настоящий отказ (dnsmasq) — прежней фразой."""
    _publish(services)
    services.gwlink_peer_services_ack_in(2, {"ok": False, "hash": H_NAS, "error":
                                             "ошибка записи файла: нет места на диске"})
    text, _ = await _card(services, fake_bot, 2)
    assert _svc_line(text) == HEAD, _svc_line(text)
    assert _svc_note(text) == "⚠️ Шлюз Pi2: ошибка записи файла: нет места на диске", _svc_note(text)
    assert "не смог принять" not in text, f"поломка на шлюзе названа отказом: {text}"
    services.gwlink_peer_services_ack_in(2, {"ok": False, "hash": H_NAS, "error": "не пройдена проверка строк"})
    text, _ = await _card(services, fake_bot, 2)
    assert _svc_note(text) == "⚠️ Шлюз Pi2 не смог принять записи: не пройдена проверка строк", (
        f"отказ проверки строк потерял «не смог принять записи»: {_svc_note(text)}")


async def test_a_breakage_text_from_the_gateway_is_escaped(services, peers, fake_bot, slots):
    """Текст поломки пришёл с малины — чужая строка в разметке ВПС: и она, и
    имя шлюза экранированы ровно один раз, иначе Telegram отвергнет карточку."""
    _, _, pi2 = slots
    services.rename_device(pi2.id, "Pi & <2>")
    _publish(services)
    services.gwlink_peer_services_ack_in(2, {"ok": False, "hash": H_NAS, "error":
                                             "ошибка записи файла: <b>&</b>"})
    text, _ = await _card(services, fake_bot, 2)
    assert _svc_note(text) == "⚠️ Шлюз Pi &amp; &lt;2&gt;: ошибка записи файла: &lt;b&gt;&amp;&lt;/b&gt;", (
        _svc_note(text))


@pytest.mark.parametrize("error,note", [
    ("ошибка записи файла: нет места на диске", "⚠️ Шлюз: ошибка записи файла: нет места на диске"),
    ("dnsmasq: bad option", "⚠️ Шлюз не смог принять записи: dnsmasq: bad option"),
    # «ошибка записи файла» не в начале — это чужой текст, а не поломка агента
    ("rc=1: ошибка записи файла", "⚠️ Шлюз не смог принять записи: rc=1: ошибка записи файла"),
])
def test_the_breakage_branch_without_a_name_reads_whole(error, note):
    """Без имени шлюза фраза поломки целая — «Шлюз: …», без двойного пробела;
    ветку выбирает только начало текста ошибки."""
    from awgbot.bot.texts.routing import services_line
    assert services_line({"own": 0, "peer": 1, "state": "failed", "error": error}) == HEAD + "\n" + note


@pytest.mark.parametrize("state,note", [
    ("reissue", "⚠️ Необходим перевыпуск конфигурации шлюза"),
    ("old_agent", "⚠️ Необходимо обновить шлюз"),
    ("failed", "⚠️ Шлюз не смог принять записи"),
    ("pending", "⏳ Синхронизация с другими шлюзами…"),
])
def test_without_a_name_the_note_does_not_repeat_the_word_gateway(state, note):
    """Имени нет — фраза целая, без «шлюза шлюза» и двойного пробела."""
    from awgbot.bot.texts.routing import services_line
    assert services_line({"own": 0, "peer": 1, "state": state}) == HEAD + "\n" + note


async def test_no_services_anywhere_is_one_short_line(services, peers, fake_bot):
    text, _ = await _card(services, fake_bot, 2)
    assert _svc_line(text) == "🗂 SMB: не найдены", text
    assert _svc_note(text) is None, f"записей нет — сообщать о судьбе нечего: {text}"


async def test_without_peer_access_the_card_has_no_services_line(services, slots, fake_bot, monkeypatch):
    """Функция живёт там, где связь подсетей: выключена — строки нет."""
    _peer_conf(monkeypatch)
    _two_lan_slots(services, slots)
    _publish(services)
    text, _ = await _card(services, fake_bot, 2)
    assert _svc_line(text) is None, text


@pytest.mark.parametrize("own, peer, line", [
    (1, 2, "🗂 SMB: свои — 1, извне — 2"), (1, 0, "🗂 SMB: свои — 1"), (0, 2, "🗂 SMB: извне — 2"),
    (0, 0, "🗂 SMB: не найдены"),
])
def test_smb_line_does_not_print_zeros(own, peer, line):
    """Нулевая часть не выводится, обе нулевые — «не найдены»: «свои — 0»
    читается как поломка там, где серверов просто нет."""
    from awgbot.bot.texts.routing import services_line
    assert services_line({"own": own, "peer": peer, "state": "applied"}) == (
        line + (" 🟢 доступны" if peer else ""))


async def test_the_peer_access_dialog_mentions_finder_only_when_turning_on(services, peers, fake_bot):
    cb, nav = _acb(fake_bot)
    peers["app.routing.peer_nets.enabled"] = False
    await sh.gw_slot_peer_ask(cb, services)
    text = _screen(nav)[0]
    smb = ("SMB-серверы подсетей: на Windows — <code>\\\\имя.awg.internal</code>, на macOS — в Finder: "
           "«Сеть» → awg.internal")
    assert smb in text.split("\n"), text
    assert text.index(smb) < text.index("После включения перевыпусти"), "про SMB — до совета о перевыпуске"
    assert "подсети шлюзов живут в конфиге линка, а его везёт только файл" in text, text
    assert "avahi-daemon" in text.split("\n")[-1], "условие avahi — под «подробнее»"
    peers["app.routing.peer_nets.enabled"] = True
    await sh.gw_slot_peer_ask(cb, services)
    off = _screen(nav)[0]
    assert "awg.internal" not in off, "при выключении про Finder говорить нечего"
    assert "Подсети шлюзов перестанут видеть друг друга сразу" in off, off


# ── агент ────────────────────────────────────────────────────────────────────

@pytest.fixture()
def gw_svc(tmp_path, monkeypatch):
    d = Database(tmp_path / "gw.db"); d.init_schema()
    svc = GatewayServices(d)
    env = {"LAN_MODE": "1", "HOME_SUBNETS": "192.168.68.0/24", "PEER_HOME_NETS": "192.168.1.0/24",
           "LINK_CHANNEL": "1"}
    monkeypatch.setattr(gwguard, "lan_mode", lambda: True)
    monkeypatch.setattr(gwguard, "unit_env", lambda k: env.get(k, ""))
    monkeypatch.setattr(gwguard, "avahi_active", lambda: True)
    monkeypatch.setattr(gwguard, "dns_local", lambda name, qtype="PTR": ["x"])
    svc.env = env
    yield svc
    d.close()


def _lan_status(svc_info: dict) -> GwStatus:
    return GwStatus(link_up=True, handshake_age=5.0,
                    lan={"iface": "end0", "addr": "192.168.68.222", "resolver": "10.9.1.1", "domains": 3,
                         "nets": 4, "updated_at": "", "own_vpn": 1, "own_ru": 0, "lan_pkts": 9,
                         "svc": svc_info})


def _peer(svc, names):
    import json
    items = [{"t": "_smb._tcp", "n": n, "h": n.lower().replace(" ", "-").replace("<", "").replace(">", ""),
              "p": 445, "a": f"192.168.1.{i + 1}"} for i, n in enumerate(names)]
    svc.db.set_state("gw_peer_svc", json.dumps({"hash": "ab" * 32, "items": items}))


async def _agent_screens(svc, fake_bot, monkeypatch):
    import shutil
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: "/usr/bin/avahi-browse" if n == "avahi-browse" else real(n, *a, **k))
    info, _ = svc.services_status()
    monkeypatch.setattr(svc, "cached_status", lambda max_age: _lan_status(info))
    # свои списки здесь не предмет: один домен, синхронизация не действует
    monkeypatch.setattr(svc, "lan_own_lists", lambda: [("vpn", "a.com")])
    monkeypatch.setattr(svc, "own_status", lambda: ({"active": False, "state": "off"}, []))
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_panel(cb, svc, FakeState())
    panel, panel_kb = msg.sent[-1][1], msg.sent[-1][2]
    await gh.gw_transit(cb, svc, FakeState())
    lan, lan_kb = msg.sent[-1][1], msg.sent[-1][2]
    labels = lambda m: [b.text for row in m.inline_keyboard for b in row]   # noqa: E731
    return panel, labels(panel_kb), lan, labels(lan_kb)


def _lines(text: str) -> list[str]:
    return text.splitlines()


async def test_agent_panel_and_lan_screen_count_smb_in_one_line(gw_svc, fake_bot, monkeypatch):
    """Панель и экран «🔀 VPN-транзит» — одной строкой «🗂 SMB» с числами:
    своих не нашлось — нулевая часть не выводится («свои — 0» читается как
    поломка); имён и avahi здесь нет (они — в здоровье): имя с чужой малины
    не попадает в разметку вовсе."""
    _peer(gw_svc, ["naspi5", "backup", "Time Machine", "<b>x</b>", "media"])
    panel, panel_labels, lan, lan_labels = await _agent_screens(gw_svc, fake_bot, monkeypatch)
    line = "🗂 SMB: извне — 5"
    # на панели — сразу под строкой списков, на экране — под своими списками
    p = _lines(panel)
    assert p[p.index(line) - 1].startswith("📋 Списки: "), panel
    lan_lines = _lines(lan)
    assert lan_lines[lan_lines.index(line) - 1].startswith("Свои списки: "), lan
    for t in (panel, lan):
        assert "naspi5" not in t and "&lt;b&gt;" not in t and "<b>x</b>" not in t, f"имена соседей на экране: {t}"
        assert "avahi" not in t, f"про avahi — только в здоровье: {t}"
    assert "Finder" not in lan, "абзаца про Finder на экране нет"
    assert lan_labels == ["➕ В туннель", "➕ Напрямую", "➖ 🌍 a.com", "❓ Роутер", "⬅️ В меню"], lan_labels
    assert "🔀 VPN-транзит" in panel_labels


async def test_the_lan_screen_groups_address_traffic_lists_and_smb(gw_svc, fake_bot, monkeypatch):
    """Экран «🔀 VPN-транзит»: состояние, интерфейс с адресом и трафиком одной
    строкой, DNS, списки со временем обновления, свои списки, SMB — подряд,
    без пустых строк; объяснение — под «подробнее» последним."""
    _peer(gw_svc, ["naspi5"])
    _, _, lan, _ = await _agent_screens(gw_svc, fake_bot, monkeypatch)
    head = ("🔀 <b>VPN-транзит</b> 🟢 работает\n"
            "<code>end0</code> · <code>192.168.68.222</code> · 9 пакетов с роутера\n"
            "DNS — <code>10.9.1.1</code> через <code>аплинк</code>\n"
            "📋 Списки: 3 домена, 4 подсети (ещё не обновлялись)\n"
            "Свои списки: 1 в туннель\n"
            "🗂 SMB: извне — 1\n")
    assert lan.startswith(head), lan
    assert lan.endswith("</blockquote>") and lan.count("<blockquote expandable>") == 1, lan
    about = lan.split("<blockquote expandable>", 1)[1]
    assert "свои списки синхронизируются между шлюзами" in about and \
        "правила «напрямую» приоритетнее правил «в туннель»" in about, about
    assert "Личные" not in lan, "«Личные» → «Свои» во всех строках"


async def test_agent_panel_before_anything_arrived_and_after_an_empty_feed(gw_svc, fake_bot, monkeypatch):
    """Записей от других шлюзов нет — строка целиком про сервисы: сервер ещё
    ничего не присылал — «обновляю…», прислал пустое — «не найдены»; почему
    своих не видно (нет avahi) — пишет здоровье, не панель."""
    panel, *_ = await _agent_screens(gw_svc, fake_bot, monkeypatch)
    assert "🗂 SMB: обновляю…" in _lines(panel), panel
    gw_svc.db.set_state("gw_peer_svc", '{"hash": "", "items": []}')
    monkeypatch.setattr(gwguard, "avahi_active", lambda: False)
    panel, *_ = await _agent_screens(gw_svc, fake_bot, monkeypatch)
    assert "🗂 SMB: не найдены" in _lines(panel), panel
    assert "avahi" not in panel, panel


@pytest.mark.parametrize("svc, line", [
    ({"own": ["x"], "peer": ["a", "b"], "ever": True}, "🗂 SMB: свои — 1, извне — 2"),
    ({"own": ["x", "y"], "peer": [], "ever": True}, "🗂 SMB: свои — 2"),
    ({"own": [], "peer": ["a"], "ever": True}, "🗂 SMB: извне — 1"),
    ({"own": [], "peer": [], "ever": True}, "🗂 SMB: не найдены"),
    ({"own": [], "peer": [], "ever": False}, "🗂 SMB: обновляю…"),
    ({"avahi": True, "browse": False, "own": [], "peer": ["a"], "ever": True}, "🗂 SMB: извне — 1"),
], ids=["both", "own-only", "peer-only", "none", "not-yet", "no-browse"])
def test_smb_line_does_not_print_zeros_on_the_agent_either(svc, line):
    """Как в карточке слота: нулевая часть не выводится, обе нулевые — «не
    найдены», сервер ещё ничего не присылал — «обновляю…»; avahi-browse нет —
    своя подсеть не посчитана и в строку не идёт."""
    from awgbot.bot.texts.gateway import smb_line
    assert smb_line(svc) == line


async def test_agent_screens_are_silent_where_the_function_does_not_work(gw_svc, fake_bot, monkeypatch):
    """Соседей в юните нет — ни строк панели, ни экрана: функции нет."""
    gw_svc.env["PEER_HOME_NETS"] = ""
    _peer(gw_svc, ["naspi5"])
    panel, _, lan, _ = await _agent_screens(gw_svc, fake_bot, monkeypatch)
    assert "🗂" not in panel and "🗂" not in lan and "awg.internal" not in lan, (panel, lan)
