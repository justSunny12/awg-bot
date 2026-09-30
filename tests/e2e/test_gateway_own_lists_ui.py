"""Свои списки, общие для всех шлюзов, на экранах: у агента — состояние хвостом
«свои: …» в строке «📋 Списки» панели и строкой «Свои списки: …» на экране
«🔀 VPN-транзит», абзац о синхронизации — под «подробнее», строка состояния —
открыто, «➖» — сразу со всплывашкой (итог и хвост синхронизации — только
когда он правдив), итог ввода — первыми строками экрана; у основного бота — строка
числами в карточке слота и судьба канона на шлюзе отдельной строкой.

Цена ошибки: «синхронизированы», когда правка лежит без канала, — человек
ждёт домен на соседнем шлюзе, а он не придёт; «уходит на другие шлюзы», когда
канала нет, — то же; «убран на всех» без синхронизации — лишний страх;
неэкранированная ошибка шлюза — Telegram отвергает всю карточку.
"""
from __future__ import annotations

import json

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
    # экран «🔀 VPN-транзит» рисуется из снимка тика; тика не было — снимок
    # с локальной сетью, а не живые пробы хоста (ip, awg — не на этой машине)
    real = svc.cached_status
    monkeypatch.setattr(svc, "cached_status", lambda max_age: real(max_age) or _lan(svc.own_status()[0]))
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
    await gh.gw_transit(cb, svc, FakeState())
    lan, lkb = msg.sent[-1][1], msg.sent[-1][2]
    labels = lambda m: [b.text for row in m.inline_keyboard for b in row]   # noqa: E731
    return panel, lan, (labels(pkb), labels(lkb))


def _panel_own(text: str) -> str:
    """Хвост «свои: …» строки «📋 Списки: …» панели."""
    line = next(ln for ln in text.splitlines() if ln.startswith("📋 Списки: "))
    return line[line.index("свои: "):]


def _own_line(text: str) -> str:
    """Строка «Свои списки: …» экрана «🔀 VPN-транзит»."""
    return next(ln.strip() for ln in text.splitlines() if ln.startswith("Свои списки: "))


async def test_the_panel_line_says_the_lists_are_shared_and_how_they_stand(gw, host, fake_bot, monkeypatch):
    """Строка одна и та же с синхронизацией и без; хвост — ждут или не
    применились, и только когда синхронизация действует. Кнопок столько же,
    сколько без неё. Значок здоровья в хвосте — 🩺, как у кнопки."""
    host.env["LINK_CHANNEL"] = "0"
    panel, lan, labels0 = await _panel_and_lan(gw, fake_bot, monkeypatch)
    assert _panel_own(panel) == "свои: 4 в туннель, 1 напрямую", panel
    assert _own_line(lan) == "Свои списки: 4 в туннель, 1 напрямую", lan
    host.env["LINK_CHANNEL"] = "1"
    _synced(gw, host, {"a.com": "vpn"})
    panel, lan, labels = await _panel_and_lan(gw, fake_bot, monkeypatch)
    assert _panel_own(panel) == "свои: 4 в туннель, 1 напрямую", panel
    assert _own_line(lan) == "Свои списки: 4 в туннель, 1 напрямую", lan
    assert labels[0] == labels0[0], "синхронизация добавила или убрала кнопки панели"
    host.write(vpn=["a.com", "b.com"])
    gw.own_reconcile()
    panel, lan, _ = await _panel_and_lan(gw, fake_bot, monkeypatch)
    assert _panel_own(panel) == "свои: 4 в туннель, 1 напрямую · ⏳ ждут синхронизации", panel
    assert _own_line(lan) == "Свои списки: 4 в туннель, 1 напрямую · ⏳ ждут синхронизации", lan
    gw.db.set_state("gw_own_err", "dnsmasq отверг")
    panel, _, _ = await _panel_and_lan(gw, fake_bot, monkeypatch)
    assert _panel_own(panel) == "свои: 4 в туннель, 1 напрямую · ⚠️ не применились (🩺 Здоровье)", panel


