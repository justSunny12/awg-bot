"""
backups.py — бэкап сервера AWG в сервисном слое: снятие копии, копия по
событию, ожидание конца смены ключей линка, подпись копии с описью шлюзов,
строки раздела «💾 Бэкапы» по слотам.

Без ключевой фразы копий нет вовсе — ни месячной, ни по кнопке, ни по
событию. Копия по событию: смена ключей линка, назначение, замена и снятие
слота, смена токена бота шлюза, порта канала или адреса сервера ставят
отметку backup_event_due; задача планировщика снимает копию, когда с
последнего события прошло ≥ 10 минут и смены ключей в работе нет, и снимает
отметку только по доставке. Автобэкапы выключены — событий нет.
"""
from __future__ import annotations

import json
import logging

from awgbot.core import settings
from awgbot.util import timeutil

log = logging.getLogger("awgbot.services")

EVENT_KEY = "backup_event_due"
EVENT_FILE_KEY = "backup_event_file"   # {"at", "path"} — собранная, но не доставленная копия по событию
EVENT_SETTLE_S = 600              # с последнего события до копии


class _JoinSlot:
    """Новый слот в сопряжении: строки в gateways ещё нет, а линк на ключах
    токена уже стоит — для «копия ждёт» ведёт себя как шлюз без устройства."""

    def __init__(self, slot_id: int, name: str):
        self.id, self.name, self.device_id, self.label, self.link_if = slot_id, name, None, "", ""
        self.joining = True


