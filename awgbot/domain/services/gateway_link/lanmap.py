"""lanmap.py — адреса в туннеле для локальных сетей шлюзов: записи таблицы
`inet awg_lan_map` из слотов, её сверка вместе с таблицами и правилами слотов,
перевод адресов в записях SMB, проверка пространства, разовая уборка прежних
маршрутов и уведомление после обновления (схема и формулы — util/lanspace,
таблица — infra/routing/lanmap)."""

from __future__ import annotations

import ipaddress

from awgbot.core import config, settings
from awgbot.domain.services.types import Notification
from awgbot.infra import routing
from awgbot.util import lanspace

from .common import log


class LanMapMixin:
    """Адреса в туннеле для локальных сетей шлюзов."""

    _LANMAP_TEXT_KEY = "lanmap_text"                 # текст последней поставленной таблицы
    _LAN_ANNOUNCED_KEY = "lan_space_announced"       # уведомление после обновления отправлено
    # Тумблер связи подсетей до 3.3.0. Связь подсетей теперь часть VPN-транзита,
    # ключ в conf мёртвый; читается один раз — для уведомления после обновления.
    _PEER_NETS_LEGACY_KEY = "app.routing.peer_nets.enabled"

    def lan_space_problem(self) -> str:
        """Причина, по которой адреса в туннеле включать нельзя, или "": служебное
        пространство задевает подсети клиентов (с подсетью переезда) или /30
        линков, либо резолвер админа или адрес перехвата DNS стоят внутри него
        вне служебной /16. Линки — и из настроек слотов, и заведённые в БД."""
        from awgbot.infra import admindns
        clients = [net for net, _iface in config.routing_client_subnets()]
        links = list(config.ROUTING_LINK_CIDRS)
        for gw in self.db.gateways():
            if gw.link_cidr and gw.link_cidr not in links:
                links.append(gw.link_cidr)
        return lanspace.space_problem(lanspace.space(), clients, links,
                                      admindns.addr(), routing.DNS_INTERCEPT_ADDR)

    # ── записи таблицы ───────────────────────────────────────────────────────

    @staticmethod
    def _lan_split(gw) -> tuple[list[tuple[str, str]], list[dict]]:
        """([(настоящая, двойник)], [отброшенные]) подсетей слота по порядку
        списка. Отброшенные — данные до 3.3.0, которые ввод теперь не примет:
        подсеть в служебном пространстве (kind «space»), шире /16 («wide»),
        двойник пересекается с двойником прежней подсети слота («clash» —
        видна только первая)."""
        kept: list[tuple[str, str]] = []
        issues: list[dict] = []
        sid = int(gw.id)
        for raw in gw.lan_subnets or []:
            try:
                net = ipaddress.IPv4Network(str(raw), strict=False)
            except ValueError:
                continue
            if sid > lanspace.MAX_SLOTS:
                continue
            if lanspace.too_wide(net):
                issues.append({"kind": "wide", "net": str(net)})
                continue
            if lanspace.in_space(net):
                issues.append({"kind": "space", "net": str(net), "space": str(lanspace.space())})
                continue
            other = lanspace.twin_clash(sid, net, [r for r, _t in kept])
            if other:
                issues.append({"kind": "clash", "net": str(net), "twin": str(lanspace.twin_net(sid, net)),
                               "other": other, "other_twin": str(lanspace.twin_net(sid, other))})
                continue
            kept.append((str(net), str(lanspace.twin_net(sid, net))))
        return kept, issues

    def lan_map_entries(self) -> list:
        """Записи таблицы по всем слотам с подсетями: [Entry(слот, линк,
        настоящая, двойник)]. Пространство неисправно — пусто: половина
        пространства хуже, чем ни одного."""
        if self.lan_space_problem():
            return []
        out = []
        for gw in self.db.gateways():
            for real, twin in self._lan_split(gw)[0]:
                out.append(routing.lanmap.Entry(int(gw.id), gw.link_if, real, twin))
        return out

    def lan_twins(self, gw) -> list[tuple[str, str]]:
        """[(настоящая, в туннеле)] подсетей слота, которые стоят в таблице —
        для экранов; пространство неисправно — пусто."""
        if gw is None or self.lan_space_problem():
            return []
        return self._lan_split(gw)[0]

    def lan_space_card(self, gw) -> dict:
        """Для карточки слота: {'problem': причина неисправного пространства
        или '', 'issues': отброшенные подсети слота}."""
        problem = self.lan_space_problem()
        return {"problem": problem, "issues": [] if problem or gw is None else self._lan_split(gw)[1]}

    def lan_items_twinned(self, gw, items) -> list[dict]:
        """Записи SMB слота (настоящие адреса его сети, после чистки) — с
        адресами в туннеле; адрес вне подсетей таблицы — запись не идёт."""
        pairs = [(ipaddress.IPv4Network(r), t) for r, t in self.lan_twins(gw)]
        out: list[dict] = []
        for it in items or []:
            try:
                a = ipaddress.IPv4Address(str(it.get("a", "")).strip())
            except ValueError:
                continue
            if any(a in real for real, _t in pairs):
                out.append({**it, "a": str(lanspace.twin_addr(gw.id, a))})
        return out

    # ── сверка таблицы ───────────────────────────────────────────────────────

    def lan_map_ensure(self) -> bool:
        """Таблицы и правила слотов с подсетями — независимо от РФ-доступа
        (доступ админа к сетям от него не зависит), MSS-кламп в их линки и
        таблица awg_lan_map. Таблицу перевыставляет, только если текст
        разошёлся с поставленным или её нет в ядре. False — что-то не
        поставлено (причина — в журнале)."""
        entries = self.lan_map_entries()
        ok = True
        for sid, link in sorted({(e.slot, e.link) for e in entries}):
            try:
                routing.ensure_slot_policy(sid, link)
                routing.ensure_mss_clamp(link)
            except routing.RoutingError as e:
                ok = False
                log.warning("адреса в туннеле: политика слота %s (%s) не поставлена: %s", sid, link, e)
        sp = lanspace.space()
        prefixes = {e.slot: str(lanspace.slot_prefix(e.slot, sp)) for e in entries}
        text = routing.lanmap.render(entries, str(sp), prefixes)
        try:
            if text == (self.db.get_state(self._LANMAP_TEXT_KEY) or "") and (not text or routing.lanmap.present()):
                return ok
            if text:
                routing.lanmap.apply(text)
            else:
                routing.lanmap.remove()
        except routing.RoutingError as e:
            # тот же отказ каждые 10 тактов — в журнал один раз, до смены причины
            if getattr(self, "_lanmap_err", "") != str(e):
                log.warning("адреса в туннеле: таблица %s не поставлена: %s", routing.lanmap.TABLE, e)
            self._lanmap_err = str(e)
            return False
        self._lanmap_err = ""
        self.db.set_state(self._LANMAP_TEXT_KEY, text)
        log.info("адреса в туннеле: таблица %s — %s", routing.lanmap.TABLE,
                 f"подсетей {len(entries)}" if text else "снята")
        return ok

    def lan_map_tick(self) -> None:
        """Такт живости: первый и каждый 10-й — сверка таблицы (дешёвая: текст
        против state и список таблиц ядра); события — правка подсетей, снятие
        слота, старт — сверяют сами."""
        n = getattr(self, "_lanmap_tick", -1) + 1
        self._lanmap_tick = n
        if n % 10:
            return
        try:
            self.lan_map_ensure()
        except Exception as e:                            # noqa: BLE001
            log.warning("адреса в туннеле: сверка на такте: %s", e)

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
        """Старт бота: прежние маршруты настоящих подсетей — прочь, таблицы
        слотов и awg_lan_map — на место до первого такта, затем разовое
        уведомление после обновления."""
        self.lan_real_routes_drop()
        self.lan_map_ensure()
        return self.lan_space_announce()

    def lan_space_announce(self) -> list[Notification]:
        """Один раз после обновления: прежний тумблер связи подсетей был
        выключен, а слотов с VPN-транзитом два и больше — их сети теперь
        связаны без вопроса, и это надо сказать прямо."""
        from awgbot.domain.alerts import armer
        from awgbot.domain.notices.lanmap import lan_peers_joined
        if self.db.get_state(self._LAN_ANNOUNCED_KEY) == "1":
            return []
        transit = [g for g in self.db.gateways() if g.lan_mode and g.lan_subnets]
        was_off = not settings.get_bool(self._PEER_NETS_LEGACY_KEY, False)
        if not (was_off and len(transit) >= 2):
            self.db.set_state(self._LAN_ANNOUNCED_KEY, "1")
            return []
        return [Notification(config.ADMIN_ID, lan_peers_joined([self._gw_link(g) for g in transit]),
                             on_sent=armer(self.db, self._LAN_ANNOUNCED_KEY, "1"))]
