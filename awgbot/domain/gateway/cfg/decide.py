"""decide.py — разделы с сервера, которые стоят и на шлюзе (почта, аплинк): молча или вопрос, памятка «оставить своё»."""
from __future__ import annotations

import json
import threading

from awgbot.domain import gwcfg

# порядок очереди: аплинк (дорога агента в Telegram) — первым, затем почта и
# копия настроек шлюза с сервера (keep: живёт в своём состоянии, domain/gateway/keep);
# ключи линка стоят ещё раньше, но живут в своём состоянии перехода (rekey)
QUESTION_SECTIONS = ("uplink", "mail", "keep")


class CfgDecideMixin:
    """Правило: стоящее — заглушка (ящик не задан, аплинка нет) или
    совпадает с серверным — применяется молча; стоящее своё и отличается —
    вопрос человеку; «оставить своё» запоминается по отпечатку серверного
    значения: тот же отпечаток не спрашивается снова, новое — новый вопрос.
    Признака «своё / с сервера» нет, и «сервер всегда прав» — тоже."""
    _CFG_KEEP_KEY = "gwcfg_keep"          # {раздел: отпечаток серверного значения, от которого отказались}
    _CFG_PENDING_KEY = "gwcfg_pending"    # {раздел: серверное значение}, пока вопрос висит
    _CFG_WAIT_KEY = "gwcfg_wait"          # {раздел: {"mine": отпечаток стоящего, "server": отпечаток, "gen", "hash"}}
    # вопрос вешает поток канала (применение документа), снимает — поток экрана
    # (ответ человека): оба читают и переписывают pending/wait целиком — под
    # одним замком, иначе одна запись затирает другую
    _cfg_q_lock = threading.RLock()

    def _cfg_json(self, key: str) -> dict:
        return self.db.get_state_json(key, {}) or {}

    def _cfg_json_set(self, key: str, value: dict) -> None:
        self.db.set_state(key, json.dumps(value, ensure_ascii=False) if value else "")

    def cfg_keep(self) -> dict:
        return self._cfg_json(self._CFG_KEEP_KEY)

    def cfg_wait(self) -> dict:
        return self._cfg_json(self._CFG_WAIT_KEY)

    def cfg_pending(self) -> dict:
        data = self._cfg_json(self._CFG_PENDING_KEY)
        if isinstance(data.get("uplink"), dict):
            data["uplink"] = self._cfg_uplink_pending_fill(data["uplink"])   # конфиг аплинка — из файла
        return data

    def _cfg_section_prints(self, section: str, value: dict) -> tuple[str, str]:
        """(отпечаток стоящего, отпечаток серверного) для раздела."""
        if section == "mail":
            return self.cfg_mail_print(), gwcfg.mail_print(value)
        if section == "uplink":
            return self.cfg_uplink_print(), gwcfg.uplink_print(value.get("conf", ""), value.get("pubkey", ""),
                                                               value.get("prev_pubkey", ""))
        return "", ""                                     # раздела в документе больше нет

    def cfg_decide(self, section: str, value: dict) -> tuple[str, str]:
        """Что делать с разделом: ("apply" — молча, "same" — ничего, "ask" —
        вопрос, "declined" — отказались от этого же серверного значения),
        и отпечаток серверного значения."""
        mine, server = self._cfg_section_prints(section, value)
        if not mine:
            return "apply", server                       # заглушка: применяем молча
        if mine == server:
            return "same", server
        if self.cfg_keep().get(section) == server:
            return "declined", server
        return "ask", server

    def cfg_hold(self, section: str, value: dict, server_print: str, gen: int, digest: str) -> None:
        """Вопрос повис: серверное значение — в pending, отпечатки — в wait; новое
        серверное значение заменяет прежний вопрос."""
        mine, _ = self._cfg_section_prints(section, value)
        with self._cfg_q_lock:
            pending = self._cfg_json(self._CFG_PENDING_KEY)
            pending[section] = self._cfg_uplink_pending_put(value) if section == "uplink" else value
            wait = self.cfg_wait(); wait[section] = {"mine": mine, "server": server_print, "gen": int(gen), "hash": digest}
            with self.db.transaction():                   # значение и отпечатки — одной записью
                self._cfg_json_set(self._CFG_PENDING_KEY, pending)
                self._cfg_json_set(self._CFG_WAIT_KEY, wait)

    def cfg_release(self, section: str) -> None:
        """Снять вопрос: pending и wait раздела стираются."""
        with self._cfg_q_lock:
            for key in (self._CFG_PENDING_KEY, self._CFG_WAIT_KEY):
                data = self._cfg_json(key)
                if section in data:
                    del data[section]
                    self._cfg_json_set(key, data)
            if section == "uplink":
                self._cfg_uplink_pending_drop()

    def cfg_question_stale(self, section: str) -> bool:
        """Стоящее поменяли руками, пока вопрос висел: отпечаток стоящего не тот,
        что был при вопросе — вопрос снимается без памятки."""
        wait = self.cfg_wait().get(section)
        if not wait:
            return False
        mine, _ = self._cfg_section_prints(section, self.cfg_pending().get(section) or {})
        return mine != wait.get("mine", "")

    def cfg_drop_stale_questions(self) -> list[str]:
        """Снять вопросы, потерявшие смысл; вернуть снятые разделы."""
        dropped = []
        for section in list(self.cfg_wait()):
            if self.cfg_question_stale(section):
                self.cfg_release(section)
                dropped.append(section)
        return dropped

    # ── применение ───────────────────────────────────────────────────────────

    def cfg_apply_mail(self, mail: dict) -> dict:
        """Сохранить почту с сервера и проверить вход IMAP и SMTP. Проверка не
        прошла — почта всё равно сохранена, итог — в note."""
        self.email_save(mail["login"], mail["password"], mail["imap_host"], int(mail["imap_port"]),
                        mail["smtp_host"], int(mail["smtp_port"]))
        try:
            ok, why = self.email_check()
        except Exception as e:                           # noqa: BLE001 — таймаут IMAP, сеть
            ok, why = False, str(e)
        return {"state": "ok" if ok else "fail", "why": "" if ok else why}

    def cfg_answer(self, section: str, accept: bool) -> dict:
        """Ответ человека на вопрос: принять с сервера — применить отложенное
        значение (памятка стирается); оставить своё — памятка с серверным
        отпечатком. Возвращает {"ok", "section", "accepted", "note", "gen", "hash",
        "print"} — для ответа серверу и итога в чате; вопроса нет — ok False."""
        with self._cfg_q_lock:
            return self._cfg_answer(section, accept)

    def _cfg_answer(self, section: str, accept: bool) -> dict:
        wait = self.cfg_wait().get(section)
        value = self.cfg_pending().get(section)
        if not wait or value is None:
            return {"ok": False, "section": section, "accepted": accept, "note": {}, "gen": 0, "hash": "", "print": ""}
        keep = self.cfg_keep()
        note: dict = {}
        if accept and section == "uplink":
            res = self.cfg_uplink_apply(value)
            if res.get("state") == "busy":
                # обвязка занята прогоном юнита — ничего не тронуто, вопрос остаётся
                return {"ok": True, "section": section, "accepted": True, "busy": True,
                        "note": {"state": "busy", "why": res["error"]}, "gen": 0, "hash": "", "print": ""}
            self.cfg_release(section)                     # прежний на месте или новый стоит — вопрос отслужил
            if not res["ok"]:
                return {"ok": True, "section": section, "accepted": True,
                        "note": {"state": "fail", "why": res["error"], "how": res.get("state", ""),
                                 "back_why": res.get("back_why", "")},
                        "failed": res["error"], "gen": int(wait.get("gen") or 0), "hash": str(wait.get("hash") or ""),
                        "print": wait.get("server", "")}
            keep.pop(section, None)
            self._cfg_json_set(self._CFG_KEEP_KEY, keep)
            return {"ok": True, "section": section, "accepted": True, "note": {"state": "ok"},
                    "gen": int(wait.get("gen") or 0), "hash": str(wait.get("hash") or ""), "print": wait.get("server", "")}
        if accept:
            try:
                note = self.cfg_apply_mail(value)
            except (ValueError, OSError) as e:
                # не применилось (диск, значение): вопрос остаётся, серверу — отказ с причиной
                return {"ok": True, "section": section, "accepted": True, "note": {"state": "fail", "why": str(e)},
                        "failed": str(e), "gen": int(wait.get("gen") or 0), "hash": str(wait.get("hash") or ""),
                        "print": wait.get("server", "")}
            keep.pop(section, None)
        else:
            keep[section] = wait.get("server", "")
        self._cfg_json_set(self._CFG_KEEP_KEY, keep)
        self.cfg_release(section)
        return {"ok": True, "section": section, "accepted": accept, "note": note,
                "gen": int(wait.get("gen") or 0), "hash": str(wait.get("hash") or ""), "print": wait.get("server", "")}
