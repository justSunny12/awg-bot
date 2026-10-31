"""
План восстановления (domain/backup/plan.py): линия базы и сопоставление
шлюзов копии с текущими по номеру слота — по строке на каждый случай; число
пропадающих профилей и устройств — только у чужой копии; план бота и план
слияния — один и тот же.
"""
from __future__ import annotations

import json

import pytest

from awgbot.core import config
from tests.integration._restore_merge_common import World, _sql

pytestmark = pytest.mark.unit

ADMIN = config.ADMIN_ID


@pytest.fixture()
def world(services, make_active_client, monkeypatch, tmp_path, fake_awg):
    monkeypatch.setattr(config, "INSTALLED_VERSION", "4.0.0")
    w = World(services, tmp_path / "live")
    monkeypatch.setattr(config, "AWG_DIR", str(w.awg))
    w.admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    w.gw1 = services.add_device(w.admin.id, "NASPi").device_id
    w.slot(1, w.gw1)
    w.ivan = make_active_client(name="Иван", tg_id=2001)
    services.add_device(w.ivan.id, "Телефон")
    w.write_client_conf()
    return w


def _plan(w, copy, cur_db=None):
    from awgbot.domain.backup import plan
    meta = json.loads((copy / "state" / "backup-meta.json").read_text(encoding="utf-8"))
    return plan.plan_for_dir(meta, str(copy), cur_db=cur_db or w.db.path, conf_dir=str(w.conf),
                             awg_dir=str(w.awg), root_dir=str(w.gwroot))


def _cases(p) -> dict[int, str]:
    return {s.slot: s.case for s in p.slots}


def test_the_same_slot(world, tmp_path):
    p = _plan(world, world.take_copy(tmp_path / "c"))
    assert p.line == "same" and _cases(p) == {1: "same"} and p.replaced == (0, 0) and not p.empty


def test_link_keys_changed_after_the_copy(world, tmp_path):
    copy = world.take_copy(tmp_path / "c")
    world.link(1, "GWPUB1-NEW", "VPSPUB1-NEW")
    assert _cases(_plan(world, copy)) == {1: "rekeyed"}


def test_another_device_in_the_slot(world, services, tmp_path):
    copy = world.take_copy(tmp_path / "c")
    other = services.add_device(world.admin.id, "Pi-новый").device_id
    services.db.gateway_update(1, device_id=other)
    assert _cases(_plan(world, copy)) == {1: "device"}


def test_a_twin_appeared(world, services, tmp_path):
    copy = world.take_copy(tmp_path / "c")
    twin = services.add_device(world.admin.id, "NASPi-двойник").device_id
    _sql(services.db, "UPDATE devices SET twin_of = ? WHERE id = ?", world.gw1, twin)
    assert _cases(_plan(world, copy)) == {1: "twin"}


def test_removed_with_the_journal_and_before_it(world, services, tmp_path):
    gw2 = services.add_device(world.admin.id, "Pi4").device_id
    world.slot(2, gw2)
    copy = world.take_copy(tmp_path / "c")
    services._run_link_script = lambda mode, env=None: None
    services.gateway_remove(2)
    journal = json.loads(services.db.get_state("gw_removed"))
    assert journal[-1]["slot"] == 2 and journal[-1]["link_gw_pub"] == "GWPUB2"
    assert _cases(_plan(world, copy)) == {1: "same", 2: "removed"}
    services.db.set_state("gw_removed", "")          # снят до 3.4.0: журнал о нём не знает
    assert _cases(_plan(world, copy)) == {1: "same", 2: "removed"}, "на непустом сервере слот воскрес"


def test_added_after_the_copy(world, services, tmp_path):
    copy = world.take_copy(tmp_path / "c")
    gw2 = services.add_device(world.admin.id, "Pi4").device_id
    world.slot(2, gw2)
    assert _cases(_plan(world, copy)) == {1: "same", 2: "added"}


def test_an_empty_server_gets_the_gateway_part_and_with_a_journal_nothing_returns(world, services, tmp_path):
    copy = world.take_copy(tmp_path / "c")
    _sql(services.db, "DELETE FROM gateways")
    p = _plan(world, copy)
    assert p.line == "same" and not p.empty and _cases(p) == {1: "removed"}, \
        "на той же линии «слотов нет» — значит сняли, а не новый сервер"
    services.db.set_state("db_line", "0123456789abcdef")    # другой сервер: слотов нет и никто не снимал
    p = _plan(world, copy)
    assert p.empty and _cases(p) == {1: "restored"} and p.copy_slots() == [1]
    services.db.set_state("gw_removed", json.dumps([{"slot": 1, "at": "2026-10-01T00:00:00+03:00"}]))
    p = _plan(world, copy)
    assert not p.empty and _cases(p) == {1: "removed"}, "снятый слот воскрес на пустом сервере с журналом"


