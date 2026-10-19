"""linki1.py — I1 линка: тот же I1, что у
клиентских устройств, — строка `#I1` клиентского интерфейса сервера как есть.

Бот этот I1 не создаёт и не меняет: его задают на хосте руками, генератор
конфигов берёт его и из закомментированной строки (`infra/awg/parsers.py`).
Линк берёт значение тем же разбором и только на шлюзовую сторону
(`LINK_I1` в `--rekey-stage`). Строки нет или она пустая — линк без I1.
"""
from __future__ import annotations

import time

from awgbot.infra import awg

from .common import log

READ_FAIL_BACKOFF_S = 600      # не прочитался — такт не зовёт exec снова раньше


class LinkI1Mixin:
    """Значение I1 для линков и то, что стоит у линка сейчас."""

    def gateway_client_i1(self, cached: bool = False) -> tuple[bool, str]:
        """(прочитано, значение) I1 клиентского интерфейса; пусто — строки нет
        или она пустая. Не прочитано (интерфейс не отвечает) — (False, "").
        cached (такт живости) — после неудачи чтения READ_FAIL_BACKOFF_S
        секунд без exec: интерфейс, который не отвечает, такт иначе дёргал бы
        каждые 30 с."""
        failed = self.__dict__.get("_client_i1_failed_at")
        if cached and failed is not None and time.monotonic() - failed < READ_FAIL_BACKOFF_S:
            return False, ""
        try:
            params = awg.read_server_params()
        except Exception as e:                            # noqa: BLE001 — нет интерфейса, exec, права
            if failed is None or time.monotonic() - failed >= READ_FAIL_BACKOFF_S:
                log.info("I1 клиентского интерфейса не прочитан: %s", e)
            self.__dict__["_client_i1_failed_at"] = time.monotonic()
            return False, ""
        self.__dict__.pop("_client_i1_failed_at", None)
        return True, str((params.get("obfuscation") or {}).get("I1") or "").strip()

    def _gw_link_conf_path(self, gw) -> str:
        return f"/root/gw-{gw.link_if}.conf"

    def gateway_link_i1(self, gw) -> tuple[bool, str]:
        """(прочитано, значение) I1 выданной шлюзовой стороны линка."""
        try:
            with open(self._gw_link_conf_path(gw), encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return False, ""
        return True, str(awg._extract_param(text, "I1") or "").strip()
