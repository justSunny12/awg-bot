"""lanmap_sync.py — адреса в туннеле: сверка таблицы `inet awg_lan_map` вместе
с таблицами и правилами слотов, такт живости, старт (таблица, затем уборка
маршрутов настоящих подсетей 3.2.x) и разовое уведомление после обновления.

Адресам в туннеле РФ-доступ не указ (шапка lanmap): таблица и политика слотов
ставятся, пока у слотов есть подсети, при любом состоянии выключателя
РФ-доступа и его рубильника."""

from __future__ import annotations

from awgbot.core import config, settings
from awgbot.domain.services.types import Notification
from awgbot.infra import routing
from awgbot.util import lanspace

from .common import log


class LanMapSyncMixin:
    """Сверка таблицы адресов в туннеле, старт и уведомление."""

    _LAN_PEERS_ANNOUNCED_KEY = "lan_peers_announced"   # хвост уведомления о связи подсетей сказан
    # Тумблер связи подсетей до 3.3.0. Связь подсетей теперь часть VPN-транзита,
    # ключ в conf мёртвый; читается один раз — для уведомления после обновления.
    _PEER_NETS_LEGACY_KEY = "app.routing.peer_nets.enabled"

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
            try:
                if text:
                    if text == stored and routing.lanmap.present():
                        return ok
                    routing.lanmap.apply(text)
                elif routing.lanmap.present():
                    routing.lanmap.remove()
                elif not stored:
                    return ok
            except routing.RoutingError as e:
                self._lan_log_once("table", str(e), "адреса в туннеле: таблица %s не поставлена: %s",
                                   routing.lanmap.TABLE)
                self.db.set_state(self._LANMAP_ERROR_KEY, str(e)[:300])
                return False
            self._lan_log_once("table", "", "")
            self.db.set_state(self._LANMAP_ERROR_KEY, "")
            self.db.set_state(self._LANMAP_TEXT_KEY, text)
            log.info("адреса в туннеле: таблица %s — %s", routing.lanmap.TABLE,
                     f"подсетей {len(entries)}" if text else "снята")
            return ok

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

    def lan_real_routes_drop(self) -> None:
        """Уборка маршрутов настоящих подсетей слотов в основной таблице,
        поставленных 3.2.x: они живы до ребута, и без уборки настоящие адреса
        у полного туннеля работали бы до него — полупереход. Снимается со
        сдвигом минимума на 3.3.0."""
        if not config.ROUTING_GW_INTERFACE:
            return
        pairs = [(n, g.link_if) for g in self.db.gateways() for n in g.lan_subnets or []]
        if not pairs:
            return
        try:
            routing.drop_lan_routes(pairs)
        except routing.RoutingError as e:
            log.warning("адреса в туннеле: прежние маршруты подсетей не сняты: %s", e)

    def lan_map_startup(self) -> list[Notification]:
        """Старт бота: таблицы слотов и awg_lan_map — на место до первого такта;
        прежние маршруты настоящих подсетей — прочь только при стоящей
        таблице: не встала (отказ nft, пространство неисправно) — настоящие
        адреса пусть работают, пока не починят. Затем — разовое уведомление."""
        self.lan_map_ensure()
        if self.lan_map_up():
            self.lan_real_routes_drop()
        return self.lan_space_announce()

    def lan_space_announce(self) -> list[Notification]:
        """Один раз после обновления: прежний тумблер связи подсетей был
        выключен, а слотов с VPN-транзитом два и больше — их сети теперь
        связаны без вопроса, и это надо сказать прямо. Пока адреса в туннеле
        выключены — молчит и ключ не взводит: связи нет. Свой ключ — у
        полного уведомления об адресах в туннеле он отдельный."""
        from awgbot.domain.alerts import armer
        from awgbot.domain.notices.lanmap import lan_peers_joined
        if self.db.get_state(self._LAN_PEERS_ANNOUNCED_KEY) == "1" or self.lan_space_problem():
            return []
        transit = self.gateway_transit_slots()
        was_off = not settings.get_bool(self._PEER_NETS_LEGACY_KEY, False)
        if not (was_off and len(transit) >= 2):
            self.db.set_state(self._LAN_PEERS_ANNOUNCED_KEY, "1")
            return []
        return [Notification(config.ADMIN_ID, lan_peers_joined([self._gw_link(g) for g in transit]),
                             on_sent=armer(self.db, self._LAN_PEERS_ANNOUNCED_KEY, "1"))]
