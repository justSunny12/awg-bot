"""Свои списки, общие для всех шлюзов: у агента — что правка кнопкой меняет
на хосте, в очереди канала и в снимке панели («➖» — сразу, устаревшая кнопка
ничего не трогает, правка уходит серверу тем же нажатием).
Экраны агента (хвост «свои: …» панели, строка состояния и подсказки экрана
«🔀 VPN-транзит», всплывашки «➖» и итоги ввода со всеми хвостами
синхронизации, итог сообщением при опоздавшем нажатии) — в эталоне
tests/screens/gateway.txt; строка своих списков в карточке слота основного
бота (числа канона, судьба на шлюзе: в пути, применён, отказ, поломка, старый
агент; соседние строки без пустых) — в эталоне tests/screens/admin.txt
(adm.gw.card.own.*, adm.gw.card.peers, adm.gw.card.overlap).

Цена ошибки: правка, не ушедшая серверу сразу, — соседний шлюз получает домен
минутами позже; «➖» по устаревшей кнопке убирает соседний домен; числа панели
после правки старые — домен добавляют ещё раз; неэкранированная ошибка шлюза —
Telegram отвергает всю карточку.
"""
from __future__ import annotations

import pytest

import awgbot.core.config as cfg
from awgbot.bot.callbacks import GwCB
from awgbot.bot.handlers import gateway as gh
from awgbot.domain.gateway import GatewayServices, GwStatus
from awgbot.infra.db import Database
from awgbot.runtime import linkclient
from awgbot.util import gwlink
from tests.conftest import FakeCallback, FakeMessage, FakeState
from tests.unit.test_gwlink_idle import PRIV, SN, _Wire
from tests.unit.test_gwownlists import _Host, _synced

pytestmark = pytest.mark.e2e


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


async def _own_screen_parts(svc, fake_bot):
    """(текст, клавиатура) — когда нужен настоящий callback_data кнопки."""
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_transit(cb, svc, FakeState())
    return msg.sent[-1][1], msg.sent[-1][2]


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
        return True, "\n".join(f"<code>{d}</code>: {out_word}" for d in domains)   # как скрипт
    monkeypatch.setattr(svc, "lan_domains", lambda cmd, domains: lan_domains(cmd, domains))


@pytest.mark.parametrize("channel,online,others", [
    ("1", True, None), ("1", False, None), ("0", False, None), ("1", True, True), ("0", False, True),
    ("1", True, False), ("1", False, False), ("0", False, False)])
async def test_minus_removes_at_once_whatever_the_channel(
        gw, host, fake_bot, monkeypatch, tmp_path, channel, online, others):
    """«➖» — без подтверждения: домен убран тем же нажатием при любом
    состоянии канала и соседей; кнопка без метки записи (список мог
    измениться) не трогает ничего. Всплывашки с хвостами синхронизации — в
    эталоне (gw.lan.rm*)."""
    _synced(gw, host, {"shop.ru": "ru", "a.com": "vpn"})
    host.env["LINK_CHANNEL"] = channel
    if others is not None:
        gw.set_link_role(True, standby=others, name="NASPi")
    if online:
        _online(gw, monkeypatch, tmp_path)
    _add_via_script(gw, host, monkeypatch, out_word="убран")
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    _text, markup = await _own_screen_parts(gw, fake_bot)
    btn = next(b for row in markup.inline_keyboard for b in row if b.text == "➖ 🇷🇺 shop.ru")
    # кнопка без метки (номер один) — не наша: список мог измениться, соседа не трогаем
    await gh.gw_transit_remove(cb, GwCB(action="lan_rm", val=btn.callback_data.split(":")[-1].split(".")[0]), gw)
    assert host.lists() == {"shop.ru": "ru", "a.com": "vpn"}, "по кнопке без метки убрано"
    await gh.gw_transit_remove(cb, GwCB.unpack(btn.callback_data), gw)
    assert host.lists() == {"a.com": "vpn"}, "домен не убран тем же нажатием"


