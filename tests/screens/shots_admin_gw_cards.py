"""Снимки админа: карточка слота — свои списки, SMB соседей и «🗂 SMB в туннеле», рецепт роутера."""
from __future__ import annotations

from awgbot.bot.callbacks import GwSlotCB
from awgbot.core import config

from tests.screens.base import Shot
from tests.screens.shots_admin_common_gw import LAN1, LAN2, PI4, _card, _gw


# свои списки: слот 1 прислал 5 доменов в туннель и один напрямую, слот 2
# на связи и умеет синхронизацию — канон уходит ему

def _own_sent(services, mp):
    services.gwlink_own_in(1, "rx", [[i + 1, f"d{i}.com", "vpn", False] for i in range(5)]
                           + [[6, "shop.ru", "ru", False]])
    services.gwlink_session_opened(2, "3.1.0", 2)
    services.gwlink_own_hello_in(2, True)


def _own_ack(ok: bool = True, error: str = ""):
    def step(services, mp):
        digest = services.gwlink_own_for(services.db.gateway(2))[0]
        body = {"ok": True, "hash": digest, "n": 6} if ok else {"ok": False, "hash": digest, "error": error}
        services.gwlink_own_ack_in(2, body)
    return step


def _rename_pi4(services, mp):
    services.rename_device(PI4, "Pi & <2>")


def _own_old_agent(services, mp):
    services.gwlink_session_opened(2, "3.0.2", 2)
    services.gwlink_own_hello_in(2, False)
    services.set_gw_bot_identity(2, "pi2_gw_bot", "Шлюз <2> & co")


def _same_lan(services, mp):
    services.db.gateway_update(2, lan_subnets=[LAN1])


def _no_transit(slot: int):
    def step(services, mp):
        services.db.gateway_update(slot, lan_mode=0)
    return step


def _lan_old(slot: int, nets: list):
    """Подсети слота, заведённые до 3.3.0, которые ввод теперь не примет."""
    def step(services, mp):
        services.db.gateway_update(slot, lan_subnets=nets)
    return step


def _many_transit(n: int):
    """Слоты 3..n с VPN-транзитом и своими подсетями: соседей у слота 2 больше трёх."""
    def step(services, mp):
        from tests.screens.shots_admin_common import ADM
        for slot in range(3, n + 1):
            created = services.add_device(ADM, f"Pi{slot}")
            services.db.gateway_add(created.device_id, f"awglink{slot}", 9000 + slot,
                                    f"10.99.99.{4 * (slot - 1)}/30", slot_id=slot)
            services.db.gateway_update(slot, lan_subnets=[f"192.168.{slot}.0/24"], lan_mode=1)
    return step


def _table_failed(services, mp):
    """nft не принял таблицу адресов в туннеле — причина в state."""
    services.db.set_state("lanmap_error", "nft -c -f -: Error: Could not process rule: No such file or directory")


def _space_off(services, mp):
    """Служебное пространство адресов в туннеле задевает подсеть клиентов."""
    mp.setattr(config, "ROUTING_LAN_SPACE", "10.0.0.0/12")


def _own_off(services, mp):
    services.gwlink_own_card = lambda gw: {"vpn": 0, "ru": 0, "state": "off", "error": ""}


# SMB соседей: слот 1 назвал сервер и ушёл со связи; слот 2 прислал снимок
# с тем, что у него стоит (peers — подсети соседей, уже применённые: префикс
# слота 1 в туннеле)

_NAS = {"t": "_smb._tcp", "n": "NASPi5", "h": "naspi5", "p": 445, "a": "192.168.1.10"}


def _smb_sent(peers: str = "10.240.0.0/16"):
    def step(services, mp):
        services.gwlink_services_in(1, [_NAS])
        services.gwlink_session_closed(1)
        services.gwlink_snapshot_in(2, {"bundle": {"lan_mode": "1", "home_subnets": LAN2, "resolver": "10.9.1.1",
                                                   "peer_home_nets": peers, "admin_ips": "10.8.1.2"},
                                        "agent_version": "3.1.0", "link_contract": "1", "rev": 1}, 1, True)
    return step


