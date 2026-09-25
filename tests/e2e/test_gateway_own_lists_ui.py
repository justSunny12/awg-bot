"""Свои списки, общие для всех шлюзов, на экранах (концепт «синхронизация
своих списков» §6, §7, вычитка 3.1.0): у агента — состояние хвостом строки
панели и экрана локальной сети, абзац и строка состояния на экране
«📋 Свои списки», «на всех шлюзах» в подтверждении удаления, хвост итога
правки; у основного бота — строка числами в карточке слота и судьба канона
на шлюзе отдельной строкой. Новых кнопок нет.

Цена ошибки: «синхронизированы», когда правка лежит без канала, — человек
ждёт домен на соседнем шлюзе, а он не придёт; «уходит на другие шлюзы», когда
канала нет, — то же; «уберётся на всех» без синхронизации — лишний страх;
неэкранированная ошибка шлюза — Telegram отвергает всю карточку.
"""
from __future__ import annotations

import pytest

import awgbot.core.config as cfg
from awgbot.bot.callbacks import GwCB, GwSlotCB
from awgbot.bot.handlers import gateway as gh
from awgbot.bot.handlers import settings as sh
from awgbot.domain.gateway import GatewayServices, GwStatus
from awgbot.infra.db import Database
from awgbot.runtime import linkclient
from awgbot.util import gwlink
from tests.conftest import FakeCallback, FakeMessage, FakeState
from tests.e2e import test_gateway_slots_ui as _slots_ui
from tests.e2e.test_gateway_router_peers_ui import _two_lan_slots
from tests.e2e.test_gateway_slots_ui import _acb, _peer_conf, _screen
from tests.unit.test_gwlink_idle import PRIV, SN, _Wire
from tests.unit.test_gwownlists import _Host, _synced

pytestmark = pytest.mark.e2e
slots = _slots_ui.slots


# ── агент ────────────────────────────────────────────────────────────────────

@pytest.fixture()
def host(tmp_path, monkeypatch):
    return _Host(tmp_path, monkeypatch)


@pytest.fixture()
def gw(tmp_path, monkeypatch, host):
    monkeypatch.setattr(GatewayServices, "_own_run", "")
    monkeypatch.setattr(linkclient, "_client", None)
    d = Database(tmp_path / "gw.db"); d.init_schema()
    svc = GatewayServices(d)
    yield svc
    d.close()


def _lan(own: dict) -> GwStatus:
    return GwStatus(link_up=True, handshake_age=5.0,
                    lan={"iface": "end0", "addr": "192.168.68.222", "resolver": "10.9.1.1", "domains": 3,
                         "nets": 4, "updated_at": "", "own_vpn": 4, "own_ru": 1, "lan_pkts": 9, "own": own})


async def _panel_and_lan(svc, fake_bot, monkeypatch):
    monkeypatch.setattr(svc, "cached_status", lambda max_age: _lan(svc.own_status()[0]))
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_panel(cb, svc, FakeState())
    panel, pkb = msg.sent[-1][1], msg.sent[-1][2]
    await gh.gw_lan(cb, svc, FakeState())
    lan, lkb = msg.sent[-1][1], msg.sent[-1][2]
    labels = lambda m: [b.text for row in m.inline_keyboard for b in row]   # noqa: E731
    return panel, lan, (labels(pkb), labels(lkb))


def _own_line(text: str) -> str:
    return next(ln.strip() for ln in text.splitlines() if "Свои списки" in ln)


