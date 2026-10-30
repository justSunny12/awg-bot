"""Узлы кворума резерва для стенда без Telegram и базы: ответчик правила лидера
на UDP, пробы голосующего, запись пиров-зондов и второго адреса в конфиг
интерфейса AWG основы, файервол хоста основы и поиск аплинка шлюза.

Ответчик — машина `util/quorum/leader.Leader` с пробами и ответами
`util/quorum/probe` (Probe, Reply, Timing): сверяет узел пробы с адресом
источника (`probenet.node_of_addr`), считает «да» по окну W, отвечает ролью и
счётом. Это не ответчик основного бота (тот живёт в сервисах и на стенде
работает в паре настоящих ботов, s06), а то же правило без базы: здесь
проверяется сеть — что доходит до ответчика, что нет.

    ip netns exec lab-A python -m tests.lab.quorum_node responder --side a --addr 169.254.88.1 \\
        --iface awg0 --members a,b,a1,a2 --g 1 --epoch 5 --status /var/lib/awglab-quo/resp.json
    ip netns exec awg-quo-B python -m tests.lab.quorum_node probe --src 169.254.88.4 \\
        --to 169.254.88.1 --l a --g 1 --n b --e 5 --w see
    ip netns exec awg-quo-B python -m tests.lab.quorum_node voter --src 169.254.88.4 --to 169.254.88.1 \\
        --l a --g 1 --n b --e 5 --every 3 --status /var/lib/awglab-quo/B.json
    ip netns exec lab-A python -m tests.lab.quorum_node awg0 --conf /var/lib/awglab-quo/A/awg0.conf \\
        --resp 169.254.88.1/26 --peer <pub>,<psk>,169.254.88.4 …
    python -m tests.lab.quorum_node guard --awg-port 45871 --users 10.8.1.0/24   (печатает таблицу)
    ip netns exec lab-G1 python -m tests.lab.quorum_node uplink --link-if awglink

Статус ответчика (раз в секунду): {y, v, role, seen: {узел: {w, e, age}}, got, counted,
rejected: [{n, src}]} — got: датаграмм всего, counted: проб, принятых в счёт.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path


def _write_json(path: str, data: dict) -> None:
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


# ── ответчик ────────────────────────────────────────────────────────────────

def responder(a: argparse.Namespace) -> None:
    from awgbot.util import probenet
    from awgbot.util.quorum import members as qm
    from awgbot.util.quorum.leader import Leader
    from awgbot.util.quorum.probe import Info, Probe
    from awgbot.util.quorum.timing import Timing

    timing = Timing()
    comp = qm.Members.make(a.side, int(a.g), a.members.split(","))
    voters = comp.voters(a.side)
    ld = Leader(a.side, len(voters), int(a.epoch), candidate=probenet.other_side(a.side), timing=timing, voters=voters)
    ld.gen.bump(int(a.g), time.monotonic(), timing.grace_s)
    lock = threading.Lock()
    stat = {"got": 0, "counted": 0, "rejected": []}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if a.iface:
        sock.setsockopt(socket.SOL_SOCKET, 25, a.iface.encode() + b"\x00")     # SO_BINDTODEVICE, как у основы
    sock.bind((a.addr, int(a.port)))
    sock.settimeout(1.0)

    def status() -> None:
        now = time.monotonic()
        with lock:
            seen = {n: {"w": s.w, "e": s.e, "age": round(now - s.at, 1)} for n, s in ld.seen.items()}
            _write_json(a.status, {"t": time.time(), "y": ld.yes_count(now), "v": ld.v, "role": ld.role(),
                                   "yes": list(ld.yes_nodes(now)), "seen": seen, **stat})

    last_tick = last_status = 0.0
    while True:
        try:
            data, src = sock.recvfrom(2048)
        except socket.timeout:
            data, src = b"", None
        now = time.monotonic()
        if src is not None:
            with lock:
                stat["got"] += 1
                p = Probe.parse(data)
                if p is None or p.l != ld.l or probenet.node_of_addr(ld.l, src[0]) != p.n \
                        or ld.probe_in(p, now) == "ignore":
                    stat["rejected"] = (stat["rejected"] + [{"n": p.n if p else "", "src": src[0]}])[-20:]
                    out = None
                else:
                    stat["counted"] += 1
                    out = ld.reply_for(p, now, Info(hs=0, flow=False, at=time.strftime("%Y-%m-%dT%H:%M:%S"))).encode()
            if out:
                try:
                    sock.sendto(out, src)
                except OSError:
                    pass
        if now - last_tick >= timing.tick_s:
            last_tick = now
            with lock:
                ld.tick(now)
        if now - last_status >= 1.0:
            last_status = now
            status()


# ── пробы голосующего ───────────────────────────────────────────────────────

def _probe_once(sock: socket.socket, a: argparse.Namespace, s: int, w: str) -> dict | None:
    from awgbot.util.quorum.probe import Probe, Reply
    p = Probe(a.l, int(a.g), a.n, int(a.e), w, s)
    sock.sendto(p.encode(), (a.to, int(a.port)))
    deadline = time.monotonic() + float(a.timeout)
    while time.monotonic() < deadline:
        sock.settimeout(max(0.05, deadline - time.monotonic()))
        try:
            data, _src = sock.recvfrom(2048)
        except socket.timeout:
            return None
        r = Reply.parse(data)
        if r is not None and r.s == s:
            return {"l": r.l, "g": r.g, "e": r.e, "r": r.r, "s": r.s, "y": r.y, "v": r.v, "i": r.i.to_dict()}
    return None


def _sock(src: str) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((src, 0))
    return s


def probe(a: argparse.Namespace) -> int:
    """Одна проба; ответ — JSON на stdout, код 0; ответа нет — «timeout», код 1;
    сокет не создался или проба не ушла — «error: …», код 2."""
    try:
        sock = _sock(a.src)
        rep = _probe_once(sock, a, int(a.s), a.w)
    except OSError as e:
        print(f"error: {e}")
        return 2
    if rep is None:
        print("timeout")
        return 1
    print(json.dumps(rep))
    return 0


def voter(a: argparse.Namespace) -> None:
    """Пробы раз в --every с; последний ответ и счёт ответов/пропусков — в --status.
    Сокет пересоздаётся, если адрес зонда пропал (--down) и вернулся."""
    st = {"ok": 0, "miss": 0, "err": 0, "last": None, "last_ok_t": 0.0}
    sock = None
    s = 0
    while True:
        s += 1
        try:
            if sock is None:
                sock = _sock(a.src)
            rep = _probe_once(sock, a, s, a.w)
        except OSError as e:
            st["err"] += 1
            st["error"] = str(e)
            rep = None
            if sock is not None:
                sock.close()
            sock = None
        if rep is None:
            st["miss"] += 1
        else:
            st["ok"] += 1
            st["last"] = rep
            st["last_ok_t"] = time.time()
        st["t"] = time.time()
        _write_json(a.status, st)
        time.sleep(float(a.every))


# ── пиры-зонды и второй адрес на интерфейсе AWG основы ──────────────────────

def awg0(a: argparse.Namespace) -> None:
    """Тем же кодом, что основа (`infra/awg` add_peer и ensure_second_address): пиры-зонды
    в конфиг и на живой интерфейс, второй адрес ответчика отдельной строкой Address."""
    from awgbot.core import config
    from awgbot.infra import awg
    conf = Path(a.conf)
    config.AWG_INTERFACE = conf.stem
    config.AWG_DIR = str(conf.parent)
    config.CONF_PATH = str(conf)
    for spec in a.peer or []:
        pub, psk, addr = spec.split(",")
        awg.add_peer(pub, psk, addr, iface=conf.stem)
    awg.ensure_second_address(a.resp, iface=conf.stem)
    print("ok")


# ── файервол хоста основы ───────────────────────────────────────────────────

def guard(a: argparse.Namespace) -> None:
    """Таблица awg_bot_guard основы в форме с фильтром (или NAT-only): правило ответчика
    кворума — тем же рендером, что у бота."""
    from awgbot.infra import nftguard
    from awgbot.infra.nftguard.render import render
    spec = nftguard.GuardSpec(ssh_port=22, ssh_open=True, tunnel_nets4=[a.users], tunnel_ifs=[a.iface],
                              udp_ports=[int(a.awg_port)], own_forward=True, nat=True, filter=not a.nat_only,
                              probe_if=a.iface, probe_net4=a.line_net, probe_resp4=a.resp, probe_port=int(a.port))
    sys.stdout.write(render(spec))


# ── поиск аплинка шлюза ─────────────────────────────────────────────────────

def uplink(a: argparse.Namespace) -> None:
    """Аплинк, который нашёл бы агент шлюза без файла статуса обвязки и без
    `uplink_interface` в conf: среди интерфейсов `awg show interfaces` своего пространства."""
    from awgbot.core import config
    from awgbot.infra import gwguard
    config.GW_UPLINK_IF = ""
    config.GW_LINK_IF = a.link_if
    gwguard.STATUS_FILE = "/nonexistent/awglab-gateway.status"
    print(gwguard.uplink_interface() or "-")


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("responder")
    r.add_argument("--side", required=True)
    r.add_argument("--addr", required=True)
    r.add_argument("--port", default="8789")
    r.add_argument("--iface", default="")
    r.add_argument("--members", required=True)
    r.add_argument("--g", default="1")
    r.add_argument("--epoch", default="0")
    r.add_argument("--status", required=True)
    for name in ("probe", "voter"):
        q = sub.add_parser(name)
        q.add_argument("--src", required=True)
        q.add_argument("--to", required=True)
        q.add_argument("--port", default="8789")
        q.add_argument("--l", required=True)
        q.add_argument("--g", default="1")
        q.add_argument("--n", required=True)
        q.add_argument("--e", default="0")
        q.add_argument("--w", default="see")
        q.add_argument("--s", default="1")
        q.add_argument("--timeout", default="3")
        if name == "voter":
            q.add_argument("--every", default="3")
            q.add_argument("--status", required=True)
    w = sub.add_parser("awg0")
    w.add_argument("--conf", required=True)
    w.add_argument("--resp", required=True)
    w.add_argument("--peer", action="append")
    g = sub.add_parser("guard")
    g.add_argument("--iface", default="awg0")
    g.add_argument("--users", required=True)
    g.add_argument("--awg-port", dest="awg_port", required=True)
    g.add_argument("--line-net", dest="line_net", default="169.254.88.0/26")
    g.add_argument("--resp", default="169.254.88.1")
    g.add_argument("--port", default="8789")
    g.add_argument("--nat-only", dest="nat_only", action="store_true")
    u = sub.add_parser("uplink")
    u.add_argument("--link-if", dest="link_if", default="awglink")
    a = p.parse_args()
    if a.cmd == "responder":
        responder(a)
    elif a.cmd == "probe":
        return probe(a)
    elif a.cmd == "voter":
        voter(a)
    elif a.cmd == "awg0":
        awg0(a)
    elif a.cmd == "guard":
        guard(a)
    elif a.cmd == "uplink":
        uplink(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
