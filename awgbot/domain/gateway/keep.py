"""keep.py — копия настроек шлюза для бэкапа сервера AWG, сторона агента:
сбор (domain/gwkeep), чистота установки, применение блока с сервера, вопрос
человеку и ответ.

Агент — источник правды: его блок уезжает серверу сообщением `keep`, когда
настройки разошлись с подтверждёнными сервером (тик монитора ловит и правки
мимо чата — `awg-bot ssh`, `awg-bot firewall`), и раз в 6 часов с джиттером,
когда вырос месячный счёт линка. Своё агент не шлёт, пока сервер в этой
сессии не ответил на hello (keep_back / keep_none / keep_ok): иначе
умолчания новой установки перетёрли бы блок слота.

Чистая установка — агент поставлен файлом первого применения (отметка
первой установки), блок ни разу не уходил и не применялся, и человек не
правил ни одного ключа из списка. Сервер возвращает блок того же устройства
— применяется молча (Т18); другого устройства, после восстановления сервера
или поправленной руками новой установке — вопросом в чате бота шлюза.
"""
from __future__ import annotations

import json
import random

from awgbot.core import settings
from awgbot.domain import gwkeep
from awgbot.util import timeutil

from .base import log

TRAFFIC_EVERY_S = 6 * 3600          # счёт линка — не чаще, ×uniform(0.6, 1.4)


