"""Общее для тестов подключения шлюза командой (test_join_*): сервер AWG со
скриптом линка моделью тех же файловых эффектов, что у настоящего (его режимы
--join-stage / --join-revert / --join-drop проверяет test_link_script_join),
и шлюзовая сторона сопряжения — так, как её вёл бы агент: эфемерный ключ,
код, ключ сопряжения.

Конфиги линка — в config.AWG_DIR и во временном «/root»: чтение /root/… в
модулях, которые его читают, перенаправлено. Ключи — настоящие X25519.
"""
from __future__ import annotations

import base64

import pytest
from nacl.public import PrivateKey

from awgbot.core import config
from awgbot.domain.services.gateway_link import rekey as rekeymod
from awgbot.domain.services.gwjoin import issue as issuemod
from awgbot.domain.services.gwjoin import state as statemod
from awgbot.domain.services.types import ServiceError
from awgbot.util import joinsas, jointoken, wgconf

ADMIN = config.ADMIN_ID
STAMP = "# awg-bot: сопряжение "
LINKS = {1: ("awglink", 443, "10.99.99.0/30"), 2: ("awglink2", 8443, "10.99.99.4/30")}


def keypair() -> tuple[str, str]:
    k = PrivateKey.generate()
    return base64.b64encode(bytes(k)).decode(), base64.b64encode(bytes(k.public_key)).decode()


def vps_conf(priv: str, peer_pub: str, *, psk: str = "", port: int = 443, addr: str = "10.99.99.1/30",
             stamp: str = "") -> str:
    head = f"{STAMP}{stamp}\n" if stamp else "# Линк ВПС ↔ шлюз\n"
    return (f"{head}[Interface]\nAddress = {addr}\nListenPort = {port}\nPrivateKey = {priv}\nTable = off\n"
            "Jc = 4\nJmin = 40\nJmax = 70\nS1 = 10\nS2 = 20\nS3 = 0\nS4 = 0\n"
            "H1 = 100-200\nH2 = 300-400\nH3 = 500-600\nH4 = 700-800\n\n"
            f"[Peer]\nPublicKey = {peer_pub}\n" + (f"PresharedKey = {psk}\n" if psk else "")
            + "AllowedIPs = 0.0.0.0/0\n")


def gw_conf(priv: str, peer_pub: str, *, psk: str = "", port: int = 443, addr: str = "10.99.99.2/30",
            i1: str = "") -> str:
    return ("# awg-bot: контракт линка 1\n[Interface]\n"
            f"Address = {addr}\nPrivateKey = {priv}\n"
            "Jc = 4\nJmin = 40\nJmax = 70\nS1 = 10\nS2 = 20\nS3 = 0\nS4 = 0\n"
            "H1 = 100-200\nH2 = 300-400\nH3 = 500-600\nH4 = 700-800\n" + (f"I1 = {i1}\n" if i1 else "")
            + f"\n[Peer]\nPublicKey = {peer_pub}\n" + (f"PresharedKey = {psk}\n" if psk else "")
            + f"Endpoint = vpn-server.example.org:{port}\nAllowedIPs = 10.8.1.0/24, 10.99.99.0/30\n"
            "PersistentKeepalive = 25-35\n")


def _hosts(cidr: str) -> tuple[str, str]:
    base = cidr.split("/")[0].rsplit(".", 1)
    return f"{base[0]}.{int(base[1]) + 1}/30", f"{base[0]}.{int(base[1]) + 2}/30"