async def _own_screen(svc, fake_bot):
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_transit(cb, svc, FakeState())
    return msg.sent[-1][1], [b.text for row in msg.sent[-1][2].inline_keyboard for b in row]


async def _own_screen_parts(svc, fake_bot):
    """(текст, клавиатура) — когда нужен настоящий callback_data кнопки."""
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_transit(cb, svc, FakeState())
    return msg.sent[-1][1], msg.sent[-1][2]


ONLY_HERE = ("Списки применятся только для этого шлюза: для синхронизации нужен упр. канал до сервера "
             "AWG — перевыпусти конфигурацию шлюза")
SYNCED = "Изменения синхронизируются с другими шлюзами"
LATER = "Изменения будут синхронизированы с другими шлюзами, когда появится связь с сервером AWG"
LOCAL = ("Изменения применятся только для этого шлюза: для синхронизации нужен упр. канал до сервера "
         "AWG — перевыпусти конфигурацию шлюза")
EMPTY_HINT = "добавь домены кнопками «➕ В туннель» и «➕ Напрямую»"
SHARED_HINT = "списки общие для всех шлюзов — добавленное здесь появится и на остальных"
SYNC_ABOUT = ("свои списки синхронизируются между шлюзами: добавленное или убранное здесь уходит "
              "через сервер AWG на остальные шлюзы — сразу, если они на связи, иначе при подключении")


def _open_lines(text: str) -> list[str]:
    """Строки экрана до свёрнутого «подробнее»."""
    assert text.count("<blockquote expandable>") == 1, text
    return text.split("<blockquote expandable>", 1)[0].splitlines()


async def test_the_own_lists_screen_explains_sharing_and_shows_the_state(gw, host, fake_bot):
    """Абзац о синхронизации — под «подробнее» (он нужен раз, а экран читают
    часто); строка состояния — открыто и только при расхождении."""
    _synced(gw, host, {"a.com": "vpn", "b.ru": "ru"})
    text, labels = await _own_screen(gw, fake_bot)
    assert SYNC_ABOUT in text.split("<blockquote expandable>", 1)[1], f"абзаца о синхронизации нет: {text}"
    assert not any(ln.startswith(("⏳", "⚠️")) for ln in _open_lines(text)), "расхождения нет — строки нет"
    assert labels == ["➕ В туннель", "➕ Напрямую", "➖ 🇷🇺 b.ru", "➖ 🌍 a.com",
                      "❓ Роутер", "⬅️ В меню"], labels
    # правка ждёт, канала нет — строка состояния открыто
    host.write(vpn=["a.com", "c.com"], ru=["b.ru"])
    gw.own_reconcile()
    gw.channel.online = False
    text, labels2 = await _own_screen(gw, fake_bot)
    assert "⏳ Ждут синхронизации: 1 правка — нет связи с сервером AWG" in _open_lines(text), text
    assert len(labels2) == len(labels) + 1, "строка состояния сама по себе кнопок не добавляет"
    gw.channel.online = True
    text, _ = await _own_screen(gw, fake_bot)
    assert "⏳ Ждут синхронизации: 1 правка — сервер AWG ещё не ответил" in _open_lines(text), text
    # поломка на шлюзе — отдельной фразой, без второго двоеточия подряд
    gw.db.set_state("gw_own_err", "ошибка записи файла: нет места на диске")
    text, _ = await _own_screen(gw, fake_bot)
    assert "⚠️ Не удалось применить. Ошибка записи файла: нет места на диске" in _open_lines(text), text


