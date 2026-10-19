"""Карантин сопряжения (§3.7 концепта «единые обновления»): сессия канала на
ключах токена — живыми сокетами, боевым слушателем; файервол ВПС по штампу
линка.

В сессии сопряжения сервер не шлёт ни роли, ни документа, ни списков, ни
SMB, ни keep — только join_eph, join_wrong, join_refused, join_ok и смену
ключей; любое другое сообщение шлюза, кроме link_ack и link_done, — разрыв.
Файервол: линк со штампом — в quarantine_ifs (forward из линка и в линк —
drop, input — только порт канала от пира линка); после фиксации — нет.

Цена ошибки: токен из чата, по которому чужая машина получает роль слота,
документ с аплинком и токеном бота или ходит из линка в сеть ВПС.
"""
from __future__ import annotations

import asyncio

import pytest

from awgbot.runtime import linkserver
from awgbot.util import gwlink, joinsas

from tests.integration import _join_common as jc
from tests.integration._join_common import Gateway
from tests.integration.test_gwlink_channel import _Gw, _free_port, _until

pytestmark = pytest.mark.integration
admin, vps = jc.admin, jc.vps                     # фикстуры сцены


@pytest.fixture()
async def join(services, vps, monkeypatch):
    """Команда подключения нового слота 1 и слушатель канала на петле: адрес
    линка сопряжения — 127.0.0.1 (на петле локальный адрес и адрес пира
    совпадают — сопоставление пары подменено)."""
    res = services.gwjoin_issue()
    real = services.gwjoin_addresses
    monkeypatch.setattr(services, "gwjoin_addresses", lambda: [(s, "127.0.0.1") for s, _a in real()])
    monkeypatch.setattr(services, "gwjoin_for_addr", lambda local, peer: services.gwjoin(1))
    port = _free_port()
    monkeypatch.setattr(linkserver, "channel_port", lambda: port)
    events: list[tuple[str, dict]] = []

    async def on_join(event, st, extra):
        events.append((event, st))
    monkeypatch.setattr(linkserver, "_on_join", on_join)
    srv = linkserver.LinkServer(services)
    await srv.ensure()
    assert srv._bound, "слушатель сопряжения не поднялся"
    srv.events = events
    srv.command = res["command"]
    yield srv
    await srv.stop()


async def _hello(srv, *, host: str = "raspberrypi") -> tuple[_Gw, Gateway, dict]:
    side = Gateway(srv.command)
    key = gwlink.channel_key(side.token.gw_priv_b64)
    gw = _Gw(*await asyncio.open_connection(*srv._bound[0]), key=key)
    await gw.send("hello", {"proto": gwlink.PROTO, "agent": "3.4.0", "caps": ["join1"], **side.hello(host=host)})
    first = await gw.recv()
    if first.get("t") == "join_eph":
        side.eph_in(first)
    return gw, side, first


async def test_the_join_session_gets_the_ephemeral_key_and_nothing_else(services, join):
    gw, side, first = await _hello(join)
    assert first["t"] == "join_eph", first
    assert side.code, "код не вычислился"
    assert await gw.silent(0.4), "в сессию сопряжения ушло что-то кроме join_eph"
    assert await _until(lambda: join.events) and join.events[0][0] == "connected"
    from awgbot.domain.services.gwjoin import state as statemod
    assert statemod._LIVE[1]["code"] == side.code
    await gw.close()


async def test_any_other_message_from_the_gateway_breaks_the_session(services, join):
    gw, _side, first = await _hello(join)
    assert first["t"] == "join_eph"
    await gw.send("snap", {"full": True, "rev": 1, "snap": {}})
    assert await gw.next_msg(1.0) is None, "сессия сопряжения приняла снимок"
    assert services.gwlink_snapshot(1) == {}, "снимок из сессии сопряжения сохранён"


async def test_wrong_codes_and_the_third_close_the_session_and_drop_the_link(services, join, vps):
    gw, side, _first = await _hello(join)
    jid = services.gwjoin(1)["id"]
    wrong = "AAAAAA" if side.code != "AAAAAA" else "BBBBBB"
    out = services.gwjoin_code(1, jid, wrong)
    await join.join_wrong(1, out["left"])
    m = await gw.recv()
    assert m["t"] == "join_wrong" and m["left"] == 2, m
    services.gwjoin_code(1, jid, wrong)
    out = services.gwjoin_code(1, jid, wrong)
    assert out["result"] == "refused"
    await join.join_close(1, "code")
    msgs = []
    while (m := await gw.next_msg(1.0)) is not None:
        msgs.append(m)
    assert [m["t"] for m in msgs][-1] == "join_refused", msgs
    assert vps.modes[-1] == "--rekey" and services.gwjoin(1) == {}
    assert await _until(lambda: not join._bound), "слушатель остался на адресе закрытого сопряжения"


