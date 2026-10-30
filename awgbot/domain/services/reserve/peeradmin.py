"""
peeradmin.py — часть пакета reserve (см. __init__): роль админа второго
сервера в этом боте.

Ровно один аккаунт — `reserve_peer.peer_admin_id` (приходит при сопряжении
под ключом сопряжения, меняется по `st.admin_id`). Роль — только чтение
сведений о паре и ввод аварийного кода, пока открыто окно кода (codewin:
этот сервер не видит второй, а голосов или точек для переезда не хватает). Сводка экрана собирается из таблицы
пары, ключей `rsv_*` и счётчиков — ни одного поля пользователей этого сервера.
"""
from __future__ import annotations

from awgbot.core import config, settings
from awgbot.util import timeutil


class ReservePeerAdminMixin:

    def reserve_peer_admin_id(self) -> int:
        peer = self.reserve_peer()
        return int((peer or {}).get("peer_admin_id") or 0)

    def reserve_is_peer_admin(self, uid: int) -> bool:
        """Этот аккаунт — админ второго сервера (и не админ этого: совпавший
        ADMIN_ID — админ)."""
        pid = self.reserve_peer_admin_id()
        return bool(pid) and int(uid) == pid and int(uid) != int(config.ADMIN_ID)

    def reserve_self_peer_admin(self) -> bool:
        """Один человек — админ обоих серверов: роль не наступает, экран роли —
        разделом его же «🛟 Резерв» («🛰 Для «Alpha»»), ввод кода там же."""
        pid = self.reserve_peer_admin_id()
        return bool(pid) and pid == int(config.ADMIN_ID)

    def reserve_code_sender_ok(self, uid: int) -> bool:
        """Кому принимать аварийный код: админу второго сервера в его роли или
        общему админу в своём разделе."""
        return self.reserve_is_peer_admin(int(uid)) or (int(uid) == int(config.ADMIN_ID) and self.reserve_self_peer_admin())

    def reserve_peer_admin_client(self):
        """Пользователь этого сервера с аккаунтом админа второго (в. 51) или None."""
        pid = self.reserve_peer_admin_id()
        if not pid or pid == int(config.ADMIN_ID):
            return None
        try:
            return self.db.get_client_by_tg(pid)
        except Exception:                                     # noqa: BLE001
            return None

    def reserve_peer_admin_is_client(self) -> bool:
        """Админ второго сервера — ещё и пользователь этого: на экране роли «👤 Меню пользователя»."""
        return self.reserve_peer_admin_client() is not None

    def reserve_peer_admin_opened(self) -> None:
        """Админ второго сервера открыл бот (/start) — ему теперь можно писать."""
        cur = self.rsv_get("rsv_peer_admin")
        if not cur.get("opened"):
            self.rsv_update("rsv_peer_admin", opened=True, opened_at=timeutil.now_iso())

    def reserve_peer_admin_summary(self) -> dict:
        """Данные экрана роли (Т-РН1): как этот сервер видит второй, готовность
        направления «его пользователи → сюда», пакет, трафик; без пользователей."""
        peer = self.reserve_peer() or {}
        ps = self.reserve_peer_state()
        dirs = self.reserve_directions()
        pkg_in = self.rsv_get("rsv_pkg_in")
        return {
            "peer": peer, "me": str(config.SERVER_NAME or ""), "version": str(config.INSTALLED_VERSION).lstrip("v"),
            "direction_in": dirs["in"],                          # его пользователи → сюда
            "awg_ok": bool(self.server_ok()) if hasattr(self, "server_ok") else True,
            "link_online": self.reserve_online(), "seen": self.reserve_peer_seen(),
            "down_since": self.reserve_down_since(),
            "inn": self.reserve_in(), "in_since": self.rsv_get("rsv_in").get("since") or "",
            "pkg_at": pkg_in.get("at") or "", "pkg_gen": int(pkg_in.get("gen") or 0),
            "rf_via_mine": settings.get_bool("app.reserve.rf_via_mine", True),
            "rfpeer": self.reserve_rfpeer_view() if hasattr(self, "reserve_rfpeer_view") else {},
            "has_gateway": bool(self.db.gateways()),
            "traffic": self._reserve_traffic_view(),
            "peer_ready": bool(ps.get("ready")), "my_ready": settings.get_bool("app.reserve.ready", False),
            "code_needed": bool(self.reserve_code_window()["open"]),
            "code_by": str((self.rsv_get("rsv_view").get("code") or {}).get("by") or ""),
            "code_since": self.reserve_code_window()["since"],
            "in_kind": str(self.rsv_get("rsv_in").get("kind") or ""),
            "gw_views": self.reserve_gw_links_view() if hasattr(self, "reserve_gw_links_view") else [],
            "zone_links": [int(k) for k in self.reserve_guest_links() if str(k).isdigit()],
            "mem_free": self.reserve_mem_free_mb(), "has_client": self.reserve_peer_admin_is_client(),
            "self_admin": self.reserve_self_peer_admin(), "tg_pool": self.reserve_tg_pool_view(),
            "quorum": self._peer_admin_quorum(),
        }

    def _peer_admin_quorum(self) -> dict:
        """Т-К22: голоса о его сервере по моему зонду — {y, v, w, since}; пусто — кворума нет."""
        if not hasattr(self, "reserve_quorum_other_view") or not self.reserve_quorum_peer_line().get("active"):
            return {}
        view = self.reserve_quorum_other_view()
        if not view or "y" not in view:
            return {}
        return {"y": int(view.get("y") or 0), "v": int(view.get("v") or 0), "w": str(view.get("w") or ""),
                "since": str(view.get("since") or "")}

    def reserve_code_prompt_data(self) -> dict:
        """Т-РН2: имена, момент аварии, запись основы (из заголовка пакета — нет: из st)."""
        return {"peer": self.reserve_peer_name(), "me": str(config.SERVER_NAME or ""),
                "since": self.reserve_code_window()["since"],
                "record": str(self.reserve_peer_state().get("record") or "")}