async def test_the_panel_line_says_the_lists_are_shared_and_how_they_stand(gw, host, fake_bot, monkeypatch):
    """Строка одна и та же с синхронизацией и без (пометку «для всех шлюзов»
    сняли при вычитке 3.1.0); хвост — ждут или не применились, и только когда
    синхронизация действует. Кнопок столько же, сколько без неё."""
    host.env["LINK_CHANNEL"] = "0"
    panel, lan, labels0 = await _panel_and_lan(gw, fake_bot, monkeypatch)
    assert _own_line(panel) == _own_line(lan) == "Свои списки: 4 в туннель, 1 напрямую", panel
    host.env["LINK_CHANNEL"] = "1"
    _synced(gw, host, {"a.com": "vpn"})
    panel, lan, labels = await _panel_and_lan(gw, fake_bot, monkeypatch)
    assert _own_line(panel) == _own_line(lan) == "Свои списки: 4 в туннель, 1 напрямую", panel
    assert labels == labels0, "синхронизация добавила или убрала кнопки"
    host.write(vpn=["a.com", "b.com"])
    gw.own_reconcile()
    panel, lan, _ = await _panel_and_lan(gw, fake_bot, monkeypatch)
    assert _own_line(panel) == "Свои списки: 4 в туннель, 1 напрямую · ⏳ ждут синхронизации", panel
    assert _own_line(lan) == _own_line(panel)
    gw.db.set_state("gw_own_err", "dnsmasq отверг")
    panel, _, _ = await _panel_and_lan(gw, fake_bot, monkeypatch)
    assert _own_line(panel) == ("Свои списки: 4 в туннель, 1 напрямую · "
                                "⚠️ не применились (🌡 Монитор здоровья)"), panel


async def _own_screen(svc, fake_bot):
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_lan_list(cb, svc, FakeState())
    return msg.sent[-1][1], [b.text for row in msg.sent[-1][2].inline_keyboard for b in row]


SHARED = ("Списки общие для всех шлюзов: добавленное или убранное здесь уходит через сервер AWG на "
          "остальные шлюзы — сразу, если они на связи, иначе при подключении. Правки командой "
          "<code>awg-bot lan</code> на самом шлюзе уходят в течение нескольких минут.")
ONLY_HERE = ("Списки применятся только для этого шлюза: для синхронизации нужен канал до сервера "
             "AWG — перевыпусти конфигурацию шлюза с сервера AWG и примени её здесь")


async def test_the_own_lists_screen_explains_sharing_and_shows_the_state(gw, host, fake_bot):
    _synced(gw, host, {"a.com": "vpn", "b.ru": "ru"})
    text, labels = await _own_screen(gw, fake_bot)
    assert text.endswith("\n\n" + SHARED), f"абзаца про общие списки нет последним: {text}"
    assert labels == ["➕ В туннель", "➕ Напрямую", "➖ b.ru (🇷🇺)", "➖ a.com (📤)", "⬅️ Назад"], labels
    # правка ждёт, канала нет — строка состояния под абзацем
    host.write(vpn=["a.com", "c.com"], ru=["b.ru"])
    gw.own_reconcile()
    gw.channel.online = False
    text, labels2 = await _own_screen(gw, fake_bot)
    assert text.endswith(SHARED + "\n\n⏳ Ждут синхронизации: 1 правка — нет связи с сервером AWG"), text
    assert len(labels2) == len(labels) + 1, "строка состояния сама по себе кнопок не добавляет"
    gw.channel.online = True
    text, _ = await _own_screen(gw, fake_bot)
    assert text.endswith("⏳ Ждут синхронизации: 1 правка — сервер AWG ещё не ответил"), text
    gw.db.set_state("gw_own_err", "dnsmasq <b>отверг</b>")
    text, _ = await _own_screen(gw, fake_bot)
    assert text.endswith("⚠️ Не применились: dnsmasq &lt;b&gt;отверг&lt;/b&gt;"), text


async def test_an_empty_shared_list_says_it_will_appear_everywhere(gw, host, fake_bot):
    _synced(gw, host, {})
    text, labels = await _own_screen(gw, fake_bot)
    assert text == ("📋 <b>Свои списки</b>\n\nПока пусто: добавь домены кнопками «➕ В туннель» и «➕ Напрямую».\n"
                    "Списки общие для всех шлюзов — добавленное здесь появится и на остальных."), text
    assert labels == ["➕ В туннель", "➕ Напрямую", "⬅️ Назад"]


