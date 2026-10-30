"""Процесс открытия на резерве (tools/rsvopen.py): пакет основы + точки ключа
кворума с ящиком, или код с точкой и ящиком кода, или K → хранилище гостя, клон, запись DNS, ожидание факта, отметка «ставь
вебхук».

Скрипт зоны и провайдер DNS подменены (`rsvzone.store/take`, `dnsapi`);
хранилище и каталог состояния — временные. Входной JSON — через подставной
stdin, строки хода — через stdout, ход для бота резерва — `open.status`.

Цена ошибки: процесс, оставивший ключ, точку, ящик или токен основы в stdout или в
`open.status`, — журнал юнита и чат бота резерва читает чужой администратор;
отметка «ставь вебхук» раньше факта записи — бот основы на резерве отвечает в
Telegram, пока записью DNS всё ещё владеет основа (два бота на один токен);
процесс, повисший без ответа или завершённый «успешно» после отказа, — человек
в аварию смотрит на крутящийся экран; открытие из старого пакета поверх
свежего `rep.bin` — профили, заведённые на резерве за время обслуживания,
пропадают после перезагрузки резерва.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from awgbot.core import config
from awgbot.infra import rsvzone
from awgbot.util import dnsapi, rsvkeys
from awgbot.util.rsvpkg import bot as pkgbot, seal, vpn as pkgvpn
from tools import rsvopen

pytestmark = pytest.mark.integration

TOKEN = "12345:DUMMY-token"
PRIV = "DUMMY-private-key-not-printed"


def _b64(n: int) -> str:
    import base64
    return base64.b64encode(bytes([n]) * 32).decode()


LIVE = f"""[Interface]
PrivateKey = {_b64(1)}
Address = 10.8.1.1/24
ListenPort = 51820
MTU = 1376
Jc = 4
Jmin = 10
Jmax = 50
S1 = 20
S2 = 30
H1 = 100-200
H2 = 500000100-500000900
H3 = 1000000100-1000090000
H4 = 1500000100-1999999999

