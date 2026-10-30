"""
keys.py — часть пакета reserve (см. __init__): `K`, `M`, поколения и аварийный
код на стороне основы.

Ключ `rsv_keys`: `m` (hex) — главный ключ текущего поколения, `m_gen`; `k`
(hex) и `k_gen` — ключ пакета; `code_shown_at` — когда код показан;
`compromised` — ключ могла узнать вторая сторона (переезд по точкам или по
коду, снятие или замена шлюза): после возврата — новое поколение. `K`
меняется после каждого возврата, `M` — только когда скомпрометирован.

Прямой «2 из n» нет: `M` всегда закрыт ящиком кода (прямая `Mc`: точка
второго сервера + аварийный код админа, quorum/keys) с сопряжения, а пока
кворум линии включён — ещё ящиком кворума (многочлен `Mq`, точки у второго
сервера и шлюзов). Смена поколения `M` обновляет оба ящика; прямая кода и
код меняются только по `code` (после переезда по коду, «🔄 Сменить код»).
"""
from __future__ import annotations

from awgbot.core import config
from awgbot.util import rsvkeys, timeutil
from awgbot.util.rsvpkg import seal

from ..gateway_link.common import log
from .state import _LOCK


class ReserveKeysMixin:

    def reserve_keys(self) -> dict:
        return self.rsv_get("rsv_keys")

    def reserve_keys_init(self) -> dict:
        """Первое поколение при сопряжении: `M`, `K`; ящик кода — сразу (quorum/keys)."""
        with _LOCK:
            data = {"m": rsvkeys.new_master().hex(), "m_gen": 1, "k": seal.new_k().hex(), "k_gen": 1,
                    "compromised": False, "created_at": timeutil.now_iso()}
            self.rsv_put("rsv_keys", data)
        if hasattr(self, "reserve_quorum_keys_init"):
            self.reserve_quorum_keys_init()
        return data

    def reserve_keys_ensure(self) -> None:
        """Защитная ветка: состояние прежних выпусков (прямая `M` без ящика кода) — один раз
        новое поколение и ящик кода, код — заново (Т-РУ14 тактом). На установках с парой
        ниже 4.2.0 недостижимо: таких пар нет."""
        if not self.reserve_paired() or not hasattr(self, "reserve_quorum_keys_on"):
            return
        keys = self.reserve_keys()
        if not keys.get("m") or self.reserve_quorum_keys_on():
            return
        self.reserve_quorum_keys_init()
        self.reserve_new_generation("ключи резерва 4.2.0: аварийный код на своей прямой", code=True)
        self.rsv_update("rsv_keys", code_notify_why="ключи резерва 4.2.0 — прежний код не действует")
        log.warning("резерв: ключи переведены на ящик кода — новое поколение M и новый код")

    def reserve_new_generation(self, reason: str, *, code: bool = True, notify: bool = True) -> dict:
        """Новое поколение `M` — новый `K`, оба ящика заново (quorum/keys); новая прямая
        кода и код — только с `code` (снятие шлюза и возврат после переезда по точкам
        код не меняют); `notify` — слать ли код тактом (у включения — своё слово)."""
        del notify
        with _LOCK:
            keys = self.reserve_keys()
            keys.pop("a", None)                                   # прямой «2 из n» больше нет
            keys.update(m=rsvkeys.new_master().hex(), m_gen=int(keys.get("m_gen") or 0) + 1, k=seal.new_k().hex(),
                        k_gen=int(keys.get("k_gen") or 0) + 1, compromised=False,
                        code_shown_at="", regenerated_at=timeutil.now_iso(), regenerated_why=reason)
            self.rsv_put("rsv_keys", keys)
        if hasattr(self, "reserve_quorum_after_m"):
            self.reserve_quorum_after_m(reason, code=code)   # многочлен заново (если есть), прямая кода — по `code`
        if hasattr(self, "reserve_pkg_mark"):
            self.reserve_pkg_mark()                           # пакет прежнего поколения у резерва — заменить сразу
        return keys

    def _reserve_after_gateway_change(self, why: str) -> None:
        """Снятие или замена шлюза: его точка многочлена могла остаться у чужого —
        новое поколение `M` и `K`; прямая кода со шлюзом не связана, код прежний."""
        if not self.reserve_paired():
            return
        try:
            self.reserve_new_generation(why, code=False)
            self.reserve_pkg_mark() if hasattr(self, "reserve_pkg_mark") else None
        except Exception as e:                            # noqa: BLE001
            log.warning("резерв: поколение ключа после смены шлюза не сменено: %s", e)

    def reserve_code_notify_pending(self) -> str:
        """Причина отложенной отправки нового кода админу; пусто — нечего слать."""
        return str(self.reserve_keys().get("code_notify_why") or "")

    def reserve_code_notify_clear(self) -> None:
        self.rsv_update("rsv_keys", code_notify_why="")

    def reserve_new_k(self) -> dict:
        """Новый ключ пакета после каждого возврата: резерв прежний знал. Журнал — снимком
        со сбросом: записи под прежним ключом новым не открыть."""
        with _LOCK:
            keys = self.reserve_keys()
            keys.update(k=seal.new_k().hex(), k_gen=int(keys.get("k_gen") or 0) + 1)
            self.rsv_put("rsv_keys", keys)
        if hasattr(self, "reserve_jrn_new_k"):
            self.reserve_jrn_new_k()
        return keys

    def reserve_mark_compromised(self, why: str) -> None:
        self.rsv_update("rsv_keys", compromised=True, compromised_why=why)

    def reserve_compromised(self) -> bool:
        return bool(self.reserve_keys().get("compromised"))

    # ── код ───────────────────────────────────────────────────────────────────

    def reserve_code(self) -> str:
        """Аварийный код — точка x = 255 прямой `Mc` (quorum/keys), строкой для человека."""
        return self.reserve_quorum_code() if hasattr(self, "reserve_quorum_code") else ""

    def reserve_code_shown(self) -> None:
        self.rsv_update("rsv_keys", code_shown_at=timeutil.now_iso())

    def reserve_m(self) -> bytes:
        return bytes.fromhex(self.reserve_keys().get("m") or "")

    def reserve_k(self) -> bytes:
        if config.GUEST:                                      # бот основы на резерве: K — в его хранилище
            from awgbot.infra import rsvzone
            try:
                return bytes.fromhex(rsvzone.K_FILE.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                return b""
        return bytes.fromhex(self.reserve_keys().get("k") or "")
