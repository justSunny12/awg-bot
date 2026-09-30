"""coldstart.py — холодный старт: выбор активного слота при старте."""

from __future__ import annotations

from typing import Optional
from awgbot.infra import routing
from awgbot.infra import bootid
from .common import log


class RoutingColdStartMixin:
    """Холодный старт: выбор активного слота при старте."""
    # ── холодный старт ───────────────────────────────────────────────────────
    def routing_cold_start(self) -> Optional[object]:
        """Выбор активного на старте. Тёплый
        старт (перезапустился только бот) — сохранённый активный, людям адрес
        не меняем. Холодный (ВПС перезагружался: аптайм хоста меньше, чем
        прошло с последнего такта; или состояния нет) — предпочтительный, если
        отвечает; иначе сохранённый, если отвечает; иначе любой отвечающий;
        иначе предпочтительный всё равно. Возвращает выбранный слот."""
        slots = self.db.gateways()
        if not slots:
            return None
        saved = self.active_gateway()
        preferred = self.preferred_gateway()
        if not self._rt_is_cold_start():
            return saved
        if preferred is None:
            return saved
        if not self.routing_engaged():
            self._rt_set_active(preferred)
            return preferred
        order = [preferred] + [g for g in (saved,) if g.id != preferred.id] \
            + [g for g in slots if g.id not in (preferred.id, saved.id)]
        for g in order:
            if self._probe_slot(g, active=False) == routing.PROBE_OK:
                self._rt_set_active(g)
                return g
        self._rt_set_active(preferred)
        return preferred

    def _rt_set_active(self, gw) -> None:
        cur = self.active_gateway()
        if cur is not None and cur.id == gw.id:
            return
        self.db.set_state(self._RT_ACTIVE_KEY, str(gw.id))
        try:
            routing.switch_active(gw.link_if)
        except routing.RoutingError as e:
            log.warning("routing: холодный старт, слот %s: %s", gw.id, e)
        log.info("routing: холодный старт — трафик через слот %s (%s)", gw.id, gw.link_if)

    _RT_BOOT_KEY = "routing_host_boot_id"      # boot_id хоста, виденный ботом

    def _rt_is_cold_start(self) -> bool:
        """Холодный старт — хост перезагружался с прошлого запуска бота:
        boot_id ядра сменился (свой ключ, не тот, что у hostboot: там метка
        одноразовая и снимается раньше). Именно загрузка, а не последний такт:
        первый такт живости стартует вместе с планировщиком, раньше этой
        проверки, и по нему холодный старт не отличить. Нет сохранённого
        активного — тоже холодный. boot_id запоминается при каждом вызове."""
        cur = bootid.read_boot_id()
        stored = self.db.get_state(self._RT_BOOT_KEY) or ""
        cold = not self.db.get_state(self._RT_ACTIVE_KEY)
        if cur:
            cold = cold or not stored or stored != cur
            self.db.set_state(self._RT_BOOT_KEY, cur)
        return cold

    def routing_startup_warnings(self) -> list[str]:
        """Замечания preflight по слотам: активный — прежний текст, резерв —
        «резерва нет». Слотов нет — один замер линка обвязки."""
        from awgbot.bot import texts as _texts
        slots = self.db.gateways()
        if not slots:
            warn = _texts.routing_gateway_warning(self.routing_probe(), at_start=True)
            return [warn] if warn else []
        active = self.active_gateway()
        verdicts = {g.id: self._probe_slot(g, active=(active is not None and active.id == g.id))
                    for g in slots}
        dead = [g for g in slots if verdicts[g.id] != routing.PROBE_OK]
        if not dead:
            return []
        if len(dead) == len(slots) and len(slots) > 1:
            names = " и ".join(self._gw_display_h(g) for g in slots)
            return [f"Шлюзы РФ-доступа {names} не отвечают.\n"
                    + self._rt_effect_line()]
        out = []
        for g in dead:
            if active is not None and active.id == g.id:
                warn = _texts.routing_gateway_warning(verdicts[g.id], at_start=True)
                if warn:
                    out.append(f"{warn[0].upper()}{warn[1:]} (шлюз {self._gw_display_h(g)})")
            else:
                out.append(f"Резервный шлюз {self._gw_display_h(g)} не отвечает на старте — "
                           "резерва сейчас нет, трафик идёт через "
                           f"{self._gw_display_h(active) if active else 'основной'}")
        return out
