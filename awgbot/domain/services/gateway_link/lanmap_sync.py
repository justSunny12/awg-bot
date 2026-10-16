"""lanmap_sync.py — адреса в туннеле: сверка таблицы `inet awg_lan_map` вместе
с таблицами и правилами слотов, такт живости и старт (таблица).

Адресам в туннеле РФ-доступ не указ (шапка lanmap): таблица и политика слотов
ставятся, пока у слотов есть подсети, при любом состоянии выключателя
РФ-доступа и его рубильника."""

from __future__ import annotations

from awgbot.infra import routing
from awgbot.util import lanspace

from .common import log


class LanMapSyncMixin:
    """Сверка таблицы адресов в туннеле и старт."""

    def _lan_log_once(self, key: str, err: str, fmt: str, *args) -> None:
        """Тот же отказ каждые 10 тактов — в журнал один раз, до смены причины."""
        seen = self.__dict__.setdefault("_lanmap_errs", {})
        if err and seen.get(key) != err:
            log.warning(fmt, *args, err)
        seen[key] = err

    def lan_map_ensure(self, slot_policy: bool = True) -> bool:
        """Таблицы и правила слотов с подсетями — независимо от РФ-доступа
        (доступ админа к сетям от него не зависит), MSS-кламп в их линки и
        таблица awg_lan_map. slot_policy=False — политику слотов в этот такт
        уже утвердил РФ-доступ (ensure_policy), второй раз не надо. Таблицу
        перевыставляет, только если текст разошёлся с поставленным или её нет в
        ядре; пустой текст — снимает, если стоит. Отказ установки — причина в
        state (карточка слота, замечание при запуске). False — что-то не
        поставлено."""
        with routing.mutation_lock:
            entries = self.lan_map_entries()
            ok = True
            if slot_policy:
                for sid, link in sorted({(e.slot, e.link) for e in entries}):
                    try:
                        routing.ensure_slot_policy(sid, link)
                        routing.ensure_mss_clamp(link)
                        self._lan_log_once(f"slot{sid}", "", "")
                    except routing.RoutingError as e:
                        ok = False
                        self._lan_log_once(f"slot{sid}", str(e),
                                           "адреса в туннеле: политика слота %s (%s) не поставлена: %s", sid, link)
            sp = lanspace.space()
            prefixes = {e.slot: str(lanspace.slot_prefix(e.slot, sp)) for e in entries}
            text = routing.lanmap.render(entries, str(sp), prefixes)
            stored = self.db.get_state(self._LANMAP_TEXT_KEY) or ""
            # пусто и было пусто — в ядро смотрим только первой сверкой процесса
            # (таблица могла остаться от прошлой жизни), дальше — по state
            first = not self.__dict__.get("_lanmap_checked")
            self._lanmap_checked = True
            try:
                if text:
                    if text == stored and routing.lanmap.present():
                        self._lan_table_ok()
                        return ok
                    routing.lanmap.apply(text)
                else:
                    if not stored and not first:
                        self._lan_table_ok()
                        return ok
                    try:
                        up = routing.lanmap.present()
                    except routing.RoutingUnavailable:
                        up = False                     # nft нет — и таблицы нет: не ошибка
                    if up:
                        routing.lanmap.remove()
                    elif not stored:
                        self._lan_table_ok()
                        return ok
            except routing.RoutingError as e:
                self._lan_log_once("table", str(e), "адреса в туннеле: таблица %s не поставлена: %s",
                                   routing.lanmap.TABLE)
                self.db.set_state(self._LANMAP_ERROR_KEY, str(e)[:300])
                return False
            self._lan_table_ok()
            self.db.set_state(self._LANMAP_TEXT_KEY, text)
            log.info("адреса в туннеле: таблица %s — %s", routing.lanmap.TABLE,
                     f"подсетей {len(entries)}" if text else "снята")
            return ok

    def _lan_table_ok(self) -> None:
        """Таблица в желаемом состоянии: прежний отказ — из state и журнала прочь."""
        self._lan_log_once("table", "", "")
        if self.db.get_state(self._LANMAP_ERROR_KEY):
            self.db.set_state(self._LANMAP_ERROR_KEY, "")

    def lan_map_tick(self, slot_policy: bool = True) -> None:
        """Такт живости: первый и каждый 10-й — сверка таблицы (дешёвая: текст
        против state и список таблиц ядра); события — правка подсетей, снятие
        слота, старт — сверяют сами."""
        n = getattr(self, "_lanmap_tick", -1) + 1
        self._lanmap_tick = n
        if n % 10:
            return
        try:
            self.lan_map_ensure(slot_policy=slot_policy)
        except Exception as e:                            # noqa: BLE001
            log.warning("адреса в туннеле: сверка на такте: %s", e)

    def lan_map_up(self) -> bool:
        """Таблица с записями стоит: текст поставлен без отказа и есть в ядре."""
        if not self.db.get_state(self._LANMAP_TEXT_KEY) or self.db.get_state(self._LANMAP_ERROR_KEY):
            return False
        try:
            return routing.lanmap.present()
        except routing.RoutingError:
            return False

    def lan_map_startup(self) -> None:
        """Старт бота: таблицы слотов и awg_lan_map — на место до первого такта."""
        self.lan_map_ensure()
