"""
Прыжок через ступени: хост на минимальной поддерживаемой версии (v2.10.0 —
манифест ядра, файл состояния, единый архив) обновляется на текущую одним
шагом. Всё, что промежуточные версии делали с данными, обязано сработать разом:
миграции БД идемпотентны, conf досеивается, отсутствующие ключи закрыты
дефолтами, поколение усыновляется как первое.

Данные образца v2.10.0 создаются КОДОМ v2.10.0 (git archive во временный
каталог, отдельный интерпретатор), а не рукописной схемой: рукописная копия
разошлась бы с реальностью первой же правкой.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FLOOR_TAG = "v2.10.0"


def _have_tag() -> bool:
    r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "-q", "--verify", f"{FLOOR_TAG}^{{commit}}"],
                       capture_output=True, text=True)
    return r.returncode == 0


@pytest.fixture(scope="module")
def floor_host(tmp_path_factory):
    """Каталоги conf/ и data/ так, как их оставила бы установка v2.10.0 после
    первого старта бота: шаблоны conf той версии, БД её схемы с админом,
    клиентом и устройством."""
    if not _have_tag():
        pytest.skip(f"нет тега {FLOOR_TAG} в репозитории")
    base = tmp_path_factory.mktemp("floor")
    tree = base / "tree"; tree.mkdir()
    tar = subprocess.run(["git", "-C", str(ROOT), "archive", FLOOR_TAG],
                         capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(tree)], input=tar, check=True)
    conf = base / "conf"; data = base / "data"
    conf.mkdir(); data.mkdir()
    import re
    for f in (tree / "conf").glob("*.yaml"):
        text = f.read_text(encoding="utf-8")
        if f.name == "app.yaml":                     # то, что вписывает установщик
            text = re.sub(r'^  server_host: ""', '  server_host: "203.0.113.10"', text, flags=re.M)
            text = re.sub(r'^  server_port:\s*(#.*)?$', '  server_port: 51820', text, flags=re.M)
        (conf / f.name).write_text(text, encoding="utf-8")
    env = dict(os.environ, AWG_BOT_CONF_DIR=str(conf), AWG_BOT_DATA_DIR=str(data),
               BOT_TOKEN="1:x", ADMIN_ID="1", PYTHONPATH=str(tree))
    seed = """
from awgbot.core import config, settings
from awgbot.infra.db import Database
settings.init(config.CONF_DIR)
db = Database(config.DB_PATH); db.init_schema()
from awgbot.util import timeutil
from datetime import datetime, timedelta
end = timeutil.to_iso(datetime.now(timeutil.TZ) + timedelta(days=100))
cid = db.create_client("Старый клиент", 3, timeutil.now_iso(), end, "code1", period_kind="year")
db.activate_client("code1", 555)
did = db.create_device(cid, "Телефон", "PUB1", "PSK1", "10.8.1.7", private_key="PRIV1")
db.set_state("last_server_ok", "1")
print("SEEDED", cid, did)
"""
    r = subprocess.run([sys.executable, "-c", seed], cwd=tree, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SEEDED" in r.stdout
    return conf, data


@pytest.fixture()
def current_on_floor_host(floor_host, monkeypatch):
    """Текущий код поверх данных v2.10.0 — как после `awg-bot update`."""
    conf, data = floor_host
    monkeypatch.setenv("AWG_BOT_CONF_DIR", str(conf))
    monkeypatch.setenv("AWG_BOT_DATA_DIR", str(data))
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("ADMIN_ID", "1")
    import awgbot.core.config as c
    cfg = importlib.reload(c)
    from awgbot.core import settings
    from awgbot.infra import awglock
    settings.init(conf)
    monkeypatch.setattr(awglock, "STATE_PATH", conf.parent / "awg.state")
    yield cfg, conf, data
    monkeypatch.delenv("AWG_BOT_CONF_DIR"); monkeypatch.delenv("AWG_BOT_DATA_DIR")
    importlib.reload(c)
    settings.init(c.CONF_DIR)


def test_current_code_opens_and_migrates_the_floor_database(current_on_floor_host):
    cfg, conf, data = current_on_floor_host
    from awgbot.infra.db import Database
    db = Database(cfg.DB_PATH)
    db.init_schema()                                  # все миграции разом, повторно — тоже
    db.init_schema()
    clients = db.list_clients()
    assert [c.name for c in clients] == ["Старый клиент"]
    dev = db.list_devices(clients[0].id)[0]
    assert dev.name == "Телефон" and dev.address == "10.8.1.7" and dev.private_key == "PRIV1"
    assert dev.is_gateway == 0 and dev.iface == "" and dev.twin_of is None
    assert db.count_devices(clients[0].id) == 1
    assert db.get_state("last_server_ok") == "1"
    db.close()


def test_current_code_validates_the_floor_conf_and_serves_screens(current_on_floor_host):
    """Ключи, появившиеся после v2.10.0, отсутствуют в conf — код обязан жить на
    дефолтах, а экраны рисоваться."""
    cfg, conf, data = current_on_floor_host
    from awgbot.core import settings
    from awgbot.infra.db import Database
    from awgbot.domain.services import Services
    cfg.validate()
    assert settings.get("app.network.subnet_prefix") == "10.8.1"
    db = Database(cfg.DB_PATH); db.init_schema()
    services = Services(db)
    screen = services.server_screen()
    assert screen["subnet"] == "10.8.1.0/24" and screen["iface"] == cfg.AWG_INTERFACE
    assert screen["port_conf"] == 51820 and screen["host"] == "203.0.113.10"
    assert settings.get_bool("firewall.enabled", False) is False, "ключей firewall.* в conf v2.10 нет — дефолт"
    assert services.device_slots(services.db.list_clients()[0].id) == (1, 3)
    db.close()


def test_floor_host_without_state_file_is_generation_one(current_on_floor_host):
    """v2.10.0 файла состояния не заводила — текущий код считает хост первым
    поколением, а поставка того же поколения переезда не требует."""
    from awgbot.infra import awglock
    assert not awglock.STATE_PATH.exists()
    assert awglock.applied_generation() == 1
    assert awglock.needs_migration() is (awglock.generation() > 1)


def test_floor_updater_will_find_the_current_delivery(tmp_path):
    """Апдейтер v2.10.0 ищет в архиве `*/awgbot/__main__.py` не глубже трёх
    уровней и передаёт управление новому скрипту. Пока раскладка архива это
    держит, прыжок с floor'а возможен."""
    dist = tmp_path / "dist"
    r = subprocess.run(["bash", str(ROOT / "build_release.sh"), str(dist)], cwd=ROOT,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:]
    names = subprocess.run(["tar", "-tzf", str(dist / "awg-bot.tgz")],
                           capture_output=True, text=True, check=True).stdout.split()
    mains = [n for n in names if n.endswith("awgbot/__main__.py")]
    assert mains and all(n.strip("./").count("/") <= 2 for n in mains), mains
