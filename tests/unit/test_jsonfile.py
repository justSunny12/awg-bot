"""util/jsonfile — файл состояния JSON-объектом: read_dict / load / write — и
читатели и писатели поверх него.

Что стережём: нет файла, не читается, не JSON, не UTF-8, не объект — пустой
словарь, а не исключение посреди такта; пустой файл — пустой объект; запись —
через util/fsatomic (свой временный, 0600 по умолчанию, None — права прежнего
файла), так что общее имя `<файл>.tmp` у двух писателей больше не встречается;
у каждого читателя и писателя (зона резерва и её статус открытия, пул
Telegram, согласие пары на РФ-трафик, хранилище резерва у шлюза, сопряжение и
его ход, смена ключей линка, команда подключения, метка отката обновления,
фиды транзита; режим сопряжения агента и порт канала, приколы аплинка по
записи, пул Telegram в файерволе, запасной рычаг «вернуть без резерва»,
эпоха линии у резерва при открытии, журнал транзакции аплинка) — одно
поведение.

Цена ошибки: файл со списком вместо объекта ронял `.get` у сопряжения и у
команды подключения (терминал падал трассировкой); общий `.tmp` — смесь двух
записей пула или согласия пары; журнал транзакции аплинка списком ронял
откат (прежний юнит не возвращался), пул числом — сборку файервола; файл хода
сопряжения шире 0600 — код
подтверждения читает любой пользователь шлюза; битая метка отката, снятая
молча, — админ не узнал бы об откате.
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from awgbot.util import jsonfile

pytestmark = pytest.mark.unit


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


@pytest.mark.parametrize("raw", [None, b"not json", b"[1, 2]", b"\xff\xfe", b"42", b'"s"'],
                         ids=["missing", "garbage", "list", "not-utf8", "number", "string"])
def test_read_dict_is_empty_on_anything_but_an_object(tmp_path, raw):
    p = tmp_path / "state.json"
    if raw is not None:
        p.write_bytes(raw)
    assert jsonfile.read_dict(p) == {}


def test_an_empty_file_is_an_empty_object_and_a_directory_is_empty(tmp_path):
    (tmp_path / "e.json").write_bytes(b"")
    assert jsonfile.load(tmp_path / "e.json") == {} and jsonfile.read_dict(tmp_path) == {}
    with pytest.raises(OSError):
        jsonfile.load(tmp_path / "nope")
    (tmp_path / "bad").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        jsonfile.load(tmp_path / "bad")


def test_write_is_private_by_default_keeps_unicode_and_can_keep_the_mode(tmp_path):
    p = tmp_path / "a" / "s.json"
    jsonfile.write(p, {"имя": "шлюз", "b": 1, "a": 2}, sort_keys=True)
    assert p.read_text(encoding="utf-8") == '{"a": 2, "b": 1, "имя": "шлюз"}' and _mode(p) == 0o600
    os.chmod(p, 0o644)
    jsonfile.write(p, {"x": 1}, None)
    assert _mode(p) == 0o644 and jsonfile.read_dict(p) == {"x": 1}
    os.makedirs(f"{p}.tmp")                               # остаток прерванной записи на общем имени
    jsonfile.write(p, {"y": 2})
    assert jsonfile.read_dict(p) == {"y": 2} and _mode(p) == 0o600


# ── читатели и писатели ─────────────────────────────────────────────────────

def test_zone_state_and_open_status(tmp_path, monkeypatch):
    from awgbot.infra import rsvzone
    (tmp_path / "state.json").write_text("[1]", encoding="utf-8")
    assert rsvzone.state(tmp_path / "state.json") == {} and rsvzone.read_json(tmp_path / "state.json") == {}
    st = tmp_path / "open.status"
    monkeypatch.setattr(rsvzone, "OPEN_STATUS", st)
    os.makedirs(f"{st}.tmp")
    rsvzone.open_status_write("clone_up", iface="awg0")
    assert rsvzone.open_status()["step"] == "clone_up" and _mode(st) == 0o644, "статус без секретов — права как были"


def test_the_pair_rf_consent(tmp_path, monkeypatch):
    from awgbot.core import config
    from awgbot.infra.routing import rfpeer
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    p = rfpeer.rfpeer_path()
    p.parent.mkdir(parents=True)
    os.makedirs(f"{p}.tmp")
    rfpeer.write(True, False, "2026-11-01T10:00:00")
    assert rfpeer.read() == {"offer": True, "slot": False, "at": "2026-11-01T10:00:00"} and _mode(p) == 0o600
    p.write_text("[]", encoding="utf-8")
    assert rfpeer.read() == {}


def test_the_telegram_pool(tmp_path, monkeypatch):
    from awgbot.core import config
    from awgbot.domain.services.reserve import tgpool
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    p = tgpool.pool_path()
    p.parent.mkdir(parents=True)
    os.makedirs(f"{p}.tmp")
    tgpool._write({"b": [1], "a": [2]})
    assert p.read_text(encoding="utf-8") == '{"a": [2], "b": [1]}' and _mode(p) == 0o600
    assert tgpool._read() == {"a": [2], "b": [1]}


def test_the_gateway_reserve_store(tmp_path):
    from awgbot.domain.gateway.reserve import store
    p = tmp_path / "reserve.json"
    os.makedirs(f"{p}.tmp")
    store._write({"state": "ok", "имя": "резерв"}, p)
    assert store._read(p) == {"state": "ok", "имя": "резерв"} and _mode(p) == 0o600
    p.write_text("[]", encoding="utf-8")
    assert store._read(p) == {}


def test_the_link_rekey_state(tmp_path, monkeypatch):
    from awgbot.domain.gateway.cfg.rekey import CfgRekeyMixin
    monkeypatch.setattr(CfgRekeyMixin, "_rk_path", staticmethod(lambda name: str(tmp_path / name)))
    assert CfgRekeyMixin().rekey_state() == {}
    (tmp_path / "link.txn").write_text('["x"]', encoding="utf-8")
    assert CfgRekeyMixin().rekey_state() == {}
    (tmp_path / "link.txn").write_text('{"state": "ready"}', encoding="utf-8")
    assert CfgRekeyMixin().rekey_state() == {"state": "ready"}


class _Join:
    def __new__(cls, tmp_path):
        from awgbot.domain.gateway.join import GatewayJoinMixin
        obj = type("_J", (GatewayJoinMixin,), {})()
        obj._join_file = str(tmp_path / "join.json")
        obj._join_status_file = str(tmp_path / "join.status")
        return obj


def test_the_join_file_and_its_status(tmp_path):
    j = _Join(tmp_path)
    (tmp_path / "join.json").write_text("[1]", encoding="utf-8")
    assert j.join_state() == {}
    j._join_save({"id": "x", "slot": 2, "name": "шлюз"})
    assert j.join_state()["slot"] == 2 and _mode(tmp_path / "join.json") == 0o600
    (tmp_path / "join.status").write_text("[1]", encoding="utf-8")
    j.join_wrong_in({"left": 2})                          # ход не объект — не падение, а ход заново
    st = json.loads((tmp_path / "join.status").read_text(encoding="utf-8"))
    assert st["state"] == "wrong" and st["left"] == 2 and st["code"] == ""
    assert _mode(tmp_path / "join.status") == 0o600, "в ходе сопряжения код — 0600"


def test_the_join_command_reads_a_broken_status_as_none(tmp_path, monkeypatch):
    from awgbot.domain.gateway import join
    from tools import join as cli
    monkeypatch.setattr(join, "STATUS_FILE", str(tmp_path / "join.status"))
    monkeypatch.setattr(join, "JOIN_FILE", str(tmp_path / "join.json"))
    (tmp_path / "join.status").write_text("[1]", encoding="utf-8")
    (tmp_path / "join.json").write_text("[1]", encoding="utf-8")
    assert cli._status() == {}
    assert cli.cmd_status() == 1, "список вместо объекта — «подключения нет», а не трассировка"


def test_the_rolled_back_mark_is_taken_once_and_a_broken_one_is_kept(tmp_path):
    from awgbot.domain.selfupdate import marks
    path = marks.data_path(marks.ROLLED_MARK_FILE, str(tmp_path))
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"from": "4.2.0"}')
    assert marks.pop_rolled_mark(str(tmp_path)) == {"from": "4.2.0"} and not os.path.exists(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{")
    assert marks.pop_rolled_mark(str(tmp_path)) == {} and os.path.exists(path)


def test_the_transit_feeds_on_the_server(tmp_path, monkeypatch):
    from awgbot.domain.services.gwchannel.feeds import FeedsMixin as M
    me = M.__new__(M)
    monkeypatch.setattr(M, "_lan_feeds_path", lambda self: tmp_path / "lan-feeds.json")
    assert me.gwlink_lan_feeds() == {}
    (tmp_path / "lan-feeds.json").write_bytes(b"\xff")
    assert me.gwlink_lan_feeds() == {}
    (tmp_path / "lan-feeds.json").write_text('{"domains": "x"}', encoding="utf-8")
    assert me.gwlink_lan_feeds() == {}, "фиды без отпечатка — нет фидов"
    (tmp_path / "lan-feeds.json").write_text('{"hash": "h", "domains": "x"}', encoding="utf-8")
    assert me.gwlink_lan_feeds() == {"hash": "h", "domains": "x"}


def test_the_agent_join_mode_and_the_channel_port(tmp_path, monkeypatch):
    from awgbot.core import config
    from awgbot.domain.gateway import join
    from awgbot.infra import gwguard
    from awgbot.runtime import linkclient
    from awgbot.runtime.main import join as mjoin
    jf = tmp_path / "join.json"
    monkeypatch.setattr(join, "JOIN_FILE", str(jf))
    monkeypatch.setattr(config, "ROLE", "gateway")
    monkeypatch.setattr(config, "BOT_TOKEN", "")
    monkeypatch.setattr(gwguard, "unit_env", lambda key: "9999")
    for raw in ("[1]", "{", '"pairing"'):
        jf.write_text(raw, encoding="utf-8")
        assert mjoin.join_pending() is False and linkclient.server_port() == 9999, raw
    jf.write_text('{"state": "pairing", "channel_port": "x"}', encoding="utf-8")
    assert mjoin.join_pending() is True and linkclient.server_port() == 9999, "порт не число — из юнита"
    jf.write_text('{"state": "pairing", "channel_port": 8787}', encoding="utf-8")
    assert linkclient.server_port() == 8787


def test_the_uplink_pins(tmp_path, monkeypatch):
    from awgbot.domain.gateway.reserve import uplink
    monkeypatch.setattr(uplink, "_PIN", None)
    monkeypatch.setattr(uplink, "PIN_PATH", tmp_path / "state" / "uplink-pin.json")
    uplink.PIN_PATH.parent.mkdir()
    uplink.PIN_PATH.write_text("[1]", encoding="utf-8")
    assert uplink._pins() == {}
    os.makedirs(f"{uplink.PIN_PATH}.tmp")
    uplink._pin("awgup", "203.0.113.7", 300)
    assert _mode(uplink.PIN_PATH) == 0o600
    on_disk = json.loads(uplink.PIN_PATH.read_text(encoding="utf-8"))
    assert list(on_disk) == ["awgup"] and on_disk["awgup"]["ip"] == "203.0.113.7" and on_disk["awgup"]["span"] == 300
    monkeypatch.setattr(uplink, "_PIN", None)
    assert uplink._pins()["awgup"][0] == "203.0.113.7", "прикол не перечитался с диска"


@pytest.mark.parametrize("raw", ["[1]", "{", '{"pool": 7}', '{"pool": "1.2.3.0/24"}'],
                         ids=["list", "garbage", "number", "string"])
def test_the_telegram_pool_in_the_firewall(tmp_path, monkeypatch, raw):
    from awgbot.core import config
    from awgbot.infra import nftguard
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "reserve").mkdir()
    (tmp_path / "reserve" / "tg_pool.json").write_text(raw, encoding="utf-8")
    assert nftguard.tg_pool() == nftguard.noverlap(list(nftguard.DEFAULT_TG_POOL))
    (tmp_path / "reserve" / "tg_pool.json").write_text('{"pool": ["95.161.64.0/20", "нет", "95.161.64.0/20"]}',
                                                       encoding="utf-8")
    assert "95.161.64.0/20" in nftguard.tg_pool() and "нет" not in nftguard.tg_pool()


def test_the_home_lever_request_and_its_result(tmp_path, monkeypatch):
    import time
    from awgbot.core import config
    from awgbot.runtime import rsvhome
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    rsvhome.req_path().parent.mkdir()
    rsvhome.req_path().write_text("[1]", encoding="utf-8")
    assert rsvhome._take_request() is None and not rsvhome.req_path().exists(), "запрос не объект — взят и пропущен"
    rsvhome.req_path().write_text(json.dumps({"id": "abc1", "until": time.time() + 30}), encoding="utf-8")
    assert rsvhome._take_request() == {"id": "abc1"}
    os.makedirs(f"{rsvhome.res_path()}.tmp")
    rsvhome._write_result("abc1", 3, "отказ: резерв молчит")
    assert rsvhome.res_path().read_text(encoding="utf-8") == '{"id": "abc1", "rc": 3, "text": "отказ: резерв молчит"}'
    assert _mode(rsvhome.res_path()) == 0o600


def test_the_line_epoch_known_to_the_reserve(tmp_path):
    from awgbot.util.rsvjrn import opener
    assert opener._meta_e(tmp_path, "L1") == 0
    for raw in ("[1]", "{", '{"line": "L1", "e": "x"}', '{"line": "L2", "e": 5}'):
        (tmp_path / "meta.json").write_text(raw, encoding="utf-8")
        assert opener._meta_e(tmp_path, "L1") == 0, raw
    (tmp_path / "meta.json").write_text('{"line": "L1", "e": 5}', encoding="utf-8")
    assert opener._meta_e(tmp_path, "L1") == 5


@pytest.mark.parametrize("raw", ["[1]", "{", '"x"'], ids=["list", "garbage", "string"])
def test_an_uplink_txn_journal_that_is_not_an_object_is_rolled_back_without_a_crash(tmp_path, monkeypatch, raw):
    """Журнал транзакции аплинка списком (или строкой) ронял откат на `.get`:
    файлы транзакции с ключом устройства оставались, а такт восстановления
    падал на каждом тике. Теперь — пустой журнал: юнит не трогается, файлы прочь."""
    from awgbot.domain.gateway.cfg.uplinktxn import CfgUplinkTxnMixin
    from awgbot.infra import gwguard
    monkeypatch.setattr(gwguard, "GW_ETC", str(tmp_path))
    restored = []
    monkeypatch.setattr(gwguard, "unit_restore", lambda text: restored.append(text))
    me = type("_U", (CfgUplinkTxnMixin,), {})()
    nxt, txn = me._cfg_uplink_paths()
    (tmp_path / "uplink.txn").write_text(raw, encoding="utf-8")
    (tmp_path / "uplink.next").write_text("[Interface]\n", encoding="utf-8")
    assert me._cfg_uplink_back("", txn, finish=False) is True
    assert restored == [] and not os.path.exists(txn) and not os.path.exists(nxt)
    (tmp_path / "uplink.txn").write_text('{"iface": "awgup", "unit": "[Unit]\\n"}', encoding="utf-8")
    assert me._cfg_uplink_back("", txn, finish=False) is True and restored == ["[Unit]\n"]


def test_the_join_command_writes_the_join_file_private_and_unescaped(tmp_path, monkeypatch):
    """Файл сопряжения команды подключения — 0600, JSON без экранирования
    не-ASCII, поля прежние: его читают агент (режим сопряжения) и клиент канала."""
    import types
    from awgbot.domain.gateway import join
    from awgbot.infra import gwguard
    from awgbot.util import jointoken
    from tools import join as cli
    jf = tmp_path / "awg-gw" / "join.json"
    monkeypatch.setattr(join, "JOIN_FILE", str(jf))
    monkeypatch.setattr(join, "STATUS_FILE", str(tmp_path / "join.status"))
    tok = types.SimpleNamespace(join_id="j1", slot=2, version="4.2.0", expires=4102444800, host="сервер.example",
                                channel_port=8787, link_port=51820)
    monkeypatch.setattr(jointoken, "decode", lambda text: tok)
    monkeypatch.setattr(cli, "_link_conf", lambda t: "[Interface]\n")
    seen = {}

    def link(conf, port, script=""):
        seen["text"] = jf.read_text(encoding="utf-8")
        seen["mode"] = _mode(jf)
        return False, "отказ"
    monkeypatch.setattr(gwguard, "join_link", link)
    assert cli.cmd_join("токен") == 1
    assert seen["mode"] == 0o600
    assert seen["text"] == ('{"id": "j1", "slot": 2, "version": "4.2.0", "expires": 4102444800, '
                            '"host": "сервер.example", "channel_port": 8787, "link_port": 51820, '
                            '"state": "pairing", "name": ""}')
