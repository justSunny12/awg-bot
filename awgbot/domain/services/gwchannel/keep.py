"""keep.py — копия настроек шлюза на сервере (domain/gwkeep): хранение блока
gwkeep_<слот>, ответ на hello — что вернуть шлюзу и вернуть ли вообще,
приём `keep`, строка карточки.

Источник правды — шлюз: сервер блок только хранит. Обратно — в трёх случаях:
новая установка того же устройства (молча — решает агент по своей чистоте),
новое устройство в слоте (вопросом), восстановление сервера из бэкапа
(`offer = restore`, вопросом, без traffic: расход живого шлюза — его). Блок
снимается вместе со слотом и переживает замену устройства (реестр slotkeys).
"""
from __future__ import annotations

import json

from awgbot.domain import gwkeep
from awgbot.domain.services.gateway_link import slotkeys
from awgbot.util import gwlink, timeutil

from .common import log


class KeepMixin:
    def gwkeep(self, slot_id: int) -> dict:
        raw = self.db.get_state(slotkeys.key("gwkeep", slot_id)) or ""
        try:
            data = json.loads(raw) if raw.startswith("{") else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _gwkeep_save(self, slot_id: int, block: dict) -> None:
        self.db.set_state(slotkeys.key("gwkeep", slot_id), json.dumps(block, ensure_ascii=False) if block else "")

    def _slot_device_pub(self, slot_id: int) -> str:
        gw = self.db.gateway(slot_id)
        dev = self.db.get_device(gw.device_id) if gw is not None else None
        return dev.public_key if dev is not None else ""

    def gwkeep_hello(self, slot_id: int, keep_hash: str, keep_new: bool, keep_fresh: bool) -> tuple[str, dict]:
        """Ответ на hello агента с keep1: (вид, тело) — keep_back, keep_none или
        keep_ok с отпечатком хранимого (агент сам пришлёт своё, если другое).
        Агент своё не шлёт, пока не получит этот ответ."""
        b = self.gwkeep(slot_id)
        have = gwlink.clean_hex(keep_hash, 16)
        dev = self._slot_device_pub(slot_id)
        if b.get("items") is not None and b.get("offer") == "restore":
            if have and have == b.get("hash"):
                b.pop("offer", None); b.pop("offer_from", None); b.pop("asked", None)
                self._gwkeep_save(slot_id, b)
                return "keep_ok", {"hash": have}
            b["asked"] = "restore"
            self._gwkeep_save(slot_id, b)
            return "keep_back", {"why": "restore", "from": str(b.get("offer_from") or b.get("at") or ""),
                                 "at": str(b.get("at") or ""), "other": bool(b.get("dev") and dev and b["dev"] != dev),
                                 "hash": b.get("hash", ""), "items": gwkeep.clean(b.get("items"))}
        if keep_new and b.get("items") is not None:
            same = bool(dev) and b.get("dev") == dev
            if same and have == b.get("hash"):
                return "keep_ok", {"hash": have}
            body = {"why": "same" if same else "device", "from": str(b.get("at") or ""),
                    "at": str(b.get("at") or ""), "hash": b.get("hash", ""), "items": gwkeep.clean(b.get("items"))}
            t = gwkeep.traffic_clean(b.get("traffic"))
            if t is not None:
                body["traffic"] = t                   # расход места, а не настройка: переносится сразу
            if not (same and keep_fresh):
                b["asked"] = body["why"]
                self._gwkeep_save(slot_id, b)
            return "keep_back", body
        if keep_new:
            return "keep_none", {}
        return "keep_ok", {"hash": str(b.get("hash") or "")}

    def gwkeep_in(self, slot_id: int, body: dict) -> str:
        """Блок от агента: сохранить (got — по часам сервера, dev — ключ
        устройства слота), снять предложение. Первый блок нового устройства —
        событие бэкапа. Возвращает отпечаток сохранённого."""
        items = gwkeep.clean(body.get("items"))
        digest = gwkeep.print_(items)
        prev = self.gwkeep(slot_id)
        dev = self._slot_device_pub(slot_id)
        traffic = gwkeep.traffic_clean(body.get("traffic")) or gwkeep.traffic_clean(prev.get("traffic"))
        sess = self.gwlink_session(slot_id) if hasattr(self, "gwlink_session") else {}
        block = {"v": gwkeep.V, "hash": digest, "at": str(body.get("at") or "")[:40],
                 "got": timeutil.to_iso(timeutil.now()), "agent": str((sess or {}).get("agent") or "")[:32],
                 "dev": dev, "items": items}
        if traffic is not None:
            block["traffic"] = traffic
        self._gwkeep_save(slot_id, block)
        if prev.get("dev") != dev and hasattr(self, "backup_event_mark"):
            try:
                self.backup_event_mark("keep", slot=slot_id)   # первый блок нового устройства
            except Exception as e:                    # noqa: BLE001
                log.warning("бэкап по событию keep не отмечен: %s", e)
        if prev.get("hash") != digest:
            log.info("канал линка: слот %s прислал копию настроек (%s)", slot_id, digest)
        return digest

    def gwkeep_card(self, slot_id: int, agent_caps=None, online: bool = False) -> dict:
        """Строка карточки: {"state": saved|asked|outdated|"", "at"}. «Не
        сохраняются» — только когда агент на связи и без keep1."""
        b = self.gwkeep(slot_id)
        caps = list(agent_caps if agent_caps is not None else self.gwlink_agent_caps(slot_id))
        if online and gwkeep.CAP not in caps:
            return {"state": "outdated", "at": ""}
        if b.get("asked") or b.get("offer") == "restore":
            return {"state": "asked", "at": str(b.get("at") or "")}
        if b.get("at"):
            return {"state": "saved", "at": str(b["at"])}
        return {"state": "", "at": ""}