async def test_without_the_channel_the_screen_says_the_lists_are_local(gw, host, fake_bot):
    host.env["LINK_CHANNEL"] = ""
    host.write(vpn=["a.com"])
    text, _ = await _own_screen(gw, fake_bot)
    assert text.endswith("\n\n" + ONLY_HERE), text
    assert "общие для всех шлюзов" not in text
    host.env["LAN_MODE"] = "0"
    text, _ = await _own_screen(gw, fake_bot)
    assert ONLY_HERE not in text and "общие" not in text, "режим выключен — про канал говорить нечего"


@pytest.mark.parametrize("channel,tail", [("1", "\nДомен уберётся на всех шлюзах."), ("0", "")])
async def test_the_remove_dialog_warns_about_all_gateways_only_when_shared(gw, host, fake_bot, channel, tail):
    host.env["LINK_CHANNEL"] = channel
    host.write(ru=["shop.ru"])
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_lan_remove(cb, GwCB(action="lan_rm", val="0"), gw)
    text = msg.sent[-1][1]
    assert text == ("➖ <b>Убрать <code>shop.ru</code> из своих списков?</b>\n\n"
                    "Сейчас домен идёт напрямую; после удаления — как решат списки." + tail), text
    assert [b.text for row in msg.sent[-1][2].inline_keyboard for b in row] == ["⬅️ Отмена", "➖ Убрать"]


def _online(svc, monkeypatch, tmp_path) -> _Wire:
    """Канал открыт: клиент с подставным сокетом, байты которого читаем."""
    conf = tmp_path / "awglink.conf"
    conf.write_text(f"# awg-bot: контракт линка 1\n[Interface]\nPrivateKey = {PRIV}\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "GW_LINK_CONF", str(conf))
    c = linkclient.LinkClient(svc)
    c._writer = _Wire()
    c._sn = SN
    monkeypatch.setattr(linkclient, "_client", c)
    return c._writer


def _add_via_script(svc, host, monkeypatch, out_word: str = "добавлен"):
    def lan_domains(cmd, domains):
        cur = host.lists()
        for d in domains:
            if cmd == "del":
                cur.pop(d, None)
            else:
                cur[d] = "ru" if cmd == "ru" else "vpn"
        host.write([d for d, k in cur.items() if k == "vpn"], [d for d, k in cur.items() if k == "ru"])
        return True, "\n".join(f"{d}: {out_word}" for d in domains)
    monkeypatch.setattr(svc, "lan_domains", lambda cmd, domains: lan_domains(cmd, domains))


async def _type_domain(svc, fake_bot, kind: str, text: str) -> str:
    st = FakeState()
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_lan_ask(cb, GwCB(action=kind), svc, st)
    reply = FakeMessage(text=text, chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_lan_domain_received(reply, st, svc)
    return next(s[1] for s in reply.sent if s[0] == "answer" and s[1].startswith(("✅", "⚠️")))


async def test_a_button_edit_leaves_at_once_and_says_so(gw, host, fake_bot, monkeypatch, tmp_path):
    """Канал жив: правка кнопкой уходит серверу тем же нажатием (не тиком
    монитора), итог говорит, что она уходит на другие шлюзы."""
    _synced(gw, host, {"a.com": "vpn"})
    wire = _online(gw, monkeypatch, tmp_path)
    _add_via_script(gw, host, monkeypatch)
    result = await _type_domain(gw, fake_bot, "lan_add", "example.com")
    assert result == "✅ example.com: добавлен\nИзменения синхронизируются с другими шлюзами", result
    msgs = wire.messages(gwlink.channel_key(PRIV))
    assert [m["t"] for m in msgs] == ["own_ev"], f"правка не ушла серверу сразу: {msgs}"
    assert [e[1:3] for e in msgs[0]["ev"]] == [["example.com", "vpn"]]


async def test_without_a_channel_the_edit_waits_and_the_result_says_when_it_leaves(gw, host, fake_bot, monkeypatch):
    _synced(gw, host, {"a.com": "vpn"})
    _add_via_script(gw, host, monkeypatch)
    result = await _type_domain(gw, fake_bot, "lan_ru", "shop.ru")
    assert result == ("✅ shop.ru: добавлен\n"
                      "Изменения синхронизируются с другими шлюзами, когда появится связь с сервером AWG"), result
    assert gw.own_status()[0]["pending"] == 1, "правка без канала не легла в очередь"


@pytest.mark.parametrize("word,channel", [("уже в списке", "1"), ("добавлен", "0")])
async def test_no_tail_when_nothing_changed_or_nothing_is_shared(gw, host, fake_bot, monkeypatch, word, channel):
    # «уже в списке» — домен действительно уже есть: файлы после правки те же
    _synced(gw, host, {"a.com": "vpn", **({"example.com": "vpn"} if word == "уже в списке" else {})})
    host.env["LINK_CHANNEL"] = channel
    _add_via_script(gw, host, monkeypatch, out_word=word)
    result = await _type_domain(gw, fake_bot, "lan_add", "example.com")
    assert result == f"✅ example.com: {word}", result


async def test_already_listed_gives_no_tail_but_still_sends_earlier_unsent_edits(
        gw, host, fake_bot, monkeypatch, tmp_path):
    """Правка руками лежит неотправленной (канала не было), человек добавляет
    кнопкой домен, который уже в списке: хвоста «синхронизируются» нет — этой
    кнопкой ничего не поменялось, — но накопленное уходит серверу тем же
    нажатием, а не ждёт тика."""
    _synced(gw, host, {"a.com": "vpn"})
    host.write(vpn=["a.com", "b.com"])
    assert gw.own_reconcile() is True, "правка руками не нашлась"
    assert gw.own_unsent(), "правка без канала должна лежать неотправленной"
    wire = _online(gw, monkeypatch, tmp_path)
    _add_via_script(gw, host, monkeypatch, out_word="уже в списке")
    result = await _type_domain(gw, fake_bot, "lan_add", "b.com")
    assert result == "✅ b.com: уже в списке", f"хвост без новых правок: {result!r}"
    msgs = wire.messages(gwlink.channel_key(PRIV))
    assert [e[1:3] for m in msgs for e in m["ev"]] == [["b.com", "vpn"]], f"неотправленное не ушло: {msgs}"


async def test_removing_by_button_answers_with_the_sync_tail(gw, host, fake_bot, monkeypatch, tmp_path):
    _synced(gw, host, {"a.com": "vpn", "b.com": "vpn"})
    wire = _online(gw, monkeypatch, tmp_path)
    _add_via_script(gw, host, monkeypatch, out_word="убран")
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_lan_remove(cb, GwCB(action="lan_rm!", val="0"), gw)
    assert cb.answers[-1] == ("✅ a.com: убран\nИзменения синхронизируются с другими шлюзами", False), cb.answers
    msgs = wire.messages(gwlink.channel_key(PRIV))
    assert [e[1:3] for m in msgs for e in m["ev"]] == [["a.com", "del"]], msgs


# ── основной бот: карточка слота ─────────────────────────────────────────────

LAN_ON = "🏠 За шлюзом — без VPN: включено"


async def _card(services, fake_bot, slot: int):
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=slot), services, FakeState())
    return _screen(nav)


