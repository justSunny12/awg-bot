"""Сервисы соседних сетей на экранах (концепт «сервисы соседних сетей» §6, §7):
строка в карточке слота основного бота — только числа и состояние на шлюзе;
предложение в диалоге доступа между подсетями; у агента — строки панели и
абзац экрана «Локальная сеть без VPN». Новых кнопок нет.

Цена ошибки: имя с малины на экране ВПС — чужой текст в разметке сервера;
неэкранированная ошибка шлюза ломает всю карточку (Telegram отвергает
сообщение с битой разметкой); «опубликованы», когда шлюз отверг записи, —
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


async def test_the_slot_card_counts_services_and_follows_their_fate(services, peers, fake_bot):
    """Карточка получателя: сколько SMB у него и сколько пришло от соседей, и
    что с ними — уходят, опубликованы, не приняты. Имена на экран ВПС не идут."""
    _, labels_before = await _card(services, fake_bot, 2)
    _publish(services)
    text, labels = await _card(services, fake_bot, 2)
    assert _svc_line(text) == "🗂 Сервисы SMB: в этой подсети — 0, из подсетей других шлюзов — 1, уходят на шлюз", text
    assert "NASPi5" not in text and "naspi5" not in text, "имя с малины на экране ВПС"
    assert labels == labels_before, "строка сервисов добавила или убрала кнопки"
    services.gwlink_peer_services_ack_in(2, {"ok": True, "hash": H_NAS, "n": 1})
    text, _ = await _card(services, fake_bot, 2)
    assert _svc_line(text).endswith("из подсетей других шлюзов — 1, опубликованы на шлюзе"), _svc_line(text)
    services.gwlink_peer_services_ack_in(2, {"ok": False, "hash": H_NAS, "error": "<b>dnsmasq</b> & rc=1"})
    text, _ = await _card(services, fake_bot, 2)
    assert _svc_line(text).endswith("⚠️ шлюз не принял: &lt;b&gt;dnsmasq&lt;/b&gt; &amp; rc=1"), (
        f"ошибка шлюза не экранирована: {_svc_line(text)}")
    text1, _ = await _card(services, fake_bot, 1)
    assert _svc_line(text1) == "🗂 Сервисы SMB: в этой подсети — 1, из подсетей других шлюзов — нет", text1


@pytest.mark.parametrize("peers_applied,version,tail", [
    ("", "3.1.0", "ждут перевыпуска конфигурации шлюза"),
    ("192.168.1.0/24", "3.0.2", "агент шлюза их не понимает — обнови его"),
])
async def test_the_slot_card_says_why_services_are_not_there_yet(services, peers, fake_bot,
                                                                 peers_applied, version, tail):
    """Подсети соседей на шлюзе ещё не применены — «ждут перевыпуска»; агент
    старый — «обнови его». Не «уходят», которые не дойдут никогда."""
    _publish(services, peers=peers_applied, version=version)
    text, _ = await _card(services, fake_bot, 2)
    line = _svc_line(text)
    assert line == f"🗂 Сервисы SMB: в этой подсети — 0, из подсетей других шлюзов — 1, {tail}", (
        f"у соседа сервер есть, а карточка не говорит, почему его нет на шлюзе: {line}")


async def test_no_services_anywhere_is_one_short_line(services, peers, fake_bot):
    text, _ = await _card(services, fake_bot, 2)
    assert _svc_line(text) == "🗂 Сервисы SMB в подсетях шлюзов не найдены", text


async def test_without_peer_access_the_card_has_no_services_line(services, slots, fake_bot, monkeypatch):
    """Функция живёт там, где доступ между подсетями: выключен — строки нет."""
    _peer_conf(monkeypatch)
    _two_lan_slots(services, slots)
    _publish(services)
    text, _ = await _card(services, fake_bot, 2)
    assert _svc_line(text) is None, text


async def test_the_peer_access_dialog_mentions_finder_only_when_turning_on(services, peers, fake_bot):
    cb, nav = _acb(fake_bot)
    peers["app.routing.peer_nets.enabled"] = False
    await sh.gw_slot_peer_ask(cb, services)
    text = _screen(nav)[0]
    sentence = "SMB-серверы каждой подсети станут видны в Finder на Mac в подсетях других шлюзов: «Сеть» → awg.internal."
    assert sentence in text and text.index(sentence) < text.index("После включения перевыпусти"), text
    peers["app.routing.peer_nets.enabled"] = True
    await sh.gw_slot_peer_ask(cb, services)
    assert "awg.internal" not in _screen(nav)[0], "при выключении про Finder говорить нечего"


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
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_panel(cb, svc, FakeState())
    panel, panel_kb = msg.sent[-1][1], msg.sent[-1][2]
    await gh.gw_lan(cb, svc, FakeState())
    lan, lan_kb = msg.sent[-1][1], msg.sent[-1][2]
    labels = lambda m: [b.text for row in m.inline_keyboard for b in row]   # noqa: E731
    return panel, labels(panel_kb), lan, labels(lan_kb)


async def test_agent_panel_and_lan_screen_name_neighbour_servers(gw_svc, fake_bot, monkeypatch):
    """Имена — у агента, в его чате: до трёх, дальше «и ещё N»; имя с малины
    соседа экранировано. Абзац экрана учит, как дойти по имени."""
    _peer(gw_svc, ["naspi5", "backup", "Time Machine", "<b>x</b>", "media"])
    panel, panel_labels, lan, lan_labels = await _agent_screens(gw_svc, fake_bot, monkeypatch)
    assert "сервисы соседей: 5 SMB — naspi5, backup, time machine и ещё 2" in panel, panel
    assert "свои сервисы для соседей: нет" in panel
    assert "Сервисы соседей: 5 SMB — naspi5, backup, time machine и ещё 2. На Mac они видны в Finder" in lan
    assert "<code>smb://naspi5.awg.internal</code>" in lan
    _peer(gw_svc, ["<b>x</b>"])
    panel, *_ = await _agent_screens(gw_svc, fake_bot, monkeypatch)
    assert "&lt;b&gt;x&lt;/b&gt;" in panel and "<b>x</b>" not in panel, "имя с чужой малины не экранировано"
    assert lan_labels == ["📋 Свои списки", "❓ Настройка роутера", "⬅️ В меню"], lan_labels
    assert "🏠 Локальная сеть без VPN" in panel_labels


async def test_agent_panel_before_anything_arrived_and_without_avahi(gw_svc, fake_bot, monkeypatch):
    panel, *_ = await _agent_screens(gw_svc, fake_bot, monkeypatch)
    assert "сервисы соседей: пока не пришли" in panel, panel
    gw_svc.db.set_state("gw_peer_svc", '{"hash": "", "items": []}')
    monkeypatch.setattr(gwguard, "avahi_active", lambda: False)
    panel, *_ = await _agent_screens(gw_svc, fake_bot, monkeypatch)
    assert "сервисы соседей: нет" in panel
    assert "свои сервисы для соседей: avahi-daemon не запущен" in panel


async def test_agent_screens_are_silent_where_the_function_does_not_work(gw_svc, fake_bot, monkeypatch):
    """Соседей в юните нет — ни строк панели, ни абзаца: функции нет."""
    gw_svc.env["PEER_HOME_NETS"] = ""
    _peer(gw_svc, ["naspi5"])
    panel, _, lan, _ = await _agent_screens(gw_svc, fake_bot, monkeypatch)
    assert "сервисы соседей" not in panel.lower() and "awg.internal" not in lan, (panel, lan)
