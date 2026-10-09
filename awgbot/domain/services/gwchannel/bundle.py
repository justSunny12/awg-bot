"""bundle.py — итог применения файла конфигурации, пришедший каналом; где лежит файл в чате."""
from __future__ import annotations

import json

from awgbot.util import gwlink, timeutil

from .common import log


class BundleMixin:
    # ── итог применения конфигурации, пришедший каналом ──────────────────────
    _GWLINK_APPLIED_KEY = "gwlink_applied"       # {ok, at, fp, agent_at, error} — последний итог применения
    _GWLINK_BUNDLE_MSG_KEY = "gwlink_bundle_msg" # где в чате лежит файл конфигурации слота

    def gwlink_applied_in(self, slot_id: int, body: dict) -> dict:
        """Шлюз применил файл конфигурации из своего чата (или не смог) и сразу
        сказал об этом каналом: {ok, error, fp, at}. Тот же итог второй раз
        (агент не дождался подтверждения) — dup: показывать нечего, подтвердить надо."""
        ok = bool(body.get("ok"))
        err = " ".join(str(body.get("error") or "").split())[:300]
        fp = gwlink.clean_hex(body.get("fp"), 32)
        at = str(body.get("at") or "")[:40]
        key = self._gwlink_key(self._GWLINK_APPLIED_KEY, slot_id)
        prev = self.db.get_state_json(key, {})
        dup = bool(fp) and prev.get("fp") == fp and prev.get("agent_at") == at
        if not dup:
            self.db.set_state(key, json.dumps({"ok": ok, "at": timeutil.to_iso(timeutil.now()),
                                               "fp": fp, "agent_at": at, "error": err}, ensure_ascii=False))
            log.info("канал линка: слот %s %s конфигурацию%s", slot_id,
                     "применил" if ok else "не применил", f": {err}" if err else "")
        return {"ok": ok, "error": err, "fp": fp, "at": at, "dup": dup}

    def gw_bundle_msg_set(self, slot_id: int, chat_id: int, file_id: int, instr_id: int | None,
                          fp: str = "", plain: bool = False) -> None:
        """Запомнить, где лежит выданный файл и его отпечаток: итог с шлюза
        уберёт файл, «В меню» тоже. Итог о другом файле этот не трогает.
        plain — файл первого применения: его убирает выход шлюза на связь."""
        self.db.set_state(self._gwlink_key(self._GWLINK_BUNDLE_MSG_KEY, slot_id),
                          json.dumps({"chat": int(chat_id), "file": int(file_id),
                                      "instr": int(instr_id) if instr_id else None,
                                      "fp": str(fp or "")[:32], "plain": bool(plain)}))

    def gw_bundle_msg_get(self, slot_id: int) -> dict:
        return self.db.get_state_json(self._gwlink_key(self._GWLINK_BUNDLE_MSG_KEY, slot_id), {})

    def gw_bundle_msg_clear(self, slot_id: int) -> None:
        self.db.set_state(self._gwlink_key(self._GWLINK_BUNDLE_MSG_KEY, slot_id), "")
