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
— применяется молча; другого устройства, после восстановления сервера
или поправленной руками новой установке — вопросом в чате бота шлюза.

Сбой применения ничего не теряет: своё серверу не уходит (единственная копия
блока — на сервере), вопрос остаётся, записанное в conf откатывается.
"""
from __future__ import annotations

import json
import random

from awgbot.core import settings
from awgbot.domain import gwkeep
from awgbot.util import timeutil

from .base import log

TRAFFIC_EVERY_S = 6 * 3600          # счёт линка — не чаще, ×uniform(0.6, 1.4)

# Следы своих копий шлюза прежних версий: фраза и случайный ключ, отметки
# месячной копии и почты «вслед за сервером», ключи расписания в conf. Своих
# копий у шлюза больше нет, возврата на прежние версии тоже — держать незачем.
LEFTOVER_STATE = ("backup_passphrase", "backup_key", "last_backup", "backup_nokey_noticed",
                  "gwcfg_mail_hint", "gwcfg_backup_follow")
LEFTOVER_CONF = ("app.scheduler.backup_enabled", "app.scheduler.backup_channel",
                 "app.scheduler.backup_day", "app.scheduler.backup_hour")
LEFTOVER_SECTION = "backup"          # раздел документа «фраза бэкапов» — в вопросах и памятках
# файлы копий прежних версий в каталоге бэкапов агента: внутри ключи линка, а
# применить их уже нельзя
LEFTOVER_FILES = ("*.tgz", "*.tgz.enc", "*.tar.gz", "*.tar.gz.enc", "*.tgz.dec", "restore-pending*")
# маркеры awg-bot restore прежних версий в каталоге данных агента
LEFTOVER_MARKERS = ("restore-*.json", ".restore-prune")
KINDS = ("restore", "device", "same")       # вопрос: после восстановления, новое устройство, та же машина заново


class KeepMixin:
    _KEEP_BASE_KEY = "gw_keep_base"            # отпечаток настроек при первой установке
    _KEEP_SYNCED_KEY = "gw_keep_synced"        # «1» — блок хоть раз ушёл или применён
    _KEEP_ACKED_KEY = "gw_keep_acked"          # отпечаток, который сервер подтвердил
    _KEEP_ASK_KEY = "gw_keep_ask"              # висящий вопрос: {why, from, at, other, hash, items}
    _KEEP_ANSWERED_KEY = "gw_keep_answered"    # отпечаток серверного блока, на который ответили
    _KEEP_TRAFFIC_KEY = "gw_keep_traffic"      # {"at": iso, "due": сек, "sum": rx+tx} — последний ушедший счёт
    _KEEP_TRAFFIC_GOT_KEY = "gw_keep_traffic_got"   # месяц, чей счёт с сервера уже перенесён
    _KEEP_FW_PENDING_KEY = "gw_keep_fw_pending"     # «1» — файервол с сервера записан, юнит обвязки не перезапущен
    _KEEP_MSG_KEY = "gw_keep_msg"              # [chat, message] — сообщение с вопросом: снятый вопрос правится в итог

    # ── следы своих копий прежних версий ─────────────────────────────────────

    def backup_leftovers_drop(self) -> bool:
        """На старте: стереть фразу, отметки, файлы и ключи расписания своих
        копий, снять вопрос и памятку о фразе с сервера. Повторный вызов — ничего.
        True — что-то стёрто."""
        hit = False
        for key in LEFTOVER_STATE:
            if self.db.get_state(key):
                self.db.set_state(key, "")
                hit = True
        for key in (self._CFG_KEEP_KEY, self._CFG_PENDING_KEY, self._CFG_WAIT_KEY):
            data = self.db.get_state_json(key, {}) or {}
            if isinstance(data, dict) and LEFTOVER_SECTION in data:
                del data[LEFTOVER_SECTION]
                self.db.set_state(key, json.dumps(data, ensure_ascii=False) if data else "")
                hit = True
        from awgbot.core import config
        try:
            olds = {p for pat in LEFTOVER_FILES for p in config.BACKUP_DIR.glob(pat) if p.is_file()}
        except OSError:
            olds = set()
        try:
            olds |= {p for pat in LEFTOVER_MARKERS for p in config.DATA_DIR.glob(pat) if p.is_file()}
        except OSError:
            pass
        for path in sorted(olds):
            try:
                path.unlink()
                hit = True
            except OSError as e:
                log.warning("gateway: файл копии прежних версий %s не удалён: %s", path.name, e)
        try:
            hit = bool(settings.drop_keys(LEFTOVER_CONF)) or hit
        except Exception as e:                            # noqa: BLE001 — conf сломан руками: ключи подождут
            log.warning("gateway: ключи расписания копий в conf не сняты: %s", e)
        if hit:
            log.info("gateway: следы своих копий прежних версий стёрты — настройки шлюза в бэкапе сервера AWG")
        return hit

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
        if last.get("month") and last["month"] != t["month"]:
            return True                                   # счёт нового месяца — как впервые (сумма с нуля)
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
             "sum": int(t.get("rx") or 0) + int(t.get("tx") or 0), "month": str(t.get("month") or "")}))

    def keep_acked(self, digest: str) -> None:
        """keep_ok: сервер подтвердил отпечаток — установка синхронизирована."""
        self.db.set_state(self._KEEP_ACKED_KEY, str(digest or "")[:16])
        self.db.set_state(self._KEEP_SYNCED_KEY, "1")

    def keep_ask_drop(self) -> bool:
        """keep_ok / keep_none: серверу предлагать нечего — висящий вопрос
        снимается (иначе «вернуть» применило бы снятое предложение). True —
        вопрос был."""
        if not self._keep_ask():
            return False
        self.db.set_state(self._KEEP_ASK_KEY, "")
        return True

    def keep_mark_msg(self, chat_id: int, message_id: int, text: str = "") -> None:
        """Вопрос показан этим сообщением (text — его текст): снятие вопроса
        свернёт его под кат с пометкой «не актуально»."""
        if self._keep_ask():
            self.db.set_state(self._KEEP_MSG_KEY, json.dumps([int(chat_id), int(message_id), str(text)],
                                                             ensure_ascii=False))

    def keep_msg_pop(self) -> list | None:
        """[chat, message, text] сообщения с вопросом; нет — None."""
        raw = self.db.get_state_json(self._KEEP_MSG_KEY, [])
        self.db.set_state(self._KEEP_MSG_KEY, "")
        if not isinstance(raw, list) or len(raw) < 2:
            return None
        return [raw[0], raw[1], str(raw[2]) if len(raw) > 2 else ""]

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
        применения, своя почта с памяткой. Сначала проверка значений
        файервола, потом запись; сбой посреди — записанное откатывается.
        {"ok", "error", "groups"}."""
        from awgbot.infra import gwguard
        items = gwkeep.clean(items)
        groups = gwkeep.differs(self.keep_collect(), items)
        conf_was: list[tuple[str, object]] = []
        fw_was: dict | None = None
        try:
            fw_now = gwguard.read_env()
            fw = {k: v for k, v in items["fw"].items() if fw_now.get(k, "") != v}
            for k, v in fw.items():
                gwguard._check_env_value(k, v)            # до первой записи: отказ — ничего не тронуто
            for k, v in items["conf"].items():
                if settings.get(k) != v:
                    conf_was.append((k, settings.get(k)))
                    settings.set_value(k, v)
            if fw:
                fw_was = {k: fw_now.get(k, "") for k in fw}
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
            self._keep_undo(conf_was, fw_was)
            return {"ok": False, "error": str(e), "groups": groups}
        if fw:
            self.db.set_state(self._KEEP_FW_PENDING_KEY, "1")
            self.keep_fw_finish(throttled=False)
        self.db.set_state(self._KEEP_SYNCED_KEY, "1")
        return {"ok": True, "error": "", "groups": groups}

    def _keep_undo(self, conf_was, fw_was) -> None:
        """Откат частично применённого блока: conf — к прежним значениям
        (ключа не было — снимается), firewall.env — к прежним."""
        from awgbot.infra import gwguard
        for k, v in reversed(conf_was):
            try:
                if v is None:
                    settings.drop_keys([k])
                else:
                    settings.set_value(k, v)
            except Exception as e:                        # noqa: BLE001
                log.warning("gateway: откат %s не удался: %s", k, e)
        if fw_was:
            try:
                gwguard.write_env(**fw_was)
            except (ValueError, OSError) as e:
                log.warning("gateway: откат firewall.env не удался: %s", e)

    def keep_fw_finish(self, *, throttled: bool = True) -> bool:
        """Файервол с сервера записан, а юнит обвязки не перезапущен (занят
        применением или запускается): перезапуск — сразу после записи, иначе
        тактом монитора (троттлинг общий). True — перезапущен."""
        if not self.db.get_state(self._KEEP_FW_PENDING_KEY) or not hasattr(self, "reassert_guarded"):
            return False
        why = "настройки файервола с сервера AWG"
        if throttled:
            ok = self._reassert_throttled(why) if hasattr(self, "_reassert_throttled") else False
        else:
            ok, err = self.reassert_guarded(why)
            if not ok:
                log.info("gateway: файервол с сервера ждёт перезапуска обвязки — добью тактом: %s", err)
        if ok:
            self.db.set_state(self._KEEP_FW_PENDING_KEY, "")
        return ok

    def keep_back_in(self, body: dict) -> dict:
        """keep_back с сервера. {"action": "send" — отправить своё (после
        применения или без него) | "ask" — вопрос в чате | "wait" — вопрос
        уже висит по тому же блоку или применение не удалось: своё не шлётся,
        "note": текст для чата или ""}."""
        why = str(body.get("why") or "")
        why = why if why in KINDS else "device"
        items = gwkeep.clean(body.get("items"))
        digest = str(body.get("hash") or "")[:16]
        if why in ("same", "device") and body.get("traffic") is not None:
            self.keep_traffic_apply(body.get("traffic"))      # расход места: переносится сразу
        if gwkeep.print_(items) == self.keep_print():
            self.db.set_state(self._KEEP_SYNCED_KEY, "1")
            self.keep_ask_drop()
            return {"action": "send", "note": ""}
        if why == "same" and self.keep_fresh():
            res = self.keep_apply(items)
            if not res["ok"]:
                # своё не уходит: единственная копия блока — на сервере; повтор — со следующей сессией
                from awgbot.domain.notices.gateway import keep_failed
                return {"action": "wait", "note": keep_failed(res["error"])}
            from awgbot.domain.notices.gateway import keep_received
            return {"action": "send", "note": keep_received(res["groups"], str(body.get("at") or ""))}
        if self.db.get_state(self._KEEP_ANSWERED_KEY) == digest:
            return {"action": "send", "note": ""}          # на этот блок уже ответили — своё серверу
        held = self._keep_ask()
        ask = {"why": why, "from": str(body.get("from") or "")[:40], "at": str(body.get("at") or "")[:40],
               "other": bool(body.get("other")), "hash": digest, "items": items}
        self.db.set_state(self._KEEP_ASK_KEY, json.dumps(ask, ensure_ascii=False))
        if held and held.get("hash") == digest and held.get("why") == why:
            return {"action": "wait", "note": ""}          # тот же вопрос уже в чате — молча обновлён
        return {"action": "ask", "note": ""}

    # ── вопрос и ответ ───────────────────────────────────────────────────────

    def _keep_ask(self) -> dict:
        data = self.db.get_state_json(self._KEEP_ASK_KEY, {})
        return data if isinstance(data, dict) and data.get("why") else {}

    def keep_section(self) -> str:
        """Раздел очереди вопросов: keep — из бэкапа, keep_dev — прежнего
        устройства, keep_same — этого же шлюза до новой установки; вопроса
        нет — пусто."""
        ask = self._keep_ask()
        if not ask:
            return ""
        return {"restore": "keep", "same": "keep_same"}.get(ask["why"], "keep_dev")

    def keep_question(self) -> dict | None:
        ask = self._keep_ask()
        if not ask:
            return None
        return {"section": self.keep_section(), "why": ask["why"], "other": bool(ask.get("other")),
                "from": ask.get("from", ""), "at": ask.get("at", ""),
                "groups": gwkeep.differs(self.keep_collect(), ask.get("items"))}

    def keep_answer(self, accept: bool) -> dict:
        """«Вернуть / перенести» — применить блок; «оставить» — своё остаётся,
        сервер сохранит его. Не применилось — вопрос остаётся, ответа нет:
        своё серверу не уходит. {"ok", "accepted", "why", "from", "at", "error"}."""
        ask = self._keep_ask()
        if not ask:
            return {"ok": False, "accepted": accept, "why": "", "from": "", "at": "", "error": ""}
        out = {"ok": True, "accepted": accept, "why": ask["why"], "from": ask.get("from", ""),
               "at": ask.get("at", ""), "error": ""}
        if accept:
            res = self.keep_apply(ask.get("items"))
            if not res["ok"]:
                out["error"] = res["error"]
                return out
        self.db.set_state(self._KEEP_ANSWERED_KEY, str(ask.get("hash") or ""))
        self.db.set_state(self._KEEP_SYNCED_KEY, "1")
        self.db.set_state(self._KEEP_ASK_KEY, "")
        self.db.set_state(self._KEEP_MSG_KEY, "")      # ответ правит это сообщение сам
        return out