async def _type_domain(svc, fake_bot, kind: str, text: str) -> None:
    """Ввод домена кнопкой «➕ В туннель» / «➕ Напрямую» (итог на экране — в
    эталоне)."""
    st = FakeState()
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_transit_ask(cb, GwCB(action=kind), svc, st)
    reply = FakeMessage(text=text, chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_transit_domain_received(reply, st, svc)


async def test_a_button_edit_leaves_at_once_and_says_so(gw, host, fake_bot, monkeypatch, tmp_path):
    """Канал жив: правка кнопкой уходит серверу тем же нажатием, а не тиком
    монитора — иначе соседний шлюз получает домен минутами позже."""
    _synced(gw, host, {"a.com": "vpn"})
    wire = _online(gw, monkeypatch, tmp_path)
    _add_via_script(gw, host, monkeypatch)
    await _type_domain(gw, fake_bot, "lan_add", "example.com")
    msgs = wire.messages(gwlink.channel_key(PRIV))
    assert [m["t"] for m in msgs] == ["own_ev"], f"правка не ушла серверу сразу: {msgs}"
    assert [e[1:3] for e in msgs[0]["ev"]] == [["example.com", "vpn"]]


async def test_without_a_channel_the_edit_waits_in_the_queue(gw, host, fake_bot, monkeypatch):
    """Канала нет — правка ложится в очередь и уйдёт при подключении (итог с
    хвостом «будут синхронизированы» — в эталоне, gw.lan.add.later)."""
    _synced(gw, host, {"a.com": "vpn"})
    _add_via_script(gw, host, monkeypatch)
    await _type_domain(gw, fake_bot, "lan_ru", "shop.ru")
    assert gw.own_status()[0]["pending"] == 1, "правка без канала не легла в очередь"


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
    await _type_domain(gw, fake_bot, "lan_add", "b.com")
    msgs = wire.messages(gwlink.channel_key(PRIV))
    assert [e[1:3] for m in msgs for e in m["ev"]] == [["b.com", "vpn"]], f"неотправленное не ушло: {msgs}"


async def test_removing_by_button_sends_the_edit_at_once(gw, host, fake_bot, monkeypatch, tmp_path):
    """«➖» при живом канале: удаление уходит серверу тем же нажатием."""
    _synced(gw, host, {"a.com": "vpn", "b.com": "vpn"})
    wire = _online(gw, monkeypatch, tmp_path)
    _add_via_script(gw, host, monkeypatch, out_word="убран")
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    from awgbot.bot.keyboards.gateway import lan_own_tag
    await gh.gw_transit_remove(cb, GwCB(action="lan_rm!", val=f"0.{lan_own_tag('vpn', 'a.com')}"), gw)
    assert host.lists() == {"b.com": "vpn"}, "домен не убран"
    msgs = wire.messages(gwlink.channel_key(PRIV))
    assert [e[1:3] for m in msgs for e in m["ev"]] == [["a.com", "del"]], msgs


# ── основной бот: строка своих списков без имени шлюза ──────────────────────

def test_the_lists_breakage_branch_without_a_name_reads_whole():
    from awgbot.bot.texts.routing import own_lists_line
    line = own_lists_line({"vpn": 1, "ru": 0, "state": "failed",
                           "error": "ошибка записи файла: диск только для чтения"})
    assert line == ("📋 Свои списки: 1 в туннель\n⚠️ Шлюз: ошибка записи файла: "
                    "диск только для чтения"), line


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


def _snapshot_lan(svc) -> dict:
    """Блок локальной сети в снимке тика — из него рисуется панель."""
    return GwStatus.from_json(svc.db.get_state("gw_status")).lan


async def test_the_panel_snapshot_gets_new_counts_right_after_an_edit(gw, host, fake_bot, monkeypatch):
    """Добавил домен — снимок, из которого рисуется панель, тут же получает
    новые числа, не дожидаясь тика монитора: иначе человек видит «4 в
    туннель» после добавления пятого и добавляет его ещё раз или думает, что
    не сработало. Остальной снимок (адрес, трафик) правка не трогает."""
    host.env["LINK_CHANNEL"] = "0"
    host.write(vpn=["a.com", "b.com"], ru=["c.ru"])
    _script_edits(host, monkeypatch)
    _old_snapshot(gw)
    lan = _snapshot_lan(gw)
    assert (lan["own_vpn"], lan["own_ru"]) == (4, 1), "сцена собрана не так"
    await _type_domain(gw, fake_bot, "lan_add", "example.com")
    lan = _snapshot_lan(gw)
    assert (lan["own_vpn"], lan["own_ru"]) == (3, 1), f"числа своих списков в снимке старые: {lan}"
    assert lan["lan_pkts"] == 9 and lan["addr"] == "192.168.68.222", f"правка затёрла остальной снимок: {lan}"
    await _type_domain(gw, fake_bot, "lan_ru", "shop.ru")
    lan = _snapshot_lan(gw)
    assert (lan["own_vpn"], lan["own_ru"]) == (3, 2), lan


async def test_a_failed_edit_leaves_the_panel_counts_as_they_were(gw, host, fake_bot, monkeypatch):
    """Скрипт отказал — снимок не переписывается: числа прежние, как и файлы."""
    host.env["LINK_CHANNEL"] = "0"
    host.write(vpn=["a.com"])
    _script_edits(host, monkeypatch, ok=False)
    _old_snapshot(gw)
    raw = gw.db.get_state("gw_status")
    await _type_domain(gw, fake_bot, "lan_add", "example.com")
    assert host.lists() == {"a.com": "vpn"}, "сцена собрана не так: скрипт не отказал"
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