def test_the_kind_of_line(world, services, tmp_path, make_active_client):
    copy = world.take_copy(tmp_path / "c")
    services.db.set_state("db_line", "0123456789abcdef")      # другой сервер с профилями
    p = _plan(world, copy)
    assert p.line == "foreign" and p.replaced == (1, 1), p.replaced   # Иван и его телефон; шлюз — текущий
    broken = tmp_path / "broken.db"; broken.write_bytes(b"junk")
    assert _plan(world, copy, cur_db=str(broken)).line == "unreadable"
    _sql(services.db, "DELETE FROM clients WHERE tg_id = 2001")
    p = _plan(world, copy)
    assert p.line == "clean" and p.replaced == (0, 0), "у чистого сервера пропадать нечему"


def test_the_bot_plan_and_the_merge_plan_are_the_same(world, services, tmp_path, monkeypatch):
    """Бот показывает план в вопросе, слияние исполняет тот же: по одному и
    тому же файлу бэкапа случаи слотов совпадают."""
    import io
    import tarfile
    from awgbot.domain.backup import merge, meta as meta_mod
    from awgbot.infra import awg
    from awgbot.util import secrets_util
    gw2 = services.add_device(world.admin.id, "Pi4").device_id
    world.slot(2, gw2)
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "bk")
    monkeypatch.setattr(config, "DB_PATH", world.db.path)
    monkeypatch.setattr(config, "CONF_DIR", world.conf)
    monkeypatch.setattr(config, "ENV_PATH", world.env)
    monkeypatch.setattr(meta_mod, "ROOT_DIR", str(world.gwroot))
    monkeypatch.setattr(awg, "read_file", lambda p: (_ for _ in ()).throw(awg.AwgError("нет")))
    services.backup_set_passphrase("correct horse battery")
    blob = open(services.make_backup()[0], "rb").read()
    world.link(1, "GWPUB1-NEW", "VPSPUB1-NEW")           # после бэкапа сменились ключи линка слота 1
    info = services.inspect_backup(blob, "x.tgz.enc")
    assert info["ok"], info
    bot = {s["slot"]: s["case"] for s in info["plan"]["slots"]}
    copy = tmp_path / "copy"
    plain = secrets_util.decrypt(blob, passphrase="correct horse battery")
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:*") as tar:
        tar.extractall(copy, filter="data")
    report = merge.run(str(copy), cur_db=world.db.path, conf_dir=str(world.conf), env_path=str(world.env),
                       awg_dir=str(world.awg), root_dir=str(world.gwroot), client_if="awg0")
    assert bot == {1: "rekeyed", 2: "same"}
    assert {s["slot"]: s["case"] for s in report["slots"]} == bot


def test_a_slot_removed_and_assigned_again_after_the_copy(world, services, tmp_path):
    """Снят после бэкапа и назначен заново: в бэкапе — снятый, сейчас —
    добавленный; копия настроек снятого новому не предлагается."""
    copy = world.take_copy(tmp_path / "c")
    services._run_link_script = lambda mode, env=None: None
    services.gateway_remove(1)
    other = services.add_device(world.admin.id, "Pi-новый").device_id
    world.slot(1, other, "GWPUB1-B", "VPSPUB1-B")
    p = _plan(world, copy)
    assert [(s.slot, s.case) for s in p.slots] == [(1, "removed"), (1, "added")]
    assert p.current_slots() == [1] and p.offer_slots() == [] and p.removed_slots() == []


def test_the_server_address_is_kept_even_when_it_is_the_same(world, tmp_path):
    """Адрес сервера в плане — отдельно от признака «отличается»: пустой
    сервер с тем же адресом называет его, а не «по адресу <code></code>»."""
    from awgbot.domain.backup.plan import Plan
    from awgbot.domain.notices import backup as nb
    copy = world.take_copy(tmp_path / "c")
    p = _plan(world, copy)
    assert p.host_copy == p.host_cur == "vpn.example.org", (p.host_copy, p.host_cur)
    empty = Plan.from_dict({**p.to_dict(), "line": "clean", "empty": True,
                            "slots": [{"slot": 1, "case": "restored", "name": "NASPi", "label": "", "link_if": "awglink"}]})
    text = nb.restore_plan(empty)
    assert "по адресу <code>vpn.example.org</code>" in text and "<code></code>" not in text, text
    assert "В бэкапе адрес сервера" not in text, "одинаковый адрес выдан за другой"


def test_the_plan_closes_the_copy_database(world, tmp_path, monkeypatch):
    """«with sqlite3.connect(...)» — транзакция, а не закрытие: соединение с базой
    копии держало бы файл (и дескриптор) до сборщика мусора, план считается на
    каждый показ экрана восстановления. После плана все соединения закрыты."""
    import sqlite3
    from awgbot.domain.backup import plan
    opened = []
    real = sqlite3.connect

    def connect(*a, **kw):
        c = real(*a, **kw)
        opened.append(c)
        return c
    monkeypatch.setattr(plan.sqlite3, "connect", connect)
    _plan(world, world.take_copy(tmp_path / "c"))
    assert opened, "план не открыл ни одной базы — тест ничего не проверил"
    for c in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            c.execute("SELECT 1")