def _smb_ack(ok: bool = True, error: str = ""):
    def step(services, mp):
        from awgbot.domain import gwservices
        h = gwservices.feed_hash([{**_NAS, "a": "10.240.1.10"}])     # у получателя — адрес в туннеле
        services.gwlink_peer_services_ack_in(2, {"ok": True, "hash": h, "n": 1} if ok
                                             else {"ok": False, "hash": h, "error": error})
    return step


CARDS = [
    # ── свои списки в карточке слота ─────────────────────────────────────
    _card(2, _gw(2, _own_sent, lan2=True), "свои списки: канон уходит на шлюз (домены — только числом)",
          "adm.gw.card.own.pending"),
    _card(2, _gw(2, _own_sent, _own_ack(), lan2=True),
          "свои списки применены — строки судьбы нет, под ними связь подсетей", "adm.gw.card.own.applied"),
    _card(2, _gw(2, _own_sent, _own_ack(False, "<b>dnsmasq</b> & rc=1"), lan2=True),
          "свои списки: отказ шлюза, ошибка экранирована", "adm.gw.card.own.refused"),
    _card(2, _gw(2, _own_sent, _own_ack(False, ""), lan2=True),
          "свои списки: отказ без объяснения — без двоеточия", "adm.gw.card.own.refused.bare"),
    _card(2, _gw(2, _own_sent, _own_ack(False, "ошибка записи файла: нет места на диске"), lan2=True),
          "свои списки: поломка на шлюзе — не «не смог принять»", "adm.gw.card.own.broken"),
    _card(2, _gw(2, _rename_pi4, _own_sent, _own_ack(False, "ошибка записи файла: <i>&"), lan2=True),
          "свои списки: имя шлюза и текст поломки экранированы ровно раз", "adm.gw.card.own.broken.escaped"),
    _card(2, _gw(2, _own_old_agent, lan2=True),
          "свои списки: агент без синхронизации — обновить, ссылка на бота", "adm.gw.card.own.old_agent"),
    _card(2, _gw(2, lan2=True),
          "связь подсетей включена: под строкой судьбы списков — «↔️ Связь подсетей ✅», SMB не найдены",
          "adm.gw.card.peers"),
    _card(2, _gw(2, _same_lan, lan2=True),
          "одинаковые подсети у двух слотов — не пересечение: строки предупреждения нет", "adm.gw.card.same_lan"),
    # ── адреса в туннеле: подсети, не попавшие в таблицу, и пространство ────
    _card(2, _gw(2, _lan_old(2, [LAN2, "10.241.0.0/24"]), lan2=True),
          "подсеть в служебных адресах сервера — в туннеле не видна", "adm.gw.card.lan.space"),
    _card(2, _gw(2, _lan_old(2, ["192.168.0.0/22", "10.0.2.0/24"]), lan2=True),
          "адреса в туннеле двух подсетей слота пересекаются — видна первая", "adm.gw.card.lan.clash"),
    _card(2, _gw(2, _space_off, lan2=True),
          "служебное пространство неисправно — адреса в туннеле выключены", "adm.gw.card.lan.off"),
    _card(1, _gw(2, _lan_old(1, [LAN1, "10.241.0.0/24"]), lan2=True),
          "подсеть в служебных адресах накрывает префикс соседа — конфиг линка не соберётся",
          "adm.gw.card.lan.blocks_link"),
    _card(2, _gw(2, _table_failed, lan2=True),
          "таблица адресов в туннеле не поставилась — причина строкой", "adm.gw.card.lan.failed"),
    _card(2, _gw(2, _lan_old(2, [LAN2, "10.241.0.0/24", "10.241.1.0/24", "10.242.0.0/24", "10.243.0.0/24",
                                 "10.244.0.0/24"]), lan2=True),
          "больше трёх подсетей вне таблицы — три строки и «Ещё N»", "adm.gw.card.lan.issues.more"),
    _card(2, _gw(2, _many_transit(5), lan2=True),
          "соседей с VPN-транзитом больше трёх — «и ещё N»", "adm.gw.card.peers.more"),
    _card(2, _gw(2, _own_off, lan2=True),
          "синхронизация у слота не действует — строки «📋» нет", "adm.gw.card.own.off"),
    # ── SMB соседей в карточке слота ─────────────────────────────────────
    _card(2, _gw(2, _smb_sent(), lan2=True), "SMB извне: записи уходят на шлюз",
          "adm.gw.card.smb.pending"),
    _card(2, _gw(2, _smb_sent(), _smb_ack(), lan2=True), "SMB извне: на шлюзе, доступны",
          "adm.gw.card.smb.ok"),
    _card(2, _gw(2, _smb_sent(), _smb_ack(False, "<b>dnsmasq</b> & rc=1"), lan2=True),
          "SMB извне: отказ шлюза, ошибка экранирована", "adm.gw.card.smb.refused"),
    _card(1, _gw(2, _smb_sent(), _smb_ack(False, "<b>dnsmasq</b> & rc=1"), lan2=True),
          "SMB у публикующего: только «свои», о судьбе — ничего", "adm.gw.card.smb.own"),
    _card(2, _gw(2, _smb_sent(), _smb_ack(False, ""), lan2=True),
          "SMB извне: отказ без объяснения — без двоеточия", "adm.gw.card.smb.refused.bare"),
    _card(2, _gw(2, _smb_sent(""), lan2=True),
          "SMB извне: подсети соседей на шлюзе не применены — нужен перевыпуск", "adm.gw.card.smb.reissue"),
    _card(2, _gw(2, _rename_pi4, _smb_sent(), _smb_ack(False, ""), lan2=True),
          "SMB извне: имя шлюза в заголовке и в отказе экранировано ровно раз", "adm.gw.card.smb.escaped"),
    _card(2, _gw(2, _smb_sent(), _smb_ack(False, "ошибка записи файла: нет места на диске"), lan2=True), "SMB извне: поломка на шлюзе — не «не смог принять»", "adm.gw.card.smb.broken"),
    _card(2, _gw(2, _rename_pi4, _smb_sent(), _smb_ack(False, "ошибка записи файла: <b>&</b>"), lan2=True), "SMB извне: имя и текст поломки экранированы", "adm.gw.card.smb.broken.escaped"),
    _card(2, _gw(2, _smb_sent(), _no_transit(1), lan2=True), "у соседа нет VPN-транзита — связи подсетей и строки SMB нет",
          "adm.gw.card.smb.off"),
    # ── рецепт роутера при связанных подсетях: о соседях ни строки ───────
    Shot("adm.gw.router.peers", role="admin", press=[GwSlotCB(action="router", slot=1)],
         data=_gw(2, lan2=True), title="сети связаны — рецепт без маршрутов к соседям и абзаца о самом роутере"),
    Shot("adm.gw.router.peers.ow", role="admin", press=[GwSlotCB(action="router", slot=1, val="ow")],
         data=_gw(2, lan2=True), title="OpenWrt: сети связаны — рецепт без маршрутов к соседям"),
]


