"""linki1.py — I1 линка (§2.6 концепта «единые обновления»): тот же I1, что у
клиентских устройств, — строка `#I1` клиентского интерфейса сервера как есть.

Бот этот I1 не создаёт и не меняет: его задают на хосте руками, генератор
конфигов берёт его и из закомментированной строки (`infra/awg/parsers.py`).
Линк берёт значение тем же разбором и только на шлюзовую сторону
(`LINK_I1` в `--rekey-stage`). Строки нет или она пустая — линк без I1.
"""
from __future__ import annotations

from awgbot.infra import awg

from .common import log


class LinkI1Mixin:
    """Значение I1 для линков и то, что стоит у линка сейчас."""

    def gateway_client_i1(self) -> tuple[bool, str]:
        """(прочитано, значение) I1 клиентского интерфейса; пусто — строки нет
        или она пустая. Не прочитано (интерфейс не отвечает) — (False, "")."""
        try:
            params = awg.read_server_params()
        except Exception as e:                            # noqa: BLE001 — нет интерфейса, exec, права
            log.info("I1 клиентского интерфейса не прочитан: %s", e)
            return False, ""
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
