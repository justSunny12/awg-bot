"""
vote.py — часть пакета reserve агента (см. __init__): гостевой канал со
стороны шлюза — словарь `pairproto.GUEST_KINDS`: `hello{slot, caps}` в начале
сессии, `pong` на `ping`; входящие `state`, `ping` и слова кворума (`q*` —
quorum.py). Взгляд шлюза на сервер AWG — только зонд кворума (`qview` первым
словом новой сессии, quorum.py); уведомление о переезде (Т-РА3) — по своему
взгляду зонда в момент открытия пересылки.
"""
from __future__ import annotations

from awgbot.util import timeutil

from ..base import log

GW_CAPS: tuple[str, ...] = ("rsv1", "quo1", "rsvtg1")   # quo1 — кворум: зонды, взгляды, точка ключа кворума;
                                                          # rsvtg1 — выход в Telegram через awgrsv при мёртвом аплинке


class ReserveVoteMixin:
    def reserve_guest_hello_body(self) -> dict | None:
        """Тело hello гостевого канала; None — раздела резерва нет."""
        cur = self.reserve_section()
        if not cur:
            return None
        return {"slot": int(cur.get("slot") or 0), "caps": list(GW_CAPS)}

    def reserve_guest_channel_key(self) -> bytes:
        """Ключ подписи гостевого канала — из PSK линка к резерву."""
        import re
        from awgbot.util import pairproto
        conf = str(self.reserve_section().get("conf") or "")
        m = re.search(r"(?m)^\s*PresharedKey\s*=\s*(\S+)", conf)
        return pairproto.guest_key(m.group(1)) if m else b""

    def reserve_guest_target(self) -> tuple[str, int]:
        """(адрес стороны зоны, порт гостевого канала)."""
        from awgbot.core import settings
        cur = self.reserve_section()
        port = int(cur.get("chan") or 0) or int(settings.get_int("app.gateway.guest_channel_port", 8787) or 8787)
        return str(cur.get("peer_addr") or ""), port

    def reserve_guest_in(self, kind: str, body: dict) -> list[tuple]:
        """Сообщение резерва по гостевому каналу → действия (как в watch)."""
        self.reserve_guest_word()
        if kind == "ping":
            return [("guest", "pong", {"n": str(body.get("n") or "")})]
        if kind == "pong":
            self.reserve_guest_pong_in(str(body.get("n") or ""))
            return []
        if kind == "state":
            actions = self.reserve_state_in(str(body.get("state") or ""), int(body.get("move") or 0),
                                            str(body.get("reason") or ""), e=int(body.get("e") or 0))
            return actions + [("push",)]                      # Т-РА3 даёт само открытие (reserve_forward_decide)
        if kind.startswith("q") and hasattr(self, "quorum_guest_in"):
            return self.quorum_guest_in(kind, body)           # кворум: qask, qtake, qrel, qneed, qpause, qstate
        return []

    def _moved_notice(self, actions: list[tuple]) -> list[tuple]:
        """Т-РА3: пересылка открылась по `serving` резерва — пользователи переехали.
        По своему взгляду зонда на сервер AWG: «не вижу» — 🔴 со звуком и почтой;
        «вижу» — 🟡 без звука (Т-РА3б: так решило большинство)."""
        if not any(a and a[0] == "open" for a in actions):
            return []
        from awgbot.domain.notices import reserve as rn
        cur = self.reserve_section()
        move = int(cur.get("reserve_move") or 0)
        if cur.get("moved_told") == move and move:
            return []                                         # этот переезд уже объявлен
        cur["moved_told"] = move
        w, since = self.quorum_own_view() if hasattr(self, "quorum_own_view") else ("", "")
        sees = w in ("see", "held")
        case = "seen" if sees else "dead"
        text = rn.gateway_moved(str(cur.get("name") or ""), case, since, record=str(cur.get("record") or ""),
                                ip=str(cur.get("peer_host") or ""))
        cur["open_at"] = cur.get("open_at") or timeutil.now_iso()
        # со звуком — и с почтой как запасным каналом (critical): Telegram агента идёт по аплинку,
        # который в этот момент может быть мёртв
        note = self.reserve_note(cur, text, sound=not sees, silent=sees, cond={"open": True}, critical=not sees)
        from .store import _write
        _write(cur, self._RSV_STORE)
        log.info("reserve: пользователи переехали на резерв (взгляд зонда: %s)", w or "нет")
        return [("notify", [note])]
