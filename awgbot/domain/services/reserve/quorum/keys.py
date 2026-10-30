"""
keys.py — часть пакета quorum (см. __init__): ключи вокруг `M` (`rsv_qkeys`) —
прямая аварийного ключа `Mc` и ящик кода (с сопряжения, всегда), многочлен
большинства `Mq` и ящик кворума (пока кворум линии решает: v ≥ 2), обмен
`qkeys`/`qkeys_ok` со вторым сервером, точки шлюзам разделом `quorum`
документа (util/rsvkeys).

У основы: `{"on", "c_gen", "mc", "ma", "q_gen", "t", "poly": [hex…], "acked":
{m_gen, q_gen, c_gen}}` — коэффициенты многочлена и прямая кода живут только
здесь. У второго сервера: `{"peer": {l, m_gen, c_gen, cy, cbox, q_gen, t, x, y,
qbox, at}}` — его точки и ящики рядом с пакетом (`rsv_pkg_in`); часть
многочлена пуста, пока кворум линии не решает.

Включение кворума (шлюз `quo1` в составе) — только новый многочлен и ящик
кворума: `M`, `K`, пакет, прямая кода и код прежние (Т-К12 без звука).
Смена порога — новый многочлен. Снятие или замена держателя точки и возврат
после переезда по точкам — новое поколение `M` и `K` (keys.reserve_new_generation),
оба ящика заново, код прежний; после переезда по коду и по «🔄 Сменить код» —
ещё новая прямая и новый код. Выключение (шлюзов `quo1` в составе не
осталось) — многочлен и ящик кворума прочь, код прежний.
"""
from __future__ import annotations

import time

from awgbot.core import config
from awgbot.util import rsvkeys, shamir, timeutil
from awgbot.util.quorum import members as qm

from ...gateway_link.common import log
from ..state import _LOCK

RESEND_S = 60
_HEX32 = 32


def _hex16(v) -> bytes:
    b = bytes.fromhex(str(v or ""))
    if len(b) != rsvkeys.M_LEN:
        raise ValueError("не 16 байт")
    return b


