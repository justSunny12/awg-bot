"""peersvc.py — сервисы соседних сетей."""

from __future__ import annotations

import json
import os
from awgbot.domain import gwservices
from awgbot.domain.gwchecks import WRITE_ERROR, failure_detail
from awgbot.util import gwlink  # noqa: E402
from awgbot.domain.gateway.base import GwCheck, log


class PeerServicesMixin:
    """Сервисы соседних сетей."""
    # ── сервисы соседних сетей ────────────
    # Не настраивается: работает там и тогда, где работает доступ между
    # подсетями (PEER_HOME_NETS в юните) при режиме без VPN и канале линка.
    _SVC_LOCAL_KEY = "gw_svc_local"          # свой список после обзора, JSON
    _SVC_PEER_KEY = "gw_peer_svc"            # применённый чужой: {"hash", "items"}
    _SVC_PEER_ERR_KEY = "gw_peer_svc_err"    # последняя ошибка применения
    _SVC_NUDGE_KEY = "gw_peer_svc_nudge"     # как _OWN_NUDGE_KEY, для записей SMB
    _SVC_PENDING_KEY = "gw_peer_svc_pending" # не применённое из-за обвязки: {"hash","items"} — повтор
    _SVC_MISSES = 3                          # обзоров подряд без сервиса — снимаем

    def services_active(self) -> bool:
        from awgbot.infra import gwguard
        return (gwguard.lan_mode() and bool(gwguard.unit_env("PEER_HOME_NETS").split())
                and gwguard.unit_env("LINK_CHANNEL") == "1")

    def services_local(self) -> list[dict]:
        """SMB-серверы этой сети, найденные обзором (после чистки)."""
        return [r for r in self.db.get_state_json(self._SVC_LOCAL_KEY, []) if isinstance(r, dict)]

    def services_scan(self) -> bool:
        """Обзор mDNS своей сети. True — свой список изменился, пора слать серверу.
        Гистерезис: появление — сразу, исчезновение — после _SVC_MISSES обзоров
        подряд (уснувший NAS не роняет кэш DNS соседей рестартами dnsmasq);
        обзор, не удавшийся вовсе, промахом не считается."""
        from awgbot.infra import gwguard
        prev = self.services_local()
        if not self.services_active():
            self.__dict__.pop("_svc_misses", None)
            if prev:
                self.db.set_state(self._SVC_LOCAL_KEY, "[]")
                return True
            return False
        out = gwguard.avahi_browse()
        if out is None:
            return False
        nets = gwguard.unit_env("HOME_SUBNETS").split()
        found = {(r["a"], r["p"]): r for r in gwservices.parse_avahi(out, nets)}
        misses: dict = self.__dict__.setdefault("_svc_misses", {})
        cur: list[dict] = []
        for r in prev:
            key = (r.get("a"), r.get("p"))
            if key in found:
                misses.pop(key, None)
                cur.append(found.pop(key))
            else:
                misses[key] = misses.get(key, 0) + 1
                if misses[key] < self._SVC_MISSES:
                    cur.append(r)
                else:
                    misses.pop(key, None)
        cur += list(found.values())
        cur = gwservices.clean(cur, nets, gwservices.MAX_OWN)
        if cur == gwservices.clean(prev, nets, gwservices.MAX_OWN):
            return False
        self.db.set_state(self._SVC_LOCAL_KEY, json.dumps(cur, ensure_ascii=False))
        log.info("gateway: SMB-серверы этой сети: %s", len(cur))
        return True

    def services_peer(self) -> dict:
        return self.db.get_state_json(self._SVC_PEER_KEY, {})

    def services_applied_hash(self) -> str:
        """Отпечаток применённых записей соседей — в hello канала."""
        return str(self.services_peer().get("hash") or "")

    def apply_peer_services(self, digest: str, items) -> dict:
        """Записи соседей от сервера → dnsmasq: {ok, error, n}. Данные с ВПС —
        недоверенные: чистка по подсетям соседей из ЮНИТА, сборка файла из
        шаблона, построчный белый список в помощнике. Тот же файл, что стоит,
        — без рестарта dnsmasq; пустой список — файл снимается."""
        from awgbot.infra import gwguard
        digest = gwlink.clean_hex(digest)
        if not gwguard.lan_mode():
            return self._svc_store(digest, [], False, "VPN-транзит на шлюзе выключен")
        peers = gwguard.unit_env("PEER_HOME_NETS").split()
        clean_items = gwservices.clean(items, peers, gwservices.MAX_PEER) if peers else []
        text = gwservices.render_dnsmasq(clean_items, gwguard.unit_env("HOME_SUBNETS").split(), digest)
        try:
            with open(gwguard.PEER_SERVICES_CONF, encoding="utf-8") as f:
                current = f.read()
        except OSError:
            current = ""
        if text == current:
            return self._svc_store(digest, clean_items, True, "")
        if not os.path.exists(gwguard.LAN_SERVICES_SCRIPT):
            err = self._defer_for_reassert("помощника сервисов соседей нет", self._SVC_PENDING_KEY,
                                           {"hash": digest, "items": clean_items}, "отсутствует скрипт")
            if not os.path.exists(gwguard.LAN_SERVICES_SCRIPT):
                return self._svc_store(digest, [], False, err)
            self.db.set_state(self._SVC_PENDING_KEY, "")    # реассерт уже положил помощник
        if not text:
            ok, tail = gwguard.run_lan_services("")
        else:
            # второй рубеж перед помощником: файл собран из шаблона, но данные — с ВПС
            if not gwservices.lines_ok(text):
                return self._svc_store(digest, [], False, "не пройдена проверка строк")
            try:
                os.makedirs(os.path.dirname(gwguard.PEER_SERVICES_NEW), mode=0o700, exist_ok=True)
                with open(gwguard.PEER_SERVICES_NEW, "w", encoding="utf-8") as f:
                    f.write(text)
            except OSError as e:
                return self._svc_store(digest, [], False,
                                       f"{WRITE_ERROR}: {gwguard.os_error_text(e)}")
            ok, tail = gwguard.run_lan_services(gwguard.PEER_SERVICES_NEW)
        return self._svc_store(digest, clean_items, ok,
                               "" if ok else (tail or "скрипт записей SMB отказал без объяснений"))

    def _svc_store(self, digest: str, items: list, ok: bool, err: str) -> dict:
        with self.db.transaction():
            if ok:
                self.db.set_state(self._SVC_PEER_KEY,
                                  json.dumps({"hash": digest, "items": items}, ensure_ascii=False))
                self.db.set_state(self._SVC_PENDING_KEY, "")
            self.db.set_state(self._SVC_PEER_ERR_KEY, err)
            self._nudge_note(self._SVC_NUDGE_KEY, err)
        return {"ok": ok, "error": err, "n": len(items)}

    def services_retry(self) -> dict | None:
        """Повтор отложенного применения (помощника не было): помощник появился
        — применить то, что прислал сервер; None — повторять нечего. Итог
        уходит серверу как peer_svc_ack, он его ждёт с тем же отпечатком."""
        from awgbot.infra import gwguard
        raw = self.db.get_state(self._SVC_PENDING_KEY) or ""
        if not raw:
            return None
        if not os.path.exists(gwguard.LAN_SERVICES_SCRIPT):
            self._reassert_throttled("помощника сервисов соседей нет")
            if not os.path.exists(gwguard.LAN_SERVICES_SCRIPT):
                return None
        pending = self.db.get_state_json(self._SVC_PENDING_KEY, {})
        if not pending:
            self.db.set_state(self._SVC_PENDING_KEY, "")
            return None
        digest = str(pending.get("hash") or "")
        result = self.apply_peer_services(digest, pending.get("items") or [])
        if not result.get("ok") and (self.db.get_state(self._SVC_PENDING_KEY) or "") == raw:
            # тот же отказ, не про помощника — второй раз не пробуем
            self.db.set_state(self._SVC_PENDING_KEY, "")
        result["hash"] = digest
        return result

    def services_status(self) -> tuple[dict, list[GwCheck]]:
        """(блок панели, проверки группы «svc»): проверки видны в мониторе, но
        уведомлений не шлют — соседи, которых нет, не авария."""
        from awgbot.infra import gwguard
        info: dict = {"active": self.services_active()}
        checks: list[GwCheck] = []
        if not info["active"]:
            return info, checks
        own = self.services_local()
        info["own"] = [r.get("n", "") for r in own]
        info["browse"] = gwguard.avahi_browse_available()
        info["avahi"] = gwguard.avahi_active()
        # сначала демон: без него обвязка avahi-utils не ставит, и совет про
        # мастер восстановления на шлюзе без NAS был бы пустым
        if info["avahi"] is False:
            checks.append(GwCheck("SMB этой подсети", None,
                                  "avahi-daemon не запущен: SMB-серверы этой подсети не видны из подсетей "
                                  "других шлюзов"))
        elif not info["browse"]:
            checks.append(GwCheck("SMB этой подсети", None,
                                  "нет avahi-browse, пакет avahi-utils (🔧 Восстановить)"))
        else:
            checks.append(GwCheck("SMB этой подсети", True, f"{len(own)} SMB"))
        peer = self.services_peer()
        items = [r for r in (peer.get("items") or []) if isinstance(r, dict)]
        info["peer"] = [r.get("n", "") for r in items]
        info["ever"] = bool(peer)
        info["err"] = self.db.get_state(self._SVC_PEER_ERR_KEY) or ""
        if info["err"]:
            checks.append(GwCheck("SMB подсетей других шлюзов", False,
                                  failure_detail("записи не применились", info["err"])))
        elif items:
            got = gwguard.dns_local(f"_smb._tcp.{gwservices.BROWSE_DOMAIN}", "PTR")
            ok = None if got is None else bool(got)
            checks.append(GwCheck("SMB подсетей других шлюзов", ok,
                                  f"{len(items)} SMB доступны" if ok else
                                  ("резолвер не отдаёт записи: journalctl -u dnsmasq -e"
                                   if ok is False else "не проверено: dig не ответил")))
        for c in checks:
            c.group = "svc"
        return info, checks
