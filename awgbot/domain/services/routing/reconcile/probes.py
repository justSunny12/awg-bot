"""probes.py — часть пакета reconcile (см. __init__): зонды слотов — активного
(улики и зонд), резерва (редкий зонд и хендшейк), всех разом."""

from __future__ import annotations

from awgbot.core import settings
from awgbot.infra import routing
from awgbot.domain.evidence import EvidenceProbe


class RoutingProbesMixin:
    """Зонды слотов шлюза."""

    def routing_link_ok(self) -> bool:
        """Проходит ли трафик через шлюз — по последнему замеру зонда.

        Читаем сохранённый результат, а не зондируем на месте: метод дёргают
        экраны и preflight, а зонд — это пинги с таймаутами, им в отрисовке
        интерфейса не место. Замер делает routing_liveness_tick.
        """
        return self.db.get_state(self._RT_LINK_KEY) != "0"

    def routing_engaged(self) -> bool:
        """Должна ли маркировка быть включена в принципе — до вопроса о живости.

        Один предикат на всех, кто трогает рубильник. Реконсиляция и тик живости
        уже однажды разошлись во мнениях и спорили за него: один снимал политику,
        другой немедленно возвращал. Пока условие живёт в одном месте, разойтись
        им негде. routing.available() — это здоровье ОБВЯЗА, а не выключатель
        фичи; их легко перепутать, и тут они сведены явно.
        """
        return (routing.available()
                and settings.get_bool("app.routing.enabled", False))

    def routing_probe(self) -> str:
        """Замер прямо сейчас — АКТИВНОГО шлюза, тем путём, которым ходят
        пользователи (метка фичи, таблица фичи). Отдельно от routing_link_ok, чтобы
        было видно, где реальные пинги, а где чтение кэша."""
        targets, port = self._rt_probe_targets()
        active = self.active_gateway()
        return routing.probe_gateway(list(targets), port,
                                     iface=active.link_if if active else "")

    @staticmethod
    def _rt_probe_targets() -> tuple[list, int]:
        # Две цели по умолчанию: один внешний хост — сам по себе точка отказа,
        # и его заминка выглядела бы как отвал шлюза.
        targets = settings.get("app.routing.probe_targets", None) or ["77.88.8.8", "8.8.8.8"]
        port = int(settings.get("app.routing.probe_port", 53))
        return list(targets), port

    # Зонд резерва: те же цели и две попытки, но короче таймаут — резерв не
    # держит трафик, и его ответ ничего не откладывает.
    _RT_STANDBY_TIMEOUT = 2.0
    # Свежесть хендшейка резерва: keepalive линка — диапазон из client_config
    # (по умолчанию 25–35 с, как у пиров устройств), хендшейк раз в ~2 мин.
    _RT_STANDBY_HANDSHAKE_MAX = 180

    def _probe_slot(self, gw, *, active: bool) -> str:
        targets, port = self._rt_probe_targets()
        if active:
            return routing.probe_gateway(targets, port, iface=gw.link_if)
        return routing.probe_gateway(targets, port, timeout=self._RT_STANDBY_TIMEOUT,
                                     iface=gw.link_if, mark=gw.mark)

    def _rt_standby_interval(self) -> int:
        """Минут между зондами резерва; 0 — каждый такт (тесты)."""
        return settings.get_int("app.routing.failover.standby_probe_minutes", 5)

    def _standby_verdict(self, gw) -> str:
        """Живость РЕЗЕРВА без маячка каждые полминуты: наружу зондируем редко и
        с джиттером (строго периодический коннект с адреса шлюза — сигнатура
        для ТСПУ), а между зондами живость — по свежести хендшейка линка (это
        локальный exec, наружу ничего не уходит). Резерв «в порядке» = последний
        зонд прошёл И хендшейк свежий: такой шлюз примет нагрузку."""
        import random
        import time as _time
        nxt = self.__dict__.setdefault("_rt_standby_next", {})
        last = self.__dict__.setdefault("_rt_standby_last", {})
        every = self._rt_standby_interval()
        now = _time.monotonic()
        if every <= 0 or gw.id not in last or now >= nxt.get(gw.id, 0.0):
            last[gw.id] = self._probe_slot(gw, active=False)
            nxt[gw.id] = now + every * 60 * random.uniform(0.6, 1.4)
        # Хендшейк — всегда, и в такт зонда тоже: пройденный зонд без свежего
        # хендшейка означает, что путь ушёл не через линк (обвязка слота не на
        # месте) — такой резерв нагрузку не примет.
        age = routing.link_handshake_age(gw.link_if)
        if age is None or age > self._RT_STANDBY_HANDSHAKE_MAX:
            return routing.PROBE_DOWN
        return last[gw.id]

    def _standby_forget(self, slot_id: int) -> None:
        """Забыть память зондов слота — и резервную, и активную: после смены
        роли или удаления слота кэшированный вердикт относится к другому миру."""
        self.__dict__.setdefault("_rt_standby_next", {}).pop(slot_id, None)
        self.__dict__.setdefault("_rt_standby_last", {}).pop(slot_id, None)
        self.__dict__.setdefault("_rt_active_probe", {}).pop(slot_id, None)

    # Рост rx линка за такт, который НЕ подделать служебным трафиком: keepalive
    # это 32 байта, хендшейк вместе с джанком AmneziaWG (Jc ≤ 10 пакетов по
    # 8–70 байт, S1/S2 ≤ 150) — меньше килобайта. Всё, что выше, прислал шлюз,
    # а прислать ему нечего, кроме обратного трафика пользователей.
    _RT_RETURN_BYTES = 4096

    def _rt_idle_probe_interval(self) -> float:
        """Секунд между зондами активного слота, когда через линк никто не
        ходит; 0 — каждый такт (тесты)."""
        base = settings.get_int("app.routing.probe_seconds", 30)
        return base * float(settings.get("app.routing.probe_idle_multiplier", 10) or 0)

    def _active_verdict(self, gw) -> str:
        """Живость АКТИВНОГО слота: сначала бесплатные улики, потом зонд
        (domain/evidence.EvidenceProbe — одна механика с агентом).

        Вернувшийся через линк трафик пользователей — прямое доказательство пути
        ВПС → линк → шлюз → интернет → обратно, и оно сильнее зонда: зонд
        проверяет одну цель на 53-м порту, а это настоящие сессии живых людей.
        Улика засчитывается только при живом хендшейке; между зондами живость
        держит свежесть хендшейка — это локальный exec, наружу не уходит ничего.
        """
        probes = self.__dict__.setdefault("_rt_active_probe", {})
        st = routing.link_peer_state(gw.link_if)
        if st is None:
            probes.pop(gw.id, None)
            return self._probe_slot(gw, active=True)
        ep = probes.get(gw.id)
        if ep is None:
            ep = probes[gw.id] = EvidenceProbe(self._RT_RETURN_BYTES)
        fresh = st["age"] is not None and st["age"] <= self._RT_STANDBY_HANDSHAKE_MAX
        result, src = ep.verdict(
            st["rx"], st["tx"], minus=lambda rx, tx: self.channel.minus(gw.id, rx, tx),
            probe=lambda: self._probe_slot(gw, active=True), every=self._rt_idle_probe_interval(),
            on_traffic=lambda _prev: routing.PROBE_OK, back_is_rx=True, fresh=fresh)
        if src == "cache" and not fresh:
            # кэшированный «в порядке» живёт ровно до тех пор, пока жив
            # хендшейк: молчащий шлюз обязан проявиться сам, без зонда
            return routing.PROBE_DOWN
        return result

    def _probe_slots(self, slots, active) -> dict:
        """Все слоты разом: зонды независимы, а последовательно при лежащем
        резерве такт держал бы поток ещё на 2 × 2 с."""
        if len(slots) == 1:
            g = slots[0]
            if active is not None and active.id == g.id:
                return {g.id: self._active_verdict(g)}
            return {g.id: self._standby_verdict(g)}
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(slots)) as pool:
            futs = {}
            for g in slots:
                is_active = active is not None and active.id == g.id
                if hasattr(self, "rt_is_spare") and self.rt_is_spare(g):
                    futs[g.id] = pool.submit(self._spare_verdict, g, is_active)
                elif is_active:
                    futs[g.id] = pool.submit(self._active_verdict, g)
                else:
                    futs[g.id] = pool.submit(self._standby_verdict, g)
            return {sid: f.result() for sid, f in futs.items()}
