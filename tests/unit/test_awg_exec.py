"""Unit: docker-exec обёртки awg.py с замоканными примитивами (_exec/_exec_sh/
_exec_i/_run). Фиксируем и разбор вывода, и КОМАНДЫ (валидация + конструкция).

Эти тесты НЕ используют fake_awg (он подменяет весь модуль) — работаем с
настоящими функциями awg, мокая только низкоуровневый ввод-вывод.
"""
import subprocess

import pytest

from awgbot.infra import awg

pytestmark = pytest.mark.unit

_VALID_PUB = "A" * 43 + "="            # проходит _RE_PUBKEY


def _cp(stdout=b"", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=b"")


# ── валидация ────────────────────────────────────────────────────────────────
def test_validate_key_ok_and_bad():
    assert awg._validate_key(_VALID_PUB) == _VALID_PUB
    with pytest.raises(awg.AwgError):
        awg._validate_key("not-a-key")


def test_validate_ip_ok_and_bad():
    assert awg._validate_ip("10.8.0.5") == "10.8.0.5"
    for bad in ("10.8.0", "999.1.1.1", "abc"):
        with pytest.raises(awg.AwgError):
            awg._validate_ip(bad)


# ── чтение файлов ────────────────────────────────────────────────────────────
def test_read_file_decodes(monkeypatch):
    monkeypatch.setattr(awg, "_exec", lambda args, **k: _cp(b"hello \xff world"))
    assert "hello" in awg.read_file("/x")


def test_read_clients_table_json_empty_bad(monkeypatch):
    monkeypatch.setattr(awg, "read_file", lambda p: '[{"clientId":"p"}]')
    assert awg.read_clients_table() == [{"clientId": "p"}]
    monkeypatch.setattr(awg, "read_file", lambda p: "   ")
    assert awg.read_clients_table() == []
    monkeypatch.setattr(awg, "read_file", lambda p: "{not json")
    assert awg.read_clients_table() == []


# ── show_dump ────────────────────────────────────────────────────────────────
def test_show_dump_parses(monkeypatch):
    dump = ("IFACE_PRIV\tIFACE_PUB\t43125\toff\n"
            "pubkey\tpsk\t1.2.3.4:51820\t10.8.0.2/32\t1700000000\t100\t200\t25\n")
    monkeypatch.setattr(awg, "_exec", lambda args, **k: _cp(dump.encode()))
    peers = awg.show_dump()
    assert peers and peers[0]["rx"] == 100 and peers[0]["tx"] == 200


# ── iptables block/unblock/is_blocked (команды) ──────────────────────────────
def test_is_blocked_reads_returncode(monkeypatch):
    monkeypatch.setattr(awg, "_exec", lambda args, **k: _cp(returncode=0))
    assert awg.is_blocked("10.8.0.5") is True
    monkeypatch.setattr(awg, "_exec", lambda args, **k: _cp(returncode=1))
    assert awg.is_blocked("10.8.0.5") is False


def test_block_ip_inserts_drop_when_absent(monkeypatch):
    calls = []
    monkeypatch.setattr(awg, "is_blocked", lambda ip: False)
    monkeypatch.setattr(awg, "_exec", lambda args, **k: calls.append(args) or _cp())
    awg.block_ip("10.8.0.9")
    assert calls and calls[0][:3] == ["iptables", "-I", "FORWARD"]
    assert "10.8.0.9/32" in calls[0]


def test_block_ip_noop_when_already_blocked(monkeypatch):
    calls = []
    monkeypatch.setattr(awg, "is_blocked", lambda ip: True)
    monkeypatch.setattr(awg, "_exec", lambda args, **k: calls.append(args) or _cp())
    awg.block_ip("10.8.0.9")
    assert calls == []                                       # идемпотентно


def test_unblock_ip_deletes_drop(monkeypatch):
    calls = []
    monkeypatch.setattr(awg, "is_blocked", lambda ip: True)
    monkeypatch.setattr(awg, "_exec", lambda args, **k: calls.append(args) or _cp())
    awg.unblock_ip("10.8.0.9")
    assert calls and calls[0][:3] == ["iptables", "-D", "FORWARD"]


def test_block_ip_rejects_bad_ip(monkeypatch):
    with pytest.raises(awg.AwgError):
        awg.block_ip("nope")


# ── контейнер (docker inspect / restart) ─────────────────────────────────────
def test_container_running_and_started(monkeypatch):
    monkeypatch.setattr(awg, "_inspect", lambda fmt: "true")
    assert awg.container_running() is True
    monkeypatch.setattr(awg, "_inspect", lambda fmt: "2026-01-01T00:00:00Z")
    assert awg.container_started_at() == "2026-01-01T00:00:00Z"
    monkeypatch.setattr(awg, "_inspect", lambda fmt: "12345")
    assert awg.container_pid() == 12345


def test_container_running_swallows_error(monkeypatch):
    def boom(fmt):
        raise awg.AwgError("down")
    monkeypatch.setattr(awg, "_inspect", boom)
    assert awg.container_running() is False
    assert awg.container_started_at() is None
    assert awg.container_pid() is None


def test_awg_responding(monkeypatch):
    monkeypatch.setattr(awg, "_exec", lambda args, **k: _cp())
    assert awg.awg_responding() is True

    def boom(args, **k):
        raise awg.AwgError("no daemon")
    monkeypatch.setattr(awg, "_exec", boom)
    assert awg.awg_responding() is False