def _own_block(text: str) -> tuple[str | None, str | None]:
    """(строка 📋 сразу после «без VPN: включено», строка судьбы сразу под ней —
    без пустой, вычитка 3.1.0)."""
    lines = text.splitlines()
    if LAN_ON not in lines:
        return None, None
    i = lines.index(LAN_ON)
    head = lines[i + 1] if i + 1 < len(lines) and lines[i + 1].startswith("📋") else None
    note = (lines[i + 2] if head and i + 2 < len(lines)
            and lines[i + 2].startswith(("⏳", "⚠️")) else None)
    return head, note


@pytest.fixture()
def lan_slots(services, slots, monkeypatch):
    _peer_conf(monkeypatch)
    _two_lan_slots(services, slots)


async def test_the_slot_card_counts_the_lists_and_follows_the_canon(services, lan_slots, fake_bot):
    """Карточка: только числа канона (домены на экран ВПС не идут) и что с ним
    на шлюзе — уйдёт, отправлен, применён, отказ. Кнопок столько же."""
    head, note = _own_block((await _card(services, fake_bot, 2))[0])
    assert head == "📋 Свои списки: пусто", head
    assert note == "⏳ Синхронизируется со шлюзом «Pi2», когда он выйдет на связь", note
    services.gwlink_own_in(1, "rx", [[i + 1, f"d{i}.com", "vpn", False] for i in range(5)]
                           + [[6, "shop.ru", "ru", False]])
    services.gwlink_session_opened(2, "3.1.0", 2)
    services.gwlink_own_hello_in(2, True)
    text, labels = await _card(services, fake_bot, 2)
    head, note = _own_block(text)
    assert head == "📋 Свои списки: 5 в туннель, 1 напрямую", text
    assert note == "⏳ Синхронизация с другими шлюзами…", text
    assert "d0.com" not in text and "shop.ru" not in text, "домены на экране ВПС"
    digest = services.gwlink_own_for(services.db.gateway(2))[0]
    services.gwlink_own_ack_in(2, {"ok": True, "hash": digest, "n": 6})
    text, labels_applied = await _card(services, fake_bot, 2)
    assert _own_block(text) == ("📋 Свои списки: 5 в туннель, 1 напрямую", None), text
    assert labels_applied == labels, "строка своих списков добавила или убрала кнопки"
    services.gwlink_own_ack_in(2, {"ok": False, "hash": digest, "error": "<b>dnsmasq</b> & rc=1"})
    _, note = _own_block((await _card(services, fake_bot, 2))[0])
    assert note == "⚠️ Шлюз «Pi2» отказался принимать: &lt;b&gt;dnsmasq&lt;/b&gt; &amp; rc=1", note
    services.gwlink_own_ack_in(2, {"ok": False, "hash": digest, "error": ""})
    _, note = _own_block((await _card(services, fake_bot, 2))[0])
    assert note == "⚠️ Шлюз «Pi2» отказался принимать", "висящее двоеточие без ошибки"


