"""ask.py — очередь вопросов человеку: что висит, что открыть первым, что снято ручной правкой."""
from __future__ import annotations

from .decide import QUESTION_SECTIONS


class CfgAskMixin:
    """Одно живое меню — один вопрос: остальные ждут в очереди (аплинк, почта,
    копия настроек шлюза — в этом порядке); решил одно — открывается
    следующее. Срока у вопроса нет."""

    def cfg_questions(self) -> list[str]:
        """Разделы с висящими вопросами по порядку очереди; вопросы, потерявшие
        смысл после ручной правки, по дороге снимаются."""
        self.cfg_drop_stale_questions()
        wait = self.cfg_wait()
        keep = self.keep_section() if hasattr(self, "keep_section") else ""
        return self.rekey_pending_section() + [keep if s == "keep" else s for s in QUESTION_SECTIONS
                                               if s in wait or (s == "keep" and keep)]

    def cfg_question(self, section: str) -> dict | None:
        """Что показать: раздел и чем отличается (без значений секретов)."""
        if section not in self.cfg_questions():
            return None
        if section == "link":
            return self.rekey_question()
        if section in ("keep", "keep_dev"):
            return self.keep_question()
        value = self.cfg_pending().get(section) or {}
        if section == "mail":
            return {"section": "mail", "differs": self._cfg_mail_differs(value)}
        from awgbot.util import wgconf
        try:
            device = wgconf.pubkey_of(wgconf.parse(value.get("conf", "")).get("Interface", "PrivateKey"))
        except wgconf.WgConfError:
            device = ""
        return {"section": "uplink", "fp": self.cfg_uplink_fp(value), "device": device[:4]}

    def _cfg_mail_differs(self, value: dict) -> list[str]:
        """«ящик» (адрес или пароль) и/или «серверы IMAP/SMTP» — что именно
        отличается; значений в ответе нет."""
        acc = self.email_account()
        out = []
        if acc is None:
            return ["ящик"]
        if (acc.login, acc.password) != (value.get("login"), value.get("password")):
            out.append("ящик")
        if (acc.imap_host, int(acc.imap_port), acc.smtp_host, int(acc.smtp_port)) != (
                value.get("imap_host"), int(value.get("imap_port") or 0), value.get("smtp_host"),
                int(value.get("smtp_port") or 0)):
            out.append("серверы IMAP/SMTP")
        return out or ["ящик"]