class ReserveQuorumKeysMixin:

    def reserve_qkeys(self) -> dict:
        return self.rsv_get("rsv_qkeys")

    def reserve_quorum_keys_on(self) -> bool:
        """Ящик кода есть (с сопряжения): аварийный код — на прямой `Mc`."""
        return bool(self.reserve_qkeys().get("on"))

    def reserve_quorum_poly_on(self) -> bool:
        """Ящик кворума есть: многочлен `Mq` (пока кворум линии решает)."""
        return self._quorum_poly() is not None

    def reserve_quorum_keys_init(self) -> None:
        """С сопряжения: прямая кода и ящик кода; многочлена нет, пока нет шлюза в составе."""
        with _LOCK:
            gen = rsvkeys.CodeGen.new(1)
            self.rsv_put("rsv_qkeys", {"on": True, "enabled_at": timeutil.now_iso(), "c_gen": gen.c_gen, "mc": gen.mc.hex(),
                                       "ma": gen.line.slope.hex(), "code_at": timeutil.now_iso(), "q_gen": 0, "t": 0,
                                       "poly": [], "acked": {}})
        log.info("кворум: ящик кода создан — прямая аварийного ключа 1")

    # ── поколения ───────────────────────────────────────────────────────────
    def _quorum_poly(self) -> shamir.Poly | None:
        q = self.reserve_qkeys()
        try:
            coeffs = tuple(_hex16(c) for c in (q.get("poly") or []))
            return shamir.Poly(coeffs) if len(coeffs) >= 2 else None
        except ValueError:
            return None

    def _quorum_code_line(self) -> shamir.Line | None:
        q = self.reserve_qkeys()
        try:
            return shamir.Line(_hex16(q.get("mc")), _hex16(q.get("ma")))
        except ValueError:
            return None

    def _quorum_new_poly(self, t: int, why: str) -> None:
        """Новый многочлен под порог t: новые `Mq`, точки всем держателям, ящик кворума."""
        with _LOCK:
            q = self.reserve_qkeys()
            gen = rsvkeys.QuorumGen.new(int(q.get("q_gen") or 0) + 1, max(2, int(t)))
            q.update(q_gen=gen.q_gen, t=gen.t, poly=[c.hex() for c in gen.poly.coeffs], poly_at=timeutil.now_iso())
            self.rsv_put("rsv_qkeys", q)
        log.info("кворум: многочлен ключа кворума %s (порог %s) — %s", gen.q_gen, gen.t, why)

    def _quorum_new_code(self, why: str) -> None:
        """Новая прямая аварийного ключа `Mc` и новый код."""
        with _LOCK:
            q = self.reserve_qkeys()
            gen = rsvkeys.CodeGen.new(int(q.get("c_gen") or 0) + 1)
            q.update(c_gen=gen.c_gen, mc=gen.mc.hex(), ma=gen.line.slope.hex(), code_at=timeutil.now_iso())
            self.rsv_put("rsv_qkeys", q)
        log.info("кворум: прямая аварийного ключа %s — %s", gen.c_gen, why)

    def reserve_quorum_after_m(self, why: str, *, code: bool) -> None:
        """Крючок после нового поколения `M` (keys.reserve_new_generation): многочлен заново,
        если он есть; `code` — и новая прямая кода."""
        if not self.reserve_quorum_keys_on():
            return
        if self.reserve_quorum_poly_on():
            m = self.reserve_quorum_proposed() if hasattr(self, "reserve_quorum_proposed") else None
            t = m.t(m.l) if m is not None else int(self.reserve_qkeys().get("t") or 2)
            self._quorum_new_poly(t, why)
        if code:
            self._quorum_new_code(why)

    def reserve_quorum_code(self) -> str:
        """Аварийный код под кворум — точка x = 255 прямой `Mc`; пусто — ключей нет."""
        line = self._quorum_code_line()
        return rsvkeys.code_encode(line.point(rsvkeys.CODE_X)[1]) if line is not None else ""

    # ── включение и выключение ──────────────────────────────────────────────
    def reserve_quorum_keys_sync(self, m: qm.Members | None, want_on: bool) -> list[tuple]:
        """Многочлен по составу: `want_on` — кворум линии решает (шлюз `quo1` в составе, v ≥ 2):
        нет многочлена — создать (Т-К12/Т-К12б, `M` и код прежние); состав без шлюзов или
        кворум выключен — многочлен и ящик кворума прочь, код прежний; смена порога — новый
        многочлен. Действия — уведомления."""
        if not self.reserve_quorum_keys_on():
            return []
        poly_on = self.reserve_quorum_poly_on()
        decide = bool(want_on and m is not None and m.enabled)
        actions: list[tuple] = []
        if decide and not poly_on:
            self._quorum_new_poly(m.t(m.l), f"включение голосования о сервере (состав {m.g})")
            actions += self._quorum_enabled_notes(m)
            log.warning("кворум: голосование о «%s» включено — состав %s, порог %d; ключ и код прежние",
                        str(config.SERVER_NAME or m.l), ", ".join(m.members), m.t(m.l))
            return actions
        if poly_on and not decide:
            with _LOCK:
                self.rsv_update("rsv_qkeys", poly=[], t=0, disabled_at=timeutil.now_iso())
            log.warning("кворум: голосование о «%s» выключено — многочлен и ящик кворума прочь, код прежний",
                        str(config.SERVER_NAME or ""))
            return actions
        if poly_on and m is not None and int(self.reserve_qkeys().get("t") or 0) != max(2, m.t(m.l)):
            self._quorum_new_poly(m.t(m.l), f"порог {m.t(m.l)} по составу {m.g}")
        return actions

    def _quorum_enabled_notes(self, m: qm.Members) -> list[tuple]:
        from awgbot.domain.notices import reserve as rn
        from ...types import Notification
        names = [self._quorum_node_name(n, m.l) for n in m.gateways] if hasattr(self, "_quorum_node_name") else list(m.gateways)
        notes = [Notification(tg_id=int(config.ADMIN_ID), text=rn.quorum_enabled(self.reserve_peer_name(), names, str(config.SERVER_NAME or "")), silent=True)]
        pid = self.reserve_peer_admin_id() if hasattr(self, "reserve_peer_admin_id") else 0
        if pid and bool(self.rsv_get("rsv_peer_admin").get("opened")):
            notes.append(Notification(tg_id=pid, text=rn.quorum_enabled_peer(str(config.SERVER_NAME or "")), silent=True))
        return [("notify", notes)]

    # ── точки и ящики ───────────────────────────────────────────────────────
    def reserve_quorum_gw_point(self, slot: int) -> dict:
        """Точка ключа кворума шлюза слота — в раздел `quorum` документа; пусто — ключей нет."""
        poly = self._quorum_poly()
        if poly is None or not self.reserve_quorum_keys_on():
            return {}
        q = self.reserve_qkeys()
        x, y = poly.point(rsvkeys.gateway_x(int(slot)))
        return {"x": int(x), "y": y.hex(), "q_gen": int(q.get("q_gen") or 0)}

    def reserve_qkeys_body(self) -> dict | None:
        """`qkeys` второму серверу: его точки многочлена и прямой кода, оба ящика; None —
        подтверждено или слать нечего (повтор не чаще RESEND_S)."""
        if not self.reserve_quorum_keys_on():
            return None
        poly, line = self._quorum_poly(), self._quorum_code_line()
        side = self.reserve_quorum_side()
        keys = self.reserve_keys()
        if line is None or not side or not keys.get("m"):
            return None
        q = self.reserve_qkeys()
        want = {"m_gen": int(keys.get("m_gen") or 0), "q_gen": int(q.get("q_gen") or 0) if poly is not None else 0,
                "c_gen": int(q.get("c_gen") or 0)}
        if dict(q.get("acked") or {}) == want:
            return None
        now = time.monotonic()
        if now - float(self.__dict__.get("_qkeys_sent_at") or 0.0) < RESEND_S and self.__dict__.get("_qkeys_sent") == want:
            return None
        self.__dict__["_qkeys_sent_at"], self.__dict__["_qkeys_sent"] = now, want
        m = bytes.fromhex(keys["m"])
        cg = rsvkeys.CodeGen(want["c_gen"], line)
        body = {"l": side, "m_gen": want["m_gen"], "x": rsvkeys.RESERVE_X, "c_gen": want["c_gen"],
                "cy": cg.reserve_point()[1].hex(), "cbox": cg.cbox(m).hex()}
        if poly is not None:                                  # ящик кворума — пока кворум линии решает
            qg = rsvkeys.QuorumGen(want["q_gen"], poly)
            body.update(q_gen=want["q_gen"], t=poly.t, y=qg.reserve_point()[1].hex(), qbox=qg.qbox(m).hex())
        return body

    def reserve_qkeys_in(self, body: dict) -> list[tuple]:
        """`qkeys` от основы второй линии: точки и ящики — рядом с пакетом; ответ `qkeys_ok`."""
        l = str(body.get("l") or "")
        if l not in qm.SIDES or l == self.reserve_quorum_side():
            return []
        try:
            _hex16(body.get("cy"))
            cbox = bytes.fromhex(str(body.get("cbox") or ""))
            if len(cbox) != rsvkeys.BOX_LEN:
                raise ValueError("ящик кода не той длины")
            x = int(body.get("x") or 0)
            if x != rsvkeys.RESERVE_X:
                raise ValueError("не моя точка")
            poly_part = bool(body.get("y") or body.get("qbox"))
            if poly_part:
                _hex16(body.get("y"))
                if len(bytes.fromhex(str(body.get("qbox") or ""))) != rsvkeys.BOX_LEN:
                    raise ValueError("ящик кворума не той длины")
        except (ValueError, TypeError) as e:
            log.warning("кворум: qkeys линии %s не приняты: %s", l, e)
            return []
        rec = {"l": l, "m_gen": int(body.get("m_gen") or 0), "x": x, "c_gen": int(body.get("c_gen") or 0),
               "cy": str(body["cy"]), "cbox": str(body["cbox"]), "at": timeutil.now_iso(),
               "q_gen": int(body.get("q_gen") or 0) if poly_part else 0, "t": int(body.get("t") or 0) if poly_part else 0,
               "y": str(body.get("y") or "") if poly_part else "", "qbox": str(body.get("qbox") or "") if poly_part else ""}
        with _LOCK:
            self.rsv_update("rsv_qkeys", peer=rec)
        log.info("кворум: точки и ящики линии %s приняты (M %s, многочлен %s, прямая кода %s, порог %s)", l, rec["m_gen"],
                 rec["q_gen"], rec["c_gen"], rec["t"])
        return [("send", "qkeys_ok", {"l": l, "m_gen": rec["m_gen"], "q_gen": rec["q_gen"], "c_gen": rec["c_gen"]})]

    def reserve_qkeys_ok_in(self, body: dict) -> list[tuple]:
        if str(body.get("l") or "") != self.reserve_quorum_side():
            return []
        acked = {"m_gen": int(body.get("m_gen") or 0), "q_gen": int(body.get("q_gen") or 0), "c_gen": int(body.get("c_gen") or 0)}
        with _LOCK:
            self.rsv_update("rsv_qkeys", acked=acked)
        log.info("кворум: «%s» подтвердил ключи кворума (M %s, многочлен %s, прямая кода %s)", self.reserve_peer_name(),
                 acked["m_gen"], acked["q_gen"], acked["c_gen"])
        return []

    def reserve_quorum_peer_keys(self) -> dict:
        """Точки и ящики второй линии у меня (сторона резерва): пусто — нет."""
        return dict(self.reserve_qkeys().get("peer") or {})

    def reserve_quorum_keys_ok(self, m: qm.Members | None) -> bool:
        """Ключи на месте у всех держателей: второй сервер подтвердил текущие поколения
        (ящик кода; многочлен — если кворум линии решает), у каждого шлюза состава — точка
        текущего многочлена (поле `quo.q` снимка)."""
        if m is None or not self.reserve_quorum_keys_on():
            return False
        q = self.reserve_qkeys()
        keys = self.reserve_keys()
        poly_on = self.reserve_quorum_poly_on()
        want = {"m_gen": int(keys.get("m_gen") or 0), "q_gen": int(q.get("q_gen") or 0) if poly_on else 0,
                "c_gen": int(q.get("c_gen") or 0)}
        if dict(q.get("acked") or {}) != want:
            return False
        if not poly_on:
            return not m.enabled                              # без шлюзов многочлен не нужен; с шлюзами — ещё не создан
        for node in m.gateways:
            try:
                _side, slot = qm.parse_node(node)
            except ValueError:
                return False
            snap = self.gwlink_snapshot(slot) if hasattr(self, "gwlink_snapshot") else {}
            quo = (snap or {}).get("quo") or {}
            if int(quo.get("q") or 0) != want["q_gen"]:
                return False
        return True

    # ── вход процесса открытия ──────────────────────────────────────────────
    def reserve_quorum_code_secret(self, code: str) -> dict | None:
        """Вход процесса открытия по коду: точка резерва прямой кода и ящик кода второй линии
        (поколение `M` — как у пакета); None — ящика кода текущего `M` нет."""
        peer = self.reserve_quorum_peer_keys()
        pkg_in = self.rsv_get("rsv_pkg_in")
        if not peer.get("cbox") or int(peer.get("m_gen") or 0) != int(pkg_in.get("m_gen") or 0):
            return None
        return {"code": code, "cpoint": [int(peer.get("x") or rsvkeys.RESERVE_X), str(peer.get("cy") or "")],
                "cbox": str(peer.get("cbox") or "")}
