"""Шлюз основы для стенда — только кворум: настоящий голосующий агента
(`domain/gateway/reserve/quorum.GatewayQuorumMixin` и сокеты `runtime/quorum`)
в пространстве шлюза (`lab-G1`, `lab-G2`), зонды — install/quorum-probe-setup.sh
в своём `awg-quo-<узел>`, пробы — настоящие, через NAT стенда к порту AWG основы.

Подменено то, чего у стенда нет (мир, а не правило голосующего):
- документ конфигурации: раздел `quorum` основа кладёт файлом
  `$PL/shared/gw<слот>.cfg.json` (pairnode, `gwcfg_quorum` слота), здесь он
  проходит `gwcfg.check.check_quorum` и `quorum_apply`, как у агента; там же —
  слот, имя основы и её состояние (дома / в отъезде) вместо раздела `reserve`;
- каналы: гостевой (к стороне зоны резерва) и канал конфигурации (к своему
  серверу) — шина `tests/lab/labbus` в `$PL/bus`; слово уходит и принимается,
  только пока ping до сервера проходит (рычаги net.sh режут и его): новая
  «сессия» гостевого канала — `hello`, на `ping` резерва — `pong`;
- снимок: поле `quo` — файлом `$PL/shared/gw<слот>.snap.json` (основа читает
  его как снимок слота), живость канала конфигурации — отметкой
  `gw<слот>.link.alive`.

    nsenter --net=/run/netns/lab-G1 env QUO_NS=awg-quo-G1 QUO_CONF_DIR=$PL/G1/quo \\
        python -m tests.lab.gwnode --work $PL/G1 --slot 1 --pair $PL

Статус (раз в секунду) — `<work>/status.json`: взгляды зондов, эпоха, обязательства,
живость каналов, что отдано (точка) и что пришло.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path

A_IP = "203.0.113.5"
B_IP = "198.51.100.7"


def _reach(ip: str) -> bool:
    try:
        return subprocess.run(["ping", "-c1", "-W1", ip], capture_output=True, timeout=3).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _read_json(p: Path) -> dict | None:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _write_json(p: Path, data) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, p)


def make_gateway(work: Path, slot: int):
    from awgbot.domain.gateway.reserve.quorum import GatewayQuorumMixin

    class LabGateway(GatewayQuorumMixin):
        _QUO_STORE = work / "quorum.json"

        def __init__(self) -> None:
            self.slot = slot
            self.doc_reserve: dict = {}
            self.reserve_state = "idle"

        def reserve_section(self) -> dict:
            return {"slot": self.slot, "name": str(self.doc_reserve.get("name") or ""),
                    "state": str(self.doc_reserve.get("state") or ""), "reserve_state": self.reserve_state}

    return LabGateway()


async def run(a: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    from awgbot.core import config, settings
    settings.init(config.CONF_DIR)
    from awgbot.domain.gwcfg.check import check_quorum
    from awgbot.runtime import quorum as rt
    from tests.lab import labbus

    work, pair, slot = Path(a.work), Path(a.pair), int(a.slot)
    me = f"G{slot}"
    bus = pair / "bus"
    shared = pair / "shared"
    work.mkdir(parents=True, exist_ok=True)
    gw = make_gateway(work, slot)
    ch = {"link": False, "guest": False}
    got: dict = {}
    doc_seen = None
    log = logging.getLogger("gwnode")

    def note(kind: str) -> None:
        got[kind] = got.get(kind, 0) + 1

    async def act(actions) -> None:
        for x in actions or []:
            if not x:
                continue
            if x[0] == "guest" and ch["guest"]:
                labbus.post(bus, "B", {"from": me, "slot": slot, "ch": "guest", "kind": x[1], "body": x[2] if len(x) > 2 else {}})
                note("out:guest:" + x[1])
            elif x[0] == "link" and ch["link"]:
                labbus.post(bus, "A", {"from": me, "slot": slot, "ch": "link", "kind": x[1], "body": x[2] if len(x) > 2 else {}})
                note("out:link:" + x[1])
            elif x[0] in ("guest", "link"):
                note("lost:" + x[0] + ":" + x[1])

    last_reach = last_tick = 0.0
    while True:
        await asyncio.sleep(0.5)
        now = time.monotonic()
        # документ основы: раздел quorum и раздел reserve
        doc = _read_json(shared / f"gw{slot}.cfg.json")
        if doc is not None and doc != doc_seen:
            doc_seen = doc
            gw.doc_reserve = dict(doc.get("reserve") or {})
            sec = doc.get("quorum")
            try:
                checked = check_quorum(sec) if sec else None
                res = await asyncio.to_thread(gw.quorum_apply, checked)
                if res.get("note"):
                    log.info("документ: %s", res["note"])
            except Exception as e:                            # noqa: BLE001 — отказ проверки — как у агента: раздел не принят
                log.warning("раздел quorum не принят: %s", e)
        # каналы: живы, пока сервер отвечает по сети стенда
        if now - last_reach >= 3.0:
            last_reach = now
            link, guest = await asyncio.to_thread(_reach, A_IP), await asyncio.to_thread(_reach, B_IP)
            if link:
                (shared / f"gw{slot}.link.alive").write_text(str(time.time()))
            if guest and not ch["guest"]:
                labbus.post(bus, "B", {"from": me, "slot": slot, "ch": "guest", "kind": "hello",
                                       "body": {"slot": slot, "caps": ["rsv1", "quo1", "rsvtg1"], "m_gen": 0, "seen": ""}})
                log.info("гостевой канал: сессия к резерву")
            if not guest and ch["guest"]:
                log.info("гостевой канал: резерв недоступен")
            ch["link"], ch["guest"] = link, guest
            gw.__dict__["_quo_reserve_online"] = guest
        # входящие слова
        for m in labbus.take(bus, me):
            c, kind, body = str(m.get("ch") or ""), str(m.get("kind") or ""), dict(m.get("body") or {})
            if not ch.get(c):
                note("dropped:" + c + ":" + kind)
                continue
            note("in:" + c + ":" + kind)
            if c == "guest" and kind == "ping":
                await act([("guest", "pong", {"n": str(body.get("n") or "")})])
                continue
            if c == "guest" and kind == "state":
                gw.reserve_state = str(body.get("state") or "idle")
                if int(body.get("e") or 0):
                    gw._quorum_epoch_in(int(body.get("e") or 0))
                continue
            if kind.startswith("q"):
                await act(await asyncio.to_thread(gw.quorum_guest_in, kind, body))
        # такт голосующего: сокеты зондов, пробы, взгляды
        if now - last_tick >= 5.0:
            last_tick = now
            try:
                await rt.tick(gw, act)
            except Exception as e:                            # noqa: BLE001
                log.warning("такт: %s", e)
            snap = await asyncio.to_thread(gw.quorum_snapshot)
            _write_json(shared / f"gw{slot}.snap.json", snap or {})
        voters = {n: {"view": v.view, "epoch": v.epoch, "pledge": v.pledge, "last": (v.last.r, v.last.y, v.last.v, v.last.e)
                      if v.last is not None else None} for n, v in gw._gw_qvoters.items()}
        sec = gw.quorum_section()
        _write_json(work / "status.json", {"t": time.time(), "ch": ch, "voters": voters, "g": sec.get("g"), "e": sec.get("e"),
                                           "point_q": (sec.get("point") or {}).get("q_gen"), "share_given_at": sec.get("share_given_at"),
                                           "pledges": sec.get("pledges"), "views": sec.get("views"), "got": got,
                                           "reserve": gw.reserve_section(), "panel": gw.quorum_panel_view()})


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--work", required=True)
    p.add_argument("--slot", required=True)
    p.add_argument("--pair", required=True)
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