async def test_an_agent_that_does_not_know_sync_is_told_to_update_with_a_bot_link(services, lan_slots, fake_bot):
    services.gwlink_session_opened(2, "3.0.2", 2)
    services.gwlink_own_hello_in(2, False)
    services.set_gw_bot_identity(2, "pi2_gw_bot", "Шлюз <2> & co")
    _, note = _own_block((await _card(services, fake_bot, 2))[0])
    assert note == ('⚠️ Для синхронизации необходимо обновить шлюз «Pi2» (бот: '
                    '<a href="https://t.me/pi2_gw_bot">Шлюз &lt;2&gt; &amp; co</a>)'), note


async def test_an_old_agent_is_recognised_by_its_snapshot_before_any_hello(services, lan_slots, fake_bot):
    """Сессии с новой версией ещё не было — судим по версии из снимка."""
    services.gwlink_snapshot_in(2, {"bundle": {"lan_mode": "1"}, "agent_version": "3.0.2",
                                    "link_contract": "1", "rev": 1}, 1, True)
    _, note = _own_block((await _card(services, fake_bot, 2))[0])
    assert note == "⚠️ Для синхронизации необходимо обновить шлюз «Pi2»", note


async def test_without_lan_mode_the_card_has_no_own_lists_line(services, lan_slots, fake_bot):
    services.db.gateway_update(2, lan_mode=0)
    text, _ = await _card(services, fake_bot, 2)
    assert "📋 Свои списки" not in text, text


def _gap_after_note(text: str) -> list[str]:
    """Строки между строкой судьбы своих списков и следующим непустым блоком."""
    lines = text.splitlines()
    i = lines.index(LAN_ON) + 2
    assert lines[i].startswith(("⏳", "⚠️")), text
    j = i + 1
    while j < len(lines) and lines[j] == "":
        j += 1
    return lines[i + 1:j]


