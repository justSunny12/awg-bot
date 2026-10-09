"""own.py — свои списки, общие для всех шлюзов: канон, слияние правок, ответы, строка карточки."""
from __future__ import annotations

import json
import secrets

from awgbot.core import config
from awgbot.util import gwlink, timeutil
from awgbot.domain import gwownlists

from .common import log, _OWN_LOCK


class OwnListsMixin:
    # ── свои списки, общие для всех шлюзов ──
    _GWLINK_OWN_KEY = "gwlink_own_lists"     # канон: {"gen", "ver", "items": {домен: [вид, слот, время]}}
    _GWLINK_OWN_UPTO_KEY = "gwlink_own_upto" # «<run> <n>» — последнее разобранное событие слота
    _GWLINK_OWN_ACK_KEY = "gwlink_own_ack"   # ответ слота на канон: {ok, hash, n, at, error}
    _GWLINK_OWN_REJ_KEY = "gwlink_own_rej"   # что из последнего пакета слота отвергнуто: [[домен, причина]]
    _GWLINK_OWN_CAP_KEY = "gwlink_own_cap"   # «1»/«0» — назвал ли агент own_hash в hello (знает ли синхронизацию)

    def gwlink_own_hello_in(self, slot_id: int, capable: bool) -> None:
        self.db.set_state(self._gwlink_key(self._GWLINK_OWN_CAP_KEY, slot_id), "1" if capable else "0")

    def gwlink_own_canon(self) -> dict:
        data = self.db.get_state_json(self._GWLINK_OWN_KEY, {})
        if not data.get("gen"):
            # поколение — случайное при создании и с этого момента постоянное:
            # по нему шлюз отличает «тот же сервер» от переустановленного;
            # под той же блокировкой, что слияние, — два потока не создадут два
            with _OWN_LOCK:
                data = self.db.get_state_json(self._GWLINK_OWN_KEY, {})
                if not data.get("gen"):
                    data = {"gen": secrets.token_hex(4), "ver": 0, "items": {}}
                    self.db.set_state(self._GWLINK_OWN_KEY, json.dumps(data))
        if not isinstance(data.get("items"), dict):
            data["items"] = {}
        return data

    def _gwlink_own_upto(self, slot_id: int) -> list:
        raw = (self.db.get_state(self._gwlink_key(self._GWLINK_OWN_UPTO_KEY, slot_id)) or "").split()
        return [raw[0], int(raw[1])] if len(raw) == 2 and raw[1].isdigit() else ["", 0]

    def gwlink_own_in(self, slot_id: int, run: str, events) -> bool:
        """События слота → канон (правила слияния — gwownlists.merge): под блокировкой и одной
        транзакцией — два слота разбираются в разных потоках. True — канон изменился."""
        with _OWN_LOCK, self.db.transaction():
            canon = self.gwlink_own_canon()
            canon, upto, rejected, changed = gwownlists.merge(
                canon, slot_id, run, events, self._gwlink_own_upto(slot_id),
                timeutil.to_iso(timeutil.now()), deny=[config.SERVER_HOST])
            self.db.set_state(self._gwlink_key(self._GWLINK_OWN_UPTO_KEY, slot_id), f"{upto[0]} {upto[1]}")
            self.db.set_state(self._gwlink_key(self._GWLINK_OWN_REJ_KEY, slot_id),
                              json.dumps(rejected, ensure_ascii=False) if rejected else "")
            if changed:
                self.db.set_state(self._GWLINK_OWN_KEY, json.dumps(canon, ensure_ascii=False))
        if changed:
            log.info("канал линка: слот %s изменил свои списки (ver %s, %s доменов)",
                     slot_id, canon.get("ver"), len(canon.get("items") or {}))
        if rejected:
            log.info("канал линка: слот %s — отвергнуто правок своих списков: %s", slot_id, len(rejected))
        return changed

    def gwlink_own_for(self, gw) -> tuple[str, dict]:
        """(отпечаток для слота, тело own_set): канон целиком плюс upto его
        правок и отвергнутое из последнего пакета."""
        canon = self.gwlink_own_canon()
        items = gwownlists.canon_items(canon)
        upto = self._gwlink_own_upto(gw.id)
        digest = gwownlists.digest(canon["gen"], canon["ver"], items, upto)
        rej = self.db.get_state_json(self._gwlink_key(self._GWLINK_OWN_REJ_KEY, gw.id), [])
        return digest, {"hash": digest, "gen": canon["gen"], "ver": int(canon["ver"]),
                        "items": sorted([d, k] for d, k in items.items()), "upto": upto, "rej": rej}

    def _gwlink_ack_put(self, key: str, slot_id: int, body: dict) -> dict:
        """Ответ шлюза на доставку (записи SMB, канон своих списков) — в state
        слота одним форматом: {ok, hash, n, at, error}; поля из чужого
        сообщения чистятся здесь."""
        try:
            n = int(body.get("n") or 0)
        except (TypeError, ValueError):
            n = 0
        ack = {"ok": bool(body.get("ok")), "hash": gwlink.clean_hex(body.get("hash")), "n": n,
               "at": timeutil.to_iso(timeutil.now()),
               "error": " ".join(str(body.get("error") or "").split())[:200]}
        self.db.set_state(self._gwlink_key(key, slot_id), json.dumps(ack, ensure_ascii=False))
        return ack

    def _gwlink_ack_get(self, key: str, slot_id: int) -> dict:
        """Ответ слота: JSON одного формата; не-JSON (строки выпусков ниже
        минимума) — «ответа нет»: следующая доставка перепишет."""
        raw = self.db.get_state(self._gwlink_key(key, slot_id)) or ""
        if raw.startswith("{"):
            try:
                return json.loads(raw)
            except ValueError:
                return {}
        return {}

    def gwlink_own_ack_in(self, slot_id: int, body: dict) -> None:
        self._gwlink_ack_put(self._GWLINK_OWN_ACK_KEY, slot_id, body)

    def gwlink_hello_ack_in(self, key: str, slot_id: int, digest: str) -> None:
        """Агент назвал в hello отпечаток, совпавший с каноном: доставка не нужна,
        и это подтверждение — иначе карточка ждала бы ответа вечно (прежний ответ
        потерян или записан в старом формате). Пишется, только если записанный
        ответ о другом отпечатке."""
        if self._gwlink_ack_get(key, slot_id).get("hash") != digest:
            self._gwlink_ack_put(key, slot_id, {"ok": True, "hash": digest, "n": 0, "error": ""})

    def gwlink_own_hello_ack(self, slot_id: int, digest: str) -> None:
        self.gwlink_hello_ack_in(self._GWLINK_OWN_ACK_KEY, slot_id, digest)

    def gwlink_peer_services_hello_ack(self, slot_id: int, digest: str) -> None:
        self.gwlink_hello_ack_in(self._GWLINK_PEER_SVC_KEY, slot_id, digest)

    def gwlink_own_ack(self, slot_id: int) -> dict:
        return self._gwlink_ack_get(self._GWLINK_OWN_ACK_KEY, slot_id)

    def gwlink_own_card(self, gw) -> dict:
        """Строка карточки слота: числа канона и что с ним на шлюзе. Домены на
        экраны сервера не идут. state: applied | pending | offline | failed |
        old_agent; режим без VPN у слота выключен — off, канон не считается."""
        if not gw.lan_mode:
            return {"vpn": 0, "ru": 0, "state": "off", "error": ""}
        digest, body = self.gwlink_own_for(gw)
        vpn, ru = gwownlists.counts(dict(body["items"]))
        ack = self.gwlink_own_ack(gw.id)
        cap = self.db.get_state(self._gwlink_key(self._GWLINK_OWN_CAP_KEY, gw.id)) or ""
        if ack and ack.get("hash") == digest:
            state = "applied" if ack.get("ok") else "failed"
        elif cap == "0":
            # агент не назвал own_hash в hello — синхронизацию не знает
            state = "old_agent"
        elif not self.gwlink_session(gw.id):
            state = "offline"
        else:
            state = "pending"
        return {"vpn": vpn, "ru": ru, "state": state, "error": ack.get("error") or ""}
