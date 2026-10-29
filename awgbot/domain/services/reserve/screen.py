"""
screen.py — часть пакета reserve (см. __init__): данные экранов раздела
«🛟 Резерв» — без пары и карточки — одним словарём, чтобы слой бота ничего
не считал сам.
"""
from __future__ import annotations

import ipaddress

from awgbot.core import config, settings
from awgbot.util import timeutil


class ReserveScreenMixin:

    def _reserve_gateways_view(self) -> list[dict]:
        out = []
        for gw in self.db.gateways():
            caps = self.gwlink_agent_caps(gw.id) if hasattr(self, "gwlink_agent_caps") else []
            ver = str((self.gwlink_session(gw.id) if hasattr(self, "gwlink_session") else {}).get("agent") or "")
            out.append({"slot": gw.id, "name": self._gw_name(gw) if hasattr(self, "_gw_name") else f"Шлюз {gw.id}",
                        "rsv": "rsv1" in (caps or []), "version": ver})
        return out

    def reserve_section_data(self) -> dict:
        host = str(settings.get("app.network.server_host", config.SERVER_HOST) or "").strip()
        try:
            ipaddress.ip_address(host)
            is_ip = True
        except ValueError:
            is_ip = False
        inv = self.reserve_invite()
        live = self.reserve_invite_live()
        return {"paired": self.reserve_paired(), "host": host, "host_is_ip": is_ip,
                "port": int(config.SERVER_PORT or 0), "gateways": self._reserve_gateways_view(),
                # запись DNS — по настройке «🌐 DNS-запись»: тот же сборщик, что у карточки пары (без DNS-запросов)
                "dns": self.reserve_dns_line_data(),
                "ready": settings.get_bool("app.reserve.ready", False),
                "invite_live": live and inv.get("role") != "acceptor",
                "accepting": live and inv.get("role") == "acceptor"}

    def reserve_card_data(self) -> dict:
        peer = self.reserve_peer() or {}
        ps = self.reserve_peer_state()
        dirs = self.reserve_directions()
        sent = self.rsv_get("rsv_pkg_sent")
        out = self.rsv_get("rsv_out")
        voters = []
        for g in self._reserve_gateways_view():
            if g["rsv"]:
                voters.append(g["name"])
        return {"peer": peer, "peer_state": ps, "ready": settings.get_bool("app.reserve.ready", False),
                "rf_on": settings.get_bool("app.reserve.rf_via_mine", True), "has_gateway": bool(self.db.gateways()),
                "ret_immediate": str(settings.get("app.reserve.return", "immediate") or "immediate") == "immediate",
                "online": self.reserve_online(), "seen": self.reserve_peer_seen(), "down_since": self.reserve_down_since(),
                "out": self.reserve_out(), "out_since": out.get("since") or "", "inn": self.reserve_in(),
                "dir_out": dirs["out"], "dir_in": dirs["in"], "voters": voters,
                "pkg_sent_at": max(str(sent.get("at") or ""), self.reserve_jrn_synced_at()), "traffic": self._reserve_traffic_view(),
                "pkg_error": str(self.rsv_get("rsv_view").get("pkg_error") or ""),
                "move_why": self.reserve_move_check(), "evict_why": self.reserve_evict_check(),
                "in_since": str(self.rsv_get("rsv_in").get("taken_at") or self.rsv_get("rsv_in").get("since") or ""),
                "in_relay_until": str(self.rsv_get("rsv_in").get("relay_until") or ""),
                "out_relay_until": str(out.get("relay_until") or ""), "out_step": str(out.get("step") or ""),
                "dns": self.reserve_dns_screen_data(),
                "gw_links": self.reserve_gateway_views(), "rf_line": self.reserve_rf_line(),
                "peer_case_line": self.reserve_peer_case_line(),
                "auto_block": str(self.rsv_get("rsv_view").get("auto_block") or ""),
                "code_open": bool(self.reserve_code_window()["open"]),
                "mem_free": int(ps.get("mem_free") or 0), "here": self._online_count(), "there": int(ps.get("there") or 0),
                "force_why": self.reserve_force_check() if self.reserve_out() == "away" else "пользователи не у резерва",
                "await_merge": bool(out.get("await_merge")),
                "jrn_on": self.reserve_jrn_on(), "jrn_moved": self.reserve_jrn_moved(),
                "jrn_follow": self.reserve_follow_view(),
                "self_admin": self.reserve_self_peer_admin(),
                "quorum": self._quorum_card_safe()}

    def _quorum_card_safe(self) -> dict:
        try:
            return self.reserve_quorum_card() if hasattr(self, "reserve_quorum_card") else {}
        except Exception as e:                                # noqa: BLE001
            from ..gateway_link.common import log
            log.info("кворум: блок карточки не собрался: %s", e)
            return {}

    def _reserve_traffic_view(self) -> dict:
        tr = self.rsv_get("rsv_traffic")
        if not tr:
            return {}
        from awgbot.util.textfmt import human_bytes, month_name
        now = timeutil.now()
        month = now.strftime("%Y-%m")
        out, inn = tr.get("out") or {}, tr.get("in") or {}
        view = {"month_label": month_name(now)}
        if out.get("month") == month and (out.get("rx") or out.get("tx")):
            view["out"] = f"↑ {human_bytes(int(out.get('tx') or 0))} · ↓ {human_bytes(int(out.get('rx') or 0))}"
        if inn.get("month") == month and (inn.get("rx") or inn.get("tx")):
            view["in"] = f"↑ {human_bytes(int(inn.get('tx') or 0))} · ↓ {human_bytes(int(inn.get('rx') or 0))}"
        return view
