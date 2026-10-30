"""lanmap.py — адреса в туннеле для локальных сетей шлюзов: почему выключены
(пространство, метка), записи таблицы `inet awg_lan_map` из слотов, пары для
экранов, перевод адресов в записях SMB (схема и формулы — util/lanspace,
таблица — infra/routing/lanmap, сверка и старт — lanmap_sync).

Адреса в туннеле — доступ устройств админа и соседних сетей к локальным
сетям шлюзов, а не РФ-доступ: ни выключатель РФ-доступа (app.routing.enabled),
ни его рубильник (хук маркировки) их не выключают — они действуют, пока у
слотов есть подсети (решение 12.10.2026)."""

from __future__ import annotations

import ipaddress

from awgbot.core import config
from awgbot.infra import routing
from awgbot.util import lanspace


class LanMapMixin:
    """Адреса в туннеле для локальных сетей шлюзов."""

    _LANMAP_TEXT_KEY = "lanmap_text"                 # текст последней поставленной таблицы
    _LANMAP_ERROR_KEY = "lanmap_error"               # таблица не поставилась: причина (карточка, preflight)

    def lan_off(self, slots=None) -> tuple[str, str]:
        """(причина, ключ conf, которым её чинить), почему адреса в туннеле
        выключены целиком; ("", "") — включены. Служебное пространство
        задевает подсети пользователей (с подсетью переезда) или /30 линков, либо
        резолвер админа или адрес перехвата DNS стоят внутри него вне служебной
        /16 (routing.lan_space); или метка РФ-доступа задевает биты меток
        слотов (routing.fwmark) — пакет ушёл бы не в тот линк. Линки — и из
        настроек слотов, и заведённые в БД."""
        from awgbot.infra import admindns
        slots = self.db.gateways() if slots is None else list(slots)   # уже загруженные — без второго запроса
        clients = [net for net, _iface in config.routing_client_subnets()]
        links = list(config.ROUTING_LINK_CIDRS)
        for gw in slots:
            if gw.link_cidr and gw.link_cidr not in links:
                links.append(gw.link_cidr)
        why = lanspace.space_problem(lanspace.space(), clients, links,
                                     admindns.addr(), routing.DNS_INTERCEPT_ADDR)
        if why:
            return why, "routing.lan_space"
        mark = int(config.ROUTING_FWMARK)
        bits = 0
        for gw in slots:
            if 1 <= int(gw.id) <= lanspace.MAX_SLOTS:
                bits |= routing.slot_mark(gw.id)
        bits |= routing.slot_mark(29) | routing.slot_mark(30)   # виртуальные слоты: «шлюзы второго» и запасной зоны
        if mark & bits:
            return f"метка РФ-доступа 0x{mark:x} задевает биты меток слотов 0x{bits:x}", "routing.fwmark"
        return "", ""

    def lan_space_problem(self) -> str:
        """Причина, по которой адреса в туннеле выключены целиком, или "" (lan_off)."""
        return self.lan_off()[0]

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

    def lan_twins(self, gw, slots=None) -> list[tuple[str, str]]:
        """[(настоящая, в туннеле)] подсетей слота, которые стоят в таблице —
        для экранов и роли канала; пространство неисправно — пусто. slots —
        уже загруженные слоты: пространство проверяется по ним."""
        if gw is None or self.lan_off(slots)[0]:
            return []
        return self._lan_split(gw)[0]

    def lan_space_card(self, gw) -> dict:
        """Для карточки слота: {'problem': почему адреса в туннеле выключены
        или '', 'key': ключ conf для починки, 'error': отказ установки
        таблицы или '', 'issues': отброшенные подсети слота}. У отброшенной
        подсети шире /16 или в пространстве, накрывающей префикс соседа, —
        'blocks_link': на шлюзе она пересеклась бы с его AllowedIPs, и конфиг
        линка не соберётся, пока её не сменят."""
        problem, key = self.lan_off()
        error = self.db.get_state(self._LANMAP_ERROR_KEY) or ""
        if problem or gw is None:
            return {"problem": problem, "key": key, "error": "", "issues": []}
        issues = self._lan_split(gw)[1]
        peers = [ipaddress.IPv4Network(p) for p in self.gateway_peer_nets(gw.id)]
        for it in issues:
            if it["kind"] in ("space", "wide"):
                net = ipaddress.IPv4Network(it["net"])
                it["blocks_link"] = any(net.overlaps(p) for p in peers)
        return {"problem": "", "key": "", "error": error, "issues": issues}

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