def test_restart_container_runs_docker(monkeypatch):
    calls = []
    monkeypatch.setattr(awg, "_run", lambda args, **k: calls.append(args) or _cp())
    awg.restart_container()
    assert calls[0][:2] == ["docker", "restart"]


# ── ключи ────────────────────────────────────────────────────────────────────
def test_gen_keypair(monkeypatch):
    monkeypatch.setattr(awg, "_exec_sh", lambda script, **k: _cp(f"PRIVKEY\n{_VALID_PUB}\n".encode()))
    priv, pub = awg.gen_keypair()
    assert priv == "PRIVKEY" and pub == _VALID_PUB


def test_gen_keypair_bad_output(monkeypatch):
    monkeypatch.setattr(awg, "_exec_sh", lambda script, **k: _cp(b"only-one-line\n"))
    with pytest.raises(awg.AwgError):
        awg.gen_keypair()


def test_pubkey_of(monkeypatch):
    monkeypatch.setattr(awg, "_exec_i", lambda args, input_data, **k: _cp(b"DERIVEDPUB\n"))
    assert awg.pubkey_of("somepriv") == "DERIVEDPUB"


# ── add_peer / remove_peer (оркестрация конфига) ─────────────────────────────
def _stub_conf_io(monkeypatch, conf_holder):
    monkeypatch.setattr(awg, "read_file", lambda p: conf_holder["conf"])
    monkeypatch.setattr(awg, "write_file", lambda p, c: conf_holder.__setitem__("conf", c))
    monkeypatch.setattr(awg, "_backup_conf", lambda: None)
    monkeypatch.setattr(awg, "apply_config", lambda: None)


def test_add_peer_appends_and_applies(monkeypatch):
    holder = {"conf": "[Interface]\nPrivateKey = X\nListenPort = 51820\n"}
    _stub_conf_io(monkeypatch, holder)
    psk = "B" * 43 + "="
    awg.add_peer(_VALID_PUB, psk, "10.8.0.7")
    assert _VALID_PUB in holder["conf"] and "10.8.0.7/32" in holder["conf"]


def test_add_peer_idempotent(monkeypatch):
    existing = f"[Interface]\nPrivateKey = X\n\n[Peer]\nPublicKey = {_VALID_PUB}\nAllowedIPs = 10.8.0.7/32\n"
    holder = {"conf": existing}
    _stub_conf_io(monkeypatch, holder)
    awg.add_peer(_VALID_PUB, "B" * 43 + "=", "10.8.0.8")
    assert holder["conf"] == existing                       # дубликат не добавлен


def test_remove_peer_drops_block(monkeypatch):
    conf = f"[Interface]\nPrivateKey = X\n\n[Peer]\nPublicKey = {_VALID_PUB}\nAllowedIPs = 10.8.0.7/32\n"
    holder = {"conf": conf}
    _stub_conf_io(monkeypatch, holder)
    awg.remove_peer(_VALID_PUB)
    assert _VALID_PUB not in holder["conf"]


def test_remove_peer_idempotent(monkeypatch):
    conf = "[Interface]\nPrivateKey = X\n"
    holder = {"conf": conf}
    _stub_conf_io(monkeypatch, holder)
    awg.remove_peer(_VALID_PUB)
    assert holder["conf"] == conf


# ── clientsTable ─────────────────────────────────────────────────────────────
def test_clientstable_upsert_add_and_update(monkeypatch):
    store = {"table": []}
    monkeypatch.setattr(awg, "read_clients_table", lambda: list(store["table"]))
    monkeypatch.setattr(awg, "_write_table", lambda t: store.__setitem__("table", t))
    awg.clientstable_upsert("pubA", "Phone")
    assert store["table"][0]["userData"]["clientName"] == "Phone"
    awg.clientstable_upsert("pubA", "Laptop")               # обновление того же clientId
    names = [e["userData"]["clientName"] for e in store["table"]]
    assert names == ["Laptop"] and len(store["table"]) == 1


def test_clientstable_remove(monkeypatch):
    store = {"table": [{"clientId": "pubA"}, {"clientId": "pubB"}]}
    monkeypatch.setattr(awg, "read_clients_table", lambda: list(store["table"]))
    monkeypatch.setattr(awg, "_write_table", lambda t: store.__setitem__("table", t))
    awg.clientstable_remove("pubA")
    assert [e["clientId"] for e in store["table"]] == ["pubB"]


# ── read_server_params (кэш/инвалидация) ─────────────────────────────────────
def test_read_server_params_caches_and_invalidates(monkeypatch):
    sep = awg._SEP
    payload = ("[Interface]\nListenPort = 51820\n" + sep + "SRVPUB==" + sep + "PSK==").encode()
    calls = {"n": 0}

    def fake_sh(script, **k):
        calls["n"] += 1
        return _cp(payload)
    monkeypatch.setattr(awg, "_exec_sh", fake_sh)
    awg.invalidate_server_params()
    r1 = awg.read_server_params()
    assert r1["listen_port"] == 51820 and r1["server_pubkey"] == "SRVPUB=="
    awg.read_server_params()                                # в пределах TTL → кэш
    assert calls["n"] == 1
    awg.read_server_params(force=True)                      # форс → новый exec
    assert calls["n"] == 2
    awg.invalidate_server_params()
    awg.read_server_params()
    assert calls["n"] == 3
    awg.invalidate_server_params()
