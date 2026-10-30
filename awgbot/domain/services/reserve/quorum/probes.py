"""
probes.py — часть пакета quorum (см. __init__): пиры-зонды и второй адрес
на интерфейсе AWG основы (`rsv_probes`), конфиги зондов голосующим — второму
серверу (`qprobe`) и своим шлюзам (раздел `quorum` документа), свои
интерфейсы зондов в пространстве `awg-quo`.

Основа своей линии делает ключи зондов всех голосующих (как ключи шлюзовых
сторон `awgrsv`, gwside) и отдаёт их по подписанным каналам. На `awg0`
пиры-зонды пишутся тем же `add_peer`/`remove_peer` (ровно один /32 из блока
в `AllowedIPs` — инвариант записи, который рельс 4.1.x узнаёт), второй адрес
— отдельной строкой `Address` после основной (`infra/awg/addrs`). Свои
интерфейсы зондов: `awgq<другая>h` — к интерфейсу AWG второго сервера (конфиг
от него, `qprobe`), `awgq<своя>r` — к своему клону в его зоне, пока мои
пользователи там.
"""
from __future__ import annotations

import hashlib
import time

from awgbot.core import config
from awgbot.util import probenet, timeutil, wgkeys
from awgbot.util.quorum import members as qm

from ...gateway_link.common import log
from ..state import _LOCK

APPLY_EVERY_S = 300               # свои интерфейсы зондов: повтор --up (идемпотентно) не чаще
_ALLOWED_OBF = (*wgkeys.OBF_KEYS, "I1")


