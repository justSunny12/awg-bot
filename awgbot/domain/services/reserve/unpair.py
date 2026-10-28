"""
unpair.py — часть пакета reserve (см. __init__): удаление пары.

«🗑 Удалить резерв» — с обеих сторон: линк пары прочь (`--rollback`), строка
пары и все ключи `rsv_*` — прочь, роль админа второго сервера снята (кэш
доступа сброшен), файервол перерисован, второй стороне — `gone` по каналу
(если жив) и уведомление. Пока мои пользователи у резерва (`away`) или его у меня
(`serving`) — отказ: сначала возврат.
"""
from __future__ import annotations

from awgbot.domain.services.types import ServiceError

from ..gateway_link.common import log
from .state import _LOCK


class ReserveUnpairMixin:

    def reserve_unpair_check(self) -> str:
        """Пусто — можно; иначе причина отказа."""
        if not self.reserve_paired():
            return "резерва нет"
        if self.reserve_away():
            return f"твои пользователи и бот сейчас у «{self.reserve_peer_name()}» — сначала верни их"
        if self.reserve_hosting():
            return f"пользователи и бот «{self.reserve_peer_name()}» сейчас здесь — сначала верни их ему"
        return ""

    def _unpair_zone(self, why: str) -> None:
        """Зона при удалении пары: фаза taking/serving/relay — юнит открытия стоп,
        `--teardown`, `--wipe` (иначе state.json держит DNAT хвоста, а клон и бот
        второй стороны жили бы без автомата)."""
        from awgbot.infra import rsvzone
        phase = str(rsvzone.state().get("phase") or "idle")
        if phase not in ("taking", "serving", "relay"):
            return
        log.warning("резерв: пара снимается (%s) при зоне в фазе %s — снимаю зону", why, phase)
        if hasattr(self, "_rsvopen_stop"):
            try:
                self._rsvopen_stop()
            except Exception as e:                            # noqa: BLE001
                log.info("резерв: юнит процесса открытия не остановлен: %s", e)
        for fn in (rsvzone.teardown, rsvzone.wipe):
            try:
                fn()
            except rsvzone.ZoneError as e:
                log.warning("резерв: зона при удалении пары: %s", e)

    def reserve_unpair(self, why: str = "user") -> dict:
        """Снять пару у себя. Возвращает прежнюю строку пары (для уведомления)."""
        why_not = self.reserve_unpair_check()
        if why_not and why != "peer":
            raise ServiceError(why_not)
        with _LOCK:
            peer = self.reserve_peer() or {}
            self._unpair_zone(why)                            # клон, пересылка хвоста, хранилище — прочь
            try:
                self._run_pair_script("--rollback", self._pair_env())
            except ServiceError as e:
                log.warning("резерв: линк пары не снят при удалении (%s): %s", why, e)
            try:
                self.reserve_zone_links_drop()                # стороны линков шлюзов основы в зоне — прочь
            except Exception as e:                        # noqa: BLE001
                log.warning("резерв: стороны зоны не сняты при удалении пары: %s", e)
            try:
                self.reserve_pkg_in_drop()                    # пакет и доля второй стороны — прочь
            except Exception as e:                            # noqa: BLE001
                log.warning("резерв: пакет второй стороны не снят при удалении пары: %s", e)
            try:
                import shutil
                from awgbot.core import config as _cfg
                shutil.rmtree(str(_cfg.DATA_DIR / "reserve" / "out"), ignore_errors=True)   # снимки base-*
            except Exception as e:                            # noqa: BLE001
                log.warning("резерв: снимки слияния не сняты: %s", e)
            self.db.reserve_peer_delete()
            self.rsv_clear_all()
            self.reserve_epoch_sync()                         # пары нет — держатель здесь
            try:
                from awgbot.core import access_cache
                access_cache.invalidate_all()
            except Exception:                             # noqa: BLE001 — слой бота не загружен (CLI)
                pass
        try:
            self._gw_firewall_refresh()
        except Exception as e:                            # noqa: BLE001
            log.warning("резерв: файервол после удаления пары не перерисован: %s", e)
        if hasattr(self, "backup_event_mark"):
            self.backup_event_mark("reserve")
        log.info("резерв: пара с «%s» снята (%s)", peer.get("name") or "?", why)
        return peer