async def test_a_refusal_on_the_gateway_keeps_the_accepted_wording_and_is_escaped(gw, host, fake_bot):
    """Отказ (не поломка) — строка 3.1.0 дословно: «⚠️ Не применились: …»;
    «Не удалось применить» — только у поломки записи файла, так их и различают
    при чтении. Ошибка с малины экранирована — иначе Telegram отвергает экран."""
    _synced(gw, host, {"a.com": "vpn"})
    gw.db.set_state("gw_own_err", "dnsmasq <b>отверг</b>")
    text, _ = await _own_screen(gw, fake_bot)
    assert "⚠️ Не применились: dnsmasq &lt;b&gt;отверг&lt;/b&gt;" in _open_lines(text), text


async def test_a_rejected_domain_is_named_on_the_screen(gw, host, fake_bot):
    """Сервер не принял домен — строка «⚠️ Сервер AWG не принял: `домен` —
    причина» открыто: иначе человек ждёт домен на соседях, а его нет."""
    _synced(gw, host, {"a.com": "vpn"})
    gw.db.set_state(gw._OWN_REJ_KEY, json.dumps([["bad.example", "максимум 500 доменов"]]))
    text, _ = await _own_screen(gw, fake_bot)
    assert "⚠️ Сервер AWG не принял: <code>bad.example</code> — максимум 500 доменов" in _open_lines(text), text


async def test_an_empty_shared_list_says_it_will_appear_everywhere(gw, host, fake_bot):
    """Пустой список — подсказки «добавь домены кнопками…» и «списки общие
    для всех шлюзов» под «подробнее», первыми: иначе первое же добавление на
    одном шлюзе удивляет на другом, а открытыми строками они занимали экран,
    который и так пуст."""
    _synced(gw, host, {})
    text, labels = await _own_screen(gw, fake_bot)
    assert not any("Пока пусто" in ln for ln in _open_lines(text)), text
    about = text.split("<blockquote expandable>", 1)[1]
    assert about.startswith(f"{EMPTY_HINT} · {SHARED_HINT} · "), about
    assert labels == ["➕ В туннель", "➕ Напрямую", "❓ Роутер", "⬅️ В меню"]
    # без синхронизации пусто — но «общие для всех» было бы неправдой
    host.env["LINK_CHANNEL"] = "0"
    text, _ = await _own_screen(gw, fake_bot)
    about = text.split("<blockquote expandable>", 1)[1]
    assert about.startswith(f"{EMPTY_HINT} · "), about
    assert "общие для всех шлюзов" not in text, text
    # не пусто — подсказки для пустого списка нет
    _synced(gw, host, {"a.com": "vpn"})
    text, _ = await _own_screen(gw, fake_bot)
    assert EMPTY_HINT not in text, text


async def test_without_the_channel_the_screen_says_the_lists_are_local(gw, host, fake_bot):
    host.env["LINK_CHANNEL"] = ""
    host.write(vpn=["a.com"])
    text, _ = await _own_screen(gw, fake_bot)
    assert ONLY_HERE in _open_lines(text), text
    assert "общие для всех шлюзов" not in text
    host.env["LAN_MODE"] = "0"
    text, _ = await _own_screen(gw, fake_bot)
    assert ONLY_HERE not in text, "режим выключен — про канал говорить нечего"


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


@pytest.mark.parametrize("channel,online,others,toast", [
    ("1", True, None, "shop.ru: убран\n" + SYNCED),
    ("1", False, None, "shop.ru: убран\n" + LATER),
    ("0", False, None, "shop.ru: убран\n" + LOCAL),
    ("1", True, True, "shop.ru: убран\n" + SYNCED),
    ("0", False, True, "shop.ru: убран\n" + LOCAL),     # другой шлюз есть, а канала нет
    # сервер сказал, что другого шлюза нет, — про синхронизацию говорить не с кем
    ("1", True, False, "shop.ru: убран"),
    ("1", False, False, "shop.ru: убран"),
    ("0", False, False, "shop.ru: убран")])