class JoinVps:
    """Сервер AWG: конфиги линков обеих сторон и скрипт линка моделью.
    `runs` — (режим, окружение) по порядку; `fail` — {режим: причина}."""

    def __init__(self, services, tmp_path, monkeypatch):
        self.awg = tmp_path / "awg"; self.awg.mkdir()
        self.root = tmp_path / "root"; self.root.mkdir()
        monkeypatch.setattr(config, "AWG_DIR", str(self.awg))
        real_open = open

        def _open(path, *a, **kw):
            p = str(path)
            if p.startswith("/root/"):
                p = str(self.root / p[len("/root/"):])
            return real_open(p, *a, **kw)
        for mod in (rekeymod, issuemod):
            monkeypatch.setattr(mod, "open", _open, raising=False)
        self.runs: list[tuple[str, dict]] = []
        self.fail: dict[str, str] = {}
        self._n = 0
        monkeypatch.setattr(services, "_run_link_script", self.run)
        from awgbot.util import bundlecrypt
        monkeypatch.setattr(services, "_link_privkey",
                            lambda gw=None: bundlecrypt.read_privkey(self.gw(gw.link_if).read_text(encoding="utf-8")))
        monkeypatch.setattr(issuemod.GwJoinIssueMixin, "_gwjoin_host",
                            staticmethod(lambda: "vpn-server.example.org"))
        statemod._LIVE.clear()

    def live(self, link_if: str = "awglink"):
        return self.awg / f"{link_if}.conf"

    def gw(self, link_if: str = "awglink"):
        return self.root / f"gw-{link_if}.conf"

    @staticmethod
    def _p(path, suffix):
        return path.with_name(path.name + suffix)

    def put(self, link_if: str, port: int, cidr: str) -> tuple[str, str]:
        """Линк слота на постоянных ключах (как после прежнего подключения)."""
        vp, vpub = keypair()
        gp, gpub = keypair()
        va, ga = _hosts(cidr)
        self.live(link_if).write_text(vps_conf(vp, gpub, psk="DUMMY-PSK=", port=port, addr=va), encoding="utf-8")
        self.gw(link_if).write_text(gw_conf(gp, vpub, psk="DUMMY-PSK=", port=port, addr=ga), encoding="utf-8")
        return gp, vpub

    def run(self, mode: str, env: dict | None = None) -> None:
        env = dict(env or {})
        self.runs.append((mode, env))
        if mode in self.fail:
            raise ServiceError(f"скрипт линка ({mode}) не отработал: {self.fail[mode]}")
        link_if = env.get("LINK_IF", "awglink")
        live, gw = self.live(link_if), self.gw(link_if)
        port = int(env.get("LINK_PORT") or 443)
        va, ga = _hosts(env.get("LINK_CIDR", "10.99.99.0/30"))
        if mode == "--join-stage":
            if env.get("JOIN_PREV") == "1" and live.exists() and not self._p(live, ".join-prev").exists():
                self._p(live, ".join-prev").write_text(live.read_text(encoding="utf-8"), encoding="utf-8")
                if gw.exists():
                    self._p(gw, ".join-prev").write_text(gw.read_text(encoding="utf-8"), encoding="utf-8")
            vp, vpub = keypair()
            gp, gpub = keypair()
            live.write_text(vps_conf(vp, gpub, port=port, addr=va, stamp=env["JOIN_ID"]), encoding="utf-8")
            gw.write_text(gw_conf(gp, vpub, port=port, addr=ga), encoding="utf-8")
        elif mode == "--join-revert":
            if self._p(live, ".join-prev").exists():
                live.write_text(self._p(live, ".join-prev").read_text(encoding="utf-8"), encoding="utf-8")
                self._p(live, ".join-prev").unlink()
                if self._p(gw, ".join-prev").exists():
                    gw.write_text(self._p(gw, ".join-prev").read_text(encoding="utf-8"), encoding="utf-8")
                    self._p(gw, ".join-prev").unlink()
        elif mode == "--join-drop":
            self._p(live, ".join-prev").unlink(missing_ok=True)
            self._p(gw, ".join-prev").unlink(missing_ok=True)
        elif mode == "--rollback":
            live.unlink(missing_ok=True)
            gw.unlink(missing_ok=True)
        elif mode == "--rekey":
            vp, vpub = keypair()
            gp, gpub = keypair()
            live.write_text(vps_conf(vp, gpub, psk="DUMMY-PSK=", port=port, addr=va), encoding="utf-8")
            gw.write_text(gw_conf(gp, vpub, psk="DUMMY-PSK=", port=port, addr=ga), encoding="utf-8")
        elif mode == "--rekey-stage":
            self._n += 1
            vp, vpub = keypair()
            gp, gpub = keypair()
            psk = f"DUMMY-NEW-PSK-{self._n}="
            self._p(live, ".next").write_text(vps_conf(vp, gpub, psk=psk, port=port, addr=va), encoding="utf-8")
            self._p(gw, ".next").write_text(gw_conf(gp, vpub, psk=psk, port=port, addr=ga,
                                                     i1=env.get("LINK_I1", "")), encoding="utf-8")
        elif mode == "--rekey-commit":
            for path in (live, gw):
                self._p(path, ".prev").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
                path.write_text(self._p(path, ".next").read_text(encoding="utf-8"), encoding="utf-8")
                self._p(path, ".next").unlink()
        elif mode == "--rekey-revert":
            for path in (live, gw):
                self._p(path, ".next").unlink(missing_ok=True)
                if self._p(path, ".prev").exists():
                    path.write_text(self._p(path, ".prev").read_text(encoding="utf-8"), encoding="utf-8")
                    self._p(path, ".prev").unlink()
        elif mode == "--rekey-drop":
            for path in (live, gw):
                self._p(path, ".next").unlink(missing_ok=True)
                self._p(path, ".prev").unlink(missing_ok=True)

    @property
    def modes(self) -> list[str]:
        return [m for m, _ in self.runs]

    def stamped(self, link_if: str) -> bool:
        return self.live(link_if).exists() and self.live(link_if).read_text(encoding="utf-8").startswith(STAMP)

    def peer_pub(self, link_if: str) -> str:
        return wgconf.parse(self.live(link_if).read_text(encoding="utf-8")).get("Peer", "PublicKey")