class ReserveQuorumProbesMixin:

    def reserve_probes(self) -> dict:
        return self.rsv_get("rsv_probes")

    # ── ключи и адреса узлов ────────────────────────────────────────────────
    def _quorum_server_obf(self) -> dict:
        """Обфускация интерфейса AWG сервера — та же, что у пользователей."""
        from awgbot.infra import awg
        try:
            obf = awg.read_server_params().get("obfuscation") or {}
        except Exception:                                     # noqa: BLE001
            return {}
        return {k: str(obf[k]).strip() for k in _ALLOWED_OBF if obf.get(k)}

    def _quorum_server_pubkey(self) -> str:
        from awgbot.infra import awg
        try:
            return str(awg.read_server_params().get("server_pubkey") or "")
        except Exception:                                     # noqa: BLE001
            return ""

    def _quorum_server_port(self) -> int:
        from awgbot.infra import awg
        try:
            return int(awg.read_server_params().get("listen_port") or config.SERVER_PORT or 0)
        except Exception:                                     # noqa: BLE001
            return int(config.SERVER_PORT or 0)

    def reserve_probes_refresh(self, m: qm.Members) -> dict:
        """Ключи и адреса зондов узлов состава своей линии: недостающие — завести,
        лишние — убрать. Возвращает запись линии `rsv_probes[l]`."""
        from awgbot.infra import quoprobe
        with _LOCK:
            probes = self.reserve_probes()
            line = dict(probes.get(m.l) or {})
            nodes = dict(line.get("nodes") or {})
            changed = False
            for node in m.members:
                rec = nodes.get(node) or {}
                addr = probenet.node_addr(m.l, node)
                if not rec.get("priv"):
                    priv, pub = wgkeys.keypair()
                    rec = {"priv": priv, "pub": pub, "psk": wgkeys.psk(), "created_at": timeutil.now_iso()}
                    changed = True
                if rec.get("addr") != addr:
                    rec["addr"] = addr
                    changed = True
                nodes[node] = rec
            for node in list(nodes):
                if node not in m.members:
                    nodes.pop(node)
                    changed = True
            want = {"net": str(probenet.line_net(m.l)), "resp": probenet.responder_addr(m.l),
                    "resp_clone": probenet.responder_addr(m.l, clone=True), "port": quoprobe.port(), "nodes": nodes}
            if any(line.get(k) != v for k, v in want.items() if k != "nodes") or changed:
                line.update(want)
                probes[m.l] = line
                self.rsv_put("rsv_probes", probes)
            return line

    # ── пиры-зонды и второй адрес на awg0 ───────────────────────────────────
    def reserve_probe_peers_sync(self, m: qm.Members | None) -> None:
        """Пиры-зонды своей линии на интерфейсе AWG: по составу (лишний — снять,
        нет — добавить), второй адрес ответчика. Без состава — всё снимается."""
        from awgbot.infra import awg
        line = self.reserve_probes().get(m.l) if m is not None else None
        want: dict[str, dict] = {}
        if m is not None and line:
            for node, rec in (line.get("nodes") or {}).items():
                if rec.get("pub"):                            # и свой: клон берёт пиры с awg0, а свой зонд идёт к клону
                    want[str(rec["pub"])] = rec
        iface = config.AWG_INTERFACE
        try:
            header, peers = awg._split_conf(awg.read_file(awg.conf_path(iface)))
        except Exception as e:                                # noqa: BLE001
            log.warning("кворум: конфиг интерфейса не прочитан: %s", e)
            return
        rule = probenet.rule_on(probenet.user_nets(probenet.iface_addrs(header)))
        have: dict[str, str] = {}
        for p in peers:
            allowed = [ln.split("=", 1)[1] for ln in p["lines"] if ln.strip().startswith("AllowedIPs")]
            if rule and p.get("pubkey") and probenet.in_block(allowed):
                have[p["pubkey"]] = probenet.values(allowed)[0] if probenet.values(allowed) else ""
        for pub, ip in have.items():
            rec = want.get(pub)
            if rec is None or str(rec.get("addr")) != str(ip).split("/", 1)[0]:
                try:
                    awg.remove_peer(pub, iface=iface)
                    log.info("кворум: пир-зонд %s… (%s) снят с %s", pub[:8], ip, iface)
                except Exception as e:                        # noqa: BLE001
                    log.warning("кворум: пир-зонд не снят: %s", e)
        for pub, rec in want.items():
            if pub in have and str(have[pub]).split("/", 1)[0] == str(rec.get("addr")):
                continue
            try:
                awg.add_peer(pub, str(rec["psk"]), str(rec["addr"]), iface=iface)
                log.info("кворум: пир-зонд %s… (%s) добавлен на %s", pub[:8], rec["addr"], iface)
            except Exception as e:                            # noqa: BLE001
                log.warning("кворум: пир-зонд не добавлен: %s", e)
        side = m.l if m is not None else self.reserve_quorum_side()
        try:
            if m is not None and want:
                awg.ensure_second_address(probenet.responder_cidr(m.l), iface=iface)
            elif side:
                awg.drop_second_address(probenet.responder_cidr(side), iface=iface)
        except Exception as e:                                # noqa: BLE001
            log.warning("кворум: второй адрес интерфейса: %s", e)

    # ── конфиги зондов голосующим ───────────────────────────────────────────
    def reserve_probe_conf(self, node: str, target: str) -> str:
        """Конфиг интерфейса зонда узла `node` своей линии: цель `h` — интерфейс
        AWG основы (Endpoint — мой адрес), `r` — её клон в зоне второго сервера
        (Endpoint — адрес второго сервера, тот же порт и ключ). Пусто — нечего."""
        m = self.reserve_quorum_proposed()
        if m is None:
            return ""
        line = self.reserve_probes().get(m.l) or {}
        rec = (line.get("nodes") or {}).get(node) or {}
        pub, port = self._quorum_server_pubkey(), self._quorum_server_port()
        host = self.reserve_own_ip() if target == "h" else self.reserve_peer_ip()
        if not rec.get("priv") or not pub or not port or not host:
            return ""
        resp = line.get("resp_clone") if target == "r" else line.get("resp")
        lines = ["[Interface]", f"Address = {rec['addr']}/32", f"PrivateKey = {rec['priv']}"]
        for k, v in self._quorum_server_obf().items():
            lines.append(f"{k} = {v}")
        lines += ["", "[Peer]", f"PublicKey = {pub}", f"PresharedKey = {rec['psk']}", f"AllowedIPs = {resp}/32",
                  f"Endpoint = {host}:{int(port)}", ""]
        return "\n".join(lines)

    def reserve_qprobe_body(self) -> dict | None:
        """`qprobe` второму серверу: его интерфейс зонда к моему AWG; None — не
        изменилось с последней отправки или слать нечего."""
        m = self.reserve_quorum_proposed()
        if m is None:
            return None
        conf = self.reserve_probe_conf(probenet.other_side(m.l), "h")
        if not conf:
            return None
        body = {"l": m.l, "g": int(m.g), "conf": conf}
        digest = hashlib.blake2b(f"{m.g}\n{conf}".encode(), digest_size=8).hexdigest()
        view = self.rsv_get("rsv_view")
        if view.get("qprobe_sent") == digest:
            return None
        return {**body, "_digest": digest}

    def reserve_qprobe_sent(self, digest: str) -> None:
        self.rsv_update("rsv_view", qprobe_sent=str(digest))

    def reserve_qprobe_reset(self) -> None:
        """Новая сессия пары — конфиг зонда второй стороне заново."""
        self.rsv_update("rsv_view", qprobe_sent="")

    def reserve_qprobe_in(self, l: str, g: int, conf: str) -> None:
        """`qprobe` от основы второй линии: мой интерфейс зонда к её AWG."""
        if l not in qm.SIDES or l == self.reserve_quorum_side():
            return
        if not _probe_conf_ok(conf):
            log.warning("кворум: конфиг зонда линии %s не прошёл проверку — не принят", l)
            return
        with _LOCK:
            probes = self.reserve_probes()
            mine = dict(probes.get("mine") or {})
            name = f"awgq{l}h"
            if (mine.get(name) or {}).get("conf") == conf:
                return
            mine[name] = {"conf": conf, "g": int(g), "at": timeutil.now_iso()}
            probes["mine"] = mine
            self.rsv_put("rsv_probes", probes)
        log.info("кворум: конфиг зонда к линии %s (поколение %s) принят", l, g)
        self.__dict__["_quo_apply_at"] = 0.0                   # применить на ближайшем такте

    def gwcfg_quorum(self, gw) -> dict | None:
        """Раздел `quorum` документа слота: состав, поколение, порог, порт, конфиги
        зондов (к основе и к клону), эпоха. None — пары нет или шлюз не в составе."""
        m = self.reserve_quorum_mine() or self.reserve_quorum_proposed()   # действующий состав; новому узлу — после qmem_ok
        if m is None:
            return None
        node = qm.gateway_node(m.l, int(gw.id))
        if node not in m.members:
            return None
        probes = {"h": self.reserve_probe_conf(node, "h"), "r": self.reserve_probe_conf(node, "r")}
        if not probes["h"]:
            return None
        point = self.reserve_quorum_gw_point(int(gw.id)) if hasattr(self, "reserve_quorum_gw_point") else {}
        return {"l": m.l, "g": int(m.g), "members": list(m.members), "t": m.t(m.l), "port": int(self.reserve_probes().get(m.l, {}).get("port") or 0),
                "probes": {k: v for k, v in probes.items() if v}, "point": point, "e": self.reserve_quorum_epoch(),
                "timing": self.reserve_quorum_timing().to_settings()}

    # ── свои интерфейсы зондов ──────────────────────────────────────────────
    def reserve_quorum_probe_ifaces(self) -> dict:
        """Какие интерфейсы зондов должны стоять у меня: имя → {conf, l, target,
        node_addr, resp, port}. К AWG второй линии — всегда, пока есть её конфиг;
        к своему клону — пока мои пользователи у резерва."""
        out: dict = {}
        probes = self.reserve_probes()
        side = self.reserve_quorum_side()
        for name, rec in (probes.get("mine") or {}).items():
            conf = str(rec.get("conf") or "")
            if name.startswith("awgq") and len(name) == 6 and conf:
                out[name] = {"conf": conf, "l": name[4], "target": "h", **_conf_addrs(conf)}
        if side and self.reserve_quorum_leader_host() == probenet.other_side(side):
            conf = self.reserve_probe_conf(side, "r")
            if conf:
                out[f"awgq{side}r"] = {"conf": conf, "l": side, "target": "r", **_conf_addrs(conf)}
        return out

    def reserve_probes_apply(self, force: bool = False) -> dict:
        """Поднять нужные интерфейсы зондов и снять лишние (скрипт идемпотентен);
        не чаще APPLY_EVERY_S, если желаемое не менялось. Возвращает желаемое."""
        from awgbot.infra import quoprobe
        want = self.reserve_quorum_probe_ifaces()
        digest = hashlib.blake2b(repr(sorted((k, v["conf"]) for k, v in want.items())).encode(), digest_size=8).hexdigest()
        now = time.monotonic()
        last_at, last_digest = self.__dict__.get("_quo_apply_at") or 0.0, self.__dict__.get("_quo_apply_digest")
        if not force and last_digest == digest and now - last_at < APPLY_EVERY_S:
            return want
        self.__dict__["_quo_apply_at"], self.__dict__["_quo_apply_digest"] = now, digest
        standing = set(quoprobe.standing())
        for name in sorted(standing - set(want)):
            try:
                quoprobe.down(name)
                log.info("кворум: интерфейс зонда %s снят", name)
            except quoprobe.QuoError as e:
                log.warning("кворум: интерфейс зонда %s не снят: %s", name, e)
        for name, rec in want.items():
            try:
                out = quoprobe.up(name, rec["conf"], int(rec.get("port") or 0))
                if "same" not in out:
                    log.info("кворум: интерфейс зонда %s поднят", name)
            except quoprobe.QuoError as e:
                log.warning("кворум: интерфейс зонда %s не поднялся: %s", name, e)
        return want


    def reserve_quorum_drop(self) -> None:
        """Удаление пары: пиры-зонды и второй адрес с интерфейса AWG, свои интерфейсы
        зондов и пространство, ключи кворума — прочь (ключи `rsv_*` снимает rsv_clear_all)."""
        from awgbot.infra import quoprobe
        m = self.reserve_quorum_mine()
        try:
            self.reserve_probe_peers_sync(None)
            if m is not None:
                from awgbot.infra import awg
                awg.drop_second_address(probenet.responder_cidr(m.l), iface=config.AWG_INTERFACE)
        except Exception as e:                                # noqa: BLE001
            log.warning("кворум: пиры-зонды при удалении пары: %s", e)
        try:
            quoprobe.wipe()
        except quoprobe.QuoError as e:
            log.warning("кворум: пространство зондов не снято: %s", e)
        for k in ("_rsv_qleader", "_rsv_qvoters", "_rsv_qgwviews", "_quo_apply_digest", "_quo_guest_sent"):
            self.__dict__.pop(k, None)


