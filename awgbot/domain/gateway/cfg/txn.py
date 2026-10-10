"""txn.py — транзакция env + link: юнит обвязки и конфиг линка одним шагом, проба, откат."""
from __future__ import annotations

import os
import time

from awgbot.core import config
from awgbot.domain import gwcfg
from awgbot.util import wgconf

from .. import base
from ..base import log

PROBE_SECONDS = 90          # после перезапуска линка — ждём свежий хендшейк столько
FRESH_SECONDS = 60          # хендшейк моложе — линк жив


class CfgTxnMixin:
    """Подсети соседей и подсеть клиентов живут и в юните, и в AllowedIPs
    конфига линка: применять их порознь значит снова получить «зелёная сверка
    при неработающем доступе». Поэтому env и link — одна транзакция: юнит,
    конфиг линка, реассерт, проба; отказ на любом шаге — юнит прежним
    текстом, прежний конфиг линка, реассерт."""

    def cfg_link_next_path(self) -> str:
        from awgbot.infra import gwguard
        return f"{gwguard.GW_ETC}/link.next"

    def cfg_link_prev_path(self) -> str:
        from awgbot.infra import gwguard
        return f"{gwguard.GW_ETC}/link.prev"

    @staticmethod
    def _cfg_write_private(path: str, text: str) -> None:
        from awgbot.util.fsatomic import write_private
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        write_private(path, text)

    def cfg_env_changes(self, env: dict) -> dict:
        """Какие строки юнита (в именах юнита) отличаются от желаемых."""
        current = self.cfg_env_current()
        return {gwcfg.unit_name(k): env[k] for k in gwcfg.ENV_KEYS if env.get(k, "") != current.get(k, "")}

    def cfg_link_changed(self, link_text: str) -> bool:
        return gwcfg.link_print(link_text) != gwcfg.link_print(self.cfg_link_text())

    def cfg_apply_env_link(self, env: dict, link_text: str | None) -> dict:
        """Применить проверенные env и (если прислан) конфиг линка одной
        транзакцией. Возвращает {"ok", "changed": [ключи документа и "link"],
        "error", "retry", "how"} — how: unchanged | syncconf | restarted."""
        from awgbot.infra import gwguard
        env_changes = self.cfg_env_changes(env)
        link_new = bool(link_text) and self.cfg_link_changed(link_text)
        changed = [k for k in gwcfg.ENV_KEYS if gwcfg.unit_name(k) in env_changes] + (["link"] if link_new else [])
        if not changed:
            return {"ok": True, "changed": [], "error": "", "retry": False, "how": "unchanged"}
        with base._APPLY_LOCK:
            if self._unit_settled(120) == "activating":
                return {"ok": False, "changed": changed, "error": self.BUSY_ACTIVATING, "retry": True, "how": ""}
            before_unit = None
            if env_changes:
                try:
                    before_unit = gwguard.unit_set_env(env_changes)
                except (OSError, gwguard.GwGuardError) as e:
                    return {"ok": False, "changed": changed, "error": f"юнит не переписан ({e})", "retry": False, "how": ""}
            how = "unchanged"
            prev_saved = False
            if link_new:
                try:
                    prev = self.cfg_link_text()
                    if prev:
                        self._cfg_write_private(self.cfg_link_prev_path(), prev)
                        prev_saved = True
                    conf = wgconf.parse(link_text)
                    self._cfg_write_private(self.cfg_link_next_path(), wgconf.render(conf, "link", stamp=conf.stamp or "1"))
                except (OSError, wgconf.WgConfError) as e:
                    self._cfg_rollback(before_unit, False)
                    return {"ok": False, "changed": changed, "error": f"конфиг линка не записан ({e})", "retry": False, "how": ""}
                ok, out = gwguard.link_apply(self.cfg_link_next_path())
                how = gwguard.link_applied_how(out) or ("syncconf" if ok else "")
                if not ok:
                    log.warning("gateway: конфиг линка с сервера не применился: %s", out[-300:])
                    self._cfg_rollback(before_unit, prev_saved)
                    return {"ok": False, "changed": changed, "error": "конфиг линка: " + (out.splitlines()[-1] if out else "скрипт отказал"),
                            "retry": False, "how": ""}
            self._last_reassert = time.monotonic()
            ok, err = gwguard.reassert(timeout=600)
            if not ok and gwguard.is_timeout(err):
                log.warning("gateway: конфигурация с сервера применяется дольше обычного: %s", err)
                state = self._unit_settled(300)
                if state == "activating":
                    return {"ok": False, "changed": changed, "error": self.STILL_APPLYING, "retry": True, "how": how}
                ok = state == "active"
                err = err if not ok else ""
            if ok and how == "restarted":
                ok, err = self._cfg_probe_link()
            if ok:
                self.invalidate_static()
                return {"ok": True, "changed": changed, "error": "", "retry": False, "how": how}
            log.warning("gateway: конфигурация с сервера не применилась (%s) — откатываю", err)
            self._cfg_rollback(before_unit, prev_saved)
            return {"ok": False, "changed": changed, "error": err or "обвязка не применилась", "retry": False, "how": how}

    def _cfg_probe_link(self) -> tuple[bool, str]:
        """После down/up линка — до PROBE_SECONDS ждём свежий хендшейк (позже
        момента применения): без него конфиг считается не поднявшимся."""
        from awgbot.infra import gwguard
        deadline = time.monotonic() + PROBE_SECONDS
        while time.monotonic() < deadline:
            age = gwguard.handshake_age(config.GW_LINK_IF)
            if age is not None and age <= FRESH_SECONDS:
                return True, ""
            time.sleep(3)
        return False, f"линк не поднялся за {PROBE_SECONDS} с — вернул прежний конфиг"

    def _cfg_rollback(self, before_unit: str | None, link_prev: bool) -> None:
        """Откат транзакции: юнит прежним текстом, прежний конфиг линка, реассерт."""
        from awgbot.infra import gwguard
        try:
            if link_prev:
                ok, out = gwguard.link_apply(self.cfg_link_prev_path())
                if not ok:
                    log.warning("gateway: прежний конфиг линка не вернулся: %s", out[-200:])
            if before_unit is not None:
                gwguard.unit_restore(before_unit)
            if before_unit is not None or link_prev:
                gwguard.reassert(timeout=600)
        except (OSError, gwguard.GwGuardError) as e:
            log.warning("gateway: откат конфигурации не удался: %s", e)