async def test_minus_removes_at_once_with_an_honest_sync_tail(
        gw, host, fake_bot, monkeypatch, tmp_path, channel, online, others, toast):
    """«➖» — без подтверждения: домен убран тем же нажатием, всплывашка —
    итог и хвост синхронизации, только когда он правдив: «синхронизируются»
    при живом канале, «будут синхронизированы» — без связи, «применятся
    только для этого шлюза» — без канала, и ничего, когда сервер сказал, что
    другого шлюза нет. Первой строки «Убран на всех шлюзах» больше нет."""
    _synced(gw, host, {"shop.ru": "ru", "a.com": "vpn"})
    host.env["LINK_CHANNEL"] = channel
    if others is not None:
        gw.set_link_role(True, standby=others, name="NASPi")
    if online:
        _online(gw, monkeypatch, tmp_path)
    _add_via_script(gw, host, monkeypatch, out_word="убран")
    text, labels = await _own_screen(gw, fake_bot)
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    _text, markup = await _own_screen_parts(gw, fake_bot)
    btn = next(b for row in markup.inline_keyboard for b in row if b.text == "➖ 🇷🇺 shop.ru")
    # кнопка без метки (номер один) — не наша: список мог измениться, соседа не трогаем
    await gh.gw_transit_remove(cb, GwCB(action="lan_rm", val=btn.callback_data.split(":")[-1].split(".")[0]), gw)
    assert host.lists() == {"shop.ru": "ru", "a.com": "vpn"} and cb.answers[-1][0].startswith("Список изменился")
    cb.answers.clear()
    await gh.gw_transit_remove(cb, GwCB.unpack(btn.callback_data), gw)
    assert host.lists() == {"a.com": "vpn"}, "домен не убран тем же нажатием"
    assert cb.answers == [(toast, False)], cb.answers
    shown = [b.text for row in msg.sent[-1][2].inline_keyboard for b in row]
    assert "➖ 🇷🇺 shop.ru" not in shown and "➖ 🌍 a.com" in shown, shown


async def test_a_slow_script_result_comes_as_a_message(gw, host, fake_bot, monkeypatch):
    """Скрипт ответил дольше, чем Telegram держит нажатие, — всплывашку уже
    не показать: итог приходит сообщением, экран всё равно перерисован."""
    from aiogram.exceptions import TelegramBadRequest
    _synced(gw, host, {"shop.ru": "ru"})
    host.env["LINK_CHANNEL"] = "0"
    _add_via_script(gw, host, monkeypatch, out_word="убран")
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)

    class _LateCallback(FakeCallback):
        async def answer(self, text=None, show_alert=False, **kw):
            raise TelegramBadRequest(method=None, message="query is too old and response timeout expired")
    cb = _LateCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    from awgbot.bot.keyboards.gateway import lan_own_tag
    await gh.gw_transit_remove(cb, GwCB(action="lan_rm", val=f"0.{lan_own_tag('ru', 'shop.ru')}"), gw)
    answers = [s[1] for s in msg.sent if s[0] == "answer"]
    assert answers == ["✅ shop.ru: убран\n" + LOCAL], msg.sent
    assert msg.sent[-1][0] == "edit_text" and msg.sent[-1][1].startswith("🔀 <b>VPN-транзит"), msg.sent[-1]


