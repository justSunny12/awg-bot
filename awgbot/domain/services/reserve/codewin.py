"""
codewin.py — часть пакета reserve (см. __init__): окно аварийного кода —
приём кода админа основы в боте резерва, когда голосов для переезда не
хватает. Окно открывает и закрывает кандидат кворума (quorum/candidate: в ходе
решения точек не хватило; вне решения — свой «не вижу», никто не видит,
большинства «нет» нет, Т-К21), ничья кворума — тихо (quorum/tie: тревогу с
кнопкой кода шлёт сама), и запасной сигнал по линку пары, когда линия без
кворума. Код из чата роли — транзитом в процесс открытия (M и K собирает
только он); итог — ответом на сообщение по ходу процесса (codewait), иначе
уведомлением (`reserve_code_failed`). Три попытки в час; нечитаемый ввод
попыткой не считается (`garbled`). Окно закрывается ДО запуска процесса
открытия. Окно, открывшееся снова в том же эпизоде: уведомление резерву не
повторяется, админу основы — ещё раз без звука (код ему снова нужен).
"""
from __future__ import annotations

import time

from awgbot.core import config
from awgbot.util import rsvkeys, timeutil

from ..types import Notification
from ..gateway_link.common import log
from . import codewait
from .opening import CODE_TRIES, CODE_WINDOW_S


