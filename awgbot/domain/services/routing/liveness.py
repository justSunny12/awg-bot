"""liveness.py — живость слотов: стрики, окно доступности, объявления."""

from __future__ import annotations

from awgbot.core import config
from awgbot.core import settings
from awgbot.infra import routing
from awgbot.util import timeutil
from awgbot.domain.services.types import Notification
from .common import log


class RoutingLivenessMixin:
    """Живость слотов: стрики, окно доступности, объявления."""
    # ── стрики по слоту ──────────────────────────────────────────────────────
    def _rt_keys(self, slot_id) -> tuple[str, str]:
        """(up, announced) ключи слота; без слотов (линк обвязки без назначенного
        шлюза) — прежние общие."""
        if slot_id is None:
            return self._RT_STREAK_KEY, self._RT_ANNOUNCED_KEY
        return f"routing_gw_{slot_id}_up_streak", f"routing_gw_{slot_id}_announced"

    _RT_HOLD_KEY = "routing_manual_hold"       # слот, на который переключили руками при отказе

    def _rt_failover_enabled(self) -> bool:
        return settings.get_bool("app.routing.failover.enabled", True)


    # ── окно доступности активного: переключение — не по стрику подряд ──────
    # Скользящее окно последних N замеров (5 мин / такт = 10). Доступность
    # ниже порога (50 %) — то есть в окне набралось ≥ 5 неуспешных замеров,
    # подряд или вразнобой, — и переключаемся тут же, на пятом неуспешном,
    # не дожидаясь заполнения окна. Старые замеры выпадают из окна, и три
    # неудачи, размазанные дольше пяти минут, поводом не становятся. Окно
    # живёт в памяти: писать его в БД каждый такт незачем.
    def _rt_window_size(self) -> int:
        """Ширина скользящего окна — в замерах (failover.window_samples)."""
        return max(2, settings.get_int("app.routing.failover.window_samples", 10))

    def _rt_window_push(self, slot_id, good: bool) -> None:
        win = self.__dict__.setdefault("_rt_windows", {})
        n = self._rt_window_size()
        win[slot_id] = (win.get(slot_id, []) + [good])[-n:]

    def _rt_window_reset(self, slot_id=None) -> None:
        win = self.__dict__.setdefault("_rt_windows", {})
        if slot_id is None:
            win.clear()
        else:
            win.pop(slot_id, None)

    def _rt_fail_need(self) -> int:
        """Сколько неуспешных замеров в окне делают шлюз недоступным:
        N × (100 − порог) / 100, с округлением вверх (10 × 50 % = 5)."""
        import math
        n = self._rt_window_size()
        limit = settings.get_int("app.routing.failover.min_availability", 50)
        return max(1, math.ceil(n * (100 - limit) / 100))

    def routing_monitor_info(self) -> dict:
        """Параметры мониторинга и резервирования для экрана настроек."""
        secs = settings.get_int("app.routing.probe_seconds", 30)
        n = self._rt_window_size()
        return {"probe_seconds": secs, "window": n, "need": self._rt_fail_need(),
                "availability": settings.get_int("app.routing.failover.min_availability", 50),
                "window_minutes": max(1, round(n * secs / 60)),
                "standby_minutes": self._rt_standby_interval(),
                "failover": self._rt_failover_enabled(),
                "interval_minutes": settings.get_int("app.routing.failover.min_interval_minutes", 10)}

    def _rt_window(self, slot_id) -> list:
        return self.__dict__.setdefault("_rt_windows", {}).get(slot_id, [])

    def _rt_unavailable(self, slot_id) -> bool:
        """Недоступен: в окне неуспешных не меньше порога."""
        return sum(1 for g in self._rt_window(slot_id) if not g) >= self._rt_fail_need()

    def _rt_dead(self, slot_id) -> bool:
        """Молчит всё окно: окно заполнено, все замеры неуспешные."""
        win = self._rt_window(slot_id)
        return len(win) >= self._rt_window_size() and not any(win)

    def _rt_bad_ticks(self, slot_id) -> int:
        """Сколько тактов длится недоступность — от первой неудачи в окне;
        для статусов «не отвечает N мин». 0 — доступен."""
        if not self._rt_unavailable(slot_id):
            return 0
        win = self._rt_window(slot_id)
        first = next((i for i, g in enumerate(win) if not g), 0)
        return len(win) - first

    def _rt_switch_interval_ok(self) -> bool:
        raw = self.db.get_state(self._RT_SWITCHED_KEY) or ""
        if not raw:
            return True
        try:
            since = (timeutil.now() - timeutil.parse_iso(raw)).total_seconds()
        except ValueError:
            return True
        return since >= 60 * settings.get_int("app.routing.failover.min_interval_minutes", 10)

    def routing_liveness_tick(self) -> list[Notification]:
        """Замер живости шлюзов, деградация и переключение. Тикает часто
        (десятки секунд).

        Метрика — проходимость наружу, а не возраст хендшейка: тот говорит,
        поднят ли туннель, а не ходит ли через него трафик.

        Одно понятие «недоступен» на все решения — скользящее окно замеров
        в окне неуспешных не меньше, чем допускает порог доступности.
        Порядок такта: 1) не задействовано — маркировка снята сразу;
        2) окно и стрик хороших каждого слота; 3) недоступен и есть кандидат
        (три хороших подряд, сам доступен) → переключение; 4) недоступен без
        кандидата → гашение, три хороших подряд → возврат; 5) объявления — в
        том же такте, что и действие. Липкость — следствие шага 3.
        """
        if not routing.available():
            return []
        engaged = self.routing_engaged()
        slots = self.db.gateways()
        active = self.active_gateway()
        # Зонды СНАРУЖИ замка и до транзакции: они длятся секунды (сеть), и
        # держать на это время БД или реконсиляцию значило бы менять один отказ
        # на другой.
        results: dict = {}
        first_tick = not hasattr(self, "_rt_tick")
        if engaged and slots and first_tick:
            # ПЕРВЫЙ такт: таблицы и правила слотов — до зондов. Без правила по
            # метке зонд резерва ушёл бы через основную таблицу ВПС и «прошёл»,
            # даже если линк мёртв.
            try:
                self._ensure_gateway_policy()
            except routing.RoutingError as e:
                log.warning("routing_liveness_tick: обвязка слотов на старте: %s", e)
        if engaged:
            results = self._probe_slots(slots, active) if slots else {None: self.routing_probe()}
        akey = active.id if active is not None else None
        verdict = results.get(akey, routing.PROBE_DOWN) if engaged else routing.PROBE_DOWN
        # Обвязку утверждаем по событию — смена вердикта, первый тик после
        # старта — и страховочно каждый 10-й тик (5 мин): утверждать маршрут и
        # правила каждые 30 с значило ~14 000 exec/сутки ради состояния,
        # которое меняется раз в неделю.
        self._rt_tick = getattr(self, "_rt_tick", -1) + 1
        last_verdict = getattr(self, "_rt_last_verdict", None)
        self._rt_last_verdict = verdict                   # ДО вызова: отказ ниже не должен повторяться каждый такт
        if engaged and not first_tick and (verdict != last_verdict or self._rt_tick % 10 == 0):
            try:
                self._ensure_gateway_policy()
            except routing.RoutingError as e:
                # линк одного слота опущен — обвязка слотов не доведена, но такт
                # (окно, стрики, переключение, алерты) обязан идти дальше
                log.warning("routing_liveness_tick: обвязка слотов не доведена: %s", e)

        was_on = self.db.get_state(self._RT_LINK_KEY) == "1"
        notes: list[Notification] = []
        switched_to = None
        refused = False
        ups: dict = {}                          # ключ слота → хороших подряд
        with self.db.transaction():
            keys = [g.id for g in slots] if slots else [None]
            if not engaged:
                # РЕШЕНИЕ, а не измерение: выключение фичи админом — не дребезг,
                # и ждать окно тут значит не выполнить прямое указание.
                for k in keys:
                    self.db.set_state(self._rt_keys(k)[0], "0")
                    ups[k] = 0
                self._rt_window_reset()
                ok = False
            else:
                # Окно — в памяти; стрик хороших — с потолком: в установившемся
                # состоянии такт не пишет на диск вовсе.
                for k in keys:
                    up_k, _a = self._rt_keys(k)
                    good = results.get(k) == routing.PROBE_OK
                    self._rt_window_push(k, good)
                    up = min(int(self.db.get_state(up_k) or 0) + 1, self._RT_UP_STREAK) if good else 0
                    self.db.set_state(up_k, str(up))
                    ups[k] = up
                    if k is not None and self._rt_unavailable(k):
                        self._gw_ping_forget(k)          # хост недоступен — пинг устарел
                        # момент падения — в state: окно замеров помнит только
                        # минуты, а «не отвечает 5 мин» вторую неделю — ложь
                        if not self.db.get_state(f"routing_gw_{k}_down_since"):
                            import time as _time
                            self.db.set_state(f"routing_gw_{k}_down_since", str(int(_time.time())))
                    elif k is not None and up >= self._RT_UP_STREAK:
                        self.db.set_state(f"routing_gw_{k}_down_since", "")
                a_up = ups[akey]
                # Ручное удержание: админ сам переложил трафик на лежащий шлюз —
                # значит, так надо, и автомат его не перекладывает обратно.
                # Ожил (три хороших) — удержание снято: упадёт снова, автомат
                # переключит, как обычно.
                hold = self.db.get_state(self._RT_HOLD_KEY) or ""
                if hold and hold == str(akey) and a_up >= self._RT_UP_STREAK:
                    self.db.set_state(self._RT_HOLD_KEY, "")
                    hold = ""
                # ПЕРЕКЛЮЧЕНИЕ: активный недоступен, есть кандидат (три хороших
                # подряд), интервал с прошлого автоматического прошёл.
                unavailable = self._rt_unavailable(akey)
                if (slots and len(slots) > 1 and unavailable
                        and self._rt_failover_enabled() and hold != str(akey)):
                    cand = next((g for g in slots if g.id != akey
                                 and ups[g.id] >= self._RT_UP_STREAK), None)
                    if cand is not None:
                        if self._rt_switch_interval_ok():
                            switched_to = cand
                            self.db.set_state(self._RT_ACTIVE_KEY, str(cand.id))
                            self.db.set_state(self._RT_SWITCHED_KEY, timeutil.to_iso(timeutil.now()))
                            # об отвале прежнего активного сообщает само
                            # переключение — его «снова в строю» придёт с
                            # хвостом «остаётся в резерве»
                            self.db.set_state(self._rt_keys(akey)[1], "1")
                            # оба — с чистого листа: у нового активного в окне
                            # могли остаться неудачи со времён резерва, и они
                            # тут же потянули бы его обратно
                            self._rt_window_reset(akey)
                            self._rt_window_reset(cand.id)
                            # и память зондов: роли поменялись, а кэш вердикта
                            # и базовые счётчики сняты для прежней
                            self._standby_forget(akey)
                            self._standby_forget(cand.id)
                            akey = cand.id
                            a_up = ups[akey]
                            unavailable = False
                            verdict = results.get(akey, routing.PROBE_DOWN)
                        else:
                            refused = True
                if verdict == routing.PROBE_OK:
                    ok = True if (was_on or switched_to is not None) else a_up >= self._RT_UP_STREAK
                else:
                    ok = was_on and not unavailable

        if switched_to is not None:
            try:
                routing.switch_active(switched_to.link_if)
            except routing.RoutingError as e:
                log.warning("routing: переключение на слот %s не удалось: %s", switched_to.id, e)
            active = switched_to
        try:
            # ПОД ЗАМКОМ: реконсиляция под ним же пересобирает ту цепочку, чей
            # рубильник мы дёргаем.
            with routing.mutation_lock:
                routing.set_marking_enabled(ok)
        except routing.RoutingError as e:
            log.warning("routing_liveness_tick: %s", e)
            return []
        self.db.set_state(self._RT_LINK_KEY, "1" if ok else "0")
        if not engaged:
            return []


        # ── объявления: в том же такте, что и действие ───────────────────────
        _a_up_k, a_ann_k = self._rt_keys(akey)
        announced = self.db.get_state(a_ann_k) == "1"
        others = [g for g in slots if g.id != akey]
        if switched_to is not None:
            prev = next((g for g in slots if g.id != switched_to.id
                         and self.db.get_state(self._rt_keys(g.id)[1]) == "1"), None)
            # переехали на резерв — люди с РФ-адресом, это не авария: без звука;
            # критичны только отказы, оставившие людей без РФ-доступа
            notes.append(Notification(config.ADMIN_ID,
                                      self._txt_rt_switched(prev, switched_to, results.get(prev.id) if prev else verdict)))
        elif ok:
            if announced:
                self.db.set_state(a_ann_k, "0")
                notes.append(Notification(config.ADMIN_ID, self._txt_rt_gw_up(active)))
        elif not announced and self._rt_unavailable(akey):
            self.db.set_state(a_ann_k, "1")
            if refused:
                text = self._txt_rt_switch_refused(active)
            else:
                # Разные причины — разный ремонт: «шлюз молчит» чинят на линке,
                # «за шлюзом нет интернета» — на самом шлюзе.
                dead = [g for g in others if self._rt_unavailable(g.id)]
                text = (self._txt_rt_gw_no_path(active, dead) if verdict == routing.PROBE_NO_PATH
                        else self._txt_rt_gw_down(active, dead))
            notes.append(Notification(config.ADMIN_ID, text, critical=True))
        # резерв: молчит всё окно — письмо без звука; ожил — «остаётся в резерве»
        for g in others:
            _u, ann_k = self._rt_keys(g.id)
            g_ann = self.db.get_state(ann_k) == "1"
            if g_ann and ups[g.id] >= self._RT_UP_STREAK:
                self.db.set_state(ann_k, "0")
                notes.append(Notification(config.ADMIN_ID, self._txt_rt_standby_up(g, active)))
            elif not g_ann and self._rt_dead(g.id):
                self.db.set_state(ann_k, "1")
                notes.append(Notification(config.ADMIN_ID,
                                          self._txt_rt_standby_down(g, active, self._rt_window_size())))
        return notes