class KeepMixin:
    _KEEP_BASE_KEY = "gw_keep_base"            # отпечаток настроек при первой установке
    _KEEP_SYNCED_KEY = "gw_keep_synced"        # «1» — блок хоть раз ушёл или применён
    _KEEP_ACKED_KEY = "gw_keep_acked"          # отпечаток, который сервер подтвердил
    _KEEP_ASK_KEY = "gw_keep_ask"              # висящий вопрос: {why, from, at, other, hash, items}
    _KEEP_ANSWERED_KEY = "gw_keep_answered"    # отпечаток серверного блока, на который ответили
    _KEEP_TRAFFIC_KEY = "gw_keep_traffic"      # {"at": iso, "due": сек, "sum": rx+tx} — последний ушедший счёт
    _KEEP_TRAFFIC_GOT_KEY = "gw_keep_traffic_got"   # месяц, чей счёт с сервера уже перенесён

    # ── сбор ─────────────────────────────────────────────────────────────────

    def keep_collect(self) -> dict:
        """Что знает только шлюз — закрытым списком."""
        from awgbot.infra import gwguard
        conf = {}
        for k in gwkeep.CONF_KEYS:
            v = settings.get(k)
            if v is not None:
                conf[k] = v
        env = gwguard.read_env()
        fw = {k: env[k] for k in gwkeep.FW_KEYS if k in env}
        mail = None
        memo = (self.cfg_keep() or {}).get("mail") if hasattr(self, "cfg_keep") else ""
        acc = self.email_account() if memo else None
        if acc is not None:
            mail = {"login": acc.login, "password": acc.password, "imap_host": acc.imap_host,
                    "imap_port": int(acc.imap_port), "smtp_host": acc.smtp_host, "smtp_port": int(acc.smtp_port),
                    "keep": str(memo)}
        return gwkeep.clean({"conf": conf, "fw": fw, "mail": mail})

    def keep_print(self) -> str:
        return gwkeep.print_(self.keep_collect())

    def keep_base_note(self) -> None:
        """Первая установка: запомнить настройки по умолчанию — по ним видно,
        правил ли человек что-то до первой сессии."""
        if not self.db.get_state(self._KEEP_BASE_KEY):
            self.db.set_state(self._KEEP_BASE_KEY, self.keep_print())

    def keep_new(self) -> bool:
        """Новая установка, ни разу не синхронизированная с сервером."""
        return bool(self.db.get_state("agent_first_start_at")) and not self.db.get_state(self._KEEP_SYNCED_KEY)

    def keep_fresh(self) -> bool:
        """Новая установка, в которой человек ничего из списка не правил."""
        return self.keep_new() and self.db.get_state(self._KEEP_BASE_KEY) == self.keep_print()

    def keep_hello(self) -> dict:
        return {"keep_hash": self.keep_print(), "keep_new": self.keep_new(), "keep_fresh": self.keep_fresh()}

    # ── отправка ─────────────────────────────────────────────────────────────

    def _traffic_now(self) -> dict | None:
        acc = self.db.get_state_json("gw_traffic", {})
        return gwkeep.traffic_clean(acc) if isinstance(acc, dict) else None

    def keep_block(self) -> dict:
        return gwkeep.block(self.keep_collect(), at=timeutil.to_iso(timeutil.now()), traffic=self._traffic_now())

    def keep_due(self) -> bool:
        """Блок пора отправить: настройки разошлись с подтверждёнными, или
        счёт линка вырос и прошло ~6 часов."""
        if self.keep_print() != (self.db.get_state(self._KEEP_ACKED_KEY) or ""):
            return True
        t = self._traffic_now()
        if t is None:
            return False
        last = self.db.get_state_json(self._KEEP_TRAFFIC_KEY, {})
        if not isinstance(last, dict) or not last.get("at"):
            return True
        if t["rx"] + t["tx"] <= int(last.get("sum") or 0):
            return False
        try:
            age = timeutil.elapsed(timeutil.parse_iso(str(last["at"])))
        except ValueError:
            return True
        return age >= float(last.get("due") or TRAFFIC_EVERY_S)

    def keep_sent(self, block: dict) -> None:
        t = block.get("traffic") or {}
        self.db.set_state(self._KEEP_SYNCED_KEY, "1")
        self.db.set_state(self._KEEP_TRAFFIC_KEY, json.dumps(
            {"at": timeutil.to_iso(timeutil.now()), "due": int(TRAFFIC_EVERY_S * random.uniform(0.6, 1.4)),
             "sum": int(t.get("rx") or 0) + int(t.get("tx") or 0)}))

    def keep_acked(self, digest: str) -> None:
        self.db.set_state(self._KEEP_ACKED_KEY, str(digest or "")[:16])

    # ── блок с сервера ───────────────────────────────────────────────────────

    def keep_traffic_apply(self, traffic) -> bool:
        """Счёт линка того же месяца продолжает цифры блока — один раз."""
        t = gwkeep.traffic_clean(traffic)
        month = timeutil.now().strftime("%Y-%m")
        if t is None or t["month"] != month or self.db.get_state(self._KEEP_TRAFFIC_GOT_KEY) == month:
            return False
        acc = self.db.get_state_json("gw_traffic", {})
        self.db.set_state("gw_traffic", json.dumps(gwkeep.traffic_merge(acc if isinstance(acc, dict) else {},
                                                                        t, month)))
        self.db.set_state(self._KEEP_TRAFFIC_GOT_KEY, month)
        return True

    def keep_apply(self, items) -> dict:
        """Применить блок: conf, firewall.env без SSH_PORT и реассерт под замком
        применения, своя почта с памяткой. {"ok", "error", "groups"}."""
        from awgbot.infra import gwguard
        items = gwkeep.clean(items)
        groups = gwkeep.differs(self.keep_collect(), items)
        try:
            for k, v in items["conf"].items():
                if settings.get(k) != v:
                    settings.set_value(k, v)
            fw_now = gwguard.read_env()
            fw = {k: v for k, v in items["fw"].items() if fw_now.get(k, "") != v}
            if fw:
                gwguard.write_env(**fw)
            mail = items.get("mail")
            if mail:
                self.email_save(mail["login"], mail["password"], mail["imap_host"], int(mail.get("imap_port") or 993),
                                mail["smtp_host"], int(mail.get("smtp_port") or 587))
                keep = self.cfg_keep()
                keep["mail"] = str(mail.get("keep") or "")
                self._cfg_json_set(self._CFG_KEEP_KEY, keep)
        except (ValueError, OSError, settings.SettingsWriteError) as e:
            log.warning("gateway: копия настроек с сервера не применена: %s", e)
            return {"ok": False, "error": str(e), "groups": groups}
        if fw and hasattr(self, "reassert_guarded"):
            ok, err = self.reassert_guarded("настройки с сервера AWG")
            if not ok:
                log.info("gateway: обвязка после настроек с сервера подхватит файервол тактом: %s", err)
        self.db.set_state(self._KEEP_SYNCED_KEY, "1")
        return {"ok": True, "error": "", "groups": groups}

    def keep_back_in(self, body: dict) -> dict:
        """keep_back с сервера. {"action": "send" — отправить своё (после
        применения или без него) | "ask" — вопрос в чате, "note": текст для
        чата или ""}."""
        why = str(body.get("why") or "")
        items = gwkeep.clean(body.get("items"))
        digest = str(body.get("hash") or "")[:16]
        if why in ("same", "device") and body.get("traffic") is not None:
            self.keep_traffic_apply(body.get("traffic"))      # расход места: переносится сразу
        if gwkeep.print_(items) == self.keep_print():
            self.db.set_state(self._KEEP_SYNCED_KEY, "1")
            return {"action": "send", "note": ""}
        if why == "same" and self.keep_fresh():
            res = self.keep_apply(items)
            if not res["ok"]:
                return {"action": "send", "note": ""}
            from awgbot.domain.notices.gateway import keep_received
            return {"action": "send", "note": keep_received(res["groups"], str(body.get("at") or ""))}
        if why == "restore" and self.db.get_state(self._KEEP_ANSWERED_KEY) == digest:
            return {"action": "send", "note": ""}          # на этот блок уже ответили — своё серверу
        self.db.set_state(self._KEEP_ASK_KEY, json.dumps(
            {"why": why if why in ("restore", "device", "same") else "device",
             "from": str(body.get("from") or "")[:40], "at": str(body.get("at") or "")[:40],
             "other": bool(body.get("other")), "hash": digest, "items": items}, ensure_ascii=False))
        return {"action": "ask", "note": ""}

    # ── вопрос и ответ ───────────────────────────────────────────────────────

    def _keep_ask(self) -> dict:
        data = self.db.get_state_json(self._KEEP_ASK_KEY, {})
        return data if isinstance(data, dict) and data.get("why") else {}

    def keep_section(self) -> str:
        """Раздел очереди вопросов: keep — из бэкапа, keep_dev — прежнего
        устройства; вопроса нет — пусто."""
        ask = self._keep_ask()
        if not ask:
            return ""
        return "keep" if ask["why"] == "restore" else "keep_dev"

    def keep_question(self) -> dict | None:
        ask = self._keep_ask()
        if not ask:
            return None
        return {"section": self.keep_section(), "why": ask["why"], "other": bool(ask.get("other")),
                "from": ask.get("from", ""), "at": ask.get("at", ""),
                "groups": gwkeep.differs(self.keep_collect(), ask.get("items"))}

    def keep_answer(self, accept: bool) -> dict:
        """«Вернуть / перенести» — применить блок; «оставить» — своё остаётся,
        сервер сохранит его. {"ok", "accepted", "why", "from", "error"}."""
        ask = self._keep_ask()
        if not ask:
            return {"ok": False, "accepted": accept, "why": "", "from": "", "error": ""}
        out = {"ok": True, "accepted": accept, "why": ask["why"], "from": ask.get("from", ""), "error": ""}
        if accept:
            res = self.keep_apply(ask.get("items"))
            if not res["ok"]:
                out["error"] = res["error"]
        self.db.set_state(self._KEEP_ANSWERED_KEY, str(ask.get("hash") or ""))
        self.db.set_state(self._KEEP_SYNCED_KEY, "1")
        self.db.set_state(self._KEEP_ASK_KEY, "")
        return out
