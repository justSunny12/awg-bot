"""Общее для тестов пары серверов (test_reserve_*): сервер со скриптом линка
пары моделью тех же файловых эффектов, что у настоящего (его режимы
проверяет tests/unit/test_reserve_pair_script.py), и вторая сторона
сопряжения — так, как её вёл бы второй бот: эфемерный ключ, код, ключ
сопряжения тем же util/joinsas с доменом пары.

Конфиг линка пары — в config.AWG_DIR (из него сервисы читают секрет канала,
файервол — адрес второй стороны). Ключи — случайные X25519 (секрет канала
обязан быть настоящими 32 байтами), адреса — из документационных диапазонов.
"""
from __future__ import annotations

import base64
import os

import pytest

from awgbot.core import config, settings
from awgbot.domain.services.reserve import compat
from awgbot.domain.services.reserve import invite as invitemod
from awgbot.domain.services.types import ServiceError
from awgbot.util import joinsas, pairtoken, timeutil

ADMIN = config.ADMIN_ID
PEER_ADMIN = 424242
HOST_A = "vpn-a.example.org"          # пригласивший (этот сервер в тестах пригласившего)
HOST_B = "vpn-b.example.net"          # второй сервер
ADDR_B = "198.51.100.7"
ADDR_X = "203.0.113.44"
STAMP = "# awg-bot: пара "


def key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(str(text) + "==")


OBF = {"JC": "5", "JMIN": "10", "JMAX": "50", "S1": "20", "S2": "30", "S3": "40", "S4": "50",
       "H1": "100-200", "H2": "600000000-600100000", "H3": "1100000000-1100100000", "H4": "1600000000-1600100000"}


def conf(addr: str, priv: str, peer_pub: str, peer_addr: str, *, psk: str = "", endpoint: str = "",
         stamp: str = "") -> str:
    return ("# Линк пары серверов AWG (резерв). Сгенерировано reserve-pair-setup.sh.\n"
            + (f"{STAMP}{stamp}\n" if stamp else "")
            + f"[Interface]\nAddress = {addr}/30\nListenPort = 47101\nPrivateKey = {priv}\nJc = 5\n\n"
            f"[Peer]\nPublicKey = {peer_pub}\n" + (f"PresharedKey = {psk}\n" if psk else "")
            + f"AllowedIPs = {peer_addr}/32\n" + (f"Endpoint = {endpoint}\n" if endpoint else "")
            + "PersistentKeepalive = 25-35\n")


