"""bundle.py — где в чате лежит выданный файл первого применения."""
from __future__ import annotations

import json


class BundleMixin:
    # ── где в чате лежит выданный файл первого применения ────────────────────
    _GWLINK_BUNDLE_MSG_KEY = "gwlink_bundle_msg" # где в чате лежит файл конфигурации слота

    def gw_bundle_msg_set(self, slot_id: int, chat_id: int, file_id: int, instr_id: int | None,
                          fp: str = "", plain: bool = False) -> None:
        """Запомнить, где лежит выданный файл и его отпечаток: его убирают
        выход шлюза на связь (installed), смена ключей линка и «В меню».
        plain — файл первого применения (других файлов сервер не выдаёт)."""
        self.db.set_state(self._gwlink_key(self._GWLINK_BUNDLE_MSG_KEY, slot_id),
                          json.dumps({"chat": int(chat_id), "file": int(file_id),
                                      "instr": int(instr_id) if instr_id else None,
                                      "fp": str(fp or "")[:32], "plain": bool(plain)}))

    def gw_bundle_msg_get(self, slot_id: int) -> dict:
        return self.db.get_state_json(self._gwlink_key(self._GWLINK_BUNDLE_MSG_KEY, slot_id), {})

    def gw_bundle_msg_clear(self, slot_id: int) -> None:
        self.db.set_state(self._gwlink_key(self._GWLINK_BUNDLE_MSG_KEY, slot_id), "")
