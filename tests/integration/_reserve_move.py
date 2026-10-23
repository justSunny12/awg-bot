"""Общее для тестов переезда и возврата (test_reserve_move*, test_reserve_return*,
test_reserve_hosting*, test_reserve_dns): пара в рабочем состоянии, подставные
запись DNS и часы автоматов.

Основа в паре с резервом `Beta` (198.51.100.7): второй сервер на связи, готов
и совместим, свежий пакет не нужен (`rsv_pkg_sent` пуст), адрес для клиентов —
имя `vpn-a.example.org`, которое системный резолвер отдаёт как 198.51.100.10
(свой адрес). Запись у авторитетного сервера по умолчанию указывает на свой
адрес. Время автоматов (`time.monotonic` в outbound/inbound/dns/package) —
часы теста: идут только когда тест говорит.

Все адреса — документационные диапазоны, ключи — заглушки.
"""
from __future__ import annotations

import types

import pytest

from awgbot.core import config
from awgbot.domain.services.reserve import compat, dns as dnsmod, inbound, outbound, outbound_return, package
from awgbot.infra import awg
from awgbot.util import dnsapi, timeutil
from awgbot.util.rsvpkg import bot as pkgbot, seal

from tests.integration import _reserve_common as rc
from tests.integration.test_reserve_package import LIVE

OWN_IP = "198.51.100.10"        # свой адрес сервера (запись указывает сюда, пока клиенты дома)
PEER_IP = rc.ADDR_B             # адрес резерва


class Clock:
    """Часы автоматов: время идёт только когда тест говорит."""

    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class DnsProvider:
    """Провайдер записи: запоминает вызовы; запись у авторитетного сервера
    меняет сам тест (авторитетный сервер отстаёт от API — так и в жизни)."""

    id = "stub"
    human = "stub"

    def __init__(self, world: "DnsWorld") -> None:
        self.w = world

    def set(self, cfg: dict, ip: str) -> None:
        if self.w.set_error:
            raise dnsapi.DnsError(self.w.set_error)
        self.w.sets.append(ip)

    def check(self, cfg: dict) -> str:
        if self.w.check_error:
            raise dnsapi.DnsError(self.w.check_error)
        return self.w.api_value

    def read(self, cfg: dict) -> str:
        return self.w.api_value


class DnsWorld:
    """Запись DNS в мире теста: что отвечает авторитетный сервер, системный
    резолвер, что делали с API провайдера."""

    def __init__(self) -> None:
        self.auth: list[str] = [OWN_IP]
        self.auth_error = False
        self.system: dict[str, list[str]] = {rc.HOST_A: [OWN_IP]}
        self.sets: list[str] = []
        self.set_error = ""
        self.check_error = ""
        self.api_value = OWN_IP
        self.auth_reads = 0
        self.system_reads = 0
        self.ttl = 60                                      # TTL A-записи у авторитетного сервера

    def read_authoritative(self, name: str) -> list[str]:
        self.auth_reads += 1
        if self.auth_error:
            raise dnsapi.DnsError("авторитетный сервер не ответил")
        return list(self.auth)

    def read_authoritative_ttl(self, name: str) -> tuple[list[str], int]:
        addrs = self.read_authoritative(name)
        return addrs, (self.ttl if addrs else 0)

    def read_system(self, name: str, timeout: float = 3.0) -> list[str]:
        self.system_reads += 1
        return list(self.system.get(name, []))

    def provider_for(self, cfg: dict) -> DnsProvider:
        return DnsProvider(self)


def peer_st(**over) -> dict:
    body = {"ready": True, "name": "Beta", "version": "4.0.0", "caps": list(compat.CAPS), "gen": 0, "ports": [45871],
            "self": PEER_IP, "out": "home", "in": "idle", "pkg": {"gen": 0, "fp": ""}, "voters": [],
            "admin_id": rc.PEER_ADMIN, "admin_opened": False, "gw": True}
    body.update(over)
    return body


def st_in(svc, **over) -> list:
    """`st` резерва принят; флаг живой сессии сохранён (его теряет сам приём st —
    это проверяет отдельный тест, остальные не должны от него зависеть)."""
    notes = svc.reserve_st_in(peer_st(**over))
    svc.rsv_update("rsv_peer_state", session=True)
    return notes