class PairHost:
    """Скрипт линка пары моделью: `runs` — (режим, окружение) по порядку,
    `fail` — {режим: причина}; конфиг — config.AWG_DIR/awgpair.conf."""

    def __init__(self, services, tmp_path, monkeypatch):
        self.awg = tmp_path / "awg"
        self.awg.mkdir(exist_ok=True)
        monkeypatch.setattr(config, "AWG_DIR", str(self.awg))
        self.runs: list[tuple[str, dict]] = []
        self.fail: dict[str, str] = {}
        self.out: dict[str, dict] = {}            # что напечатал режим (key=value)
        monkeypatch.setattr(services, "_run_pair_script", self.run)

    @property
    def conf(self):
        return self.awg / "awgpair.conf"

    def p(self, suffix: str):
        return self.conf.with_name(self.conf.name + suffix)

    @property
    def modes(self) -> list[str]:
        return [m for m, _ in self.runs]

    def env(self, mode: str) -> dict:
        return next(e for m, e in reversed(self.runs) if m == mode)

    def stamped(self) -> bool:
        return self.conf.exists() and STAMP in self.conf.read_text(encoding="utf-8")

    def run(self, mode: str, env: dict | None = None) -> dict:
        env = dict(env or {})
        self.runs.append((mode, env))
        if mode in self.fail:
            raise ServiceError(f"скрипт линка пары ({mode}) не отработал: {self.fail[mode]}")
        kv: dict = {}
        if mode == "--stage":
            vp, pp = key(), key()
            kv = {"PAIR_VPS_PRIV": vp, "PAIR_VPS_PUB": key(), "PAIR_PEER_PRIV": pp, "PAIR_PEER_PUB": key(), **OBF}
            self.conf.write_text(conf("10.99.98.2", vp, kv["PAIR_PEER_PUB"], "10.99.98.1", stamp=env["PAIR_ID"]),
                                 encoding="utf-8")
        elif mode == "--join":
            self.conf.write_text(conf("10.99.98.1", env["PAIR_PRIV"], env["PAIR_PEER_PUB"], "10.99.98.2",
                                      endpoint=env["PAIR_ENDPOINT"], stamp=env["PAIR_ID"]), encoding="utf-8")
        elif mode == "--rekey-stage":
            vp = key()
            kv = {"PAIR_VPS_PRIV": vp, "PAIR_VPS_PUB": key(), "PAIR_PEER_PRIV": key(), "PAIR_PEER_PUB": key(),
                  "PAIR_PSK": key(), **OBF}
            self.p(".next").write_text(conf("10.99.98.2", vp, kv["PAIR_PEER_PUB"], "10.99.98.1", psk=kv["PAIR_PSK"]),
                                       encoding="utf-8")
        elif mode == "--rekey-commit":
            self.p(".prev").write_text(self.conf.read_text(encoding="utf-8"), encoding="utf-8")
            self.conf.write_text(self.p(".next").read_text(encoding="utf-8"), encoding="utf-8")
            self.p(".next").unlink()
        elif mode == "--rekey-drop":
            self.p(".next").unlink(missing_ok=True)
            self.p(".prev").unlink(missing_ok=True)
        elif mode == "--set-peer":
            self.conf.write_text(conf("10.99.98.1", env["PAIR_PRIV"], env["PAIR_PEER_PUB"], "10.99.98.2",
                                      psk=env.get("PAIR_PSK", ""), endpoint=f"{HOST_A}:47101"), encoding="utf-8")
        elif mode == "--rollback":
            for p in (self.conf, self.p(".next"), self.p(".prev")):
                p.unlink(missing_ok=True)
        self.out[mode] = kv
        return kv


class Acceptor:
    """Вторая сторона, принявшая приглашение этого сервера: hello сопряжения,
    код и ключ сопряжения из pair_eph."""

    def __init__(self, pair_id: str):
        self.pair_id = pair_id
        self.priv, self.pub = joinsas.new_pair()
        self.code = ""
        self.kp = b""

    def hello(self, *, addr: str = ADDR_B, gw: bool = True, **over) -> dict:
        body = {"pair": self.pair_id, "eph": b64u(self.pub), "name": "Beta", "version": "4.0.0",
                "caps": list(compat.CAPS), "gen": 0, "ports": [45871], "seen": "", "bot": "beta_vpn_bot",
                "gw": gw, "nonce": "", "_peer_addr": addr}
        body.update(over)
        return body

    def eph_in(self, body: dict) -> str:
        i_eph = unb64u(body["eph"])
        k = joinsas.shared(self.priv, i_eph)
        self.code = joinsas.code(self.pair_id, None, self.pub, i_eph, k, tag=joinsas.PAIR_SAS_TAG)
        self.kp = joinsas.pair_key(self.pair_id, k, tag=joinsas.PAIR_KEY_TAG)
        return self.code


