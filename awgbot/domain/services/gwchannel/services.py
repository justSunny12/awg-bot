"""services.py — сервисы соседних сетей: приём, раздача, ответы, строка карточки."""
from __future__ import annotations

import json

from awgbot.util import timeutil
from awgbot.domain import gwservices

from .common import log


class ServicesMixin:
    # ── сервисы соседних сетей ────────────
    _GWLINK_SVC_KEY = "gwlink_svc"           # список SMB-серверов сети слота (после чистки)
    _GWLINK_PEER_SVC_KEY = "gwlink_peer_svc" # ответ слота на раздачу записей соседей
    _SVC_NEIGHBOUR_STALE_S = 24 * 3600       # сосед молчит дольше — его сервисы не раздаются

    def gwlink_services_in(self, slot_id: int, items) -> bool:
        """Список SMB-серверов сети слота от его агента: чистка по подсетям
        слота из БД (не из снимка), запись только при изменении. True — изменился."""
        gw = self.db.gateway(slot_id)
        if gw is None:
            return False
        cleaned = gwservices.clean(items, gw.lan_subnets, gwservices.MAX_OWN)
        raw = json.dumps(cleaned, ensure_ascii=False)
        key = self._gwlink_key(self._GWLINK_SVC_KEY, slot_id)
        if (self.db.get_state(key) or "[]") == raw:
            return False
        self.db.set_state(key, raw)
        log.info("канал линка: слот %s назвал SMB-серверы своей сети: %s", slot_id, len(cleaned))
        return True

    def gwlink_services(self, slot_id: int) -> list[dict]:
        data = self.db.get_state_json(self._gwlink_key(self._GWLINK_SVC_KEY, slot_id), [])
        return [r for r in data if isinstance(r, dict)]

    def _gwlink_seen_recent(self, slot_id: int, within_s: int) -> bool:
        if self.gwlink_session(slot_id):
            return True
        raw = self.db.get_state(self._gwlink_key(self._GWLINK_SEEN_KEY, slot_id)) or ""
        try:
            return bool(raw) and (timeutil.now() - timeutil.parse_iso(raw)).total_seconds() < within_s
        except ValueError:
            return False

    def _gwlink_peer_nets_applied(self, gw) -> tuple[list[str], list[str]]:
        """(что сервер выдаёт слоту по функции B, что из этого применено на
        шлюзе по снимку): до перевыпуска маршрута к соседу нет."""
        issued = self.gateway_peer_nets(gw.id)
        snap = self.gwlink_snapshot(gw.id)
        applied = str((snap.get("bundle") or {}).get("peer_home_nets") or "").split()
        return issued, [n for n in issued if n in applied]

    def gwlink_peer_services_for(self, gw, allowed: list[str] | None = None) -> tuple[str, list[dict]]:
        """Что раздать слоту: сервисы других слотов с адресами в пересечении
        «выдано по B» ∩ «применено на шлюзе» (allowed — если уже посчитано).
        Сосед молчит дольше суток — его сервисы не раздаются. (отпечаток,
        записи); пусто — («», [])."""
        if allowed is None:
            _issued, allowed = self._gwlink_peer_nets_applied(gw)
        items = self._gwlink_neighbour_services(gw, allowed)
        return gwservices.feed_hash(items), items

    def _gwlink_neighbour_services(self, gw, nets: list[str]) -> list[dict]:
        """Сервисы других слотов на связи (не молчащих дольше суток) с адресами
        в nets, после чистки."""
        if not nets:
            return []
        out: list[dict] = []
        for other in self.db.gateways():
            if other.id == gw.id or not self._gwlink_seen_recent(other.id, self._SVC_NEIGHBOUR_STALE_S):
                continue
            out += self.gwlink_services(other.id)
        return gwservices.clean(out, nets, gwservices.MAX_PEER)

    def gwlink_peer_services_ack_in(self, slot_id: int, body: dict) -> None:
        self._gwlink_ack_put(self._GWLINK_PEER_SVC_KEY, slot_id, body)

    def gwlink_peer_services_ack(self, slot_id: int) -> dict:
        return self._gwlink_ack_get(self._GWLINK_PEER_SVC_KEY, slot_id)

    def gwlink_services_card(self, gw) -> dict:
        """Строка карточки слота: сколько SMB у слота, сколько ему раздано и что
        с ними на шлюзе. Имена на экраны сервера не идут — только числа."""
        own = self.gwlink_services(gw.id)
        issued, applied = self._gwlink_peer_nets_applied(gw)
        digest, _items = self.gwlink_peer_services_for(gw, applied)
        ack = self.gwlink_peer_services_ack(gw.id)
        # в счёт — сервисы по ВЫДАННЫМ подсетям: до перевыпуска они уже есть,
        # просто ждут, и строка обязана это сказать, а не «не найдены»
        peer = self._gwlink_neighbour_services(gw, issued) if issued else []
        acked = bool(ack) and ack.get("hash") == digest
        if acked and digest:
            state = "applied" if ack.get("ok") else "failed"
        elif issued and not applied:
            # подсети соседей ещё не на шлюзе: агенту с документом их довезёт
            # канал (ожидание), агенту 3.2.x — только файл (перевыпуск)
            state = "pending" if "cfg1" in self.gwlink_agent_caps(gw.id) else "reissue"
        elif acked:
            # пустое к пустому: шлюз подтвердил снятие записей
            state = "applied" if ack.get("ok") else "failed"
        else:
            state = "pending"
        return {"own": len(own), "peer": len(peer), "state": state, "error": (ack.get("error") or "") if ack else ""}
