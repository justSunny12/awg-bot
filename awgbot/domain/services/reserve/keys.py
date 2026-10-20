"""
keys.py — часть пакета reserve (см. __init__): `K`, `M`, поколения, доли и
аварийный код на стороне основы.

Ключ `rsv_keys`: `m` и `a` (hex) — главный ключ и наклон прямой текущего
поколения, `m_gen`; `k` (hex) и `k_gen` — ключ пакета; `code_shown_at` —
когда код показан; `compromised` — прямая могла стать известна резерву
(переезд по доле шлюза или по коду, снятие или замена шлюза): после возврата
— новое поколение. `K` меняется после каждого возврата, `M` — только когда
прямая скомпрометирована (`util/rsvkeys`).
"""
from __future__ import annotations

from awgbot.util import rsvkeys, shamir, timeutil
from awgbot.util.rsvpkg import seal

from ..gateway_link.common import log
from .state import _LOCK


class ReserveKeysMixin:

    def reserve_keys(self) -> dict:
        return self.rsv_get("rsv_keys")

    def _reserve_line(self) -> shamir.Line | None:
        keys = self.reserve_keys()
        try:
            return shamir.Line(bytes.fromhex(keys["m"]), bytes.fromhex(keys["a"]))
        except (KeyError, ValueError):
            return None

    def reserve_keys_init(self) -> dict:
        """Первое поколение при сопряжении: `M`, наклон, `K`."""
        with _LOCK:
            gen = rsvkeys.Generation.new(1)
            data = {"m": gen.m.hex(), "a": gen.line.slope.hex(), "m_gen": 1,
                    "k": seal.new_k().hex(), "k_gen": 1, "compromised": False,
                    "created_at": timeutil.now_iso()}
            self.rsv_put("rsv_keys", data)
            return data

    def reserve_new_generation(self, reason: str) -> dict:
        """Новое поколение `M` — новые наклон, все точки и аварийный код; `K`
        тоже новый. Зовётся после возврата при скомпрометированной прямой,
        при снятии или замене шлюза и по «🔄 Сменить код»."""
        with _LOCK:
            keys = self.reserve_keys()
            gen = rsvkeys.Generation.new(int(keys.get("m_gen") or 0) + 1)
            keys.update(m=gen.m.hex(), a=gen.line.slope.hex(), m_gen=gen.gen, k=seal.new_k().hex(),
                        k_gen=int(keys.get("k_gen") or 0) + 1, compromised=False,
                        code_shown_at="", regenerated_at=timeutil.now_iso(), regenerated_why=reason)
            self.rsv_put("rsv_keys", keys)
            self.rsv_put("rsv_shares", None)
            return keys

    def _reserve_after_gateway_change(self, why: str) -> None:
        """Снятие или замена шлюза: его доля ключа переезда могла остаться у
        чужого — новое поколение и код; код админу шлёт такт планировщика."""
        if not self.reserve_paired():
            return
        try:
            self.reserve_new_generation(why)
            self.rsv_update("rsv_keys", code_notify_why=why)
            self.reserve_pkg_mark() if hasattr(self, "reserve_pkg_mark") else None
        except Exception as e:                            # noqa: BLE001
            log.warning("резерв: поколение ключа после смены шлюза не сменено: %s", e)

    def reserve_code_notify_pending(self) -> str:
        """Причина отложенной отправки нового кода админу; пусто — нечего слать."""
        return str(self.reserve_keys().get("code_notify_why") or "")

    def reserve_code_notify_clear(self) -> None:
        self.rsv_update("rsv_keys", code_notify_why="")

    def reserve_new_k(self) -> dict:
        """Новый ключ пакета после каждого возврата: резерв прежний знал."""
        with _LOCK:
            keys = self.reserve_keys()
            keys.update(k=seal.new_k().hex(), k_gen=int(keys.get("k_gen") or 0) + 1)
            self.rsv_put("rsv_keys", keys)
            return keys

    def reserve_mark_compromised(self, why: str) -> None:
        self.rsv_update("rsv_keys", compromised=True, compromised_why=why)

    def reserve_compromised(self) -> bool:
        return bool(self.reserve_keys().get("compromised"))

    # ── точки ────────────────────────────────────────────────────────────────

    def reserve_share(self, x: int) -> tuple[int, str]:
        """(поколение, доля hex) точки x текущей прямой."""
        line = self._reserve_line()
        if line is None:
            raise ValueError("ключей резерва нет")
        return int(self.reserve_keys().get("m_gen") or 0), line.point(x)[1].hex()

    def reserve_share_reserve(self) -> tuple[int, str]:
        return self.reserve_share(rsvkeys.RESERVE_X)

    def reserve_share_gateway(self, slot: int) -> tuple[int, str]:
        return self.reserve_share(rsvkeys.gateway_x(slot))

    def reserve_code(self) -> str:
        """Аварийный код — точка x = 255 текущего поколения, строкой для человека."""
        line = self._reserve_line()
        if line is None:
            return ""
        return rsvkeys.code_encode(line.point(rsvkeys.CODE_X)[1])

    def reserve_code_shown(self) -> None:
        self.rsv_update("rsv_keys", code_shown_at=timeutil.now_iso())

    def reserve_m(self) -> bytes:
        return bytes.fromhex(self.reserve_keys().get("m") or "")

    def reserve_k(self) -> bytes:
        return bytes.fromhex(self.reserve_keys().get("k") or "")