[Peer]
PublicKey = {_b64(2)}
AllowedIPs = 10.8.1.2/32
"""
DNS = {"provider": "cloudflare", "name": "vpn.example.net", "token": "DUMMY-cf-token", "zone": "example.net"}


def _replica(tmp_path: Path, names=("Аня",)) -> bytes:
    from awgbot.infra.db import Database
    db = Database(str(tmp_path / "src.db"))
    db.init_schema()
    for i, name in enumerate(names):
        cid = db.create_client(name, 3, "2026-01-01T00:00:00+03:00", "2099-01-01T00:00:00+03:00", f"INV{i}")
        db.activate_client(cid, 9000 + i)
    path = db.path
    db.close()
    return pkgbot.replica(path)


class Box:
    """Всё окружение процесса открытия: ключи, пакет, хранилище, подмены."""

    def __init__(self, tmp_path, monkeypatch, capsys):
        self.tmp, self.mp, self.capsys = tmp_path, monkeypatch, capsys
        self.m_gen = 2
        self.m, self.k = rsvkeys.new_master(), seal.new_k()
        self.q = rsvkeys.QuorumGen.new(1, 2)                  # многочлен ключа кворума (порог 2)
        self.c = rsvkeys.CodeGen.new(1)                       # прямая аварийного ключа
        self.replica = _replica(tmp_path)
        self.run_dir, self.var_dir, self.state_dir = tmp_path / "run", tmp_path / "var", tmp_path / "state"
        self.calls: list[tuple] = []
        self.fact = True
        self.record_error: Exception | None = None
        self.take_error: Exception | None = None
        self.store_error: Exception | None = None
        self.at_fact_wait: dict = {}
        mp = monkeypatch
        mp.setattr(config, "DATA_DIR", tmp_path / "data")
        for name, val in (("RUN_DIR", self.run_dir), ("VAR_DIR", self.var_dir), ("STATE_DIR", self.state_dir),
                          ("OPEN_STATUS", self.state_dir / "open.status"), ("REP_PATH", self.var_dir / "rep.bin"),
                          ("K_FILE", self.run_dir / "k"), ("WEBHOOK_FILE", self.run_dir / "webhook.json"),
                          ("TG_POOL_FILE", self.run_dir / "tg_pool.json"), ("TG_TAKE_FILE", self.run_dir / "tg_take")):
            mp.setattr(rsvzone, name, val)
        mp.setattr(rsvzone, "store", self._store)
        mp.setattr(rsvzone, "take", self._take)
        mp.setattr(rsvopen, "_make_cert", self._cert)
        mp.setattr(rsvopen, "set_record", self._set_record)
        mp.setattr(rsvopen, "wait_fact", self._wait_fact)
        self.write_package()

    # подмены
    def _store(self):
        if self.store_error:
            raise self.store_error
        self.run_dir.mkdir(parents=True, exist_ok=True)
        for sub in ("data", "conf", "awg"):
            (self.run_dir / sub).mkdir(exist_ok=True)
        self.calls.append(("store",))

    def _take(self, conf, **kw):
        if self.take_error:
            raise self.take_error
        self.calls.append(("take", conf, kw))
        return "take: ok"

    @staticmethod
    def _cert(self_host, out_dir):
        key, crt = os.path.join(out_dir, "webhook.key"), os.path.join(out_dir, "webhook.crt")
        for p in (key, crt):
            Path(p).write_text("DUMMY-cert-file")
        return key, crt

    def _set_record(self, dns, ip):
        self.calls.append(("set_record", dict(dns), ip))
        if self.record_error:
            raise self.record_error

    def _wait_fact(self, name, ip, budget_s=0):
        self.at_fact_wait = {"tg_take": rsvzone.TG_TAKE_FILE.exists(), "step": rsvzone.open_status().get("step")}
        self.calls.append(("wait_fact", name, ip))
        return self.fact

    # пакет
    def write_package(self, *, pkg_gen=5, parts=None, env=None, dns=DNS, k=None, m=None, lists=None):
        iface = pkgvpn.build([("awg0", LIVE)], admin=["10.8.1.2"], dns=["10.8.1.1"], upstreams=["198.51.100.53"],
                             tz="Asia/Yekaterinburg")
        bot_part = {"db": self.replica.hex(), "conf": {"limits": {"limits": {"default_days": 5}},
                                                       "app": {"client_config": {"server_name": "Альфа"}}},
                    "app_guest": {"network": {"server_host": "203.0.113.7", "server_port": 51820}, "awg": {"protocol_id": 2}},
                    "env": env if env is not None else {"BOT_TOKEN": TOKEN, "ADMIN_ID": 77}}
        if lists is not None:
            bot_part["lists"] = lists
        blob = seal.seal(parts or {"vpn": iface, "bot": bot_part}, k=k or self.k, m=m or self.m, pkg_gen=pkg_gen,
                         m_gen=self.m_gen, dns=dns)
        path = config.DATA_DIR / "reserve" / "guest.pkg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        self.blob = blob
        return blob

    # вход
    def points(self, *slots) -> dict:
        """Вход по точкам: точка резерва многочлена + точки шлюзов слотов, ящик кворума."""
        pts = [self.q.reserve_point()] + [self.q.gateway_point(s) for s in slots]
        return {"qpoints": [[x, y.hex()] for x, y in pts], "qbox": self.q.qbox(self.m).hex()}

    def by_code(self, code: str | None = None) -> dict:
        """Вход по коду: код, точка резерва прямой `Mc`, ящик кода."""
        x, y = self.c.reserve_point()
        return {"code": self.c.code() if code is None else code, "cpoint": [x, y.hex()], "cbox": self.c.cbox(self.m).hex()}

    def open(self, **data) -> int:
        data.setdefault("target_ip", "203.0.113.20")
        data.setdefault("webhook_port", 443)
        data.setdefault("self_host", "203.0.113.20")
        self.mp.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
        self.capsys.readouterr()
        rc = rsvopen.main()
        self.out = self.capsys.readouterr().out
        return rc

    def open_awaiting(self, second, *, budget_s: float = 5.0, **data) -> int:
        """Переезд кнопкой: вход `await_k` первой строкой в трубу, как пишет бот
        резерва; `second(eph_pub, oid)` — что бот пишет второй строкой, когда
        процесс дал разовый ключ: (строка | None, закрыть ли трубу). Срок
        ожидания конверта — `budget_s` (в жизни K_WAIT_S)."""
        import threading
        import time
        data.setdefault("target_ip", "203.0.113.20")
        data.setdefault("webhook_port", 443)
        data.setdefault("self_host", "203.0.113.20")
        data["await_k"] = True
        r, w = os.pipe()
        reader = os.fdopen(r, "r")
        self.mp.setattr(sys, "stdin", reader)
        os.write(w, (json.dumps(data) + "\n").encode())
        real = rsvopen.await_sealed_k
        self.mp.setattr(rsvopen, "await_sealed_k", lambda gen, budget_s=budget_s: real(gen, budget_s=budget_s))
        self.eph_status: dict = {}
        state = {"open": True}

        def feed():
            deadline = time.monotonic() + 5
            st: dict = {}
            while time.monotonic() < deadline:
                st = rsvzone.open_status()
                if st.get("step") == "eph" or st.get("step") == "fail":
                    break
                time.sleep(0.01)
            self.eph_status = dict(st)
            if st.get("step") != "eph":
                return
            line, close = second(bytes.fromhex(str(st.get("eph") or "")), str(st.get("oid") or ""))
            if line is not None:
                os.write(w, (line + "\n").encode())
            if close:
                state["open"] = False
                os.close(w)
        t = threading.Thread(target=feed, daemon=True)
        t.start()
        self.capsys.readouterr()
        try:
            rc = rsvopen.main()
        finally:
            t.join(5)
            if state["open"]:
                os.close(w)
            reader.close()
        self.out = self.capsys.readouterr().out
        return rc

    @property
    def steps(self) -> list[str]:
        return [ln.split()[0] for ln in self.out.splitlines() if ln.strip()]

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture()
def box(tmp_path, monkeypatch, capsys):
    return Box(tmp_path, monkeypatch, capsys)


STEPS_OK = ["m", "k", "parts", "store", "clone_up", "record_set", "record_fact", "done"]


# ── успех ────────────────────────────────────────────────────────────────────

def test_quorum_points_open_the_package_set_the_record_wait_for_the_fact_and_only_then_mark_the_webhook(box):
    assert box.open(**box.points(1), dns=None) == 0, box.out
    assert box.steps == STEPS_OK, box.out
    assert box.kinds() == ["store", "take", "set_record", "wait_fact"]
    assert box.at_fact_wait["tg_take"] is False, "вебхук отмечен раньше факта записи"
    assert rsvzone.TG_TAKE_FILE.exists(), "после факта записи вебхук не отмечен"
    st = rsvzone.open_status()
    assert st["step"] == "done"
    rec = [c for c in box.calls if c[0] == "set_record"][0]
    assert rec[2] == "203.0.113.20" and rec[1]["provider"] == "cloudflare" and rec[1]["token"] == "DUMMY-cf-token", \
        "конверт DNS не раскрыт ключом M"


def test_the_emergency_code_with_the_code_point_and_box_opens_the_package_too(box):
    assert box.open(**box.by_code()) == 0, box.out
    assert box.steps == STEPS_OK


def test_a_given_k_opens_without_m_and_needs_the_dns_from_the_input(box):
    assert box.open(k=box.k.hex(), dns=DNS) == 0, box.out
    assert box.steps == ["k_given", "parts", "store", "clone_up", "record_set", "record_fact", "done"]
    assert [c for c in box.calls if c[0] == "set_record"][0][1]["name"] == "vpn.example.net"


def test_a_given_k_without_dns_in_the_input_changes_no_record_and_still_opens(box):
    assert box.open(k=box.k.hex()) == 0, box.out
    assert "set_record" not in box.kinds() and box.steps[-1] == "done"
    st = rsvzone.open_status()
    assert st["step"] == "done" and st.get("record") == "skipped"


@pytest.mark.parametrize("extra", [{"set_record": False}, {"target_ip": ""}])
def test_when_the_record_is_not_ours_to_change_the_process_ends_without_dns_and_without_webhook_mark(box, extra):
    assert box.open(**box.points(1), **extra) == 0, box.out
    assert "set_record" not in box.kinds() and not rsvzone.TG_TAKE_FILE.exists()
    assert box.steps[-1] == "done"


def test_the_webhook_mark_for_a_button_move_is_put_without_waiting_for_a_record(box):
    assert box.open(k=box.k.hex(), tg_take=True) == 0, box.out
    assert rsvzone.TG_TAKE_FILE.exists()


# ── что легло в хранилище ────────────────────────────────────────────────────

def test_the_store_gets_env_conf_base_key_webhook_and_pool_with_private_rights(box):
    assert box.open(**box.points(1), tg_pool=["198.51.100.0/24"], set_record=False) == 0, box.out
    run = box.run_dir
    assert (run / "env").read_text() == f"BOT_TOKEN={TOKEN}\nADMIN_ID=77\n"
    assert (run / "data" / "bot.db").read_bytes() == box.replica
    assert rsvzone.K_FILE.read_text() == box.k.hex()
    import yaml
    app = yaml.safe_load((run / "conf" / "app.yaml").read_text())
    assert app["guest"] is True and app["role"] == "client"
    assert app["awg"]["interface"] == "awg0" and app["awg"]["conf_dir"] == str(run / "awg") and app["awg"]["protocol_id"] == 2
    assert app["network"]["server_host"] == "203.0.113.7" and app["network"]["server_port"] == 51820, "адрес основы для ссылок пользователей — из пакета"
    assert app["client_config"]["server_name"] == "Альфа"
    assert app["routing"]["dnsmasq_conf"] == str(run / "dnsmasq-rf.conf") and app["routing"]["dnsmasq_service"] == "awg-bot-guest-dns.service"
    assert app["timezone"] == "Asia/Yekaterinburg"
    assert yaml.safe_load((run / "conf" / "limits.yaml").read_text()) == {"limits": {"default_days": 5}}
    wh = json.loads(rsvzone.WEBHOOK_FILE.read_text())
    assert wh["port"] == 443 and wh["host"] == "203.0.113.20" and len(wh["path"]) >= 20 and len(wh["secret"]) >= 30
    assert json.loads(rsvzone.TG_POOL_FILE.read_text()) == {"pool": ["198.51.100.0/24"]}
    for p in (run / "env", run / "data" / "bot.db", rsvzone.K_FILE, rsvzone.WEBHOOK_FILE, run / "conf" / "app.yaml"):
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600, f"{p.name}: права {oct(os.stat(p).st_mode)}"


def test_the_webhook_path_and_secret_are_new_each_time(box):
    box.open(**box.points(1))
    first = json.loads(rsvzone.WEBHOOK_FILE.read_text())
    box.open(**box.points(1))
    second = json.loads(rsvzone.WEBHOOK_FILE.read_text())
    assert (first["path"], first["secret"]) != (second["path"], second["secret"])


def test_the_pool_of_the_store_is_the_hosts_firewall_pool_when_the_input_has_none(box, monkeypatch):
    """Слушатель вебхука и DNAT хоста обязаны пускать одни и те же адреса: пул
    хранилища — тот, что рисует файервол хоста резерва."""
    from awgbot.infra import nftguard
    monkeypatch.setattr(nftguard, "tg_pool", lambda: ["149.154.160.0/20", "91.108.4.0/22", "198.51.100.0/24"])
    box.open(**box.points(1), set_record=False)
    assert json.loads(rsvzone.TG_POOL_FILE.read_text())["pool"] == ["149.154.160.0/20", "91.108.4.0/22", "198.51.100.0/24"]


def test_without_any_pool_the_core_of_telegram_is_used(box, monkeypatch):
    from awgbot.infra import nftguard
    monkeypatch.setattr(nftguard, "tg_pool", lambda: (_ for _ in ()).throw(OSError("нет")))
    box.open(**box.points(1), set_record=False)
    assert json.loads(rsvzone.TG_POOL_FILE.read_text())["pool"] == list(rsvopen.DEFAULT_TG_POOL)


def test_the_clone_is_started_from_the_checked_part_without_hooks_and_with_the_zone_fields(box):
    box.open(**box.points(1), channel_port=8789)
    _kind, conf, kw = [c for c in box.calls if c[0] == "take"][0]
    assert "PostUp" not in conf and "DNS =" not in conf and f"PrivateKey = {_b64(1)}" in conf
    assert (kw["iface"], kw["addr"], kw["dns"], kw["upstreams"], kw["admin"], kw["channel_port"]) == (
        "awg0", "10.8.1.1/24", ["10.8.1.1"], ["198.51.100.53"], ["10.8.1.2"], 8789)


def test_a_stale_webhook_mark_from_an_earlier_opening_is_removed_before_the_new_one(box):
    box.run_dir.mkdir(parents=True)
    rsvzone.TG_TAKE_FILE.write_text("1")
    box.open(**box.points(1), set_record=False)
    assert not rsvzone.TG_TAKE_FILE.exists(), "отметка прежнего открытия разрешила бы вебхук до факта"


# ── ничего открытого наружу ──────────────────────────────────────────────────

def test_neither_stdout_nor_the_status_file_carries_keys_points_boxes_tokens_or_the_code(box):
    pts, bc = box.points(1), box.by_code()
    secrets_ = [box.k.hex(), box.m.hex(), TOKEN, _b64(1), "DUMMY-cf-token", bc["code"], bc["cpoint"][1], bc["cbox"],
                pts["qpoints"][0][1], pts["qpoints"][1][1], pts["qbox"], box.q.mq.hex(), box.c.mc.hex()]
    variants = [dict(**pts, dns=None), dict(**bc), dict(k=box.k.hex(), dns=DNS),
                dict(qpoints=[[1, "00" * 16], [2, "11" * 16]], qbox=pts["qbox"])]
    for data in variants:
        box.open(**data)
        status = (box.state_dir / "open.status").read_text()
        for text in (box.out, status):
            leaked = [x for x in secrets_ if x in text]
            assert not leaked, f"в выводе {sorted(data)}: {leaked}\n{text}"


def test_every_fail_line_is_one_line_without_a_stack_trace(box):
    box.open(qpoints=[[1, "00" * 16], [2, "00" * 16]], qbox="00" * rsvkeys.BOX_LEN)
    lines = box.out.splitlines()
    assert len(lines) == 1 and lines[0].startswith("fail ") and "Traceback" not in box.out


# ── отказы ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("data, word", [
    ({}, "нет ни точек"),
    ({"qpoints": [[1, "00" * 16]], "qbox": "00" * 56}, "мало или они одной точки"),
    ({"qpoints": [[1, "zz"], [2, "00" * 16]], "qbox": "00" * 56}, "доля не того вида"),
    ({"qpoints": [[0, "00" * 16], [2, "00" * 16]], "qbox": "00" * 56}, "доля не того вида"),
    ({"qpoints": [[300, "00" * 16], [2, "00" * 16]], "qbox": "00" * 56}, "доля не того вида"),
    ({"qpoints": [[1, "00" * 16], [1, "11" * 16]], "qbox": "00" * 56}, "одной точки"),
    ({"qpoints": [[1, "00" * 16], [2, "00" * 15]], "qbox": "00" * 56}, "ключ кворума не собрался"),
    ({"qpoints": [[1, "00" * 16], [2, "11" * 16]], "qbox": "00" * 56}, "ключ кворума не собрался"),
    ({"code": "не код", "cpoint": [1, "00" * 16], "cbox": "00" * 56}, "аварийный код"),
    ({"shares": [[1, "00" * 16], [2, "11" * 16]]}, "нет ни точек"),
    ({"share": [1, "00" * 16], "code": "AAAA-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA"}, "нет ни точек"),
    ({"code": "AAAA-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA", "cpoint": [1, "00" * 16]}, "нет ни точек"),
    ({"k": "zz"}, "не hex"),
    ({"k": "11" * 5}, "не той длины"),
])
def test_unusable_key_material_is_a_clean_failure_before_anything_is_touched(box, data, word):
    assert box.open(**data) == 1
    assert box.steps == ["fail"] and word in box.out, box.out
    assert box.calls == [], "до разбора ключей зона уже тронута"
    assert not box.run_dir.exists() and not rsvzone.TG_TAKE_FILE.exists()
    assert rsvzone.open_status()["step"] == "fail" and word in rsvzone.open_status()["reason"]


def test_boxes_of_another_m_do_not_open_the_package(box):
    """Ящики прежнего поколения `M` (резерв не получил новые qkeys): `M` собирается, но не
    тот — пакет нового поколения не открыт, зона не тронута."""
    old_m = rsvkeys.new_master()
    pts = box.points(1)
    assert box.open(qpoints=pts["qpoints"], qbox=box.q.qbox(old_m).hex()) == 1
    assert "ключ пакета" in box.out and box.calls == []
    x, y = box.c.reserve_point()
    assert box.open(code=box.c.code(), cpoint=[x, y.hex()], cbox=box.c.cbox(old_m).hex()) == 1
    assert "ключ пакета" in box.out and box.calls == []


def test_a_code_of_another_generation_does_not_open_the_package(box):
    """Код прежней прямой `Mc` (сменённый) с точкой и ящиком текущей — отказ, зона не тронута."""
    other = rsvkeys.CodeGen.new(9)
    assert box.open(**box.by_code(other.code())) == 1
    assert "аварийный код" in box.out and box.calls == []


def test_a_mistyped_code_is_refused_with_its_own_message(box):
    code = box.c.code()
    bad = code[:-1] + ("0" if code[-1] != "0" else "1")
    assert box.open(**box.by_code(bad)) == 1
    assert "аварийный код" in box.out


def test_a_k_that_is_not_the_key_of_the_package_fails_on_the_parts(box):
    assert box.open(k=seal.new_k().hex()) == 1
    assert "часть" in box.out and box.calls == []


def test_a_missing_package_fails_without_a_trace(box):
    (config.DATA_DIR / "reserve" / "guest.pkg").unlink()
    assert box.open(**box.points(1)) == 1
    assert "пакета нет" in box.out


def test_a_damaged_package_fails_on_the_header(box):
    path = config.DATA_DIR / "reserve" / "guest.pkg"
    blob = bytearray(path.read_bytes())
    blob[200] ^= 1
    path.write_bytes(bytes(blob))
    assert box.open(**box.points(1)) == 1
    assert "пакет:" in box.out and box.calls == []


@pytest.mark.parametrize("raw", ["", "{oops", "[1, 2]", "null"])
def test_a_garbage_input_is_a_clean_failure(box, monkeypatch, raw):
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    box.capsys.readouterr()
    rc = rsvopen.main()
    out = box.capsys.readouterr().out
    assert rc == 1 and out.startswith("fail "), out


def test_a_package_without_the_bot_part_fails(box):
    box.write_package(parts={"vpn": pkgvpn.build([("awg0", LIVE)], admin=[], dns=[], upstreams=[], tz="")})
    assert box.open(**box.points(1)) == 1 and "нет части бота" in box.out and box.calls == []


def test_a_package_with_a_vpn_part_that_does_not_pass_the_check_fails_before_the_zone(box):
    bad = pkgvpn.build([("awg0", LIVE)], admin=[], dns=[], upstreams=[], tz="")
    bad["ifaces"][0]["obf"] = {"PostUp": "rm -rf /"}
    box.write_package(parts={"vpn": bad, "bot": {"db": "00", "conf": {}, "env": {"BOT_TOKEN": TOKEN, "ADMIN_ID": 1}}})
    assert box.open(**box.points(1)) == 1 and box.calls == []


@pytest.mark.parametrize("env", [{"BOT_TOKEN": "", "ADMIN_ID": 77}, {"BOT_TOKEN": TOKEN, "ADMIN_ID": 0}, {}])
def test_a_package_without_a_token_or_admin_fails_and_the_clone_is_not_started(box, env):
    box.write_package(env=env)
    assert box.open(**box.points(1)) == 1
    assert "токена" in box.out and "take" not in box.kinds()
    assert not rsvzone.TG_TAKE_FILE.exists()


def test_an_empty_replica_fails(box):
    box.replica = b""
    box.write_package()
    assert box.open(**box.points(1)) == 1 and "реплика пуста" in box.out


@pytest.mark.parametrize("which", ["store_error", "take_error"])
def test_a_zone_failure_is_reported_with_its_reason_and_no_record_is_touched(box, which):
    setattr(box, which, rsvzone.ZoneError("клон не создался"))
    assert box.open(**box.points(1)) == 1
    assert box.out.strip().splitlines()[-1] == "fail зона: клон не создался"
    assert "set_record" not in box.kinds() and not rsvzone.TG_TAKE_FILE.exists()
    assert rsvzone.open_status()["step"] == "fail"


def test_a_failed_record_update_is_a_failure_and_the_webhook_is_not_marked(box):
    box.record_error = dnsapi.DnsError("токен не принят")
    assert box.open(**box.points(1)) == 1
    assert "запись DNS" in box.out and "токен не принят" in box.out
    assert "wait_fact" not in box.kinds() and not rsvzone.TG_TAKE_FILE.exists()


def test_a_record_that_never_shows_up_is_a_failure_and_the_webhook_is_not_marked(box):
    box.fact = False
    assert box.open(**box.points(1)) == 1
    assert "факт записи" in box.out and box.steps[-1] == "fail"
    assert "record_fact" not in box.steps and not rsvzone.TG_TAKE_FILE.exists()


def test_a_crash_inside_the_process_is_a_failure_line_not_a_traceback(box, monkeypatch):
    monkeypatch.setattr(rsvopen, "fill_store", lambda *a, **k: (_ for _ in ()).throw(KeyError("неожиданно")))
    assert box.open(**box.points(1)) == 1
    assert box.out.startswith("m\nk\nparts\n") and "fail KeyError" in box.out and "Traceback" not in box.out


# ── rep.bin новее пакета ─────────────────────────────────────────────────────

def _snapshot(box, *, pkg_gen, names, key=None):
    snapdir = box.tmp / "snap"
    snapdir.mkdir(exist_ok=True)
    db = _replica(snapdir, names=names)
    blob = seal.snapshot_seal(key or box.k, {"pkg_gen": pkg_gen, "at": "x", "db": db.hex()})
    box.var_dir.mkdir(parents=True, exist_ok=True)
    rsvzone.REP_PATH.write_bytes(blob)
    return db


def _bot_db(box) -> list[str]:
    con = sqlite3.connect(box.run_dir / "data" / "bot.db")
    try:
        return sorted(r[0] for r in con.execute("SELECT name FROM clients WHERE is_service = 0"))
    finally:
        con.close()


def test_a_snapshot_taken_after_the_package_is_the_base_of_the_guest_after_a_reboot(box):
    _snapshot(box, pkg_gen=5, names=("Аня", "Вера-за-время-обслуживания"))
    assert box.open(**box.points(1), set_record=False) == 0, box.out
    assert "Вера-за-время-обслуживания" in _bot_db(box), "профиль, заведённый на резерве, потерян после перезагрузки"
    assert rsvzone.open_status()["step"] == "done"
    store_line = [ln for ln in box.out.splitlines() if ln.startswith("store")]
    assert store_line, box.out


def test_a_snapshot_older_than_the_package_is_ignored(box):
    _snapshot(box, pkg_gen=4, names=("Аня", "Старый-снимок"))
    assert box.open(**box.points(1), set_record=False) == 0
    assert _bot_db(box) == ["Аня"]


def test_a_snapshot_under_another_key_is_ignored(box):
    _snapshot(box, pkg_gen=9, names=("Аня", "Чужой-снимок"), key=seal.new_k())
    assert box.open(**box.points(1), set_record=False) == 0
    assert _bot_db(box) == ["Аня"]


def test_a_corrupt_snapshot_is_ignored(box):
    box.var_dir.mkdir(parents=True)
    rsvzone.REP_PATH.write_bytes(b"\x00" * 100)
    assert box.open(**box.points(1), set_record=False) == 0
    assert _bot_db(box) == ["Аня"]


def test_a_snapshot_with_the_same_generation_as_the_package_wins(box):
    _snapshot(box, pkg_gen=5, names=("Аня", "Из-снимка-того-же-поколения"))
    box.open(**box.points(1), set_record=False)
    assert "Из-снимка-того-же-поколения" in _bot_db(box)


# ── сертификат вебхука на настоящем openssl ──────────────────────────────────

def _openssl_can_san() -> bool:
    if not shutil.which("openssl"):
        return False
    r = subprocess.run(["openssl", "req", "-help"], capture_output=True, text=True)
    return "-addext" in (r.stdout + r.stderr)


@pytest.mark.skipif(not _openssl_can_san(), reason="openssl с -addext нужен для проверки SAN")
@pytest.mark.parametrize("host, san", [("203.0.113.20", "IP Address:203.0.113.20"), ("reserve.example.net", "DNS:reserve.example.net")])
def test_the_webhook_certificate_names_the_reserve_address_in_san_and_the_key_is_private(tmp_path, host, san):
    key, crt = rsvopen._make_cert(host, str(tmp_path))
    text = subprocess.run(["openssl", "x509", "-in", crt, "-noout", "-text"], capture_output=True, text=True).stdout
    assert san in text, text
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600


def test_a_certificate_that_does_not_build_is_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", "openssl: нет".encode()))
    with pytest.raises(rsvopen.OpenError, match="сертификат"):
        rsvopen._make_cert("203.0.113.20", str(tmp_path))


# ── ожидание факта ───────────────────────────────────────────────────────────

def test_the_wait_for_the_fact_polls_until_the_authoritative_answer_has_the_address(monkeypatch):
    answers = iter([[], ["198.51.100.1"], dnsapi.DnsError("сервер молчит"), ["203.0.113.20"]])

    def fake(name):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(dnsapi, "read_authoritative", fake)
    monkeypatch.setattr(rsvopen.time, "sleep", lambda s: None)
    assert rsvopen.wait_fact("vpn.example.net", "203.0.113.20", budget_s=60) is True


def test_the_wait_for_the_fact_gives_up_at_the_budget(monkeypatch):
    monkeypatch.setattr(dnsapi, "read_authoritative", lambda name: ["198.51.100.1"])
    clock = {"t": 0.0}
    monkeypatch.setattr(rsvopen.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(rsvopen.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    assert rsvopen.wait_fact("vpn.example.net", "203.0.113.20", budget_s=100) is False
    assert 100 <= clock["t"] < 100 + max(rsvopen.FACT_POLL_S), "ожидание вышло за бюджет"


def test_the_record_is_set_through_the_provider_chosen_by_the_config(monkeypatch):
    seen = []

    class Prov:
        def set(self, cfg, ip):
            seen.append((cfg["name"], ip))
    monkeypatch.setattr(dnsapi, "provider_for", lambda cfg: Prov())
    rsvopen.set_record({"provider": "cloudflare", "name": "vpn.example.net"}, "203.0.113.20")
    assert seen == [("vpn.example.net", "203.0.113.20")]


# ── лежащий в основе контракт со скриптом зоны ───────────────────────────────

def test_the_clone_config_has_only_keys_that_awg_setconf_accepts_or_the_script_strips_the_rest():
    """`awg setconf` (как `wg setconf`) принимает только [Interface] с PrivateKey, ListenPort и
    ключами обфускации и [Peer]; Address, MTU, DNS, Table — расширения awg-quick, на них setconf
    отвечает «Line unrecognized». Конфиг клона от rsvopen несёт Address и MTU: скрипт зоны обязан
    снять их перед setconf (awg-quick strip или своя фильтрация)."""
    iface = pkgvpn.check(pkgvpn.build([("awg0", LIVE)], admin=[], dns=[], upstreams=[], tz=""))["ifaces"][0]
    conf = pkgvpn.clone_conf(iface)
    quick_only = {"Address", "MTU", "DNS", "Table", "PostUp", "PostDown", "PreUp", "PreDown", "SaveConfig"}
    present = sorted({ln.split("=")[0].strip() for ln in conf.splitlines() if "=" in ln} & quick_only)
    script = (Path(__file__).resolve().parents[2] / "install" / "reserve-zone-setup.sh").read_text()
    take = script[script.index("cmd_take() {"):script.index("cmd_restart_clone() {")]
    # снятие — в общей функции write_setconf (её зовут take и restart-clone)
    setconf = script[script.index("write_setconf() {"):script.index("cmd_take() {")] if "write_setconf \"$iface\"" in take else ""
    strips = any(w in body for body in (take, setconf) for w in ("awg-quick strip", "grep -v", "sed "))
    assert not present or strips, f"в конфиге клона {present}, а перед `awg setconf` скрипт зоны их не снимает"


# ── кэш списков РФ-доступа — гостю ──────────────────────────────────────────

def test_the_routing_lists_of_the_package_land_in_the_guest_data_with_private_rights(box):
    """Источники списков гостю недоступны: без кэша из пакета маршрутизация
    в зоне шла бы с пустым базовым списком — РФ-доступа у пользователей основы нет."""
    box.write_package(lists={"tun_domains": ["gosuslugi.ru", "nalog.ru"], "tun_subnets": ["198.51.100.0/24"]})
    assert box.open(**box.points(1), set_record=False) == 0, box.out
    data = box.run_dir / "data"
    assert (data / "routing-tun_domains.lst").read_text() == "gosuslugi.ru\nnalog.ru\n"
    assert (data / "routing-tun_subnets.lst").read_text() == "198.51.100.0/24\n"
    for name in ("routing-tun_domains.lst", "routing-tun_subnets.lst"):
        assert stat.S_IMODE(os.stat(data / name).st_mode) == 0o600


def test_a_list_kind_that_is_not_a_plain_name_is_not_written(box):
    """Вид списка — часть имени файла: «../x» и прочее — мимо каталога данных гостя."""
    box.write_package(lists={"../../etc/x": ["a"], "a b": ["b"], "ok_1": ["c"], "bad": "не список"})
    assert box.open(**box.points(1), set_record=False) == 0, box.out
    names = sorted(p.name for p in (box.run_dir / "data").iterdir() if p.name.startswith("routing-"))
    assert names == ["routing-ok_1.lst"], names
    assert not (box.tmp / "etc").exists()


def test_a_package_without_lists_opens_as_before(box):
    """Пакет прежней версии без части списков — открывается, файлов списков нет."""
    assert box.open(**box.points(1), set_record=False) == 0, box.out
    assert not [p for p in (box.run_dir / "data").iterdir() if p.name.startswith("routing-")]


# ── имена наборов настроек и секреты в памяти процесса (ревью зоны) ─────────

@pytest.mark.parametrize("name", ["../x", "../../etc/cron.d/x", "conf/limits", "evil", "app_guest", ""])
def test_a_settings_set_with_an_unknown_name_is_not_written_anywhere(box, name):
    """Имя набора приходит из пакета второй стороны: не из pkgbot.CONF_FILES —
    не пишется вовсе (ни в хранилище, ни путём наружу)."""
    bot = {"db": box.replica.hex(), "env": {"BOT_TOKEN": TOKEN, "ADMIN_ID": 77},
           "conf": {"limits": {"limits": {"default_days": 5}}, name: {"x": 1}}}
    iface = pkgvpn.build([("awg0", LIVE)], admin=["10.8.1.2"], dns=["10.8.1.1"], upstreams=["198.51.100.53"], tz="")
    box.write_package(parts={"vpn": iface, "bot": bot})
    assert box.open(**box.points(1), set_record=False) == 0, box.out
    conf = box.run_dir / "conf"
    assert sorted(p.name for p in conf.iterdir()) == ["app.yaml", "limits.yaml"], \
        f"в хранилище набор с именем {name!r}: {sorted(p.name for p in conf.iterdir())}"
    written = [p for p in box.tmp.rglob("*.yaml") if p.read_text(errors="replace").strip() == "x: 1"]
    assert not written, f"набор {name!r} записан вне хранилища: {written}"


def test_every_known_settings_set_is_written(box):
    bot = {"db": box.replica.hex(), "env": {"BOT_TOKEN": TOKEN, "ADMIN_ID": 77},
           "conf": {n: {n: {"on": True}} for n in pkgbot.CONF_FILES}}
    iface = pkgvpn.build([("awg0", LIVE)], admin=["10.8.1.2"], dns=["10.8.1.1"], upstreams=["198.51.100.53"], tz="")
    box.write_package(parts={"vpn": iface, "bot": bot})
    assert box.open(**box.points(1), set_record=False) == 0, box.out
    names = {p.stem for p in (box.run_dir / "conf").iterdir()} - {"app"}
    assert names == set(pkgbot.CONF_FILES), f"белый список отсёк известные наборы: {set(pkgbot.CONF_FILES) - names}"


def _frame_of_run() -> dict:
    """Локальные переменные rsvopen.run в момент вызова подмены (её вызывающий кадр выше по стеку)."""
    f = sys._getframe(1)
    while f is not None and not (f.f_code.co_name == "run" and f.f_code.co_filename == rsvopen.__file__):
        f = f.f_back
    assert f is not None, "подмену позвали не из rsvopen.run"
    return dict(f.f_locals)


def test_m_is_dropped_before_the_zone_and_the_dns_token_after_the_record(box):
    """M (ключ всех будущих пакетов) нужен только для K и конверта DNS; токен
    провайдера — только для записи: дальше процесс ждёт до 30 минут, и всё
    это время ни M, ни токена в его памяти быть не должно."""
    seen = {}
    real_take, real_wait = box._take, box._wait_fact

    def take(conf, **kw):
        seen["take"] = _frame_of_run()
        return real_take(conf, **kw)

    def wait(name, ip, budget_s=0):
        seen["wait"] = _frame_of_run()
        seen["status"] = rsvzone.open_status()
        return real_wait(name, ip, budget_s)
    box.mp.setattr(rsvzone, "take", take)
    box.mp.setattr(rsvopen, "wait_fact", wait)
    assert box.open(**box.points(1), dns=None) == 0, box.out
    assert [c for c in box.calls if c[0] == "set_record"][0][1]["token"] == "DUMMY-cf-token", "конверт DNS не раскрыт"
    assert seen["take"].get("m") is None, "M жив в памяти процесса при подъёме клона"
    assert seen["wait"].get("m") is None
    assert seen["wait"].get("dns") == {}, f"токен провайдера жив, пока ждём факт записи: {seen['wait'].get('dns')}"
    assert seen["wait"].get("name") == "vpn.example.net", "имя записи для ожидания факта потеряно"
    assert (seen["status"].get("step"), seen["status"].get("name")) == ("record_set", "vpn.example.net"), seen["status"]


# ── переезд кнопкой: ключ пакета конвертом к разовому ключу процесса ────────

OID_LEN_HEX = seal.OID_LEN


def _envelope(box, gen=None, oid=None, k=None):
    """Что бот резерва пишет второй строкой: конверт с K от основы."""
    def second(eph_pub: bytes, real_oid: str):
        sealed = seal.seal_k_to(eph_pub, k or box.k, box.gen_of_package if gen is None else gen,
                                real_oid if oid is None else oid)
        return json.dumps({"sealed": sealed.hex()}), True
    return second


@pytest.fixture()
def abox(box):
    box.gen_of_package = 5                                    # pkg_gen у write_package по умолчанию
    return box


def test_await_k_publishes_the_ephemeral_key_and_the_opening_id_and_opens_with_the_envelope(abox):
    """Процесс выпускает разовую пару, кладёт публичную часть и oid в
    open.status (шаг `eph`), ждёт конверт второй строкой и дальше идёт как по
    готовому K — с шагом `k_sealed` вместо `k_given`."""
    assert abox.open_awaiting(_envelope(abox)) == 0, abox.out
    assert abox.steps == ["eph", "k_sealed", "parts", "store", "clone_up", "done"], abox.out
    st = abox.eph_status
    assert st["step"] == "eph" and len(st["eph"]) == seal.EPH_LEN * 2 and len(st["oid"]) == OID_LEN_HEX, st
    bytes.fromhex(st["eph"]), bytes.fromhex(st["oid"])
    assert "set_record" not in abox.kinds() and rsvzone.open_status()["step"] == "done"
    assert rsvzone.K_FILE.read_text() == abox.k.hex(), "ключ из конверта не дошёл до хранилища гостя"


def test_two_openings_give_different_ephemeral_keys_and_ids(abox):
    abox.open_awaiting(_envelope(abox))
    first = dict(abox.eph_status)
    abox.open_awaiting(_envelope(abox))
    second = abox.eph_status
    assert first["eph"] != second["eph"] and first["oid"] != second["oid"], \
        "разовый ключ повторился — конверт прежнего открытия подойдёт новому"


def test_neither_stdout_nor_the_status_file_carries_the_key_or_the_envelope_on_the_sealed_path(abox):
    """На пути конверта наружу уходят только публичная часть и oid: ни K, ни
    конверт, ни токен основы — ни в stdout, ни в open.status."""
    seen = []
    real_write = rsvzone.open_status_write

    def write(step, **fields):
        real_write(step, **fields)
        seen.append((abox.state_dir / "open.status").read_text())
    abox.mp.setattr(rsvzone, "open_status_write", write)
    envelopes = []

    def second(eph_pub, oid):
        line, close = _envelope(abox)(eph_pub, oid)
        envelopes.append(json.loads(line)["sealed"])
        return line, close
    assert abox.open_awaiting(second) == 0, abox.out
    secrets_ = [abox.k.hex(), abox.m.hex(), TOKEN, _b64(1)] + envelopes
    for text in [abox.out] + seen:
        leaked = [x for x in secrets_ if x in text]
        assert not leaked, f"{leaked}\n{text}"


@pytest.mark.parametrize("case, word", [
    ("eof", "ключ пакета от основы не пришёл за 2 минуты"),
    ("timeout", "ключ пакета от основы не пришёл за 2 минуты"),
    ("foreign_gen", "поколения"),
    ("foreign_oid", "не к этому процессу открытия"),
    ("foreign_key", "не тот ключ"),
    ("garbage", "не того вида"),
    ("empty", "пуст"),
])
def test_an_envelope_that_does_not_arrive_or_does_not_fit_this_opening_is_a_clean_failure(abox, case, word):
    """EOF (бот резерва перезапустился), срок, конверт к другому пакету,
    открытию или ключу — `fail` одной строкой, зона не тронута."""
    if case == "eof":
        second, budget = (lambda pub, oid: (None, True)), 5.0
    elif case == "timeout":
        second, budget = (lambda pub, oid: (None, False)), 0.3
    elif case == "foreign_gen":
        second, budget = _envelope(abox, gen=6), 5.0
    elif case == "foreign_oid":
        second, budget = _envelope(abox, oid="f" * OID_LEN_HEX), 5.0
    elif case == "foreign_key":
        def second(pub, oid):
            _p, other = seal.new_eph()
            return _envelope(abox)(other, oid)
        budget = 5.0
    elif case == "garbage":
        second, budget = (lambda pub, oid: ("не json", True)), 5.0
    else:
        second, budget = (lambda pub, oid: (json.dumps({"sealed": ""}), True)), 5.0
    assert abox.open_awaiting(second, budget_s=budget) == 1
    assert abox.steps == ["eph", "fail"] and word in abox.out, abox.out
    assert abox.calls == [], "зона тронута до получения ключа"
    assert not rsvzone.K_FILE.exists() and not rsvzone.TG_TAKE_FILE.exists()
    st = rsvzone.open_status()
    assert st["step"] == "fail" and word in st["reason"], st
    assert "Traceback" not in abox.out


def test_an_envelope_with_a_key_that_is_not_the_package_key_fails_on_the_parts(abox):
    assert abox.open_awaiting(_envelope(abox, k=seal.new_k())) == 1
    assert abox.steps == ["eph", "k_sealed", "fail"] and "часть" in abox.out, abox.out
    assert abox.calls == []


def test_the_manual_run_still_reads_a_multi_line_json_input(box):
    """Ручной `awg-bot reserve open` подаёт JSON с переводами строк: первая
    строка не разбирается сама по себе — вход дочитывается до конца."""
    data = {"k": box.k.hex(), "target_ip": "203.0.113.20", "webhook_port": 443, "self_host": "203.0.113.20"}
    box.mp.setattr(sys, "stdin", io.StringIO(json.dumps(data, indent=2) + "\n"))
    box.capsys.readouterr()
    rc = rsvopen.main()
    box.out = box.capsys.readouterr().out
    assert rc == 0 and box.steps[0] == "k_given", box.out