def _probe_conf_ok(conf: str) -> bool:
    """Конфиг зонда от второй стороны: известные ключи, один Peer, /32 ответчика."""
    from awgbot.util import wgconf
    try:
        c = wgconf.parse(conf)
        wgconf.check_keys(c, "rsvlink")
    except wgconf.WgConfError:
        return False
    allowed = str(c.get("Peer", "AllowedIPs") or "").strip()
    addr = str(c.get("Interface", "Address") or "").strip()
    if "," in allowed or not allowed.endswith("/32") or not addr.endswith("/32"):
        return False
    if str(c.get("Interface", "Table") or "").strip():          # зонд живёт в awg-quo без таблиц маршрутов
        return False
    return probenet.is_probe(allowed) and probenet.is_probe(addr) and bool(c.get("Peer", "Endpoint"))


def _conf_addrs(conf: str) -> dict:
    """{node_addr, resp, port} из конфига зонда."""
    import re
    addr = re.search(r"(?m)^\s*Address\s*=\s*([0-9.]+)", conf)
    resp = re.search(r"(?m)^\s*AllowedIPs\s*=\s*([0-9.]+)", conf)
    from awgbot.infra import quoprobe
    return {"node_addr": addr.group(1) if addr else "", "resp": resp.group(1) if resp else "", "port": quoprobe.port()}