class Inviter:
    """Вторая сторона, пригласившая этот сервер: приглашение и её половина
    сопряжения."""

    def __init__(self, *, version: str = "4.0.0", expires_in: int = 3600, awg_gen: int = 0,
                 pair_id: str = "a1b2c3d4e5f60718"):
        self.pair_id = pair_id
        self.peer_priv = key()
        self.inviter_pub = key()
        self.tok = pairtoken.PairToken(
            version=version, pair_id=pair_id,
            expires=int(timeutil.now().timestamp()) + expires_in, host=HOST_B, pair_port=47101,
            peer_addr="10.99.98.1", inviter_pub=base64.b64decode(self.inviter_pub),
            peer_priv=base64.b64decode(self.peer_priv), jc=5, jmin=10, jmax=50, s=(20, 30, 40, 50),
            h=((100, 200), (600000000, 600100000), (1100000000, 1100100000), (1600000000, 1600100000)),
            awg_gen=awg_gen)
        self.token = pairtoken.encode(self.tok)
        self.priv, self.pub = joinsas.new_pair()
        self.kp = b""
        self.code = ""

    def eph_reply(self, hello: dict, **over) -> dict:
        """Ответ pair_eph на hello сопряжения принявшего; код и ключ — у себя."""
        a_eph = unb64u(hello["eph"])
        k = joinsas.shared(self.priv, a_eph)
        self.code = joinsas.code(self.pair_id, None, a_eph, self.pub, k, tag=joinsas.PAIR_SAS_TAG)
        self.kp = joinsas.pair_key(self.pair_id, k, tag=joinsas.PAIR_KEY_TAG)
        body = {"eph": b64u(self.pub), "name": "Beta", "version": "4.0.0", "caps": list(compat.CAPS), "gen": 0,
                "ports": [45871], "bot": "beta_vpn_bot", "gw": True}
        body.update(over)
        return body

    def ok_body(self, **over) -> dict:
        data = {"psk": key(), "priv": key(), "peer_pub": key(), "admin_id": PEER_ADMIN,
                "obf": dict(OBF), "self_host": ADDR_B}
        data.update(over)
        return {"sealed": joinsas.seal(self.kp, data), "_data": data}


@pytest.fixture()
def rsv(services, tmp_path, monkeypatch):
    """Сервер AWG, готовый к паре: домен для клиентов, «Готов быть
    резервом», выпуск 4.0.0, шлюз с rsv1 (подмена — rsv.gw), файервол —
    журнал вызовов (rsv.fw; rsv.fw_fail — отказ строгой перерисовки)."""
    monkeypatch.setattr(config, "INSTALLED_VERSION", "4.0.0")
    monkeypatch.setattr(config, "SERVER_NAME", "Alpha")
    settings.set_value("app.network.server_host", HOST_A)
    settings.set_value("app.reserve.ready", True)
    invitemod._live_set(None)
    host = PairHost(services, tmp_path, monkeypatch)
    host.gw = True
    host.fw: list[bool] = []
    host.fw_fail = False

    def fw(strict: bool = False):
        host.fw.append(strict)
        if host.fw_fail and strict:
            raise ServiceError("таблица файервола сервера не перерисована: nft: синтаксис")
    monkeypatch.setattr(services, "_gw_firewall_refresh", fw)
    monkeypatch.setattr(services, "reserve_has_gateway", lambda: host.gw)
    monkeypatch.setattr(services, "_awg_generation", lambda: 0)
    from awgbot.runtime import pairlink
    monkeypatch.setattr(pairlink, "_link", None)
    monkeypatch.setattr(pairlink, "_on_event", None)
    monkeypatch.setattr(pairlink, "_on_notes", None)
    yield host
    invitemod._live_set(None)


def invite_and_hello(services, rsv, **hello) -> tuple[dict, Acceptor, dict]:
    """Приглашение выпущено, принявший подключился (hello сопряжения)."""
    res = services.reserve_invite_issue()
    acc = Acceptor(res["state"]["id"])
    out = services.reserve_pair_hello(acc.hello(**hello))
    assert out["ok"], out
    acc.eph_in(out["body"])
    return res, acc, out


def pair_as_inviter(services, rsv, **hello) -> Acceptor:
    """Полное сопряжение пригласившим: код верный, pair_ok, pair_admin."""
    _res, acc, _out = invite_and_hello(services, rsv, **hello)
    assert services.reserve_pair_code(joinsas.show(acc.code))["result"] == "ok"
    ok = services.reserve_pair_ok_body()
    acc.secrets = joinsas.unseal(acc.kp, ok["sealed"])
    services.reserve_pair_admin_in({"sealed": joinsas.seal(acc.kp, {"admin_id": PEER_ADMIN, "self_host": ADDR_B})})
    assert services.reserve_paired()
    return acc


def pair_row(services, *, listener: bool = True, admin: int = PEER_ADMIN) -> None:
    """Строка пары напрямую (как после сопряжения) — для тестов после него."""
    services.db.reserve_peer_set(name="Beta", host=HOST_B, pair_if="awgpair", pair_port=47101, peer_port=0,
                                 pair_cidr="10.99.98.0/30", listener=listener, bot_username="beta_vpn_bot",
                                 peer_admin_id=admin)