class Gateway:
    """Шлюзовая сторона сопряжения: токен, эфемерный ключ, код и ключ
    сопряжения — тем же util/joinsas, что агент."""

    def __init__(self, command: str):
        self.token = jointoken.decode(jointoken.find(command))
        self.priv, self.pub = joinsas.new_pair()
        self.code = ""
        self.kp = b""

    def hello(self, *, host: str = "raspberrypi") -> dict:
        return {"join": self.token.join_id, "host": host,
                "eph": base64.urlsafe_b64encode(self.pub).decode().rstrip("=")}

    def eph_in(self, body: dict) -> str:
        s_pub = base64.urlsafe_b64decode(str(body["eph"]) + "==")
        k = joinsas.shared(self.priv, s_pub)
        self.code = joinsas.code(self.token.join_id, self.token.slot, self.pub, s_pub, k)
        self.kp = joinsas.pair_key(self.token.join_id, k)
        return self.code


@pytest.fixture()
def admin(services, fake_awg, fake_routing, make_active_client, monkeypatch):
    """Админ с устройством NASPi, РФ-доступ развёрнут; адрес шлюза на том конце
    линка — 203.0.113.50."""
    from awgbot.infra import routing
    a = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    services.add_device(a.id, "NASPi")
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    peer = {"addr": "203.0.113.50"}
    # подмена — на части, где имя определено (routing.probes), а не на пакете:
    # пакет отдаёт имена частей через __getattr__, и monkeypatch на пакете при
    # откате оставил бы в нём навсегда подмену fake_routing — соседние тесты
    # процесса (test_routing_domains) получали бы её вместо настоящей
    monkeypatch.setattr(routing.probes, "link_peer_endpoint", lambda iface="": peer["addr"])
    services.peer = peer
    return a


@pytest.fixture()
def vps(services, admin, tmp_path, monkeypatch):
    return JoinVps(services, tmp_path, monkeypatch)


def slot_with_device(services, vps, slot: int = 1, name: str = "NASPi"):
    """Слот на постоянных ключах с устройством name (как после прежнего
    подключения)."""
    link_if, port, cidr = LINKS[slot]
    dev = next(d for d in services.db.list_devices(services.admin_client().id) if d.name == name)
    vps.put(link_if, port, cidr)
    return services.db.gateway_add(dev.id, link_if, port, cidr, slot_id=slot)


def connect(services, gwside: Gateway, slot: int, **kw) -> dict:
    res = services.gwjoin_hello(slot, gwside.hello(**kw))
    assert res["ok"], res
    gwside.eph_in(res["body"])
    return res


def accept(services, vps, gwside: Gateway, slot: int) -> dict:
    """Код верный и смена ключей после кода доведена до фиксации (hello новым ключом)."""
    st = services.gwjoin(slot)
    res = services.gwjoin_code(slot, st["id"], joinsas.show(gwside.code))
    assert res["result"] == "ok", res
    rk = services.gateway_rekey(slot)
    assert rk.get("auto") == "join" and rk.get("state") == "staged", rk
    action, _ = services.gateway_rekey_ack_in(slot, {"id": rk["id"], "state": "ready"})
    assert action == "go", action
    services.gateway_rekey_go(slot)
    assert services.gateway_rekey_commit(slot)[0] is True
    out = services.gateway_rekey_hello(slot)
    assert out["rekeyed"], out
    return res
