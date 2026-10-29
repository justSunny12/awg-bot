"""
rfzone.py — часть пакета reserve (см. __init__): РФ-доступ пользователей основы в
гостевой зоне — сторона бота основы на резерве (гость) и слово резерва.

Маршрутизацию в зоне ведёт тот же код, что на основе (слоты из реплики,
их линки — стороны зоны `awglink<N>`). Сверх него — **запасной слот «шлюзы
резерва»**: виртуальный слот с линком `awggr-n` (veth в зону, хост резерва
направляет его трафик в линк своего активного слота). Он есть только в
гостевом режиме и только пока резерв разрешил (`rf_via` в состоянии от
резерва); в порядке кандидатов — последний; **не липкий**: как только у
слота основы три хороших замера подряд, трафик возвращается на него.

Живость запасного слота — зондом наружу через его таблицу (хендшейка у
veth нет): активный — по такту зондов активного, в резерве — по редкому
интервалу резерва с джиттером.
"""
from __future__ import annotations

import ipaddress
import random
import time

from awgbot.core import config, models

RF_SPARE_ID = 30                 # 1 << 30 — метка зонда внутри 32 бит; таблица ROUTING_TABLE + 30
RF_SPARE_IF = "awggr-n"


class ReserveRfZoneMixin:
    # ── гость: запасной слот ────────────────────────────────────────────────
    def guest_rf_spare(self):
        """Слот «шлюзы резерва» — только у гостя и только пока резерв разрешил."""
        if not config.GUEST:
            return None
        st = self.guest_state()
        if not st.get("rf_via"):
            return None
        name = str(st.get("name") or "резерв")
        return models.Gateway(id=RF_SPARE_ID, device_id=0, link_if=RF_SPARE_IF, link_port=0,
                              link_cidr=str(st.get("rf_cidr") or ""), label=f"шлюзы «{name}»")

    def _slot_route_via(self, slot_id: int) -> str:
        """Шлюз маршрута слота: у запасного слота в зоне — адрес хоста в /30 veth;
        у настоящих линков — пусто (p2p)."""
        if int(slot_id) != RF_SPARE_ID:
            return ""
        gw = self.guest_rf_spare()
        if gw is None or not gw.link_cidr:
            return ""
        try:
            return str(next(iter(ipaddress.ip_network(gw.link_cidr, strict=False).hosts())))
        except (ValueError, StopIteration):
            return ""

    @staticmethod
    def rt_is_spare(gw) -> bool:
        return gw is not None and int(getattr(gw, "id", 0) or 0) == RF_SPARE_ID

    def _spare_verdict(self, gw, active: bool) -> str:
        """Зонд наружу через таблицу запасного слота; между зондами — последний вердикт."""
        from awgbot.infra import routing
        last = self.__dict__.setdefault("_rt_spare_last", {})
        nxt = self.__dict__.setdefault("_rt_spare_next", {})
        now = time.monotonic()
        key = "a" if active else "s"
        if key in last and now < nxt.get(key, 0.0):
            return last[key]
        targets, port = self._rt_probe_targets()
        if active:
            verdict = routing.probe_gateway(targets, port, iface=gw.link_if)
            every = float(self._rt_idle_probe_interval() or 0)
        else:
            verdict = routing.probe_gateway(targets, port, timeout=self._RT_STANDBY_TIMEOUT, iface=gw.link_if, mark=gw.mark)
            every = float(self._rt_standby_interval() or 0) * 60
        last[key] = verdict
        nxt[key] = now + every * random.uniform(0.6, 1.4) if every > 0 else 0.0
        return verdict

    def _rt_spare_yields(self, akey, ups: dict) -> bool:
        """Запасной слот активен, а слот основы ожил (три хороших подряд) —
        уступить: запасной не липкий."""
        if akey != RF_SPARE_ID:
            return False
        return any(k != RF_SPARE_ID and k is not None and int(v or 0) >= self._RT_UP_STREAK for k, v in ups.items())

    # ── резерв: что разрешаю гостю ──────────────────────────────────────────
    def reserve_rf_via_view(self) -> dict:
        """Поля состояния гостю: `rf_via` — тумблер «Мои шлюзы для резерва» включён и
        есть чем дать (режим включён, активный слот доступен), `rf_cidr` — /30
        veth РФ-трафика."""
        from awgbot.core import settings
        allowed = settings.get_bool("app.reserve.rf_via_mine", True) and bool(self.db.gateways())
        ok = False
        if allowed:
            try:
                active = self.active_gateway()
                ok = bool(self.routing_engaged()) and active is not None and not self._rt_unavailable(active.id)
            except Exception:                                 # noqa: BLE001
                ok = False
        return {"rf_via": bool(ok), "rf_cidr": str(settings.get("app.reserve.zone_rf_cidr", "169.254.77.4/30") or "")}
