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
  стенд не поднимает, долю Шлюза 1 сценарий берёт у основы командой `share`;
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
set {name, field, value}, share {slot, out}, code {out}, open_share {share}, move {},
return {}, backup {out}, qpause {seconds} (кворум: плановая пауза лидера перед остановкой).
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
    app.setdefault("reserve", {}).update(ready=True, pkg_minutes=1)
    (w / "conf" / "app.yaml").write_text(yaml.safe_dump(app, allow_unicode=True), encoding="utf-8")
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

    def read_authoritative(self, name: str) -> list[str]:
        return self.read_system(name)

    def read_authoritative_ttl(self, name: str) -> tuple[list[str], int]:
        got = self.read_system(name)
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

            def check(self, cfg: dict) -> str:
                return world.read_system(os.environ.get("LAB_RECORD", ""))[0] if world.read_system(
                    os.environ.get("LAB_RECORD", "")) else ""

            def read(self, cfg: dict) -> str:
                return self.check(cfg)
        return Provider()


def lab_world(svc, shared: Path) -> None:
    from awgbot.domain.services.reserve import openproc
    from awgbot.util import dnsapi
    dns = LabDns(shared / "dns.json")
    for name in ("read_system", "read_authoritative", "read_authoritative_ttl", "provider_for"):
        setattr(dnsapi, name, getattr(dns, name))
    svc.reserve_has_gateway = lambda: True
    # запись DNS у основы — «автоматическая» (провайдер подменён LabDns): конверт DNS едет в пакете, и
    # аварийное открытие на резерве разрешено; саму запись меняет сценарий (у стенда нет провайдера
    # и авторитетного DNS), поэтому процессу открытия — set_record=False, отметку tg_take кладёт сценарий
    svc.rsv_update("rsv_dns", provider="digitalplat", token="lab")
    real_launch = svc._rsvopen_launch

    def launch(payload: dict, keep_stdin: bool = False) -> None:
        real_launch({**payload, "set_record": False}, keep_stdin)
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
    from awgbot.core import config
    config.BASE_DIR = Path(os.environ["AWG_BOT_CONF_DIR"]).parent / "base"   # venv и код — ссылками (setup)
    real_props = openproc.rsvopen_sandbox_props
    venv = os.environ.get("LAB_VENV", "")
    netns = os.environ.get("LAB_NETNS", "")

    def props(base: str) -> list[str]:
        extra = ["-p", "ProtectHome=tmpfs", "-p", f"BindReadOnlyPaths={venv}"] if venv else []
        if netns:
            extra += ["-p", f"NetworkNamespacePath=/run/netns/{netns}"]
        return real_props(base) + extra
    openproc.rsvopen_sandbox_props = props


# ── обход дефекта на время прогона (только по LAB_QKEYS_SHIM=1) ──────────────

def lab_qkeys_shim(svc) -> None:
    """Порог `t` в `qkeys` затирается полем вида конверта (`gwlink.pack` кладёт вид
    сообщения в тот же ключ `t`): приёмник отвергает слово, ключи кворума не
    подтверждаются, кворум не включается. Обход стенда — только чтобы прогнать
    сценарии дальше включения: порог берётся из состава линии второй стороны
    (`st.quo`), как его посчитала бы основа. По умолчанию выключен: без него
    сценарий падает на включении — это и есть находка."""
    from awgbot.util import pairproto
    from awgbot.util.quorum import members as qm
    real = pairproto.check

    def check(proto, kind, body, sender):
        if kind == "qkeys" and isinstance(body.get("t"), str):
            members = svc.reserve_quorum_peer_line().get("members") or []
            body = {**body, "t": qm.threshold(max(1, len(members) - 1))}
        return real(proto, kind, body, sender)
    pairproto.check = check