async def test_the_right_code_sends_the_secrets_and_the_new_keys(services, join, vps):
    gw, side, _first = await _hello(join)
    jid = services.gwjoin(1)["id"]
    assert services.gwjoin_code(1, jid, joinsas.show(side.code))["result"] == "ok"
    await join.join_accepted(1)
    ok = await gw.recv()
    assert ok["t"] == "join_ok" and joinsas.unseal(side.kp, ok["box"])["admin_id"] == jc.ADMIN
    nxt = await gw.recv()
    assert nxt["t"] == "link_next" and nxt.get("auto") == "join" and "PresharedKey" in nxt["conf"]
    await gw.send("link_ack", {"id": nxt["id"], "state": "ready"})
    go = await gw.recv()
    assert go["t"] == "link_go" and go["id"] == nxt["id"]
    assert await _until(lambda: services.gateway_rekey(1).get("committed"), timeout=6.0), \
        "сервер не переключился на новые ключи"
    await gw.close()


def _spec(**kw):
    from awgbot.infra import nftguard
    base = dict(ssh_port=22, tunnel_nets4=["10.8.1.0/24"], tunnel_ifs=["awg0"], link_ifs=["awglink", "awglink2"],
                link_peers4=["10.99.99.2", "10.99.99.6"], link_channel_port=8787,
                peer_link_ifs=["awglink", "awglink2"], peer_link_block=["awglink", "awglink2"])
    base.update(kw)
    return nftguard.GuardSpec(**base)


Q = dict(quarantine_ifs=["awglink"], quarantine_pairs=[("awglink", "10.99.99.1", "10.99.99.2")])


def test_the_firewall_quarantines_a_stamped_link():
    """Линк сопряжения в таблице ВПС: из линка и в линк — drop раньше любых
    разрешений пересылки; на входе с него — только канал своей паре (шлюз
    этого линка → адрес ВПС этого линка, quar-5), прочее — drop; без линков
    сопряжения правил нет."""
    from awgbot.infra import nftguard
    text = nftguard.render(_spec(**Q))
    inp = text.split("chain input {", 1)[1].split("\n    }", 1)[0]
    fwd = text.split("chain forward {", 1)[1].split("\n    }", 1)[0]
    lines = [ln.strip() for ln in inp.splitlines() if ln.strip()]
    q = 'iifname { "awglink" }'
    at = lines.index(f"{q} drop")
    assert lines[at - 1] == 'iifname "awglink" ip saddr 10.99.99.2 ip daddr 10.99.99.1 tcp dport 8787 accept', lines
    assert at < lines.index("ip protocol icmp accept"), "пинг с линка сопряжения открыт"
    assert at < next(i for i, ln in enumerate(lines) if "udp dport 53" in ln), "DNS с линка сопряжения открыт"
    lines = [ln.strip() for ln in fwd.splitlines() if ln.strip()]
    assert lines[1:3] == [f"{q} drop", 'oifname { "awglink" } drop'], lines[:4]
    plain = nftguard.render(_spec())
    assert f"{q} drop" not in plain and 'oifname { "awglink" } drop' not in plain


def test_the_quarantine_stands_without_the_filter_too():
    """quar-2: файервол выключен (по умолчанию) — таблица в форме NAT-only, и
    карантин в ней тоже: пересылки с линка сопряжения нет, на ВПС — только
    канал своей паре."""
    from awgbot.infra import nftguard
    text = nftguard.render(_spec(filter=False, **Q))
    fwd = text.split("chain forward {", 1)[1].split("\n    }", 1)[0]
    inp = text.split("chain input {", 1)[1].split("\n    }", 1)[0]
    assert "policy accept" in fwd and 'iifname { "awglink" } drop' in fwd and 'oifname { "awglink" } drop' in fwd
    assert "policy accept" in inp, "форма NAT-only закрыла вход хосту"
    lines = [ln.strip() for ln in inp.splitlines() if ln.strip()]
    assert lines[-2:] == ['iifname "awglink" ip saddr 10.99.99.2 ip daddr 10.99.99.1 tcp dport 8787 accept',
                          'iifname { "awglink" } drop'], lines
    assert "chain input" not in nftguard.render(_spec(filter=False)), "вход фильтруется без сопряжения"


def test_the_pairs_come_from_the_vps_side_address(tmp_path, monkeypatch):
    from awgbot.core import config
    from awgbot.infra.nftguard import collect
    monkeypatch.setattr(config, "AWG_DIR", str(tmp_path))
    (tmp_path / "awglink2.conf").write_text("# awg-bot: сопряжение 0123456789abcdef\n[Interface]\n"
                                            "Address = 10.99.99.5/30\nListenPort = 8443\n", encoding="utf-8")
    assert collect.quarantine_pairs(["awglink2"]) == [("awglink2", "10.99.99.5", "10.99.99.6")]


