"""snapshot.py — снимок состояния шлюза: приём дельт, возраст, забыть слот."""
from __future__ import annotations

import json

from awgbot.util import timeutil
from awgbot.domain import gwsnapshot
from awgbot.domain.services.gateway_link import slotkeys

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
        return self.db.get_state_json(self._gwlink_key(self._GWLINK_SNAP_KEY, slot_id), {})

    def gwlink_snapshot_age(self, slot_id: int) -> int | None:
        """Секунд с приёма ПО ЧАСАМ ВПС; None — снимка не было."""
        raw = self.db.get_state(self._gwlink_key(self._GWLINK_SNAP_AT_KEY, slot_id)) or ""
        if not raw:
            return None
        try:
            return max(0, int(timeutil.elapsed(timeutil.parse_iso(raw))))
        except ValueError:
            return None

    def gwlink_forget(self, slot_id: int) -> None:
        """Замена устройства и снятие слота: ключи канала и документа уходят
        вместе с прежним устройством — по реестру slotkeys, пометка «устройство»."""
        self.gwcfg_forget(slot_id)
        slotkeys.forget(self.db, slot_id, slotkeys.names(when=slotkeys.DEVICE, part="link"))
        # команда набора обновлений — тоже о прежнем устройстве: новое её не знает
        slotkeys.forget(self.db, slot_id, slotkeys.names(when=slotkeys.DEVICE, part="fleet"))
        # канон своих списков не трогается: он общий, вернувшийся шлюз его получит