async def _type_domain(svc, fake_bot, kind: str, text: str) -> str:
    """Ввод домена: итог — первыми строками экрана «🔀 VPN-транзит» (одним
    сообщением); возвращает итог без экрана."""
    st = FakeState()
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_transit_ask(cb, GwCB(action=kind), svc, st)
    reply = FakeMessage(text=text, chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_transit_domain_received(reply, st, svc)
    answers = [s[1] for s in reply.sent if s[0] == "answer"]
    assert len(answers) == 1, f"итог отдельным сообщением: {answers}"
    note, _, screen = answers[0].partition("\n\n")
    assert note.startswith(("✅", "⚠️")) and screen.startswith("🔀 <b>VPN-транзит"), answers[0]
    return note


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


async def test_the_typed_domain_is_already_a_button_on_the_screen_below(gw, host, fake_bot, monkeypatch):
    """После ввода экран под итогом — уже с новым доменом кнопкой: человек
    видит результат там же, где будет его убирать."""
    _synced(gw, host, {"a.com": "vpn"})
    host.env["LINK_CHANNEL"] = "0"
    _add_via_script(gw, host, monkeypatch)
    st = FakeState()
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_transit_ask(cb, GwCB(action="lan_ru"), gw, st)
    assert msg.sent[-1][1] == ("➕ <b>Напрямую</b> · пришли домены через пробел: <code>example.com</code> — "
                               "накрывает и поддомены"), msg.sent[-1][1]
    assert [b.text for row in msg.sent[-1][2].inline_keyboard for b in row] == ["✖️ Отмена"]
    reply = FakeMessage(text="shop.ru", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_transit_domain_received(reply, st, gw)
    _, _, markup = next(s for s in reply.sent if s[0] == "answer")
    assert "➖ 🇷🇺 shop.ru" in [b.text for row in markup.inline_keyboard for b in row]


async def test_without_a_channel_the_edit_waits_and_the_result_says_when_it_leaves(gw, host, fake_bot, monkeypatch):
    _synced(gw, host, {"a.com": "vpn"})
    _add_via_script(gw, host, monkeypatch)
    result = await _type_domain(gw, fake_bot, "lan_ru", "shop.ru")
    assert result == "✅ shop.ru: добавлен\n" + LATER, result
    assert gw.own_status()[0]["pending"] == 1, "правка без канала не легла в очередь"


@pytest.mark.parametrize("word,channel,others", [
    ("уже в списке", "1", None),        # ничего не изменилось
    ("уже в списке", "0", None),        # и без канала: «изменения применятся здесь» — а изменений нет
    ("добавлен", "1", False),           # сервер: другого шлюза нет
    ("добавлен", "0", False),
])
async def test_no_tail_when_nothing_changed_or_there_is_no_other_gateway(
        gw, host, fake_bot, monkeypatch, word, channel, others):
    """Хвост о синхронизации — только когда есть что и с кем
    синхронизировать: «уже в списке» ничего не меняет, а единственному
    шлюзу («другого нет» — слово сервера) синхронизироваться не с кем."""
    # «уже в списке» — домен действительно уже есть: файлы после правки те же
    _synced(gw, host, {"a.com": "vpn", **({"example.com": "vpn"} if word == "уже в списке" else {})})
    host.env["LINK_CHANNEL"] = channel
    if others is not None:
        gw.set_link_role(True, standby=others, name="NASPi")
    _add_via_script(gw, host, monkeypatch, out_word=word)
    result = await _type_domain(gw, fake_bot, "lan_add", "example.com")
    assert result == f"✅ example.com: {word}", result


async def test_without_the_channel_the_result_says_the_edit_stays_here(gw, host, fake_bot, monkeypatch):
    """Канала нет (старая конфигурация шлюза), а про другие шлюзы сервер не
    говорил: итог честно предупреждает, что правка останется на этом шлюзе и
    что чинится это перевыпуском конфигурации, — иначе человек ждёт домен на
    соседнем шлюзе."""
    _synced(gw, host, {"a.com": "vpn"})
    host.env["LINK_CHANNEL"] = "0"
    _add_via_script(gw, host, monkeypatch)
    result = await _type_domain(gw, fake_bot, "lan_add", "example.com")
    assert result == "✅ example.com: добавлен\n" + LOCAL, result


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
    from awgbot.bot.keyboards.gateway import lan_own_tag
    await gh.gw_transit_remove(cb, GwCB(action="lan_rm!", val=f"0.{lan_own_tag('vpn', 'a.com')}"), gw)
    assert cb.answers[-1] == ("a.com: убран\n" + SYNCED, False), cb.answers
    msgs = wire.messages(gwlink.channel_key(PRIV))
    assert [e[1:3] for m in msgs for e in m["ev"]] == [["a.com", "del"]], msgs


# ── основной бот: карточка слота ─────────────────────────────────────────────

def _lan_on_index(lines: list[str]) -> int | None:
    """Строка подсетей с включённым VPN-транзитом: «🗺 … · 🔀 VPN-транзит ✅»."""
    return next((k for k, ln in enumerate(lines)
                 if ln.startswith("🗺 ") and ln.endswith(" · 🔀 VPN-транзит ✅")), None)


async def _card(services, fake_bot, slot: int):
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=slot), services, FakeState())
    return _screen(nav)