@pytest.mark.parametrize("peer_access", [True, False])
async def test_the_own_lists_note_is_closed_by_exactly_one_empty_line(services, slots, fake_bot,
                                                                      monkeypatch, peer_access):
    """Под строкой судьбы своих списков — ровно одна пустая строка перед
    следующим блоком: и перед «↔️ Доступ из подсетей…», и когда доступа между
    подсетями нет и дальше идут «Конфигурация выпущена…»/пинг. Две пустые
    строки подряд в карточке — дыра посреди экрана, одна отсутствующая —
    состояние слипается со следующим блоком."""
    store = _peer_conf(monkeypatch)
    _two_lan_slots(services, slots)
    store["app.routing.peer_nets.enabled"] = peer_access
    text, _ = await _card(services, fake_bot, 2)
    assert _own_block(text)[1], f"строки судьбы нет — проверять нечего: {text}"
    assert _gap_after_note(text) == [""], f"под строкой судьбы не одна пустая строка:\n{text}"
    if peer_access:
        after = text.splitlines()[text.splitlines().index(LAN_ON) + 4]
        assert after.startswith("↔️"), text


async def test_the_overlap_warning_is_also_separated_from_the_own_lists_note(services, slots, fake_bot,
                                                                            monkeypatch):
    """Подсети слотов пересекаются, доступа между подсетями нет: под строкой
    судьбы своих списков сразу идёт «⚠️ … пересекается…». Между ними — ровно
    одна пустая строка; без неё два предупреждения слипаются в одно."""
    store = _peer_conf(monkeypatch)
    _two_lan_slots(services, slots)
    services.gateway_set_home_subnets(2, "192.168.1.0/24")
    store["app.routing.peer_nets.enabled"] = False
    text, _ = await _card(services, fake_bot, 2)
    assert _own_block(text)[1], f"строки судьбы нет — проверять нечего: {text}"
    lines = text.splitlines()
    i = lines.index(LAN_ON) + 2
    assert lines[i + 1] == "" and "пересекается" in lines[i + 2], \
        f"между строкой судьбы и предупреждением о пересечении не одна пустая строка:\n{text}"


async def test_the_own_lists_line_is_not_drawn_when_sync_is_off(services, slots, fake_bot, monkeypatch):
    """Карточка отдала «off» (синхронизация у слота не действует): строки
    «📋 Свои списки» с нулями нет, и пустая строка под ней не появляется."""
    store = _peer_conf(monkeypatch)
    _two_lan_slots(services, slots)
    store["app.routing.peer_nets.enabled"] = True
    monkeypatch.setattr(services, "gwlink_own_card", lambda gw: {"vpn": 0, "ru": 0, "state": "off", "error": ""})
    text, _ = await _card(services, fake_bot, 2)
    assert "📋" not in text, text
    lines = text.splitlines()
    assert lines[lines.index(LAN_ON) + 1].startswith("↔️"), f"под режимом без VPN лишняя строка:\n{text}"


async def test_applied_own_lists_are_followed_by_peer_access_without_a_gap(services, slots, fake_bot,
                                                                          monkeypatch):
    """Канон на шлюзе применён — строки судьбы нет, и «↔️ Доступ из подсетей…»
    идёт сразу под «📋 Свои списки», без пустой строки: пропуск нужен только
    чтобы отделить строку состояния, иначе в карточке висит дыра."""
    store = _peer_conf(monkeypatch)
    _two_lan_slots(services, slots)
    store["app.routing.peer_nets.enabled"] = True
    services.gwlink_own_in(1, "rx", [[1, "d0.com", "vpn", False]])
    services.gwlink_session_opened(2, "3.1.0", 2)
    services.gwlink_own_hello_in(2, True)
    digest = services.gwlink_own_for(services.db.gateway(2))[0]
    services.gwlink_own_ack_in(2, {"ok": True, "hash": digest, "n": 1})
    text, _ = await _card(services, fake_bot, 2)
    lines = text.splitlines()
    i = lines.index(LAN_ON)
    assert lines[i + 1] == "📋 Свои списки: 1 в туннель, 0 напрямую", text
    assert lines[i + 2].startswith("↔️ Доступ из подсетей других шлюзов"), \
        f"между применёнными списками и доступом из подсетей лишняя строка:\n{text}"


# ── панель агента после правки своих списков (вычитка 3.1.0) ─────────────────

