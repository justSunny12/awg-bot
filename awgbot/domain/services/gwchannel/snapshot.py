"""snapshot.py — снимок состояния шлюза: приём дельт, возраст, забыть слот."""
from __future__ import annotations

import json

from awgbot.util import timeutil
from awgbot.domain import gwsnapshot

from .common import log


class SnapshotMixin:
    # ── снимок ───────────────────────────────────────────────────────────────

    def gwlink_snapshot_in(self, slot_id: int, patch: dict, rev: int, full: bool) -> bool:
        """Принять `snap` или `delta`. False — разрыв нумерации: вызывающий
        попросит полный снимок и начнёт счёт заново.

        Дельта накладывается на хранимый снимок, а не заменяет его: по каналу
        едет только разница, и хранить её отдельно значило бы держать два
        источника одной картинки.
        """
        rev_key = self._gwlink_key(self._GWLINK_SNAP_REV_KEY, slot_id)
        have = int(self.db.get_state(rev_key) or 0)
        if full:
            snap = gwsnapshot.sanitize(patch)
        else:
            if rev != have + 1 or not have:
                log.info("канал линка: слот %s — разрыв нумерации (было %s, пришло %s)",
                         slot_id, have, rev)
                return False
            stored = self.gwlink_snapshot(slot_id)
            if not stored:
                return False
            snap = gwsnapshot.apply_delta(stored, gwsnapshot.sanitize(patch))
        now = timeutil.now()
        with self.db.transaction():
            self.db.set_state(self._gwlink_key(self._GWLINK_SNAP_KEY, slot_id),
                              json.dumps(snap, ensure_ascii=False))
            self.db.set_state(rev_key, str(int(rev)))
            self.db.set_state(self._gwlink_key(self._GWLINK_SNAP_AT_KEY, slot_id),
                              timeutil.to_iso(now))
            self.db.set_state(self._gwlink_key(self._GWLINK_SNAP_TS_KEY, slot_id),
                              self._gwlink_sent_at(patch.get("ts")))
        return True

    @staticmethod
    def _gwlink_sent_at(ts) -> str:
        """Время отправки по часам шлюза, ISO. В сообщение канала оно приходит
        unix-числом из конверта — и это правильный источник для сверки часов:
        время отправки, а не время тика, когда снимок был снят (тот мог быть
        три минуты назад, и сверка врала бы про часы при идеальных часах)."""
        raw = str(ts or "").strip()
        if raw.isdigit():
            from datetime import datetime, timezone
            try:
                dt = datetime.fromtimestamp(int(raw), tz=timezone.utc).astimezone(timeutil.TZ)
            except (OverflowError, OSError, ValueError):
                return ""
            return timeutil.to_iso(dt)
        return raw[:40]

    def gwlink_snapshot(self, slot_id: int) -> dict:
        snap = self.db.get_state_json(self._gwlink_key(self._GWLINK_SNAP_KEY, slot_id), {})
        if isinstance(snap, dict) and isinstance(snap.get("bundle"), dict):
            # снимок, сохранённый сервером 3.3.0, — с прежними именами полей
            # (снимается со следующим сдвигом минимума)
            snap["bundle"] = gwsnapshot.bundle_both_names(snap["bundle"])
        return snap

    def gwlink_snapshot_age(self, slot_id: int) -> int | None:
        """Секунд с приёма ПО ЧАСАМ ВПС; None — снимка не было."""
        raw = self.db.get_state(self._gwlink_key(self._GWLINK_SNAP_AT_KEY, slot_id)) or ""
        if not raw:
            return None
        try:
            return max(0, int((timeutil.now() - timeutil.parse_iso(raw)).total_seconds()))
        except ValueError:
            return None

    def gwlink_forget(self, slot_id: int) -> None:
        """Снятие слота: ключи канала уходят вместе с ключами слота."""
        self.gwcfg_forget(slot_id)
        for key in (self._GWLINK_SESSION_KEY, self._GWLINK_SEEN_KEY, self._GWLINK_SNAP_KEY,
                    self._GWLINK_SNAP_REV_KEY, self._GWLINK_SNAP_AT_KEY, self._GWLINK_SNAP_TS_KEY,
                    self._GWLINK_ERROR_KEY, self._GWLINK_CAPS_KEY, self._GWLINK_LISTS_KEY,
                    self._GWLINK_SVC_KEY, self._GWLINK_PEER_SVC_KEY, self._GWLINK_BUNDLE_MSG_KEY,
                    self._GWLINK_OWN_UPTO_KEY, self._GWLINK_OWN_ACK_KEY, self._GWLINK_OWN_REJ_KEY,
                    self._GWLINK_OWN_CAP_KEY):
            self.db.set_state(self._gwlink_key(key, slot_id), "")
        # подтверждение настроек и итог применения файла — доставки, снятой в
        # 3.3.1: у слотов, заведённых раньше, ключи ещё лежат
        for key in ("gwlink_ack", "gwlink_applied"):
            self.db.set_state(self._gwlink_key(key, slot_id), "")
        # канон своих списков не трогается: он общий, вернувшийся шлюз его получит
