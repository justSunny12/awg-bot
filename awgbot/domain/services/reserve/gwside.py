"""
gwside.py — часть пакета reserve (см. __init__): постоянные линки моих
шлюзов к резерву — сторона основы. Ключи шлюзовых сторон (`rsv_gw_links`:
по слоту — закрытый ключ шлюза, PSK, параметры стороны зоны от резерва),
обмен `slots`/`slots_ok` с резервом, раздел `reserve` документа
конфигурации шлюза (линк, доля ключа переезда, состояние пересылки с номером
переезда, адреса), взгляды шлюзов на резерв из снимков (поле `rsv`),
Т-РУ18 «шлюз не на связи с резервом».
"""
from __future__ import annotations

import ipaddress

from awgbot.core import config, settings
from awgbot.util import rsvkeys, timeutil, wgkeys

from ..types import Notification
from ..gateway_link.common import log
from .state import _LOCK

GW_LINK_IF = "awgrsv"
RSV_NOTIFY_MINUTES = 10


class ReserveGwSideMixin:
    # ── ключи шлюзовых сторон ───────────────────────────────────────────────
    def reserve_gw_links(self) -> dict:
        return self.rsv_get("rsv_gw_links")

    def _reserve_gw_slots(self) -> list:
        """Слоты с агентом новой линии — им нужен линк к резерву."""
        out = []
        for gw in self.db.gateways():
            caps = self.gwlink_agent_caps(gw.id) if hasattr(self, "gwlink_agent_caps") else []
            if "rsv1" in (caps or []):
                out.append(gw)
        return out

    def reserve_slots_body(self) -> dict | None:
        """`slots` резерву: ключи шлюзовых сторон для слотов, у которых ещё нет
        стороны зоны или сменился состав; None — слать нечего."""
        if not self.reserve_paired():
            return None
        links = self.reserve_gw_links()
        want: list[dict] = []
        changed = False
        with _LOCK:
            for gw in self._reserve_gw_slots():
                key = str(gw.id)
                rec = links.get(key) or {}
                if not rec.get("priv"):
                    priv, pub = wgkeys.keypair()
                    rec = {"priv": priv, "pub": pub, "psk": wgkeys.psk(), "created_at": timeutil.now_iso()}
                    links[key] = rec
                    changed = True
                want.append({"slot": int(gw.id), "pub": rec["pub"], "psk": rec["psk"]})
            # слоты, которых больше нет, — прочь
            for key in list(links):
                if key.isdigit() and int(key) not in {g.id for g in self._reserve_gw_slots()}:
                    links.pop(key)
                    changed = True
            if changed:
                self.rsv_put("rsv_gw_links", links)
        view = self.rsv_get("rsv_view")
        sent = view.get("slots_sent") or []
        have_zone = all(bool((links.get(str(s["slot"])) or {}).get("zone")) for s in want)
        if sorted(s["slot"] for s in want) == sorted(sent):
            if have_zone:
                return None
            try:                                              # ответа нет — повтор не чаще раза в 5 минут
                if timeutil.elapsed(timeutil.parse_iso(str(view.get("slots_at") or ""))) < 300:
                    return None
            except ValueError:
                pass
        if not want:
            return None
        return {"slots": want}

    def reserve_slots_sent(self, body: dict) -> None:
        self.rsv_update("rsv_view", slots_sent=[int(s["slot"]) for s in body.get("slots") or []], slots_at=timeutil.now_iso())

    def reserve_slots_ok_in(self, slots: list) -> None:
        """`slots_ok` резерва: параметры сторон зоны по слотам → документы шлюзов."""
        with _LOCK:
            links = self.reserve_gw_links()
            for item in slots or []:
                if not isinstance(item, dict):
                    continue
                key = str(int(item.get("slot") or 0))
                rec = links.get(key)
                if not rec:
                    continue
                rec["zone"] = {"pub": str(item.get("pub") or ""), "port": int(item.get("port") or 0),
                               "host": str(item.get("host") or ""), "cidr": str(item.get("cidr") or ""),
                               "chan": int(item.get("chan") or 0),
                               "obf": {k: str(v) for k, v in (item.get("obf") or {}).items() if k in wgkeys.OBF_KEYS}}
            self.rsv_put("rsv_gw_links", links)
        log.info("резерв: стороны зоны для %d слотов получены", len(slots or []))

    # ── раздел документа ────────────────────────────────────────────────────
    def gwcfg_reserve(self, gw) -> dict | None:
        """Раздел `reserve` документа слота; None — пары нет, стороны зоны нет
        или агент без rsv1 (раздел не едет — шлюз снимает линк, если был)."""
        if not self.reserve_paired():
            return None
        rec = self.reserve_gw_links().get(str(gw.id)) or {}
        zone = rec.get("zone") or {}
        if not rec.get("priv") or not zone.get("pub") or not zone.get("port") or not zone.get("cidr"):
            return None
        try:
            hosts = list(ipaddress.ip_network(zone["cidr"], strict=False).hosts())
            zone_addr, gw_addr = str(hosts[0]), str(hosts[1])
        except (ValueError, IndexError):
            return None
        ka = str(settings.get("app.client_config.keepalive_seconds", config.KEEPALIVE_SECONDS) or "25-35")
        lines = ["[Interface]", f"Address = {gw_addr}/30", f"PrivateKey = {rec['priv']}", "Table = off"]
        for k in wgkeys.OBF_KEYS:
            if zone.get("obf", {}).get(k):
                lines.append(f"{k} = {zone['obf'][k]}")
        from awgbot.infra import nftguard
        allowed = [f"{zone_addr}/32"] + [n for n in nftguard.tunnel_nets() if n]     # РФ-трафик пользователей основы
        # выход агента в Telegram через зону второго сервера при мёртвом аплинке — только если умеют оба (§4.11)
        caps = self.gwlink_agent_caps(gw.id) if hasattr(self, "gwlink_agent_caps") else []
        tg = "rsvtg1" in (caps or []) and "rsvtg1" in (self.reserve_peer_state().get("caps") or [])
        if tg:
            from awgbot.infra.nftguard.common import TG_OUT4
            allowed += list(TG_OUT4)
        lines += ["", "[Peer]", f"PublicKey = {zone['pub']}", f"PresharedKey = {rec['psk']}",
                  f"AllowedIPs = {', '.join(allowed)}", f"Endpoint = {zone['host']}:{int(zone['port'])}",
                  f"PersistentKeepalive = {ka}"]
        share = {}
        quorum = bool(self.reserve_quorum_keys_on() and self.reserve_quorum_act()) if hasattr(self, "reserve_quorum_keys_on") else False
        if not quorum:                                        # доля прямой «2 из n» — у линии на матрице (и при act = false)
            try:
                gen, y = self.reserve_share_gateway(int(gw.id))
                share = {"x": rsvkeys.gateway_x(int(gw.id)), "y": y, "gen": int(gen)}
            except Exception:                                 # noqa: BLE001 — ключей ещё нет
                share = {}
        from ..selfhost import own_host
        return {"self": own_host(), "name": str(config.SERVER_NAME or ""), "peer_name": self.reserve_peer_name(), "slot": int(gw.id),
                "state": self.reserve_gw_state(), "move": self.reserve_move_gen(),
                "record": self.reserve_dns_name(), "peer_host": self.reserve_peer_ip(), "chan": int(zone.get("chan") or 0),
                "share": share, "tg": tg, "link": {"conf": "\n".join(lines) + "\n"}}

    def reserve_gw_state(self) -> str:
        """Слово основы шлюзам: `away` — пользователи у резерва (после подъёма клона),
        `idle` — здесь (и при возврате: номер переезда закрывает пересылку)."""
        out = self.rsv_get("rsv_out")
        state, step = str(out.get("state") or "home"), str(out.get("step") or "")
        if state == "away" or (state == "leaving" and step in ("record", "release", "released")):
            return "away"
        if state == "returning" and step != "relay":
            return "away"                                     # пользователи ещё на клоне: пересылка шлюзов нужна до `back`
        return "idle"

    # ── взгляды шлюзов из снимков ───────────────────────────────────────────
    def reserve_gateway_views(self) -> list[dict]:
        """По слотам: имя, на связи ли с сервером, связь с резервом (из снимка),
        взгляд на основу, поколение доли, пересылка."""
        out = []
        for gw in self.db.gateways():
            snap = self.gwlink_snapshot(gw.id) if hasattr(self, "gwlink_snapshot") else {}
            rsv = (snap or {}).get("rsv") or {}
            caps = self.gwlink_agent_caps(gw.id) if hasattr(self, "gwlink_agent_caps") else []
            out.append({"slot": gw.id, "name": self._gw_name(gw) if hasattr(self, "_gw_name") else f"Шлюз {gw.id}",
                        "rsv1": "rsv1" in (caps or []), "online": bool(self.gwlink_session(gw.id)) if hasattr(self, "gwlink_session") else False,
                        "seen": str(rsv.get("seen") or ""), "since": str(rsv.get("since") or ""),
                        "primary": str(rsv.get("primary") or ""), "gen": int(rsv.get("gen") or 0),
                        "open": bool(rsv.get("open")), "state": str(rsv.get("state") or "")})
        return out

    def reserve_gw_links_tick(self) -> list[Notification]:
        """Т-РУ18: шлюз не на связи с резервом ≥ 10 мин — один раз на расхождение;
        вернулся — «снова на связи»; ни одного — отдельная строка."""
        if not self.reserve_paired():
            return []
        from awgbot.domain.notices import reserve as rn
        name = self.reserve_peer_name()
        marks = dict(self.rsv_get("rsv_view").get("gw_down") or {})
        notes: list[Notification] = []
        views = [v for v in self.reserve_gateway_views() if v["rsv1"]]
        now = timeutil.now()
        for v in views:
            key = str(v["slot"])
            down = v["seen"] == "down" or (v["seen"] == "absent" and v["online"])
            since = v.get("since") or ""
            long_enough = False
            if down and since:
                try:
                    long_enough = timeutil.elapsed(timeutil.parse_iso(since), now) >= RSV_NOTIFY_MINUTES * 60
                except ValueError:
                    long_enough = False
            if down and long_enough and not marks.get(key):
                marks[key] = since or timeutil.now_iso()
                notes.append(Notification(tg_id=int(config.ADMIN_ID), text=rn.gateway_reserve_down(v["name"], name, since)))
            elif not down and marks.get(key):
                marks.pop(key, None)
                notes.append(Notification(tg_id=int(config.ADMIN_ID), text=rn.gateway_reserve_up(v["name"], name)))
        if views and all(str(v["slot"]) in marks for v in views) and not self.rsv_get("rsv_view").get("gw_all_down"):
            self.rsv_update("rsv_view", gw_all_down=True)
            notes.append(Notification(tg_id=int(config.ADMIN_ID), text=rn.gateways_reserve_all_down(name, min(marks.values()))))
        elif self.rsv_get("rsv_view").get("gw_all_down") and not all(str(v["slot"]) in marks for v in views):
            self.rsv_update("rsv_view", gw_all_down=False)
        self.rsv_update("rsv_view", gw_down=marks)
        return notes