@pytest.fixture()
def mv(services, rsv, tmp_path, monkeypatch):
    """Основа в паре, второй сервер на связи и готов; запись DNS и часы — подставные."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)          # bot.db временной базы лежит здесь же
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "")
    monkeypatch.setattr(awg, "read_file", lambda path: LIVE, raising=False)
    world = DnsWorld()
    for name in ("read_authoritative", "read_system", "provider_for"):
        monkeypatch.setattr(dnsapi, name, getattr(world, name))
    # пакет dnsapi может не выставлять это имя (см. tests/unit/test_dnsapi.py) — подмена мира теста его ставит
    monkeypatch.setattr(dnsapi, "read_authoritative_ttl", world.read_authoritative_ttl, raising=False)
    clock = Clock()
    stub = types.SimpleNamespace(monotonic=clock.monotonic)
    for mod in (outbound, outbound_return, inbound, dnsmod, package):      # часы одни на все части автомата
        monkeypatch.setattr(mod, "time", stub)
    rc.pair_row(services)
    services.reserve_keys_init()
    st_in(services)
    services.db.set_state("last_server_ok", "1")
    h = types.SimpleNamespace(svc=services, dns=world, clock=clock, rsv=rsv)
    return h


def automatic(svc, provider: str = "cloudflare") -> None:
    """Запись меняется ботом: провайдер и токен заданы."""
    svc.reserve_dns_set_provider(provider)
    svc.reserve_dns_set_token("DUMMY-token")


def confirm_package(svc, gen: int = 7) -> None:
    """Резерв подтвердил свежий пакет (как после `pkg_ok`)."""
    svc.rsv_put("rsv_pkg_sent", {"gen": gen, "fp": "DUMMY-fp", "at": timeutil.now_iso()})


def to_step(svc, step: str, *, gen: int = 7, kind: str = "button", wait: int = 300) -> None:
    """Автомат «мои клиенты» уже на шаге переезда (минуя предыдущие)."""
    svc.reserve_set_out("leaving", kind=kind, started_at=timeutil.now_iso(), target_ip=PEER_IP,
                        record=rc.HOST_A, gen=gen, record_set_at="", record_probe=0)
    svc._out_step(step, wait)


def go_away(svc, *, gen: int = 7, kind: str = "button", last_switch: str = "") -> None:
    """Клиенты и бот основы уже у резерва; `last_switch` пуст — переключений давно не было."""
    svc.reserve_set_out("away", kind=kind, gen=gen, step="", taken_at=timeutil.now_iso(), last_switch=last_switch,
                        healthy_since="", healthy_mono=0, can_return=False)
    svc.reserve_set_poll("away")


def open_eph(svc, gen: int = 7, oid: str = "0123456789abcdef") -> tuple[bytes, list[tuple]]:
    """Резерв прислал разовый ключ своего процесса открытия (`open_eph`):
    возвращает закрытую часть (память процесса у резерва) и действия основы."""
    priv, pub = seal.new_eph()
    return priv, svc.reserve_open_eph_in(gen, pub.hex(), oid)


def actions_of(acts: list[tuple], kind: str) -> list[tuple]:
    return [a for a in acts if a and a[0] == kind]


def notices(acts: list[tuple]) -> list:
    out = []
    for a in acts:
        if a and a[0] == "notify":
            out.extend(a[1])
    return out


def sent(acts: list[tuple]) -> list[str]:
    return [a[1] for a in acts if a and a[0] == "send"]


def snapshot_blob(svc, edit=None, *, gen: int = 7, k: bytes | None = None) -> bytes:
    """Реплика бота основы на резерве под `K`: копия базы основы (как настоящая — без
    истории и rsv_*), к которой применено `edit(db)` — то, что люди сделали на резерве."""
    import os
    import shutil
    import tempfile
    from awgbot.infra.db import Database
    tmp = tempfile.mkdtemp(prefix="rsv-test-")
    path = os.path.join(tmp, "rep.db")
    with open(path, "wb") as f:
        f.write(pkgbot.replica(svc.db.path))
    if edit is not None:
        db = Database(path)
        try:
            edit(db)
        finally:
            db.close()
    with open(path, "rb") as f:
        data = f.read()
    shutil.rmtree(tmp, ignore_errors=True)
    return seal.snapshot_seal(k or svc.reserve_k(), {"pkg_gen": gen, "at": timeutil.now_iso(), "db": data.hex()})


def put_base(svc, gen: int = 7) -> str:
    """Снимок `base-<gen>.db` — базы основы в момент сборки пакета."""
    import os
    path = os.path.join(str(config.DATA_DIR), "reserve", "out")
    os.makedirs(path, exist_ok=True)
    svc._pkg_store_base(gen, pkgbot.replica(svc.db.path))
    return os.path.join(path, f"base-{gen}.db")


__all__ = ["OWN_IP", "PEER_IP", "mv", "automatic", "confirm_package", "to_step", "open_eph", "actions_of", "notices",
           "sent", "snapshot_blob", "put_base", "peer_st", "st_in"]