def _own_block(text: str) -> tuple[str | None, str | None]:
    """(строка 📋 сразу после строки подсетей с «🔀 VPN-транзит ✅», строка
    судьбы сразу под ней — без пустой, вычитка 3.1.0)."""
    lines = text.splitlines()
    i = _lan_on_index(lines)
    if i is None:
        return None, None
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
    assert note == "⚠️ Шлюз «Pi2» не смог принять списки: &lt;b&gt;dnsmasq&lt;/b&gt; &amp; rc=1", note
    services.gwlink_own_ack_in(2, {"ok": False, "hash": digest, "error": ""})
    _, note = _own_block((await _card(services, fake_bot, 2))[0])
    assert note == "⚠️ Шлюз «Pi2» не смог принять списки", "висящее двоеточие без ошибки"


async def _failed_with(services, fake_bot, error: str) -> str | None:
    """Канон отправлен на слот 2, шлюз ответил отказом с этим текстом — строка
    судьбы в карточке."""
    services.gwlink_own_in(1, "rx", [[1, "d0.com", "vpn", False]])
    services.gwlink_session_opened(2, "3.1.0", 2)
    services.gwlink_own_hello_in(2, True)
    digest = services.gwlink_own_for(services.db.gateway(2))[0]
    services.gwlink_own_ack_in(2, {"ok": False, "hash": digest, "error": error})
    return _own_block((await _card(services, fake_bot, 2))[0])[1]


async def test_a_breakage_on_the_gateway_is_not_called_a_refusal(services, lan_slots, fake_bot):
    """Файл своих списков не записался на малине — поломка, не отказ: «Шлюз
    «X»: ошибка записи файла: …» без «не смог принять». Иначе человек ищет
    плохой домен в списке, а чинить нужно диск шлюза."""
    note = await _failed_with(services, fake_bot, "ошибка записи файла: нет места на диске")
    assert note == "⚠️ Шлюз «Pi2»: ошибка записи файла: нет места на диске", note


async def test_a_real_refusal_of_the_lists_keeps_its_wording(services, lan_slots, fake_bot):
    """Отказ скрипта (не поломка) — «не смог принять списки: …»."""
    note = await _failed_with(services, fake_bot, "скрипт не ответил за 150 с")
    assert note == "⚠️ Шлюз «Pi2» не смог принять списки: скрипт не ответил за 150 с", note


async def test_a_breakage_text_of_the_lists_is_escaped(services, lan_slots, fake_bot, slots):
    """Текст с малины и имя устройства — в разметке ВПС экранированы ровно раз."""
    _, _, pi2 = slots
    services.rename_device(pi2.id, "Pi & <2>")
    note = await _failed_with(services, fake_bot, "ошибка записи файла: <i>&")
    assert note == "⚠️ Шлюз «Pi &amp; &lt;2&gt;»: ошибка записи файла: &lt;i&gt;&amp;", note


def test_the_lists_breakage_branch_without_a_name_reads_whole():
    from awgbot.bot.texts.routing import own_lists_line
    line = own_lists_line({"vpn": 1, "ru": 0, "state": "failed",
                           "error": "ошибка записи файла: диск только для чтения"})
    assert line == ("📋 Свои списки: 1 в туннель\n⚠️ Шлюз: ошибка записи файла: "
                    "диск только для чтения"), line


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


def _after_note(text: str) -> str:
    """Строка сразу под строкой судьбы своих списков."""
    lines = text.splitlines()
    i = _lan_on_index(lines) + 2
    assert lines[i].startswith(("⏳", "⚠️")), text
    return lines[i + 1]