# ── «🗂 SMB в туннеле»: тумблер в «🛰 Шлюзы», диалоги, итоги, отказ, карточка ──
# Резолвер админа «поставлен» подменой admindns.installed, его скрипт — пустышка;
# файервол включён фейком conftest. Тумблер включён — conf снимка.

_SMB_ON = {"app.routing.smb_tunnel": True}
_NAS2 = {"t": "_smb._tcp", "n": "Backup", "h": "backup", "p": 445, "a": "192.168.68.20"}


def _smb_host(resolver: bool = True, firewall: bool = True):
    def step(services, mp):
        from awgbot.infra import admindns, nftguard
        from awgbot.infra import resolver as client_resolver
        mp.setattr(client_resolver, "installed", lambda: True)
        mp.setattr(admindns, "installed", lambda: resolver)
        mp.setattr(admindns, "service_active", lambda: True)
        mp.setattr(admindns, "run", lambda mode, arg="": "")
        if not firewall:
            mp.setattr(nftguard, "enabled", lambda: False)
    return step


def _svc2(services, mp):
    """Слот 2 на связи назвал свой SMB-сервер."""
    services.gwlink_services_in(2, [_NAS2])
    services.gwlink_session_opened(2, "3.1.0", 2)


SMB = [
    Shot("adm.gw.smb_ask", role="admin", press=[GwSlotCB(action="smb_ask")], data=_gw(1, _smb_host()),
         title="SMB в туннеле — включить?"),
    Shot("adm.gw.smb_yes", role="admin", press=[GwSlotCB(action="smb_yes", val="1")], data=_gw(1, _smb_host()),
         title="SMB в туннеле включён — итог первой строкой, тумблер ✅ (один слот)"),
    Shot("adm.gw.smb_ask.off", role="admin", press=[GwSlotCB(action="smb_ask")], data=_gw(1, _smb_host()),
         conf=_SMB_ON, title="SMB в туннеле — выключить?"),
    Shot("adm.gw.smb_yes.off", role="admin", press=[GwSlotCB(action="smb_yes", val="0")],
         data=_gw(1, _smb_host()), conf=_SMB_ON, title="SMB в туннеле выключен — итог первой строкой"),
    Shot("adm.gw.smb_ask.noresolver", role="admin", press=[GwSlotCB(action="smb_ask")],
         data=_gw(1, _smb_host(resolver=False)), title="включить без резолвера админа — всплывашка"),
    Shot("adm.gw.smb_ask.nofw", role="admin", press=[GwSlotCB(action="smb_ask")],
         data=_gw(1, _smb_host(firewall=False)), title="включить без фильтра снаружи — всплывашка"),
    Shot("adm.gw.list.smb.two", role="admin", press=[GwSlotCB(action="list")], data=_gw(2, _smb_host()),
         conf=_SMB_ON, title="два слота: ✅ SMB в туннеле под автопереключением и связью подсетей"),
    Shot("adm.gw.card.smb.tunnel", role="admin", press=[GwSlotCB(action="card", slot=1)],
         data=_gw(2, _smb_host(), _smb_sent(), lan2=True), conf=_SMB_ON,
         title="карточка: свои SMB — «в туннеле ✅»"),
    Shot("adm.gw.card.smb.tunnel.peers", role="admin", press=[GwSlotCB(action="card", slot=2)],
         data=_gw(2, _smb_host(), _smb_sent(), _smb_ack(), _svc2, lan2=True), conf=_SMB_ON,
         title="карточка: свои и извне, доступны — «в туннеле ✅» в конце строки"),
]


def _svc1(services, mp):
    """Слот 1 (192.168.1.0/24) на связи назвал свой SMB-сервер."""
    services.gateway_set_lan_subnets(1, LAN1)
    services.gwlink_services_in(1, [_NAS])
    services.gwlink_session_opened(1, "3.1.0", 2)


def _smb_records_fail(services, mp):
    from awgbot.infra import admindns

    def run(mode, arg=""):
        raise admindns.AdminDnsError("подставной отказ")
    mp.setattr(admindns, "run", run)


SMB_ONE = [
    Shot("adm.gw.card.smb.tunnel.one", role="admin", press=[GwSlotCB(action="card", slot=1)],
         data=_gw(1, _smb_host(), _svc1), conf=_SMB_ON,
         title="один шлюз, без связи подсетей: «🗂 SMB: свои — 1 · в туннеле ✅»"),
    Shot("adm.gw.smb_yes.records_failed", role="admin", press=[GwSlotCB(action="smb_yes", val="1")],
         data=_gw(1, _smb_host(), _smb_records_fail), title="включён, но записи не встали — итог об этом"),
]
