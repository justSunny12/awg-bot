"""
codewin.py — часть пакета reserve (см. __init__): Н4 — окно аварийного кода
(выделено из vote: там матрица и голосование, здесь — приём кода).

Окно открыто только в Н4 (основа и её шлюзы не отвечают резерву): Т-РУ7
админу резерва, Т-РУ7б админу основы (если он открыл бота резерва). Код
из чата роли — транзитом в процесс открытия (M и K собирает только он);
итог — ответом на сообщение по ходу процесса (codewait), иначе уведомлением
(`reserve_code_failed`). Три попытки в час; нечитаемый ввод попыткой не
считается (`garbled`). Окно закрывается ДО запуска процесса открытия (иначе
такт голосования закрыл бы его как «шлюз видит основу»). Окно, открывшееся
снова в том же эпизоде: Т-РУ7 резерву не повторяется, Т-РУ7б админу основы —
ещё раз без звука (код ему снова нужен).
"""
from __future__ import annotations

import time

from awgbot.core import config
from awgbot.util import rsvkeys, timeutil

from ..types import Notification
from ..gateway_link.common import log
from . import codewait
from .vote import CODE_TRIES, CODE_WINDOW_S, MOVE_CASES


class ReserveCodeWindowMixin:
    # ── Н4: окно аварийного кода ────────────────────────────────────────────
    def reserve_code_window(self) -> dict:
        """{"open": bool, "since": iso} — приём кода открыт только в Н4."""
        cw = self.rsv_get("rsv_view").get("code") or {}
        return {"open": bool(cw.get("open")), "since": str(cw.get("since") or "")}

    def _code_window(self, case: str, *, quiet: bool = False) -> list[tuple]:
        if quiet:
            return self._code_window_quiet(case)
        view = self.rsv_get("rsv_view")
        cw = dict(view.get("code") or {})
        actions: list[tuple] = []
        primary = self.reserve_peer_name()
        if case == "H4" and self.reserve_in() == "idle":
            if not cw.get("open"):
                since = str(view.get("case_since") or timeutil.now_iso())
                if cw.get("episode") == since:
                    # тот же эпизод (окно закрывалось и открылось снова): Т-РУ7 резерву не повторяется,
                    # но админу основы код снова нужен — Т-РУ7б без звука (зона могла быть снята)
                    cw.update(open=True)
                    self.rsv_update("rsv_view", code=cw)
                    pid = self.reserve_peer_admin_id()
                    if cw.get("told") and pid:
                        actions.append(("notify", [Notification(tg_id=pid, silent=True, keyboard="peer_code",
                                                                text=self._rn().code_needed_primary(primary, str(config.SERVER_NAME or ""), since))]))
                    return actions
                opened = bool(self.rsv_get("rsv_peer_admin").get("opened"))
                pid = self.reserve_peer_admin_id()
                notes = [Notification(tg_id=int(config.ADMIN_ID), text=self._rn().code_needed_reserve(primary, since, opened))]
                if pid and opened:
                    notes.append(Notification(tg_id=pid, force_sound=True,
                                              text=self._rn().code_needed_primary(primary, str(config.SERVER_NAME or ""), since),
                                              keyboard="peer_code"))
                self.rsv_update("rsv_view", code={"open": True, "since": since, "told": bool(pid and opened), "tries": [],
                                                  "episode": since})
                log.warning("резерв: Н4 — основа и её шлюзы не отвечают; приём аварийного кода открыт")
                actions.append(("notify", notes))
        elif cw.get("open"):
            why = "primary_seen" if self.reserve_peer_seen() != "down" else ("gw_unseen" if case in MOVE_CASES else "gw_sees")
            self.rsv_update("rsv_view", code={"open": False, "closed_why": why, "closed_at": timeutil.now_iso(),
                                              "episode": str(cw.get("episode") or ""), "told": bool(cw.get("told"))})
            log.info("резерв: приём аварийного кода закрыт (%s)", why)
            pid = self.reserve_peer_admin_id()
            if cw.get("told") and pid:
                gw_name = next((self.reserve_gw_name(s) for s in self._voters()), primary)
                actions.append(("notify", [Notification(tg_id=pid, text=self._rn().code_window_closed(
                    primary if why == "primary_seen" else gw_name, why))]))
        return actions

    def _code_window_quiet(self, case: str) -> list[tuple]:
        """Окно кода без уведомлений Н4 — ничья кворума: тревогу обоим админам шлёт
        quorum/tie (Т-К5в несёт кнопку кода), отбой — Т-К6."""
        view = self.rsv_get("rsv_view")
        cw = dict(view.get("code") or {})
        if case == "H4" and self.reserve_in() == "idle":
            if not cw.get("open"):
                since = timeutil.now_iso()
                self.rsv_update("rsv_view", code={"open": True, "since": since, "told": False, "tries": [],
                                                  "episode": str(cw.get("episode") or since)})
                log.warning("резерв: ничья — приём аварийного кода открыт")
        elif cw.get("open"):
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
            secret = {"share": [rsvkeys.RESERVE_X, str(pkg_in.get("share") or "")], "code": norm}
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