@pytest.mark.parametrize("peer_access", [True, False])
async def test_the_own_lists_note_is_followed_by_the_next_block_without_a_gap(services, slots, fake_bot,
                                                                              monkeypatch, peer_access):
    """Карточка — плотный список строк без пустых: под строкой судьбы своих
    списков сразу «↔️ Связь подсетей ✅» (связь включена) или «подробнее».
    Пустая строка посреди карточки — дыра на экране."""
    store = _peer_conf(monkeypatch)
    _two_lan_slots(services, slots)
    store["app.routing.peer_nets.enabled"] = peer_access
    text, _ = await _card(services, fake_bot, 2)
    assert _own_block(text)[1], f"строки судьбы нет — проверять нечего: {text}"
    assert "" not in text.splitlines(), f"пустая строка посреди карточки:\n{text}"
    after = _after_note(text)
    if peer_access:
        assert after == "↔️ Связь подсетей ✅", text
    else:
        assert after.startswith("<blockquote"), text


async def test_the_overlap_warning_follows_the_own_lists_note_on_its_own_line(services, slots, fake_bot,
                                                                              monkeypatch):
    """Подсети слотов пересекаются, связи подсетей нет: под строкой судьбы
    своих списков сразу своей строкой «⚠️ … пересекается…» — два
    предупреждения не слипаются в одно."""
    store = _peer_conf(monkeypatch)
    _two_lan_slots(services, slots)
    services.gateway_set_home_subnets(2, "192.168.1.0/24")
    store["app.routing.peer_nets.enabled"] = False
    text, _ = await _card(services, fake_bot, 2)
    assert _own_block(text)[1], f"строки судьбы нет — проверять нечего: {text}"
    after = _after_note(text)
    assert after.startswith("⚠️ 192.168.1.0/24 пересекается с подсетью «NASPi»: "), text


async def test_the_own_lists_line_is_not_drawn_when_sync_is_off(services, slots, fake_bot, monkeypatch):
    """Карточка отдала «off» (синхронизация у слота не действует): строки
    «📋 Свои списки» с нулями нет."""
    store = _peer_conf(monkeypatch)
    _two_lan_slots(services, slots)
    store["app.routing.peer_nets.enabled"] = True
    monkeypatch.setattr(services, "gwlink_own_card", lambda gw: {"vpn": 0, "ru": 0, "state": "off", "error": ""})
    text, _ = await _card(services, fake_bot, 2)
    assert "📋" not in text, text
    lines = text.splitlines()
    assert lines[_lan_on_index(lines) + 1] == "↔️ Связь подсетей ✅", f"под подсетями лишняя строка:\n{text}"


async def test_applied_own_lists_are_followed_by_peer_access_without_a_gap(services, slots, fake_bot,
                                                                          monkeypatch):
    """Канон на шлюзе применён — строки судьбы нет, и «↔️ Связь подсетей ✅»
    идёт сразу под «📋 Свои списки»."""
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
    i = _lan_on_index(lines)
    assert lines[i + 1] == "📋 Свои списки: 1 в туннель", text
    assert lines[i + 2] == "↔️ Связь подсетей ✅", \
        f"между применёнными списками и связью подсетей лишняя строка:\n{text}"


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
    assert _panel_own(before) == "свои: 4 в туннель, 1 напрямую", before
    result = await _type_domain(gw, fake_bot, "lan_add", "example.com")
    assert result.startswith("✅"), result
    after = await _panel_from_snapshot(gw, fake_bot)
    assert _panel_own(after) == "свои: 3 в туннель, 1 напрямую", after
    assert "9 пакетов с роутера" in after, "правка списка затёрла остальной снимок панели"
    await _type_domain(gw, fake_bot, "lan_ru", "shop.ru")
    assert _panel_own(await _panel_from_snapshot(gw, fake_bot)) == "свои: 3 в туннель, 2 напрямую"


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