def lab_guard_shim() -> None:
    """Пакет nftguard не выставляет `TG_OUT4` и `SET_TG_OUT` (а `collect.zone_tg` и `render`
    берут их через пакет): при оболочке зоны с awggx сборка таблицы awg_bot_guard падает
    AttributeError, файервол хоста не ставится — у зоны нет маскарада наружу, бот основы на
    резерве не достаёт Telegram. Обход стенда — только по LAB_GUARD_SHIM=1: имена из
    common — в пакет. По умолчанию выключен: без него сценарии кворума падают на переезде."""
    from awgbot.infra import nftguard
    from awgbot.infra.nftguard import common
    for name in ("TG_OUT4", "SET_TG_OUT"):
        if not hasattr(nftguard, name):
            setattr(nftguard, name, getattr(common, name))


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
                doc["why_none"] = {"m": m.to_dict() if m else None, "peer_quo1": svc.reserve_quorum_peer_quo1(),
                                   "own_ip": svc.reserve_own_ip(), "pub": bool(svc._quorum_server_pubkey()),
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
          "jrn": {k: meta[k] for k in ("epoch", "holder", "role", "capture", "applied", "acked")},
          "last_i": svc.db.jrn_last_i(), "out": svc.rsv_get("rsv_out"), "in": svc.rsv_get("rsv_in"),
          "poll": svc.rsv_get("rsv_poll"), "pkg_sent": {k: v for k, v in svc.rsv_get("rsv_pkg_sent").items()
                                                          if k in ("gen", "at", "jrn")},
          "pkg_in": {k: v for k, v in svc.rsv_get("rsv_pkg_in").items() if k in ("gen", "at", "m_gen")},
          "m_gen": int(svc.reserve_keys().get("m_gen") or 0),
          "peer": {k: v for k, v in svc.reserve_peer_state().items() if k in ("ready", "caps", "version", "inn", "out")},
          "jrn_on": svc.reserve_jrn_on(), "code_window": svc.reserve_code_window(),
          "tail_meta": svc.rsv_get("rsv_tail"), "quo": _quo_status(svc)}
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


async def _command(svc, w: Path, name: str, args: dict) -> dict:
    from awgbot.core import config
    from awgbot.runtime import rsvactions
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
    if name == "share":
        from awgbot.util import rsvkeys
        slot = int(args.get("slot", 1))
        m_gen, y = svc.reserve_share_gateway(slot)              # доля, которую основа отдаёт шлюзу слота
        Path(args["out"]).write_text(json.dumps([rsvkeys.gateway_x(slot), y, m_gen]), encoding="utf-8")
        return {"x": rsvkeys.gateway_x(slot), "m_gen": m_gen}
    if name == "code":
        Path(args["out"]).write_text(svc.reserve_code(), encoding="utf-8")   # аварийный код — у админа основы
        return {"ok": True}
    if name == "open_share":
        # как такт голосования, получивший долю шлюза (vote._share_in): точка резерва + точка шлюза
        from awgbot.util import rsvkeys
        x, y, _m_gen = json.loads(Path(args["share"]).read_text(encoding="utf-8"))
        pkg_in = svc.rsv_get("rsv_pkg_in")
        points = [[rsvkeys.RESERVE_X, str(pkg_in.get("share") or "")], [int(x), str(y)]]
        acts = await asyncio.to_thread(svc.reserve_open_by_points, {"shares": points}, "dead", "1")
        await rsvactions.run(acts)
        return {"acts": [a[0] for a in acts]}
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

    w = Path(a.work)
    db = Database(config.DB_PATH)
    db.init_schema()
    svc = Services(db)
    lab_world(svc, Path(os.environ["LAB_SHARED"]))
    if os.environ.get("LAB_QKEYS_SHIM") == "1":
        lab_qkeys_shim(svc)
    if os.environ.get("LAB_GUARD_SHIM") == "1":
        lab_guard_shim()
    gw_slots = [int(x) for x in os.environ.get("LAB_GW_SLOTS", "").split(",") if x.strip()]
    gw_st = lab_gateways(svc, Path(os.environ["LAB_SHARED"]).parent, gw_slots) if gw_slots else None
    bot = Bot(config.BOT_TOKEN, session=AiohttpSession(api=TelegramAPIServer.from_base(os.environ["LAB_BOTAPI"])),
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
                    res = await _command(svc, w, f.name, args)
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
    r = sub.add_parser("run")
    r.add_argument("--work", required=True)
    r.add_argument("--poll", action="store_true")
    a = p.parse_args()
    if a.cmd == "setup":
        setup(a)
    elif a.cmd == "seed":
        seed(a)
    else:
        asyncio.run(run(a))


if __name__ == "__main__":
    sys.exit(main())
