"""Сервер пары для стенда: сервисы основного бота поверх своей базы, канал пары
(PairServer / PairClient) по настоящему линку AWG и задания резерва
планировщика (runtime/scheduler/reserve.py) — в сетевом пространстве узла
(`lab-A`, `lab-B`), Bot API — макет `tests.lab.botapi`.

Это не `python -m awgbot`: старт основного бота трогает хост целиком (интерфейсы,
файервол, юниты, резолвер), а на одной ВМ стоят два сервера. Здесь работает
ровно то, на чём держится резерв: база с журналом, сервисы, канал пары,
такты журнала, пакета, переезда и живости, исполнитель действий
(runtime/rsvactions), управляемый поллинг (runtime/pollctl) на макете.

Подменено то, чего на стенде нет (мир, а не код резерва):
- DNS: системный резолвер и авторитетный сервер записи — файл `dns.json`
  общего каталога стенда (`LAB_SHARED`), провайдер записи пишет в тот же файл;
- шлюз с агентом новой линии (`reserve_has_gateway`) — есть: агентов шлюзов
  стенд не поднимает (голосующие шлюзы кворума — заглушки gwnode, LAB_GW_SLOTS);
- достижимость Telegram (`_tg_reachable`) — TCP до макета Bot API;
- процесс открытия: venv стенда — в /home пользователя, а песочница скрывает
  его (ProtectHome); как в s04_open_sandbox — tmpfs с привязкой venv, и юнит
  идёт в сетевом пространстве узла (на хосте это пространство самого хоста).

Каталог узла (`--work`): conf/ (app.yaml и прочие из conf/ репозитория), env,
data/ (bot.db, reserve/), awg/ (конфиги awg0 и awgpair), cmd/ (команды —
файлами), status.json (раз в секунду), node.log.

    python -m tests.lab.pairnode setup --work W --name Alpha --role inviter ...
    ip netns exec lab-A env AWG_BOT_CONF_DIR=W/conf AWG_BOT_DATA_DIR=W/data \\
        AWG_BOT_ENV=W/env python -m tests.lab.pairnode run --work W

Команды (файл cmd/<имя>, содержимое — аргументы JSON): load {n, prefix, pause, base},
set {name, field, value}, code {out}, move {},
return {}, backup {out}, qpause {seconds} (кворум: плановая пауза лидера перед остановкой),
fw {enabled} (форма файервола хоста и перерисовка), card {} (текст карточки «🛟 Резерв»),
force {dry, tg_silent} (рычаг «вернуть без резерва» путём кнопки: обе проверки и сам рычаг;
dry — только проверки, tg_silent — Telegram не ответил на getWebhookInfo), c9drop {} («🗑 Не
ждать» отметки С9).

Сроки резерва сжимает окружение `LAB_CONST` (`модуль:ИМЯ=число,ИМЯ=число;модуль2:…`) —
константы модулей подменяются до старта (у гостя — тоже, guest_main).
`LAB_OPEN_RECORD=1` — запись при аварийном открытии меняет сам процесс открытия (как на
хостах, done без record=skipped): его модуль запускается обёрткой `tests/lab/rsvopen_lab.py` с
миром DNS стенда (каталог `base-rec`, общий каталог стенда — на запись в песочнице). Отметка
`shared/auth.hold` — авторитетный сервер записи не отвечает (факт записи не подтверждается).

`bloat --mb N` (до старта, захват журнала выключен) — база узла раздута списком доменов
профиля админа до N МБ: снимок пакета несёт его, событий журнала нет.

РФ-доступ (LAB_RT=1, s08_rf_transit): `rtseed` — слот 1 с линком `awglink` и пользователь на awg0;
в работе — файервол хоста, наборы и цепочка маркировки (адрес «РФ-сайта» LAB_RF_SET — в набор
пользователя вместо dnsmasq), такт живости слотов с тактом rfpeer, опрос трафика (таблица учёта);
в status.json — блок `rt`.
Итог команды — cmd/<имя>.done (JSON) или cmd/<имя>.err.

Шлюзы основы с кворумом (`LAB_GW_SLOTS=1,2`, сценарии кворума): их агентов заменяет
`tests/lab/gwnode.py` — настоящий голосующий в пространстве шлюза. Для основы (сторона
`a`) слоты есть в составе её линии с возможностью `quo1` (строк слотов в базе нет:
линков слотов стенд не поднимает), снимок слота — поле `quo` из файла шлюза, живость
канала конфигурации — его отметка, раздел `quorum` документа — файлом шлюзу; слова
каналов шлюзов (гостевой у резерва, конфигурации у основы) — шина `tests/lab/labbus`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_FUTURE = "2099-01-01T00:00:00+03:00"
_START = "2026-01-01T00:00:00+03:00"


# ── подготовка каталога узла (до импорта awgbot: config читает окружение) ──────

def setup(a: argparse.Namespace) -> None:
    import yaml
    w = Path(a.work)
    for d in ("conf", "data/reserve", "awg", "cmd", "base"):
        (w / d).mkdir(parents=True, exist_ok=True)
    for f in (ROOT / "conf").glob("*.yaml"):
        shutil.copy(f, w / "conf" / f.name)
    app = yaml.safe_load((w / "conf" / "app.yaml").read_text(encoding="utf-8"))
    app.setdefault("network", {}).update(server_host=a.host, self_host=a.self_ip, server_port=int(a.port),
                                         subnet_prefix=a.subnet, subnet_cidr=f"{a.subnet}.0/24")
    app.setdefault("awg", {})["conf_dir"] = str(w / "awg")
    app.setdefault("client_config", {})["server_name"] = a.name
    if os.environ.get("LAB_FAST") == "1":
        # keepalive линка пары берут отсюда и reserve-pair-setup.sh, и планка порога подозрения
        # (liveness.pair_suspect_floor_s): без ключа планка 40 перекрывала бы suspect_seconds 15
        app["client_config"]["keepalive_seconds"] = str(LAB_FAST_PAIR_KEEPALIVE)
    (w / "conf" / "app.yaml").write_text(yaml.safe_dump(app, allow_unicode=True), encoding="utf-8")
    rsv = yaml.safe_load((w / "conf" / "reserve.yaml").read_text(encoding="utf-8"))
    rsv.update(ready=True, pkg_minutes=1)
    lab_digits_into(rsv)
    (w / "conf" / "reserve.yaml").write_text(yaml.safe_dump(rsv, allow_unicode=True), encoding="utf-8")
    (w / "env").write_text(f"BOT_TOKEN={a.token}\nADMIN_ID={a.admin}\n", encoding="utf-8")
    os.chmod(w / "env", 0o600)
    # BASE_DIR процесса открытия: venv и код — ссылками (на хостах это /opt/awg-bot)
    for name, target in (("venv", Path(a.venv)), ("awgbot", ROOT / "awgbot"), ("tools", ROOT / "tools"),
                         ("install", ROOT / "install")):
        link = w / "base" / name
        if not link.exists():
            link.symlink_to(target)


def seed(a: argparse.Namespace) -> None:
    """База узла: схема, строка пары (как после сопряжения), первое поколение ключей."""
    from awgbot.core import config, settings
    settings.init(config.CONF_DIR)
    from awgbot.domain.services import Services
    from awgbot.infra.db import Database
    db = Database(config.DB_PATH)
    db.init_schema()
    svc = Services(db)
    svc.ensure_admin_client()
    db.reserve_peer_set(name=a.peer_name, host=a.peer_host, pair_if="awgpair", pair_port=47101, peer_port=0,
                        pair_cidr="10.99.98.0/30", listener=a.role == "inviter", bot_username=f"{a.peer_name.lower()}_bot",
                        peer_admin_id=int(a.peer_admin))
    svc.reserve_keys_init()
    db.close()


def bloat(a: argparse.Namespace) -> None:
    """База узла больше N МБ (s13): домены списка профиля админа — таблица решений, едет в
    снимке пакета; до старта узла захват журнала выключен — событий нет. Сжимаются хорошо:
    пакет мал, база велика (потолки памяти — от базы, а не пакета)."""
    from awgbot.core import config, settings
    settings.init(config.CONF_DIR)
    from awgbot.infra.db import Database
    db = Database(config.DB_PATH)
    db.init_schema()
    admin = db.get_client_by_tg(int(config.ADMIN_ID))
    con = db._connection()
    want = int(a.mb) * 1024 * 1024
    i = 0
    while os.path.getsize(config.DB_PATH) < want:
        con.executemany("INSERT INTO client_routing_domains (client_id, domain, added_at) VALUES (?, ?, ?)",
                        [(int(admin.id), f"d{i + k:08d}.bloat.lab.test", _START) for k in range(20000)])
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")          # размер — у файла базы, не WAL
        i += 20000
    db.close()
    print(json.dumps({"rows": i, "size": os.path.getsize(config.DB_PATH)}))


def rtseed(a: argparse.Namespace) -> None:
    """РФ-доступ узла (s08_rf_transit): слот 1 с линком `awglink` до шлюза стенда (устройство
    шлюза — запись без ключа в awg0, как у подключённого шлюза) и, если задан ключ, пользователь
    с устройством на awg0 узла — РФ-доступ разрешён и включён."""
    from awgbot.core import config, settings
    settings.init(config.CONF_DIR)
    from awgbot.infra.db import Database
    db = Database(config.DB_PATH)
    db.init_schema()
    admin = db.get_client_by_tg(int(config.ADMIN_ID))
    gw_dev = db.create_device(int(admin.id), "Шлюз стенда", pubkey(), "LAB-DUMMY-psk", f"{config.SUBNET_PREFIX}.250")
    db.gateway_add(gw_dev, a.link_if, int(a.link_port), a.link_cidr, preferred=True, slot_id=1)
    if a.user_pub:
        cid = db.create_client(a.user_name, 3, _START, _FUTURE, "LAB-INV-rt")
        db.activate_client(cid, int(a.user_tg))
        did = db.create_device(cid, "Телефон", a.user_pub, a.user_psk or "LAB-DUMMY-psk", a.user_addr)
        db.update_client_fields(cid, routing_allowed=1)
        db.update_device_fields(did, routing_on=1)
    db.close()


# ── мир стенда ──────────────────────────────────────────────────────────────

class LabDns:
    """Запись DNS стенда: имя → адрес в общем файле; провайдер пишет туда же."""

    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self.path)

    def read_system(self, name: str, timeout: float = 3.0) -> list[str]:
        v = self._read().get(name)
        return [v] if v else []

    def _held(self) -> bool:
        return (self.path.parent / "auth.hold").exists()     # авторитетный сервер не отвечает (рычаг сценария)

    def read_authoritative(self, name: str) -> list[str]:
        return [] if self._held() else self.read_system(name)

    def read_authoritative_ttl(self, name: str) -> tuple[list[str], int]:
        got = self.read_authoritative(name)
        return got, (30 if got else 0)

    def provider_for(self, cfg: dict):
        world = self

        class Provider:
            id = "lab"
            human = "lab"

            def set(self, cfg: dict, ip: str) -> None:
                data = world._read()
                data[str(cfg.get("name") or os.environ.get("LAB_RECORD", ""))] = ip
                world._write(data)
                # рычаг сценария (s14): правка легла, а процесс погиб до ответа — один раз, файл снимается
                kill = world.path.parent / "set.kill"
                if kill.exists():
                    import signal
                    kill.unlink(missing_ok=True)
                    os.kill(os.getpid(), signal.SIGKILL)

            def check(self, cfg: dict) -> str:
                return world.read_system(os.environ.get("LAB_RECORD", ""))[0] if world.read_system(
                    os.environ.get("LAB_RECORD", "")) else ""

            def read(self, cfg: dict) -> str:
                return self.check(cfg)
        return Provider()


# Быстрый уровень стенда (LAB_FAST=1): цифры кворума и «здоров» — настройками узлов, как их
# поставил бы админ (reserve.yaml); шлюзам они уходят документом, гостю — пакетом. Связка
# Timing.check держит пределы: P 5 → W ≥ 22 → up ≥ 52, T_tie > up. «Не вижу» — 30 с, не 3·P:
# при включении голосования документ доходит до шлюза раньше, чем ответчик лидера слушает, а
# первый ответ идёт ещё и после рукопожатия AWG нового пира-зонда — на стенде до 21 с от
# сокета зонда; при 15 и 20 с шлюз успевал сказать «не вижу» и держать «да» обязательством до
# pledge_minutes (теперь резерв снимает такое обязательство `qrel` сразу после «вижу» — s06 пункт «и», —
# но ложное «нет» в счёте кандидата и лишний ход взгляда остаются). Открытие ≈ no + up = 85 с вместо 145, ничья — 60 с вместо 180.
LAB_FAST_DIGITS = {"quorum": {"probe_seconds": 5, "no_seconds": 30, "yes_window_seconds": 22, "down_seconds": 10,
                              "up_seconds": 55, "unfence_seconds": 10, "ask_repeat_seconds": 10, "grace_seconds": 40,
                              "pause_seconds": 100, "tie_clear_seconds": 20, "tie_minutes": 1},
                   "rf_block_minutes": 2, "healthy_seconds": 60,
                   # живость линка пары: подозрение — тишина дольше keepalive + такт 5 с (8 + 5 < 15), проба — два
                   # ping через 5 с; «не на связи» ≈ 25 с вместо 55 — каждый разрез пары и гибель сервера в s05, s09–s12
                   "suspect_seconds": 15, "probe_seconds": 10}
LAB_FAST_PAIR_KEEPALIVE = 8                  # PersistentKeepalive awgpair стенда на быстром уровне (на хостах — 25)


def lab_digits_into(rsv: dict) -> None:
    """Цифры быстрого уровня — поверх раздела вида reserve.yaml (без LAB_FAST — как есть)."""
    if os.environ.get("LAB_FAST") != "1":
        return
    rsv.setdefault("quorum", {}).update(LAB_FAST_DIGITS["quorum"])
    rsv.update({k: v for k, v in LAB_FAST_DIGITS.items() if k != "quorum"})


def lab_timing():
    """Timing узлов стенда при текущем уровне — тем же разбором, что у сервера."""
    from awgbot.util.quorum.timing import Timing
    import yaml
    rsv = yaml.safe_load((ROOT / "conf" / "reserve.yaml").read_text(encoding="utf-8"))
    lab_digits_into(rsv)
    return Timing.from_settings(rsv.get("quorum") or {}, rsv.get("rf_block_minutes")), int(rsv.get("healthy_seconds") or 300)


def digits(_a: argparse.Namespace) -> None:
    """Цифры уровня строками оболочки — сценариям (pairlab.sh): пределы проверок от них, не числами."""
    t, healthy = lab_timing()
    bad = t.check()
    if bad:
        raise SystemExit(f"цифры стенда не сходятся: {bad}")
    print(f"QT_PAIR_KEEPALIVE={LAB_FAST_PAIR_KEEPALIVE if os.environ.get('LAB_FAST') == '1' else 25}")
    for k, v in (("PROBE", t.probe_s), ("NO", t.no_s), ("W", t.yes_window_s), ("DOWN", t.down_s), ("UP", t.up_s),
                 ("UNFENCE", t.unfence_s), ("TIE", t.tie_s), ("TIE_CLEAR", t.tie_clear_s), ("RF", t.rf_block_s),
                 ("DOWN_RF", t.down_rf_s), ("GRACE", t.grace_s), ("PAUSE", t.pause_s), ("PLEDGE", t.pledge_s),
                 ("HEALTHY", healthy)):
        print(f"QT_{k}={int(v)}")


# Быстрый уровень, константы модулей всем узлам (до LAB_CONST сценария — он их перекрывает): пакет после
# события — через 3 с, не 10 (подъём пары ждал две такие паузы подряд: отметка старта и «сначала снимок»)
LAB_FAST_CONST = "awgbot.domain.services.reserve.package:PKG_BATCH_SECONDS=3"


def lab_consts() -> None:
    """Сроки резерва, сжатые для сценария: LAB_CONST=`модуль:ИМЯ=число,ИМЯ=число;модуль2:…` —
    константа модуля подменяется до старта (модуль читает её при вызове, не при импорте); на
    быстром уровне сначала LAB_FAST_CONST."""
    import importlib
    import logging
    base = LAB_FAST_CONST if os.environ.get("LAB_FAST") == "1" else ""
    for part in f"{base};{os.environ.get('LAB_CONST') or ''}".split(";"):
        mod, _, pairs = part.strip().partition(":")
        if not mod or not pairs:
            continue
        m = importlib.import_module(mod)
        for kv in pairs.split(","):
            name, _, val = kv.partition("=")
            if not hasattr(m, name.strip()):
                raise SystemExit(f"LAB_CONST: у {mod} нет {name.strip()}")
            setattr(m, name.strip(), type(getattr(m, name.strip()))(float(val)))
            logging.getLogger("lab").info("стенд: %s.%s = %s", mod, name.strip(), val)


def _rec_base(base: Path) -> Path:
    """BASE_DIR процесса открытия с миром DNS стенда (LAB_OPEN_RECORD=1): те же ссылки, что у
    base, а `tools` — каталог, где `rsvopen` — обёртка tests/lab/rsvopen_lab.py."""
    rec = base.parent / "base-rec"
    (rec / "tools").mkdir(parents=True, exist_ok=True)
    for name in ("venv", "awgbot", "install"):
        if not (rec / name).exists():
            (rec / name).symlink_to(os.readlink(base / name))
    (rec / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (rec / "tools" / "rsvopen.py").write_text(
        "import runpy, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "runpy.run_module('tests.lab.rsvopen_lab', run_name='__main__')\n", encoding="utf-8")
    return rec


def lab_world(svc, shared: Path) -> None:
    from awgbot.domain.services.reserve import openproc
    from awgbot.util import dnsapi
    dns = LabDns(shared / "dns.json")
    for name in ("read_system", "read_authoritative", "read_authoritative_ttl", "provider_for"):
        setattr(dnsapi, name, getattr(dns, name))
    svc.reserve_has_gateway = lambda: True
    # запись DNS у основы — «автоматическая» (провайдер подменён LabDns): конверт DNS едет в пакете, и
    # аварийное открытие на резерве разрешено; саму запись меняет сценарий (у стенда нет провайдера
    # и авторитетного DNS), поэтому процессу открытия — set_record=False, отметку tg_take кладёт сценарий;
    # LAB_OPEN_RECORD=1 — запись меняет процесс открытия миром DNS стенда (rsvopen_lab), как на хостах
    svc.rsv_update("rsv_dns", provider="digitalplat", token="lab")
    open_record = os.environ.get("LAB_OPEN_RECORD") == "1"
    real_launch = svc._rsvopen_launch

    def launch(payload: dict, keep_stdin: bool = False) -> None:
        real_launch(payload if open_record else {**payload, "set_record": False}, keep_stdin)
    svc._rsvopen_launch = launch
    api = os.environ.get("LAB_BOTAPI", "")

    def tg_reachable() -> bool:
        import socket
        from urllib.parse import urlparse
        u = urlparse(api)
        try:
            with socket.create_connection((u.hostname, u.port or 80), timeout=3):
                return True
        except OSError:
            return False
    svc._tg_reachable = tg_reachable
    # скрипт линка пары берёт каталог конфигов AWG из окружения (на хосте — умолчание /etc/amnezia/amneziawg,
    # совпадающее с awg.conf_dir бота): у узла стенда он свой, иначе --rollback снял бы чужой файл
    real_pair_env = svc._pair_env
    svc._pair_env = lambda: {**real_pair_env(), "CONF_DIR": str(config.AWG_DIR)}
    from awgbot.core import config
    config.BASE_DIR = Path(os.environ["AWG_BOT_CONF_DIR"]).parent / "base"   # venv и код — ссылками (setup)
    if open_record:
        config.BASE_DIR = _rec_base(config.BASE_DIR)
    real_props = openproc.rsvopen_sandbox_props
    venv = os.environ.get("LAB_VENV", "")
    netns = os.environ.get("LAB_NETNS", "")

    def props(base: str, **kw) -> list[str]:
        extra = ["-p", "ProtectHome=tmpfs", "-p", f"BindReadOnlyPaths={venv}"] if venv else []
        if netns:
            extra += ["-p", f"NetworkNamespacePath=/run/netns/{netns}"]
        if open_record:                                        # мир DNS стенда — файл общего каталога
            extra += ["-p", f"ReadWritePaths={shared}", "-p",
                      f"Environment=LAB_SHARED={shared} LAB_RECORD={os.environ.get('LAB_RECORD', '')}"]
        return real_props(base, **kw) + extra
    openproc.rsvopen_sandbox_props = props


# ── РФ-доступ узла (LAB_RT=1, s08_rf_transit) ────────────────────────────────

def lab_rt_world(svc, w: Path) -> None:
    """Мир РФ-доступа на одной ВМ: файл таблицы файервола и dnsmasq у каждого узла свои (на
    хосте — /etc/nftables.d и /etc/dnsmasq.d, общие для обоих пространств стенда), постоянство
    правил (`/etc/nftables.conf`, юнит nftables) — не трогается. Списка доменов нет: адрес
    «РФ-сайта» (LAB_RF_SET) кладётся в набор назначений пользователя, как его положил бы dnsmasq
    по ответу на домен списка."""
    from awgbot.infra import nftguard
    from awgbot.infra.routing import feeds
    nftguard.RULES_FILE = str(w / "guard.nft")
    nftguard.ensure_persistence = lambda: []

    def write_dnsmasq_conf(text: str, path: str = None) -> bool:
        (w / "dnsmasq.conf").write_text(text, encoding="utf-8")
        return False
    feeds.write_dnsmasq_conf = write_dnsmasq_conf


def lab_rt_reconcile(svc) -> None:
    """Реконсиляция РФ-доступа (наборы, цепочка маркировки) и адрес «РФ-сайта» в наборах."""
    import subprocess
    from awgbot.core import config
    from awgbot.infra import routing
    svc.reconcile_routing()
    for cid in svc.db.routing_allowed_client_ids(config.ADMIN_ID):
        for addr in (os.environ.get("LAB_RF_SET") or "").split(","):
            if addr.strip():
                subprocess.run(["ipset", "add", routing.user_set(cid), addr.strip(), "-exist"], capture_output=True)


async def lab_rt_job(svc, bot) -> None:
    """Такт живости слотов и РФ-доступа через второй сервер — как job_routing_liveness основы
    (runtime/scheduler/client.py): вердикты слотов, затем такт rfpeer; уведомления — в макет."""
    import logging
    from awgbot.bot.notifier import send_notifications
    try:
        await send_notifications(bot, await asyncio.to_thread(svc.routing_liveness_tick))
    except Exception as e:                                     # noqa: BLE001
        logging.warning("routing_liveness: %s", e)
    try:
        await send_notifications(bot, await asyncio.to_thread(svc.reserve_rfpeer_tick))
    except Exception as e:                                     # noqa: BLE001
        logging.warning("rfpeer: %s", e)


def _rt_status(svc) -> dict:
    """РФ-доступ узла для сценария: слоты, активный, вердикт, согласие и слот транзита, `st`
    второй стороны, счётчики транзита, AllowedIPs линка пары, правило 151."""
    from awgbot.infra.routing import rfpeer as rfp
    out: dict = {}
    for key, fn in (("paired", lambda: svc.reserve_paired()), ("slots", lambda: [g.id for g in svc.rt_slots()]),
                    ("active", lambda: (svc.active_gateway().id if svc.active_gateway() else None)),
                    ("raw_active", lambda: svc.db.get_state(svc._RT_ACTIVE_KEY)),
                    ("link_ok", lambda: svc.routing_link_ok()), ("engaged", lambda: svc.routing_engaged()),
                    ("offer", lambda: list(svc.reserve_rf_offer())),
                    ("rfpeer", lambda: svc.rsv_get("rsv_rfpeer")),
                    ("peer_rfp", lambda: svc.reserve_peer_state().get("rfp")),
                    ("file", lambda: rfp.read() if rfp.rfpeer_path().exists() else None),
                    ("counters", lambda: svc.reserve_rf_peer_counters()),
                    ("allowed", lambda: rfp.allowed_ips(str(rfp.pair_conf().get("pub") or ""))),
                    ("rule151", lambda: rfp.rule_present(str(rfp.pair_conf().get("peer4") or "-"))),
                    ("windows", lambda: {str(k): v for k, v in (svc.__dict__.get("_rt_windows") or {}).items()}),
                    ("ups", lambda: {str(g.id): svc.db.get_state(f"routing_gw_{g.id}_up_streak") for g in svc.rt_slots()})):
        try:
            out[key] = fn()
        except Exception as e:                                 # noqa: BLE001
            out[key] = f"{type(e).__name__}: {e}"
    return out


# ── шлюзы основы с кворумом (gwnode) ─────────────────────────────────────────

def lab_gateways(svc, pair: Path, slots: list[int]) -> dict:
    """Слоты шлюзов-заглушек в мире узла; возвращает состояние для такта `lab_gw_step`."""
    from types import SimpleNamespace
    from awgbot.runtime import rsvactions
    from awgbot.util.quorum import members as qm
    shared, bus = pair / "shared", pair / "bus"
    caps = ["cfg1", "rsv1", "quo1", "rsvtg1"]

    def alive(slot: int) -> bool:
        try:
            return time.time() - float((shared / f"gw{slot}.link.alive").read_text()) < 15
        except (OSError, ValueError):
            return False

    def snap(slot: int) -> dict:
        try:
            return json.loads((shared / f"gw{slot}.snap.json").read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            return {}

    real_caps, real_snap, real_sess = svc.gwlink_agent_caps, svc.gwlink_snapshot, svc.gwlink_session
    svc.gwlink_agent_caps = lambda slot: list(caps) if int(slot) in slots else real_caps(slot)
    svc.gwlink_snapshot = lambda slot: {"quo": snap(int(slot))} if int(slot) in slots else real_snap(slot)
    svc.gwlink_session = lambda slot: ({"caps": list(caps)} if alive(int(slot)) else {}) if int(slot) in slots else real_sess(slot)
    me = "A" if svc.reserve_quorum_side() == "a" else "B"
    if me == "A":                                              # шлюзы стенда — линии основы; у резерва своих нет
        svc._quorum_gw_nodes = lambda side: ([qm.gateway_node(side, s) for s in slots], [])
    real_one = rsvactions._one

    async def one(act: tuple) -> None:
        kind = act[0] if act else ""
        if kind in ("gw", "gwlink") and len(act) > 2 and int(act[1]) in slots:
            from tests.lab import labbus
            labbus.post(bus, f"G{int(act[1])}", {"from": me, "slot": int(act[1]), "ch": "guest" if kind == "gw" else "link",
                                                  "kind": act[2], "body": act[3] if len(act) > 3 else {}})
            return
        await real_one(act)
    rsvactions._one = one
    return {"me": me, "slots": slots, "shared": shared, "bus": bus, "doc_at": 0.0, "SimpleNamespace": SimpleNamespace}


async def lab_gw_step(svc, st: dict) -> None:
    """Входящие слова шлюзов и (у основы) документ слотам и их снимки — раз в 5 с."""
    from awgbot.runtime import rsvactions
    from tests.lab import labbus
    for m in labbus.take(st["bus"], st["me"]):
        slot, kind, body = int(m.get("slot") or 0), str(m.get("kind") or ""), dict(m.get("body") or {})
        try:
            if m.get("ch") == "guest" and st["me"] == "B":
                if kind == "hello":
                    acts = await asyncio.to_thread(svc.reserve_guest_opened, slot, body)
                else:
                    acts = await asyncio.to_thread(svc.reserve_guest_in, slot, kind, body)
            elif m.get("ch") == "link" and st["me"] == "A":
                acts = await asyncio.to_thread(svc.reserve_quorum_link_in, slot, kind, body)
            else:
                acts = []
            await rsvactions.run(acts)
        except Exception as e:                                 # noqa: BLE001
            import logging
            logging.warning("шина стенда: слово %s шлюза %s: %s", kind, slot, e)
    if st["me"] != "A" or time.monotonic() - st["doc_at"] < 5:
        return
    st["doc_at"] = time.monotonic()
    from awgbot.core import config
    for slot in st["slots"]:
        try:
            sec = await asyncio.to_thread(svc.gwcfg_quorum, st["SimpleNamespace"](id=slot))
            state = "away" if svc.reserve_out() == "away" else "home"
            doc = {"quorum": sec, "reserve": {"slot": slot, "name": str(config.SERVER_NAME or ""), "state": state}}
            if sec is None:                                    # почему раздела нет — сценарию в разбор
                from awgbot.util.quorum import members as qm
                m = svc.reserve_quorum_mine() or svc.reserve_quorum_proposed()
                doc["why_none"] = {"m": m.to_dict() if m else None, "own_ip": svc.reserve_own_ip(), "pub": bool(svc._quorum_server_pubkey()),
                                   "port": svc._quorum_server_port(),
                                   "node_rec": bool(((svc.reserve_probes().get(m.l) or {}).get("nodes") or {}).get(
                                       qm.gateway_node(m.l, slot))) if m else None}
            tmp = st["shared"] / f"gw{slot}.cfg.json.tmp"
            tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, st["shared"] / f"gw{slot}.cfg.json")
            await asyncio.to_thread(svc.reserve_quorum_snapshot_in, slot, svc.gwlink_snapshot(slot).get("quo"))
        except Exception as e:                                 # noqa: BLE001
            import logging
            logging.warning("документ шлюза %s: %s", slot, e)


def _quo_status(svc) -> dict:
    """Кворум узла для сценариев: включён ли и почему нет, счёт лидера, кандидат, свои
    взгляды, ограждение, ход исполнения, линия второй стороны."""
    out: dict = {}
    for key, fn in (("why", lambda: svc.reserve_quorum_off_reasons()), ("count", lambda: svc.reserve_quorum_count()),
                    ("cand", lambda: svc.reserve_quorum_candidate_view()), ("views", lambda: svc.reserve_quorum_my_views()),
                    ("fenced", lambda: svc.reserve_quorum_fenced()), ("peer_line", lambda: svc.reserve_quorum_peer_line()),
                    ("mine", lambda: (svc.reserve_quorum_mine().to_dict() if svc.reserve_quorum_mine() else None)),
                    ("keys_on", lambda: svc.reserve_quorum_keys_on()),
                    ("exec", lambda: {k: v for k, v in (svc.__dict__.get("_quo_exec") or {}).items() if k in ("stage", "e", "asked")}),
                    ("gwviews", lambda: {n: v.get("w") for n, v in svc._qgwviews.items()}),
                    ("qlog", lambda: svc.rsv_get("rsv_qlog")), ("moved_by", lambda: svc.rsv_get("rsv_view").get("moved_by"))):
        try:
            out[key] = fn()
        except Exception as e:                                 # noqa: BLE001
            out[key] = f"{type(e).__name__}: {e}"
    out["active"] = out.get("why") == []
    return out


# ── работа ──────────────────────────────────────────────────────────────────

def _status(svc, w: Path) -> dict:
    from awgbot.runtime import pairlink
    meta = svc.db.jrn_meta()
    st = {"t": time.time(), "online": pairlink.online(), "session": svc.reserve_online(), "seen": svc.reserve_peer_seen(),
          "alive": svc._peer_alive(svc.reserve_peer_state()),     # то же «на связи», что у кнопки возврата
          "jrn": {k: meta[k] for k in ("epoch", "holder", "role", "capture", "applied", "acked")},
          "last_i": svc.db.jrn_last_i(), "out": svc.rsv_get("rsv_out"), "in": svc.rsv_get("rsv_in"),
          "poll": svc.rsv_get("rsv_poll"), "pkg_sent": {k: v for k, v in svc.rsv_get("rsv_pkg_sent").items()
                                                          if k in ("gen", "at", "jrn")},
          "pkg_in": {k: v for k, v in svc.rsv_get("rsv_pkg_in").items() if k in ("gen", "at", "m_gen", "db_size")},
          "m_gen": int(svc.reserve_keys().get("m_gen") or 0),
          "peer": {k: v for k, v in svc.reserve_peer_state().items() if k in ("ready", "caps", "version", "inn", "out")},
          "jrn_on": svc.reserve_jrn_on(), "code_window": svc.reserve_code_window(),
          "tail_meta": svc.rsv_get("rsv_tail"), "quo": _quo_status(svc)}
    for key, fn in (("in_tg", lambda: svc.reserve_in_tg()), ("c9", lambda: {k: v for k, v in svc.rsv_get("rsv_c9").items() if k != "k"}),
                    ("c9_view", lambda: svc.reserve_c9_wait_view())):
        try:
            st[key] = fn()
        except Exception as e:                                 # noqa: BLE001
            st[key] = f"{type(e).__name__}: {e}"
    if os.environ.get("LAB_RT") == "1":
        st["rt"] = _rt_status(svc)
    try:
        keep = svc.reserve_keep_store()
        st["keep_last"] = list(keep.last()) if keep is not None else None
    except Exception as e:                                     # noqa: BLE001
        st["keep_last"] = str(e)
    con = svc.db._connection()
    for key, sql in (("clients", "SELECT count(*) FROM clients"),
                     ("tail", "SELECT state, count(*) FROM jrn_tail GROUP BY state")):
        try:
            rows = con.execute(sql).fetchall()
            st[key] = rows[0][0] if key == "clients" else {str(r[0]): int(r[1]) for r in rows}
        except Exception as e:                                 # noqa: BLE001
            st[key] = str(e)
    return st


def pubkey() -> str:
    """Настоящий открытый ключ X25519: пир с ним awg примет (держатель заводит пиры
    устройств, следующий — по применённым событиям)."""
    import base64
    from nacl.public import PrivateKey
    return base64.b64encode(bytes(PrivateKey.generate().public_key)).decode()


def edit(db, name: str, host: int, subnet: str) -> int:
    """Правка решений, как у админа: профиль с устройством — несколько событий журнала."""
    cid = db.create_client(name, 3, _START, _FUTURE, f"LAB-INV-{name}")
    db.create_device(cid, "Телефон", pubkey(), "LAB-DUMMY-psk", f"{subnet}.{host}")
    return cid


def client_id(db, name: str) -> int:
    row = db._connection().execute("SELECT id FROM clients WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise ValueError(f"профиля «{name}» нет")
    return int(row[0])


async def _command(svc, w: Path, name: str, args: dict, bot=None) -> dict:
    from awgbot.core import config
    from awgbot.runtime import rsvactions
    if name == "card":
        from awgbot.bot.texts.reserve import section as st_t
        d = await asyncio.to_thread(svc.reserve_card_data)
        return {"text": st_t.card_text(d, svc.bot_username or "lab_bot")}
    if name == "force":
        # «⚠️ Вернуть без …» → «Да» (handlers/settings/reserve/move.force_go): возможность рычага,
        # слово Telegram (getWebhookInfo, вебхук не снимается), сам рычаг
        from awgbot.bot.handlers.settings.reserve.move import _webhook_info
        from awgbot.domain.services import ServiceError
        why = await asyncio.to_thread(svc.reserve_force_check)
        if why:
            return {"refused": why, "stage": "check"}
        info = None if args.get("tg_silent") else await _webhook_info(bot)
        try:
            await asyncio.to_thread(svc.reserve_force_webhook_check, info)
        except ServiceError as e:
            return {"refused": str(e), "stage": "webhook", "info": info}
        if args.get("dry"):
            return {"go": True, "info": info}
        try:
            acts = await asyncio.to_thread(svc.reserve_force_home)
        except ServiceError as e:
            return {"refused": str(e), "stage": "home", "info": info}
        await rsvactions.run(acts)
        return {"go": True, "info": info, "acts": [a[0] for a in acts]}
    if name == "c9drop":
        from awgbot.bot.notifier import send_notifications
        from awgbot.domain.services import ServiceError
        try:
            notes = await asyncio.to_thread(svc.reserve_c9_drop)
        except ServiceError as e:
            return {"refused": str(e)}
        if notes and bot is not None:
            await send_notifications(bot, notes)
        return {"dropped": notes is not None, "notes": len(notes or [])}
    if name == "load":
        made = []
        for i in range(int(args.get("n", 10))):
            nm = f"{args.get('prefix', 'Нагрузка')}-{i + 1}"
            try:
                await asyncio.to_thread(edit, svc.db, nm, 10 + (int(args.get("base", 0)) + i) % 240, config.SUBNET_PREFIX)
                made.append(nm)
            except Exception as e:                             # noqa: BLE001 — отказ базы — тоже итог
                return {"made": made, "error": f"{type(e).__name__}: {e}"}
            await asyncio.sleep(float(args.get("pause", 0.05)))
        return {"made": made, "last_i": svc.db.jrn_last_i()}
    if name == "set":
        try:
            cid = client_id(svc.db, str(args["name"]))
            await asyncio.to_thread(svc.db.update_client_fields, cid, **{str(args["field"]): args["value"]})
            return {"id": cid, "last_i": svc.db.jrn_last_i()}
        except Exception as e:                                 # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}
    if name == "code":
        Path(args["out"]).write_text(svc.reserve_code(), encoding="utf-8")   # аварийный код — у админа основы
        return {"ok": True}
    if name == "move":
        out = await asyncio.to_thread(svc.reserve_move_start)        # «🔀 Перевести» в карточке резерва
        return {"state": out.get("state") if isinstance(out, dict) else str(out)}
    if name == "return":
        acts = await asyncio.to_thread(svc.reserve_return_start, "button")   # «↩️ Вернуть пользователей»
        await rsvactions.run(acts)
        return {"acts": [a[0] for a in acts]}
    if name == "qpause":
        acts = await asyncio.to_thread(svc.reserve_quorum_pause_actions, int(args.get("seconds", 300)))
        await rsvactions.run(acts)
        return {"acts": [a[0] + ":" + str(a[1]) for a in acts]}
    if name == "fw":
        # форма файервола хоста (firewall.enabled) и перерисовка — как кнопка раздела файервола
        from awgbot.core import settings
        await asyncio.to_thread(settings.set_value, "app.firewall.enabled", bool(args.get("enabled")))
        await asyncio.to_thread(svc._gw_firewall_refresh, True)
        return {"enabled": bool(args.get("enabled"))}
    if name == "backup":
        dst = sqlite3.connect(str(args["out"]))
        try:
            svc.db._connection().backup(dst)
        finally:
            dst.close()
        return {"last_i": svc.db.jrn_last_i(), "acked": svc.db.jrn_meta()["acked"]}
    raise ValueError(f"неизвестная команда {name}")


async def run(a: argparse.Namespace) -> None:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer
    from aiogram.enums import ParseMode
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from awgbot.core import config, settings
    settings.init(config.CONF_DIR)
    from awgbot.bot.notifier import send_notifications
    from awgbot.bot.routers import make_dispatcher
    from awgbot.domain.services import Services
    from awgbot.infra.db import Database
    from awgbot.runtime import pairlink, pollctl, rsvactions
    from awgbot.runtime.scheduler.reserve import setup_reserve_jobs

    lab_consts()
    w = Path(a.work)
    db = Database(config.DB_PATH)
    db.init_schema()
    svc = Services(db)
    lab_world(svc, Path(os.environ["LAB_SHARED"]))
    rt = os.environ.get("LAB_RT") == "1"
    if rt:
        lab_rt_world(svc, w)
    gw_slots = [int(x) for x in os.environ.get("LAB_GW_SLOTS", "").split(",") if x.strip()]
    gw_st = lab_gateways(svc, Path(os.environ["LAB_SHARED"]).parent, gw_slots) if gw_slots else None
    try:
        # как старт основы (runtime/main/client.py): лидер линии — без права записи до первого
        # большинства «да», до поллинга; после шлюзов-заглушек — от них состав голосующих
        svc.reserve_quorum_leader_start()
    except Exception as e:                                     # noqa: BLE001
        logging.warning("кворум: ограждение на старте: %s", e)
    bot = Bot(config.BOT_TOKEN, session=AiohttpSession(api=TelegramAPIServer.from_base(os.environ["LAB_BOTAPI"]), timeout=10),
              default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    try:
        me = await bot.get_me()
        svc.bot_username, svc.bot_name = me.username or "", me.first_name or ""
    except Exception as e:                                     # noqa: BLE001
        logging.warning("getMe: %s", e)
    dp = make_dispatcher("client", svc, db)

    async def on_notes(notes) -> None:
        await send_notifications(bot, notes)
    pairlink.set_on_notes(on_notes)
    pc = pollctl.PollControl(dp, bot, svc)
    rsvactions.setup(bot, svc, pc)
    svc.reserve_pkg_hook_install()
    if svc.reserve_paired():
        svc.reserve_pkg_mark()
    await pairlink.ensure(svc)
    scheduler = AsyncIOScheduler(timezone=config.TZ)
    setup_reserve_jobs(scheduler, svc, bot)
    if rt:
        # РФ-доступ: файервол хоста (NAT пользователей и правила транзита), наборы и цепочка — как
        # на старте основы; такт живости — с цифрами app.yaml узла; опрос трафика (таблица учёта
        # с rfp_up/rfp_dn) — чаще, чем у основы, чтобы счётчики транзита были видны сценарию
        from apscheduler.triggers.interval import IntervalTrigger
        try:
            await asyncio.to_thread(svc._gw_firewall_refresh)
            await asyncio.to_thread(lab_rt_reconcile, svc)
        except Exception as e:                                 # noqa: BLE001
            logging.warning("РФ-доступ стенда на старте: %s", e)
        secs = settings.get_int("app.routing.probe_seconds", 30)
        scheduler.add_job(lab_rt_job, IntervalTrigger(seconds=secs, jitter=max(1, int(secs * 0.3))), args=(svc, bot),
                          id="routing_liveness", max_instances=1, coalesce=True, next_run_time=datetime.now(config.TZ))

        async def rt_reconcile() -> None:
            await asyncio.to_thread(lab_rt_reconcile, svc)

        async def rt_poll() -> None:
            try:
                await asyncio.to_thread(svc.poll_traffic)
            except Exception as e:                             # noqa: BLE001
                logging.warning("опрос трафика: %s", e)
        scheduler.add_job(rt_reconcile, IntervalTrigger(seconds=60), id="lab_rt_reconcile", max_instances=1, coalesce=True)
        scheduler.add_job(rt_poll, IntervalTrigger(seconds=10), id="poll", max_instances=1, coalesce=True,
                          next_run_time=datetime.now(config.TZ))
    scheduler.start()
    poll_task = asyncio.create_task(pc.run()) if a.poll else None
    cmd = w / "cmd"
    try:
        while True:
            await asyncio.sleep(0.5)
            if gw_st is not None:
                await lab_gw_step(svc, gw_st)
            for f in sorted(cmd.iterdir()):
                if f.suffix in (".done", ".err", ".tmp"):
                    continue
                try:
                    args = json.loads(f.read_text(encoding="utf-8") or "{}")
                except ValueError:
                    args = {}
                f.unlink()
                try:
                    res = await _command(svc, w, f.name, args, bot)
                    (cmd / f"{f.name}.done").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
                except Exception as e:                         # noqa: BLE001
                    (cmd / f"{f.name}.err").write_text(f"{type(e).__name__}: {e}", encoding="utf-8")
            try:
                st = await asyncio.to_thread(_status, svc, w)
                tmp = w / "status.json.tmp"
                tmp.write_text(json.dumps(st, ensure_ascii=False, default=str), encoding="utf-8")
                os.replace(tmp, w / "status.json")
            except Exception as e:                             # noqa: BLE001
                logging.warning("статус узла: %s", e)
            if poll_task is not None and poll_task.done() and poll_task.exception():
                logging.warning("поллинг упал: %r", poll_task.exception())
                poll_task = None
    finally:
        scheduler.shutdown(wait=False)
        await pairlink.shutdown()
        await bot.session.close()
        db.close()


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--work", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--host", required=True)
    s.add_argument("--self-ip", dest="self_ip", required=True)
    s.add_argument("--port", required=True)
    s.add_argument("--subnet", required=True)
    s.add_argument("--token", required=True)
    s.add_argument("--admin", required=True)
    s.add_argument("--venv", required=True)
    s2 = sub.add_parser("seed")
    s2.add_argument("--role", choices=("inviter", "acceptor"), required=True)
    s2.add_argument("--peer-name", dest="peer_name", required=True)
    s2.add_argument("--peer-host", dest="peer_host", required=True)
    s2.add_argument("--peer-admin", dest="peer_admin", required=True)
    s4 = sub.add_parser("bloat")
    s4.add_argument("--mb", required=True)
    s3 = sub.add_parser("rtseed")
    s3.add_argument("--link-if", dest="link_if", default="awglink")
    s3.add_argument("--link-port", dest="link_port", default="443")
    s3.add_argument("--link-cidr", dest="link_cidr", default="10.99.99.0/30")
    s3.add_argument("--user-pub", dest="user_pub", default="")
    s3.add_argument("--user-addr", dest="user_addr", default="")
    s3.add_argument("--user-psk", dest="user_psk", default="")
    s3.add_argument("--user-name", dest="user_name", default="Пользователь стенда")
    s3.add_argument("--user-tg", dest="user_tg", default="880301")
    sub.add_parser("digits")
    r = sub.add_parser("run")
    r.add_argument("--work", required=True)
    r.add_argument("--poll", action="store_true")
    a = p.parse_args()
    if a.cmd == "setup":
        setup(a)
    elif a.cmd == "seed":
        seed(a)
    elif a.cmd == "bloat":
        bloat(a)
    elif a.cmd == "rtseed":
        rtseed(a)
    elif a.cmd == "digits":
        digits(a)
    else:
        asyncio.run(run(a))


if __name__ == "__main__":
    sys.exit(main())
