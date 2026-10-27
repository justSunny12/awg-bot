"""Окно переезда профилей едет гостю (util/rsvpkg/bot.py GUEST_STATE +=
migration_state, migration_ready_announced): двойник (devices.twin_of),
впервые вышедший на связь на клоне второго интерфейса у резерва, получает
поздравление от бота основы на резерве; после возврата слияние несёт
device_traffic.last_handshake (MAX), и основа не поздравляет второй раз.

Базы настоящие (основа, реплика гостю, реплика обратно), awg — подменой:
конфиги интерфейсов основы и клона — словари пиров, рукопожатия — то, что
отвечает `show_dump` по интерфейсу.

Цена ошибки: состояние окна не доехало — у гостя переезд «не идёт», человек,
переимпортировавший профиль за время аварии, поздравления не получает и не
знает, что старый профиль можно удалить; оно доехало, а рукопожатие не
вернулось слиянием — основа после возврата поздравляет его второй раз, уже
через сутки; слияние переводит двойника на старый интерфейс или рвёт его
связь с исходным устройством — устройство пропадает из окна переезда, а
его пир на втором интерфейсе живёт без хозяина.
"""
from __future__ import annotations

import os
import sqlite3

import pytest
import yaml

from awgbot.core import config
from awgbot.domain.services import Services
from awgbot.infra import awg
from awgbot.infra.db import Database
from awgbot.util import timeutil
from awgbot.util.rsvpkg import bot as pkgbot, seal

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv

ANNA_TG = 7301


class _Wg:
    """awg глазами бота: пиры по интерфейсам (`conf`), журнал добавлений
    (`adds`: (ключ, интерфейс)), рукопожатия (`hs`: ключ → время), которые
    отдаёт `show_dump` пира на его интерфейсе."""

    def __init__(self, monkeypatch) -> None:
        self.conf: dict[str, dict[str, str]] = {}
        self.adds: list[tuple[str, str]] = []
        self.hs: dict[str, int] = {}
        monkeypatch.setattr(awg, "add_peer", self.add_peer)
        monkeypatch.setattr(awg, "remove_peer", self.remove_peer)
        monkeypatch.setattr(awg, "show_dump", self.show_dump, raising=False)
        monkeypatch.setattr(awg, "read_occupied_ips", lambda iface=None: set())

    def add_peer(self, pub, psk, ip, iface=None):
        name = awg.iface_of(iface)
        self.adds.append((pub, name))
        self.conf.setdefault(name, {})[pub] = ip

    def remove_peer(self, pub, iface=None):
        self.conf.get(awg.iface_of(iface), {}).pop(pub, None)

    def show_dump(self, iface=None):
        name = awg.iface_of(iface)
        return [{"public_key": p, "rx": 1, "tx": 1, "last_handshake": self.hs.get(p, 0)}
                for p in self.conf.get(name, {})]


@pytest.fixture()
def world(mv, fake_awg, make_active_client, monkeypatch, tmp_path):
    """Основа «Alpha» в паре с «Beta»: окно переезда на awg1 открыто, у профиля
    «Аня» устройство «Ноут» и его двойник (без рукопожатия), снимок пакета
    `base-7` снят, пользователи и бот ушли к резерву."""
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg0")
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "awg1")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "10.9.1")
    wg = _Wg(monkeypatch)
    svc = mv.svc
    anna = make_active_client(name="Аня", tg_id=ANNA_TG)
    dev = svc.add_device(anna.id, "Ноут")
    res = svc.migration_start()
    assert res.started and res.born == 1, res
    twin_id = svc.db.twins_by_origin()[dev.device_id]
    twin = svc.db.get_device(twin_id)
    assert twin.iface == "awg1" and twin.twin_of == dev.device_id
    assert twin.public_key in wg.conf.get("awg1", {}), "двойник не заведён на втором интерфейсе основы"
    rm.put_base(svc, gen=7)
    rm.go_away(svc, gen=7)
    mv.wg, mv.origin_id, mv.twin_id, mv.twin_pub = wg, dev.device_id, twin_id, twin.public_key
    mv.tmp = tmp_path
    return mv


def _guest(w) -> Services:
    """Бот основы на резерве: база — реплика пакета, настройки клона — те же
    интерфейсы (awg0 и второй, awg1)."""
    path = os.path.join(str(w.tmp), "guest", "bot.db")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(pkgbot.replica(w.svc.db.path))
    gdb = Database(path)
    w.gdb = gdb
    return Services(gdb)


def _back(w, gsvc) -> list:
    """Возврат: основа просит реплику, гость отдаёт свою базу под K, основа сливает."""
    rm.st_in(w.svc, **{"in": "serving"})
    w.svc.reserve_return_start("button")
    w.svc.reserve_set_out("returning", kind="button", gen=7, step="back_req", return_started_at=timeutil.now_iso())
    w.wg.adds.clear()
    blob = seal.snapshot_seal(w.svc.reserve_k(), {"pkg_gen": 7, "at": timeutil.now_iso(),
                                                  "db": pkgbot.replica(gsvc.db.path).hex()})
    return w.svc.reserve_released_in(blob)


# ── реплика ─────────────────────────────────────────────────────────────────

