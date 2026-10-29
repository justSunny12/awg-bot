"""
keys.py — часть пакета quorum (см. __init__): ключи кворума (`rsv_qkeys`) —
многочлен большинства `Mq`, прямая аварийного ключа `Mc`, два ящика вокруг
`M` (util/rsvkeys), обмен `qkeys`/`qkeys_ok` со вторым сервером, точки
шлюзам разделом `quorum` документа, включение и выключение (концепт
«кворум» §2.7, §3.7.10, §8.1).

У основы: `{"on", "q_gen", "t", "poly": [hex…], "c_gen", "mc", "ma", "acked":
{m_gen, q_gen, c_gen}}` — коэффициенты многочлена и прямая кода живут только
здесь. У второго сервера: `{"peer": {l, m_gen, q_gen, t, x, y, qbox, c_gen,
cy, cbox, at}}` — его точки и ящики рядом с пакетом (`rsv_pkg_in`).

Включение — один раз новое поколение `M` и новый код (прежняя прямая `M` у
шлюзов и прежний код открывали бы `M` мимо большинства) — Т-К12 админу
основы, Т-К12б админу второго сервера. Смена состава без снятия держателя —
новый многочлен под новое t, `M`, `K`, код прежние; снятие или замена
держателя и возврат после переезда по кворуму — новое поколение `M` и `K`
(keys.reserve_new_generation) и многочлен, код прежний; после переезда по
коду и по «🔄 Сменить код» — ещё новая прямая и новый код. Выключение
(второй сервер без `quo1`, последний шлюз `quo1` снят) — поколение `M` на
прямой «2 из n» и новый код, как было.
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
        """Ключи под кворум: аварийный код — на прямой `Mc`, точки — многочлена `Mq`."""
        return bool(self.reserve_qkeys().get("on"))

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
        """Крючок после нового поколения `M` (keys.reserve_new_generation): при ключах
        под кворум — новый многочлен; `code` — и новая прямая кода."""
        if not self.reserve_quorum_keys_on():
            return
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
        """Ключи по составу: включить (новое поколение `M`, многочлен, прямая кода,
        Т-К12/Т-К12б), выключить (поколение `M` на прямой «2 из n», код заново),
        при смене порога — новый многочлен. Действия — уведомления."""
        on = self.reserve_quorum_keys_on()
        actions: list[tuple] = []
        if want_on and m is not None and not on:
            with _LOCK:
                self.rsv_update("rsv_qkeys", on=True, enabled_at=timeutil.now_iso(), acked={})
            self.reserve_new_generation("включение голосования о сервере", code=True, notify=False)
            self.rsv_update("rsv_keys", code_notify_why="включено голосование о сервере — прежний код не действует")
            actions += self._quorum_enabled_notes(m)
            log.warning("кворум: голосование о «%s» включено — состав %s, порог %d; новое поколение ключа и код",
                        str(config.SERVER_NAME or m.l), ", ".join(m.members), m.t(m.l))
            return actions
        if on and (not want_on or m is None):
            with _LOCK:
                self.rsv_update("rsv_qkeys", on=False, disabled_at=timeutil.now_iso(), poly=[], mc="", ma="")
            self.reserve_new_generation("выключение голосования о сервере")
            self.rsv_update("rsv_keys", code_notify_why="голосование о сервере выключено — код на прежних правилах")
            log.warning("кворум: голосование о «%s» выключено — поколение ключа и код заново", str(config.SERVER_NAME or ""))
            return actions
        if on and m is not None and int(self.reserve_qkeys().get("t") or 0) != m.t(m.l):
            self._quorum_new_poly(m.t(m.l), f"порог {m.t(m.l)} по составу {m.g}")
        return actions

    def _quorum_enabled_notes(self, m: qm.Members) -> list[tuple]:
        from awgbot.domain.notices import reserve as rn
        from ...types import Notification
        names = [self._quorum_node_name(n, m.l) for n in m.gateways] if hasattr(self, "_quorum_node_name") else list(m.gateways)
        notes = [Notification(tg_id=int(config.ADMIN_ID), text=rn.quorum_enabled(self.reserve_peer_name(), names), force_sound=True)]
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
        if not self.reserve_quorum_keys_on() or not self.reserve_quorum_peer_quo1():
            return None
        poly, line = self._quorum_poly(), self._quorum_code_line()
        side = self.reserve_quorum_side()
        keys = self.reserve_keys()
        if poly is None or line is None or not side or not keys.get("m"):
            return None
        q = self.reserve_qkeys()
        want = {"m_gen": int(keys.get("m_gen") or 0), "q_gen": int(q.get("q_gen") or 0), "c_gen": int(q.get("c_gen") or 0)}
        if dict(q.get("acked") or {}) == want:
            return None
        now = time.monotonic()
        if now - float(self.__dict__.get("_qkeys_sent_at") or 0.0) < RESEND_S and self.__dict__.get("_qkeys_sent") == want:
            return None
        self.__dict__["_qkeys_sent_at"], self.__dict__["_qkeys_sent"] = now, want
        m = bytes.fromhex(keys["m"])
        qg = rsvkeys.QuorumGen(want["q_gen"], poly)
        cg = rsvkeys.CodeGen(want["c_gen"], line)
        return {"l": side, "m_gen": want["m_gen"], "q_gen": want["q_gen"], "t": poly.t, "x": rsvkeys.RESERVE_X,
                "y": qg.reserve_point()[1].hex(), "qbox": qg.qbox(m).hex(), "c_gen": want["c_gen"],
                "cy": cg.reserve_point()[1].hex(), "cbox": cg.cbox(m).hex()}

    def reserve_qkeys_in(self, body: dict) -> list[tuple]:
        """`qkeys` от основы второй линии: точки и ящики — рядом с пакетом; ответ `qkeys_ok`."""
        l = str(body.get("l") or "")
        if l not in qm.SIDES or l == self.reserve_quorum_side():
            return []
        try:
            _hex16(body.get("y"))
            _hex16(body.get("cy"))
            qbox, cbox = bytes.fromhex(str(body.get("qbox") or "")), bytes.fromhex(str(body.get("cbox") or ""))
            if len(qbox) != rsvkeys.BOX_LEN or len(cbox) != rsvkeys.BOX_LEN:
                raise ValueError("ящик не той длины")
            x = int(body.get("x") or 0)
            if x != rsvkeys.RESERVE_X:
                raise ValueError("не моя точка")
        except (ValueError, TypeError) as e:
            log.warning("кворум: qkeys линии %s не приняты: %s", l, e)
            return []
        rec = {"l": l, "m_gen": int(body.get("m_gen") or 0), "q_gen": int(body.get("q_gen") or 0), "t": int(body.get("t") or 0),
               "x": x, "y": str(body["y"]), "qbox": str(body["qbox"]), "c_gen": int(body.get("c_gen") or 0),
               "cy": str(body["cy"]), "cbox": str(body["cbox"]), "at": timeutil.now_iso()}
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
        """Ключи кворума на месте у всех держателей: второй сервер подтвердил текущие
        поколения, у каждого шлюза состава — точка текущего многочлена (поле `quo.q`
        снимка)."""
        if m is None or not self.reserve_quorum_keys_on():
            return False
        q = self.reserve_qkeys()
        keys = self.reserve_keys()
        want = {"m_gen": int(keys.get("m_gen") or 0), "q_gen": int(q.get("q_gen") or 0), "c_gen": int(q.get("c_gen") or 0)}
        if dict(q.get("acked") or {}) != want:
            return False
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
        """Вход процесса открытия по коду при ключах под кворум у второй линии: точка резерва
        прямой кода и ящик кода (поколение `M` — как у пакета); None — путь прежний."""
        peer = self.reserve_quorum_peer_keys()
        pkg_in = self.rsv_get("rsv_pkg_in")
        if not peer.get("cbox") or int(peer.get("m_gen") or 0) != int(pkg_in.get("m_gen") or 0):
            return None
        return {"code": code, "cpoint": [int(peer.get("x") or rsvkeys.RESERVE_X), str(peer.get("cy") or "")],
                "cbox": str(peer.get("cbox") or "")}
