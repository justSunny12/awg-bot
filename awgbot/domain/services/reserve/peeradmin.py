"""
peeradmin.py — часть пакета reserve (см. __init__): роль админа второго
сервера в этом боте.

Ровно один аккаунт — `reserve_peer.peer_admin_id` (приходит при сопряжении
под ключом сопряжения, меняется по `st.admin_id`). Роль — только чтение
сведений о паре и ввод аварийного кода, когда этот сервер не видит второй и
его шлюзов на связи нет (Н4, этап 5). Сводка экрана собирается из таблицы
пары, ключей `rsv_*` и счётчиков — ни одного поля клиентов этого сервера.
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

    def reserve_peer_admin_opened(self) -> None:
        """Админ второго сервера открыл бот (/start) — ему теперь можно писать."""
        cur = self.rsv_get("rsv_peer_admin")
        if not cur.get("opened"):
            self.rsv_update("rsv_peer_admin", opened=True, opened_at=timeutil.now_iso())

    def reserve_peer_admin_summary(self) -> dict:
        """Данные экрана роли (Т-РН1): как этот сервер видит второй, готовность
        направления «его клиенты → сюда», пакет, трафик; без клиентов."""
        peer = self.reserve_peer() or {}
        ps = self.reserve_peer_state()
        dirs = self.reserve_directions()
        pkg_in = self.rsv_get("rsv_pkg_in")
        return {
            "peer": peer, "me": str(config.SERVER_NAME or ""), "version": str(config.INSTALLED_VERSION).lstrip("v"),
            "direction_in": dirs["in"],                          # его клиенты → сюда
            "awg_ok": bool(self.server_ok()) if hasattr(self, "server_ok") else True,
            "link_online": self.reserve_online(), "seen": self.reserve_peer_seen(),
            "down_since": self.reserve_down_since(),
            "inn": self.reserve_in(), "in_since": self.rsv_get("rsv_in").get("since") or "",
            "pkg_at": pkg_in.get("at") or "", "pkg_gen": int(pkg_in.get("gen") or 0),
            "rf_via_mine": settings.get_bool("app.reserve.rf_via_mine", True),
            "has_gateway": bool(self.db.gateways()),
            "traffic": self._reserve_traffic_view(),
            "peer_ready": bool(ps.get("ready")), "my_ready": settings.get_bool("app.reserve.ready", False),
            "code_needed": False,                                # Н4 — этап 5
        }