class BackupsMixin:
    # ── доставка копии на почту ──────────────────────────────────────────────

    def backup_channel(self) -> str:
        """telegram | email; email — только при настроенной почте, иначе Telegram."""
        ch = str(settings.get("app.scheduler.backup_channel", "telegram") or "telegram").lower()
        return "email" if ch == "email" and self.email_account() is not None else "telegram"

    def email_send_backup(self, paths, caption: str = "") -> None:
        """Файлы бэкапа — вложениями на сам ящик. Только шифрованные: в БД
        приватные ключи. Тема — заголовок подписи копии, тело — как
        восстановить и опись шлюзов."""
        from awgbot.infra import mail
        acc = self.email_account()
        if acc is None:
            raise mail.MailError("ящик не настроен")
        if not self.backup_encryption_enabled():
            raise mail.MailError("бэкап без шифрования по почте не отправляется — "
                                 "задай ключевую фразу: 💾 Бэкапы → 🔐 Шифрование")
        import os
        att = []
        for p in paths:
            with open(p, "rb") as f:
                att.append((os.path.basename(p), f.read()))
        stamp = timeutil.fmt_dt_ui(timeutil.now(), year=True)   # «17.10.26 08:08 (UTC+5)»: письмо лежит годами
        from awgbot.domain.notices import backup as nb
        text = nb.plain(caption) if caption else f"💾 Бэкап сервера AWG от {stamp}"
        head, _, rest = text.partition("\n")
        mail.send_mail(acc, acc.login, head, nb.MAIL_BODY + (f"\n\n{rest}" if rest else ""),
                       attachments=att)
        self._email_mark_ok()

    def make_backup(self) -> list[str]:
        """Одна резервная копия — один шифрованный архив: БД (вместе со
        слотовыми ключами), conf/*.yaml, env, конфиги awg-интерфейсов, линки
        слотов и выданные шлюзовые стороны, метка схемы 2 с описью шлюзов.
        Без фразы — BackupKeyMissing. Разворачивается `awg-bot restore`."""
        return self.write_backup_archive("main", self.backup_extra())

    # ── смена ключей линка в работе: копия ждёт её конца ─────────────────────

    def backup_rekey_busy(self):
        """Слот, у которого идёт смена ключей линка (на диске .next/.prev,
        шлюз может быть уже на новых) или сопряжение (линк на ключах токена,
        прежние — в .join-prev, которого в копии нет), или None: копия ждёт
        факта — фиксации или возврата."""
        for gw in self.db.gateways():
            if self.gateway_rekey_active(gw.id) or (hasattr(self, "gwjoin_slot_busy") and self.gwjoin_slot_busy(gw.id)):
                return gw
        if hasattr(self, "gwjoin_all"):
            for st in self.gwjoin_all():                  # новый слот: строки ещё нет, линк со штампом уже стоит
                if st.get("state") in ("waiting", "connected", "accepted", "closing"):
                    return _JoinSlot(int(st.get("slot") or 0), str(st.get("name") or "") or f"Шлюз {st.get('slot')}")
        return None

    # ── копия по событию ─────────────────────────────────────────────────────

    def backup_event_mark(self, kind: str, slot: int | None = None) -> None:
        """Событие, после которого нужна свежая копия. Без фразы и при
        выключенных автобэкапах — ничего."""
        if not settings.get_bool("app.scheduler.backup_enabled", True) or not self.backup_encryption_enabled():
            return
        why = {"kind": str(kind)}
        if slot:
            gw = self.db.gateway(slot)
            if gw is not None:
                why.update(name=self._gw_name(gw), label=gw.label or "", slot=int(slot))
        due = self.db.get_state_json(EVENT_KEY, {})
        items = [w for w in (due.get("why") or []) if isinstance(w, dict)] if isinstance(due, dict) else []
        if why not in items:
            items.append(why)
        self.db.set_state(EVENT_KEY, json.dumps({"at": timeutil.to_iso(timeutil.now()), "why": items[-16:]},
                                                ensure_ascii=False))

    def backup_event_take(self) -> dict | None:
        """Отметка, по которой пора снимать копию; рано, нет или смена ключей
        идёт — None. Автобэкапы выключили после события — отметка снимается."""
        due = self.db.get_state_json(EVENT_KEY, {})
        if not isinstance(due, dict) or not due.get("at"):
            return None
        if not settings.get_bool("app.scheduler.backup_enabled", True):
            self.db.set_state(EVENT_KEY, "")
            return None
        try:
            age = timeutil.elapsed(timeutil.parse_iso(str(due["at"])))
        except ValueError:
            age = EVENT_SETTLE_S
        if age < EVENT_SETTLE_S or self.backup_rekey_busy() is not None:
            return None
        return due

    def backup_event_done(self, at: str) -> None:
        """Копия по событию доставлена: отметка снимается, если за это время
        новых событий не было (иначе следующая копия — по ним)."""
        due = self.db.get_state_json(EVENT_KEY, {})
        if isinstance(due, dict) and str(due.get("at") or "") == str(at):
            self.db.set_state(EVENT_KEY, "")
        self.db.set_state(EVENT_FILE_KEY, "")

    def backup_event_paths(self, due: dict) -> list[str]:
        """Файл копии по этой отметке: собранный, но не доставленный в прошлый
        такт, — тот же (не собирать каждые 5 минут новый); иначе — новый."""
        import os
        kept = self.db.get_state_json(EVENT_FILE_KEY, {})
        if isinstance(kept, dict) and kept.get("at") == due.get("at") and os.path.exists(str(kept.get("path"))):
            return [str(kept["path"])]
        paths = self.make_backup()
        self.db.set_state(EVENT_FILE_KEY, json.dumps({"at": due.get("at"), "path": paths[0]}))
        return paths

    def backup_event_covered(self, made_at: str) -> None:
        """Доставлена копия, снятая не раньше последнего события (кнопка,
        месячная): отметка события закрыта — второй копии не будет."""
        due = self.db.get_state_json(EVENT_KEY, {})
        if not isinstance(due, dict) or not due.get("at"):
            return
        try:
            if timeutil.elapsed(timeutil.parse_iso(str(due["at"])), timeutil.parse_iso(made_at)) >= 0:
                self.db.set_state(EVENT_KEY, "")
                self.db.set_state(EVENT_FILE_KEY, "")
        except ValueError:
            return

    # ── подпись копии и раздел ───────────────────────────────────────────────

    def _keep_at(self, slot: int) -> str:
        keep = self.db.get_state_json(f"gwkeep_{int(slot)}", {})
        return str(keep.get("at") or "") if isinstance(keep, dict) else ""

    def backup_caption(self, due: dict | None = None) -> str:
        """Подпись копии в чате и тело письма: дата, шлюзы, отклонения;
        копия по событию — с причиной в заголовке."""
        from awgbot.domain.notices import backup as nb
        bot = getattr(self, "bot_username", "") or ""
        refs, offline = [], []
        for gw in self.db.gateways():
            ref = nb.gw_ref(self._gw_name(gw), gw.label or "", slot=gw.id, bot_username=bot)
            refs.append(ref)
            seen, keep_at = self.db.get_state(f"gwlink_seen_{gw.id}") or "", self._keep_at(gw.id)
            if seen and keep_at and not self.gwlink_session(gw.id):
                offline.append((ref, seen, keep_at))
        why = []
        for w in (due or {}).get("why") or []:
            if isinstance(w, dict):
                slot = int(w.get("slot") or 0)
                live = bool(slot) and self.db.gateway(slot) is not None      # снятый — без ссылки
                why.append({"kind": w.get("kind"),
                            "ref": nb.gw_ref(w["name"], w.get("label", ""), slot=slot if live else 0,
                                             bot_username=bot) if w.get("name") else ""})
        return nb.backup_caption(timeutil.fmt_dt_ui(timeutil.now(), year=True), refs, offline,
                                 nb.event_tail(why))

    def backup_slot_states(self) -> list[tuple]:
        """[(слот, «имя», подпись, состояние, seen, keep_at)] — строки раздела,
        только где есть что сказать: агент не сохраняет настройки (старше
        3.4.0), настройки ещё не приходили, шлюз не на связи."""
        out = []
        for gw in self.db.gateways():
            seen = self.db.get_state(f"gwlink_seen_{gw.id}") or ""
            keep_at = self._keep_at(gw.id)
            caps = self.gwlink_agent_caps(gw.id)
            if (seen or self.gwlink_session(gw.id)) and "keep1" not in caps:
                state = "outdated"
            elif not keep_at:
                state = "waiting"
            elif seen and not self.gwlink_session(gw.id):
                state = "offline"
            else:
                continue
            out.append((gw.id, self._gw_name(gw), gw.label or "", state, seen, keep_at))
        return out

    # ── после восстановления: ждём выхода шлюзов на связь (факт, не отметка) ──

    WAIT_KEY = "restore_wait"

    def restore_wait_start(self, report: dict) -> None:
        """Отчёт ушёл: по каждому слоту, к которому бэкап применён, ждём hello.
        Пустой сервер — свой текст на случай, если шлюз не выйдет."""
        slots = {}
        for s in (report or {}).get("slots") or []:
            if isinstance(s, dict) and s.get("case") != "removed" and s.get("slot"):
                slots[str(int(s["slot"]))] = {"name": s.get("name", ""), "label": s.get("label", ""),
                                              "link_if": s.get("link_if", "")}
        if not slots:
            self.db.set_state(self.WAIT_KEY, "")
            return
        self.db.set_state(self.WAIT_KEY, json.dumps(
            {"at": timeutil.to_iso(timeutil.now()), "empty": bool(report.get("empty")), "slots": slots},
            ensure_ascii=False))

    def _wait(self) -> dict:
        w = self.db.get_state_json(self.WAIT_KEY, {})
        return w if isinstance(w, dict) and isinstance(w.get("slots"), dict) else {}

    def _wait_bot(self, slot: int) -> dict:
        """Бот шлюза слота — для ссылки на его чат; слота нет или бот неизвестен — пусто."""
        try:
            return self.gw_bot_identity(slot) if self.db.gateway(slot) is not None else {}
        except Exception:                                 # noqa: BLE001 — ссылка не обязательна
            return {}

    def _wait_ref(self, slot: int, rec: dict) -> str:
        from awgbot.domain.notices import backup as nb
        gw = self.db.gateway(slot)
        name, label = (self._gw_name(gw), gw.label or "") if gw is not None else (rec.get("name", ""),
                                                                                 rec.get("label", ""))
        return nb.gw_ref(name, label, slot=slot if gw is not None else 0,
                         bot_username=getattr(self, "bot_username", "") or "")

    def _wait_save(self, w: dict) -> None:
        self.db.set_state(self.WAIT_KEY, json.dumps(w, ensure_ascii=False) if w.get("slots") else "")

    def restore_wait_hello(self, slot_id: int) -> list:
        """Шлюз вышел на связь после восстановления — без звука, один раз."""
        from awgbot.core import config
        from awgbot.domain.notices import backup as nb
        from awgbot.domain.services.types import Notification
        w = self._wait()
        rec = (w.get("slots") or {}).pop(str(int(slot_id)), None)
        if rec is None:
            return []
        self._wait_save(w)
        return [Notification(config.ADMIN_ID, nb.gateway_back(self._wait_ref(int(slot_id), rec)), silent=True)]

    def restore_wait_tick(self) -> list:
        """Такт канала: кто не вышел за RESTORE_WAIT_MIN — одно уведомление на
        слот (без звука), дальше ожидание снимается."""
        from awgbot.core import config
        from awgbot.domain.notices import backup as nb
        from awgbot.domain.services.types import Notification
        w = self._wait()
        if not w:
            return []
        try:
            age = timeutil.elapsed(timeutil.parse_iso(str(w.get("at"))))
        except ValueError:
            age = nb.RESTORE_WAIT_MIN * 60
        if age < nb.RESTORE_WAIT_MIN * 60:
            return []
        notes = [Notification(config.ADMIN_ID,
                              nb.gateway_not_back(self._wait_ref(int(n), rec), empty=bool(w.get("empty")),
                                                  link_if=str(rec.get("link_if") or ""),
                                                  bot=self._wait_bot(int(n))), silent=True)
                 for n, rec in sorted((w.get("slots") or {}).items(), key=lambda x: int(x[0]))]
        self.db.set_state(self.WAIT_KEY, "")
        return notes