def _script_edits(host, monkeypatch, ok: bool = True):
    """Скрипт своих списков правит файлы хоста (add | ru | del), остальное —
    как в _Host; счётчики панели — по тем же файлам."""
    def run(cmd, domains, timeout=150):
        if cmd not in ("add", "ru", "del"):
            return host.run(cmd, domains, timeout)
        if not ok:
            return False, "awg-lan-domain.sh: занято"
        cur = host.lists()
        for d in domains:
            if cmd == "del":
                cur.pop(d, None)
            else:
                cur[d] = "ru" if cmd == "ru" else "vpn"
        host.write([d for d, k in cur.items() if k == "vpn"], [d for d, k in cur.items() if k == "ru"])
        return True, "\n".join(f"{d}: {'убран' if cmd == 'del' else 'добавлен'}" for d in domains)
    from awgbot.infra import gwguard
    monkeypatch.setattr(gwguard, "run_lan_domain", run)
    monkeypatch.setattr(gwguard, "lan_own_lists", lambda: (
        sum(1 for k in host.lists().values() if k == "vpn"),
        sum(1 for k in host.lists().values() if k == "ru")))


def _old_snapshot(svc) -> None:
    """Снимок последнего тика монитора со старыми счётчиками своих списков."""
    from awgbot.util import timeutil
    st = _lan({})
    st.ts = timeutil.to_iso(timeutil.now())
    svc.db.set_state("gw_status", st.to_json())


async def _panel_from_snapshot(svc, fake_bot) -> str:
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_panel(cb, svc, FakeState())
    return msg.sent[-1][1]


async def test_the_panel_shows_new_counts_right_after_an_edit(gw, host, fake_bot, monkeypatch):
    """Добавил домен — панель тут же показывает новые числа, не дожидаясь
    тика монитора: иначе человек видит «4 в туннель» после добавления пятого
    и добавляет его ещё раз или думает, что не сработало. Остальной снимок
    (адрес, трафик) правка не трогает."""
    host.env["LINK_CHANNEL"] = "0"
    host.write(vpn=["a.com", "b.com"], ru=["c.ru"])
    _script_edits(host, monkeypatch)
    _old_snapshot(gw)
    before = await _panel_from_snapshot(gw, fake_bot)
    assert _own_line(before) == "Свои списки: 4 в туннель, 1 напрямую", before
    result = await _type_domain(gw, fake_bot, "lan_add", "example.com")
    assert result.startswith("✅"), result
    after = await _panel_from_snapshot(gw, fake_bot)
    assert _own_line(after) == "Свои списки: 3 в туннель, 1 напрямую", after
    assert "192.168.68.222" in after, "правка списка затёрла остальной снимок панели"
    await _type_domain(gw, fake_bot, "lan_ru", "shop.ru")
    assert _own_line(await _panel_from_snapshot(gw, fake_bot)) == "Свои списки: 3 в туннель, 2 напрямую"


async def test_a_failed_edit_leaves_the_panel_counts_as_they_were(gw, host, fake_bot, monkeypatch):
    """Скрипт отказал — снимок не переписывается: числа прежние, как и файлы."""
    host.env["LINK_CHANNEL"] = "0"
    host.write(vpn=["a.com"])
    _script_edits(host, monkeypatch, ok=False)
    _old_snapshot(gw)
    raw = gw.db.get_state("gw_status")
    result = await _type_domain(gw, fake_bot, "lan_add", "example.com")
    assert result.startswith("⚠️"), result
    assert gw.db.get_state("gw_status") == raw, "отказ скрипта переписал снимок"


@pytest.mark.parametrize("raw", ["", "{битый json", '{"link_up": true}'], ids=["none", "garbage", "no-lan"])
async def test_an_edit_without_a_usable_snapshot_neither_fails_nor_invents_one(gw, host, monkeypatch, raw):
    """Снимка ещё нет (монитор не тикал), он битый или без блока локальной
    сети — правка проходит, снимок не выдумывается и не портится."""
    host.env["LINK_CHANNEL"] = "0"
    _script_edits(host, monkeypatch)
    gw.db.set_state("gw_status", raw)
    ok, out = gw.lan_domains("add", ["example.com"])
    assert ok and "example.com" in out, out
    assert gw.db.get_state("gw_status") == raw, "снимок переписан при нечем обновлять"