class ReserveCodeWindowMixin:
    # ── окно аварийного кода ────────────────────────────────────────────────
    def reserve_code_window(self) -> dict:
        """{"open": bool, "since": iso} — приём кода открыт только в Н4."""
        cw = self.rsv_get("rsv_view").get("code") or {}
        return {"open": bool(cw.get("open")), "since": str(cw.get("since") or "")}

    def reserve_code_window_set(self, open_: bool, *, since: str = "", n: int = 0, v: int = 0, t: int = 0,
                                by: str = "quorum", off_why: str = "", quiet: bool = False) -> list[tuple]:
        """Окно кода кандидата: открыть — Т-К21 админу резерва и (если он открывал бота)
        админу основы с кнопкой кода; тот же эпизод снова — админу основы без звука;
        закрыть — взгляды вернулись (Т-РН3 админу основы, если его звали)."""
        view = self.rsv_get("rsv_view")
        cw = dict(view.get("code") or {})
        primary = self.reserve_peer_name()
        rn = self._rn()
        if open_:
            if cw.get("open") or self.reserve_in() != "idle":
                return []
            since = str(since or timeutil.now_iso())
            pid = self.reserve_peer_admin_id()
            if cw.get("episode") == since:
                cw.update(open=True, by=by)
                self.rsv_update("rsv_view", code=cw)
                if cw.get("told") and pid:
                    return [("notify", [Notification(tg_id=pid, silent=True, keyboard="peer_code",
                                                     text=rn.quorum_code_needed_primary(primary, str(config.SERVER_NAME or ""), since, n, v, t, off_why))])]
                return []
            opened = bool(self.rsv_get("rsv_peer_admin").get("opened"))
            notes = [Notification(tg_id=int(config.ADMIN_ID), text=rn.quorum_code_needed_reserve(primary, since, n, v, t, opened, off_why))]
            if pid and opened:
                notes.append(Notification(tg_id=pid, force_sound=True, keyboard="peer_code",
                                          text=rn.quorum_code_needed_primary(primary, str(config.SERVER_NAME or ""), since, n, v, t, off_why)))
            self.rsv_update("rsv_view", code={"open": True, "since": since, "told": bool(pid and opened), "tries": [],
                                              "episode": since, "by": by})
            log.warning("кворум: «%s» не вижу, голосов для переезда не хватает (не видят %d из %d, нужно %d) — приём кода открыт",
                        primary, n, v, t)
            return [("notify", notes)]
        if not cw.get("open"):
            return []
        self.rsv_update("rsv_view", code={"open": False, "closed_why": by, "closed_at": timeutil.now_iso(),
                                          "episode": str(cw.get("episode") or ""), "told": bool(cw.get("told")),
                                          "by": str(cw.get("by") or "")})
        log.info("кворум: приём аварийного кода закрыт (%s)", by)
        pid = self.reserve_peer_admin_id()
        if cw.get("told") and pid and not quiet:
            return [("notify", [Notification(tg_id=pid, text=rn.code_window_closed(primary, by, str(config.SERVER_NAME or "")))])]
        return []

    def reserve_code_link_watch(self) -> list[tuple]:
        """Запасной сигнал окна кода (решение Р4, В): линия второго сервера без кворума
        (цифры, блок зондов, состав не подтверждён, ключи не у всех — кандидата нет), а сам он
        не на связи по линку пары дольше T_up — окно кода с причиной; связь вернулась — закрыть."""
        if not self.reserve_paired() or not hasattr(self, "reserve_quorum_peer_line"):
            return []
        line = self.reserve_quorum_peer_line()
        cw = dict(self.rsv_get("rsv_view").get("code") or {})
        if line.get("active"):
            if cw.get("open") and cw.get("by") == "link":
                return self.reserve_code_window_set(False, by="link", quiet=True)   # дальше ведёт кандидат (Т-К21 — своим словом)
            return []
        down = self.reserve_peer_seen() == "down"
        if down and not cw.get("open"):
            if self.reserve_open_block(need_box=False):
                return []                                     # код ничего не откроет: готовность, зона, мои пользователи не здесь
            since = str(self.reserve_down_since() or "")
            up_s = int(self.reserve_quorum_timing().up_s) if hasattr(self, "reserve_quorum_timing") else 100
            try:
                if not since or timeutil.elapsed(timeutil.parse_iso(since)) < up_s:
                    return []
            except ValueError:
                return []
            why = [str(c) for c in (line.get("why") or [])]
            return self.reserve_code_window_set(True, since=since, by="link", off_why=rn_off_why(why))
        if not down and cw.get("open") and cw.get("by") == "link":
            return self.reserve_code_window_set(False, by="link")
        return []

    def reserve_code_window_quiet(self, open_: bool) -> list[tuple]:
        """Окно кода без уведомлений — ничья кворума: тревогу обоим админам шлёт
        quorum/tie (Т-К5в несёт кнопку кода), отбой — Т-К6."""
        view = self.rsv_get("rsv_view")
        cw = dict(view.get("code") or {})
        if open_ and self.reserve_in() == "idle":
            if not cw.get("open"):
                since = timeutil.now_iso()
                self.rsv_update("rsv_view", code={"open": True, "since": since, "told": False, "tries": [],
                                                  "episode": str(cw.get("episode") or since), "by": "tie"})
                log.warning("резерв: ничья — приём аварийного кода открыт")
        elif not open_ and cw.get("open"):
            self.rsv_update("rsv_view", code={"open": False, "closed_why": "tie_over", "closed_at": timeutil.now_iso(),
                                              "episode": str(cw.get("episode") or ""), "told": False})
            log.info("резерв: приём аварийного кода закрыт (ничьей нет)")
        return []

    def _code_block(self) -> str:
        cw = self.rsv_get("rsv_view").get("code") or {}
        if not (cw.get("open") or cw.get("pending")):       # pending — окно закрыто на время этого же ввода
            return "приём кода закрыт"
        return self.reserve_open_block()

    def reserve_code_in(self, uid: int, text: str) -> dict:
        """Код из чата роли админа основы — транзитом в процесс открытия (M и K
        собирает только он). {"ok": True, "pending": True} — процесс запущен,
        итог придёт уведомлением; иначе {"ok": False, "why": closed | wrong |
        limit | busy, "left": n}. «wrong» здесь — код не разобран (контрольная
        сумма); не подошедший к пакету код процесс отдаст через `reserve_code_failed`."""
        if not self.reserve_code_sender_ok(int(uid)):
            log.warning("резерв: аварийный код не от админа основы (%s) — отказ", uid)
            return {"ok": False, "why": "closed"}
        if not self.reserve_code_window()["open"]:
            return {"ok": False, "why": "closed"}
        cw = dict(self.rsv_get("rsv_view").get("code") or {})
        tries = self._code_tries(cw)
        if len(tries) >= CODE_TRIES:
            return {"ok": False, "why": "limit", "left": 0}
        norm = rsvkeys.normalize(text) or ""
        try:
            rsvkeys.code_decode(norm)
        except ValueError:
            return {"ok": False, "why": "garbled", "left": max(0, CODE_TRIES - len(tries))}   # не код — не попытка подбора
        pkg_in = self.rsv_get("rsv_pkg_in")
        # окно закрывается ДО запуска: иначе такт голосования в окне между запуском и закрытием
        # видел бы taking при открытом окне и закрыл бы его «шлюз видит основу»
        cw.update(open=False, closed_why="used", closed_at=timeutil.now_iso(), pending=True)
        self.rsv_update("rsv_view", code=cw, moved_by={"code": True, "at": timeutil.now_iso()})
        secret = self.reserve_quorum_code_secret(norm) if hasattr(self, "reserve_quorum_code_secret") else None
        if secret is None:
            cw.update(open=True, closed_why="", pending=False)
            self.rsv_update("rsv_view", code=cw)
            log.warning("резерв: аварийный код введён, а ящика кода поколения пакета %s нет", pkg_in.get("m_gen"))
            return {"ok": False, "why": "open_failed", "checked": True, "left": max(0, CODE_TRIES - len(tries)),
                    "reason": "данных для переезда нет: ящик кода не получен", "actions": []}
        actions = self.reserve_open_by_points(secret, "code", "H4")
        if self.reserve_in() != "taking":
            cw.update(open=True, closed_why="", pending=False)
            self.rsv_update("rsv_view", code=cw)
            return {"ok": False, "why": "busy", "actions": actions}
        # итог — по ходу процесса открытия: конверт ключа код снимает за секунды, и
        # «не подошёл» админ основы видит ответом на своё сообщение, а не уведомлением
        verdict = codewait.verdict()
        if verdict != "fail":
            return {"ok": True, "pending": verdict != "ok", "actions": actions}
        from awgbot.infra import rsvzone
        reason = str(rsvzone.open_status().get("reason") or "процесс открытия отказал")
        wrong = self._code_reason_wrong(reason)
        fail = self._take_fail(reason)                          # окно снова открыто, попытка в счёт (reserve_code_failed)
        pid = self.reserve_peer_admin_id()
        fail = [a for a in fail if not (a and a[0] == "notify" and all(n.tg_id == pid for n in a[1]))]   # ответ — здесь
        left = max(0, CODE_TRIES - len(self._code_tries(dict(self.rsv_get("rsv_view").get("code") or {}))))
        return {"ok": False, "why": "wrong" if wrong else "open_failed", "checked": True, "left": left,
                "reason": reason, "actions": actions + fail}

    @staticmethod
    def _code_reason_wrong(reason: str) -> bool:
        return any(w in reason for w in ("ключ пакета", "доли не сошлись", "аварийный код"))


    @staticmethod
    def _code_tries(cw: dict) -> list:
        now = time.time()
        return [t for t in cw.get("tries") or [] if now - float(t) < CODE_WINDOW_S]

    def _code_try_failed(self, cw: dict, why: str) -> dict:
        tries = self._code_tries(cw) + [time.time()]
        cw["tries"] = tries
        self.rsv_update("rsv_view", code=cw)
        log.warning("резерв: аварийный код %s (попытка %d из %d)", why, len(tries), CODE_TRIES)
        return {"ok": False, "why": "wrong", "left": max(0, CODE_TRIES - len(tries))}

    def reserve_code_failed(self, reason: str) -> list[tuple]:
        """Процесс открытия по коду отказал: не подошёл (ключ пакета не снялся) —
        попытка в счёт, окно снова открыто, админу основы Т-РН3; иная причина —
        окно открыто, админу основы одной строкой."""
        cw = dict(self.rsv_get("rsv_view").get("code") or {})
        if not cw.get("pending"):
            return []
        pid = self.reserve_peer_admin_id()
        wrong = self._code_reason_wrong(reason)
        cw.update(open=True, pending=False, closed_why="")
        if wrong:
            res = self._code_try_failed(cw, "не подошёл")
            cw = dict(self.rsv_get("rsv_view").get("code") or {})
            cw.update(open=True, pending=False)
            self.rsv_update("rsv_view", code=cw)
            text = self._rn().code_wrong(int(res.get("left") or 0))
        else:
            self.rsv_update("rsv_view", code=cw)
            text = self._rn().code_open_failed(self.reserve_peer_name(), reason)
        return [("notify", [Notification(tg_id=pid, text=text)])] if pid else []

    def _rn(self):
        from awgbot.domain.notices import reserve as rn
        return rn


def rn_off_why(codes: list) -> str:
    """Причина «голосование не включено» для уведомления — словами, по первому коду."""
    for c in codes or []:
        if c == "no_gateways":
            return "нет шлюзов с агентом v4.2.0"
        if c == "not_acked":
            return "состав голосующих не подтверждён"
        if c == "keys":
            return "ключи голосования ещё не у всех"
        if str(c).startswith("timing:"):
            return "цифры времени не сходятся"
        if str(c).startswith("overlap:"):
            return "блок зондов пересекается с сетью"
        if str(c).startswith("slot:"):
            return "номер слота шлюза больше допустимого"
    return "голосование не включено"