def test_the_migration_window_travels_in_the_replica_and_passes_the_guard(world, tmp_path):
    svc = world.svc
    # отметка ставится до передачи бота (реплика собирается, пока основа держит эпоху)
    svc.db.jrn_pass(holder="self", role="holder", why="тест: до передачи")
    svc.db.set_state("migration_ready_announced", "1")
    raw = pkgbot.replica(svc.db.path)
    pkgbot.assert_clean(raw)                          # сторож запретного: ключи окна — не «rsv_», «update_»…
    p = tmp_path / "rep.db"
    p.write_bytes(raw)
    state = dict(sqlite3.connect(p).execute("SELECT key, value FROM server_state"))
    assert state.get("migration_state") == "running", f"окно переезда не доехало до резерва: {state}"
    assert state.get("migration_ready_announced") == "1", "отметка «готово уже сказано» не доехала"


def test_no_guest_state_key_falls_under_the_forbidden_prefixes():
    """Белый список и чёрный не пересекаются: иначе сборка пакета падает на
    каждом сервере с идущим переездом."""
    bad = [k for k in pkgbot.GUEST_STATE if k.startswith(pkgbot.FORBIDDEN_STATE)]
    assert bad == [], bad


def test_the_guest_app_yaml_names_the_second_interface_as_the_migration_one():
    """Без migration_interface в app.yaml гостя рычаг у него «не настроен» —
    и окно из реплики ничего не значит."""
    from tools import rsvopen
    app = yaml.safe_load(rsvopen._guest_app_yaml({}, {}, "awg0", "", [{"name": "awg1", "addr": "10.9.1.1/24"}]))
    assert app["awg"]["migration_interface"] == "awg1" and app["awg"]["migration_subnet_prefix"] == "10.9.1", app["awg"]
    assert app["awg"]["interface"] == "awg0"


# ── у гостя ─────────────────────────────────────────────────────────────────

def test_the_guest_sees_the_migration_running(world):
    gsvc = _guest(world)
    assert gsvc.migration_running() is True, "у бота основы на резерве окно переезда закрыто"
    assert [d.id for d in gsvc.db.pending_twins()] == [world.twin_id], "гость не ждёт двойника"


@pytest.mark.parametrize("first", ["watch", "poll"])
def test_the_twin_connecting_on_the_clone_is_greeted_once(world, first):
    """Первое рукопожатие двойника на клоне — одно поздравление владельцу;
    ни второй такт того же задания, ни другое задание его не повторяют."""
    gsvc = _guest(world)
    world.wg.hs[world.twin_pub] = int(timeutil.now().timestamp())
    one, other = ((gsvc.migration_watch, gsvc.poll_traffic) if first == "watch"
                  else (gsvc.poll_traffic, gsvc.migration_watch))
    notes = one()
    assert len(notes) == 1 and notes[0].tg_id == ANNA_TG and "Ноут" in notes[0].text, notes
    assert one() == [], "поздравление повторилось тем же заданием"
    assert other() == [], "поздравление повторилось другим заданием"


def test_the_guest_does_not_greet_without_a_handshake(world):
    gsvc = _guest(world)
    assert gsvc.migration_watch() == [] and gsvc.poll_traffic() == []


# ── возврат ─────────────────────────────────────────────────────────────────

def test_after_the_return_the_main_does_not_greet_again_and_the_twin_stays_on_awg1(world):
    gsvc = _guest(world)
    ts = int(timeutil.now().timestamp())
    world.wg.hs[world.twin_pub] = ts
    assert len(gsvc.migration_watch()) == 1
    acts = _back(world, gsvc)
    assert acts == [("restart",)], f"слияние не прошло: {world.svc.rsv_get('rsv_out')}"
    main = world.svc
    twin = main.db.get_device(world.twin_id)
    assert twin is not None and twin.iface == "awg1", f"двойник ушёл со второго интерфейса: {twin}"
    assert twin.twin_of == world.origin_id, "слияние порвало связь двойника с исходным устройством"
    assert int(twin.last_handshake or 0) == ts, "рукопожатие с резерва не вернулось слиянием"
    assert main.db.pending_twins() == [], "основа всё ещё ждёт двойника, который уже подключался"
    assert main.migration_running() is True
    assert main.migration_watch() == [], "основа поздравила второй раз после возврата"
    assert main.poll_traffic() == [], "опрос трафика основы поздравил второй раз"


def test_the_return_merge_does_not_add_the_twin_peer_a_second_time(world):
    """Пир двойника заведён на awg1 основы при рождении — слияние его не
    добавляет заново (и не пишет в отчёт «добавлен пир»)."""
    gsvc = _guest(world)
    world.wg.hs[world.twin_pub] = int(timeutil.now().timestamp())
    gsvc.poll_traffic()
    assert _back(world, gsvc) == [("restart",)]
    again = [a for a in world.wg.adds if a[0] == world.twin_pub]
    assert again == [], f"слияние добавило пир двойника повторно: {again}"
    assert list(world.wg.conf["awg1"]).count(world.twin_pub) == 1
    assert world.svc.rsv_get("rsv_out")["merge_report"]["peers_added"] == 0, world.svc.rsv_get("rsv_out")["merge_report"]


def test_a_twin_that_never_connected_at_the_reserve_is_still_awaited_after_the_return(world):
    """Не подключался у резерва — после возврата основа по-прежнему ждёт его
    и поздравит при первом рукопожатии у себя."""
    gsvc = _guest(world)
    gsvc.poll_traffic()
    assert _back(world, gsvc) == [("restart",)]
    main = world.svc
    assert [d.id for d in main.db.pending_twins()] == [world.twin_id]
    world.wg.hs[world.twin_pub] = int(timeutil.now().timestamp())
    notes = main.migration_watch()
    assert len(notes) == 1 and notes[0].tg_id == ANNA_TG, notes