def test_the_command_is_refused_and_unstaged_when_the_firewall_fails(services, vps, monkeypatch):
    """quar-6: таблица не перерисовалась — команда не выдаётся, линк под токен
    снимается сразу, записи нет."""
    from awgbot.domain.services.types import ServiceError
    from awgbot.infra import nftguard
    monkeypatch.setattr(nftguard, "enabled", lambda: False)

    def boom(*a, **kw):
        raise nftguard.GuardError("nft: синтаксис")
    monkeypatch.setattr(nftguard, "reconcile", boom)
    with pytest.raises(ServiceError, match="команда не выдана"):
        services.gwjoin_issue()
    assert services.gwjoin(1) == {} and not vps.stamped("awglink") and vps.modes[-1] == "--rekey"


def test_a_failed_stage_is_unstaged(services, vps):
    """quar-4: --join-stage отказал на полпути — линк назад, как при отмене."""
    from awgbot.domain.services.types import ServiceError
    from tests.integration._join_common import slot_with_device
    slot_with_device(services, vps, 1)
    vps.fail["--join-stage"] = "awg-quick: нет"
    with pytest.raises(ServiceError):
        services.gwjoin_issue(slot_id=1)
    assert vps.modes[-1] == "--join-revert" and services.gwjoin(1) == {}


# ── слот в замене и «Настроить заново» (pair-1) ──────────────────────────────

@pytest.fixture()
async def replacing(services, vps, monkeypatch):
    """Слот 1 на постоянных ключах (строка в gateways есть), поверх — команда
    замены; слушатель на петле. kind — параметр через services.join_kind."""
    dev = next(d for d in services.db.list_devices(services.admin_client().id) if d.name == "NASPi")
    vps.put("awglink", 443, "127.0.0.0/30")
    services.db.gateway_add(dev.id, "awglink", 443, "127.0.0.0/30", slot_id=1)
    port = _free_port()
    monkeypatch.setattr(linkserver, "channel_port", lambda: port)
    monkeypatch.setattr(linkserver, "gw_address", lambda cidr: "127.0.0.1")
    monkeypatch.setattr(services, "gwjoin_for_addr", lambda local, peer: services.gwjoin(1))
    srvs = []

    async def start(same: bool):
        res = services.gwjoin_issue(slot_id=1, same=same)
        srv = linkserver.LinkServer(services)
        await srv.ensure()
        assert srv._bound
        srv.command = res["command"]
        srvs.append(srv)
        return srv
    yield start
    for srv in srvs:
        await srv.stop()


async def _plain_hello(srv) -> _Gw:
    """Обычный hello слота (без join), подписанный ключом из токена."""
    side = Gateway(srv.command)
    gw = _Gw(*await asyncio.open_connection(*srv._bound[0]), key=gwlink.channel_key(side.token.gw_priv_b64))
    await gw.send("hello", {"proto": gwlink.PROTO, "agent": "3.4.0", "caps": ["cfg1", "keep1"]})
    return gw


@pytest.mark.parametrize("same", [False, True], ids=["replace", "reconfigure"])
async def test_a_plain_hello_on_token_keys_of_a_replaced_slot_gets_nothing(services, replacing, same):
    """Строка слота есть (замена, «Настроить заново»), ключ канала — уже ключи
    токена: hello без join — разрыв без единого слова (ни роли, ни документа,
    ни keep), сессии слота нет."""
    srv = await replacing(same)
    gw = await _plain_hello(srv)
    assert await gw.next_msg(1.0) is None, "сессия слота в сопряжении ответила"
    assert srv._sessions.get(1) is None and not services.gwlink_session(1)
    await gw.close()


async def test_accepted_but_not_switched_is_still_barred(services, replacing):
    """Вне оси: код верный (accepted), а ВПС ещё на ключах токена — hello без
    join тоже разрыв; после переключения ВПС — обычная сессия можно."""
    srv = await replacing(False)
    side = Gateway(srv.command)
    connect_res = services.gwjoin_hello(1, side.hello())
    side.eph_in(connect_res["body"])
    assert services.gwjoin_code(1, services.gwjoin(1)["id"], side.code)["result"] == "ok"
    assert services.gwjoin_session_barred(1)
    gw = await _plain_hello(srv)
    assert await gw.next_msg(1.0) is None
    await gw.close()
    rk = services.gateway_rekey(1)
    services._rekey_save(1, {**rk, "state": "switched", "committed": True})
    assert not services.gwjoin_session_barred(1)


def test_a_stamp_without_a_record_bars_the_session_too(services, vps):
    """Записи сопряжения нет (снята сроком), а штамп в линке стоит — сессии нет."""
    from tests.integration._join_common import slot_with_device
    slot_with_device(services, vps, 1)
    assert not services.gwjoin_session_barred(1)
    services.gwjoin_issue(slot_id=1)
    services._gwjoin_save(1, {})
    assert vps.stamped("awglink") and services.gwjoin_session_barred(1)
