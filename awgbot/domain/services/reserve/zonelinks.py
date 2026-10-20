"""
zonelinks.py — часть пакета reserve (см. __init__): стороны постоянных
линков шлюзов основы в гостевой зоне — сторона резерва. По `slots` основы
(открытый ключ шлюзовой стороны и PSK на слот) резерв заводит сторону зоны:
свои ключи, порт, /30 из `reserve.rsv_cidrs` (зона — первый адрес, шлюз —
второй), обфускацию; пишет конфиг в `reserve/links/awglink<N>.conf` (его
поднимает скрипт зоны `--shell`), держит параметры в `rsv_guest_links` и
отвечает `slots_ok`. Порты сторон — в файервол хоста (UDP-сокет у хоста).
"""
from __future__ import annotations

import ipaddress
import os
import secrets

from awgbot.core import config, settings
from awgbot.util import timeutil, wgkeys

from ..gateway_link.common import log
from .state import _LOCK

PORT_LO, PORT_HI = 40000, 60000


def links_dir() -> str:
    return os.path.join(str(config.DATA_DIR), "reserve", "links")


def _link_name(slot: int) -> str:
    return "awglink" if int(slot) == 1 else f"awglink{int(slot)}"


class ReserveZoneLinksMixin:
    def reserve_guest_links(self) -> dict:
        return self.rsv_get("rsv_guest_links")

    def _rsv_cidr_for(self, slot: int) -> str:
        cidrs = settings.get("app.reserve.rsv_cidrs", ["10.99.97.0/30", "10.99.97.4/30"]) or []
        if not isinstance(cidrs, list):
            cidrs = []
        i = int(slot) - 1
        if 0 <= i < len(cidrs):
            return str(cidrs[i])
        base = ipaddress.ip_network(str(cidrs[0]) if cidrs else "10.99.97.0/30", strict=False)
        return str(ipaddress.ip_network((int(base.network_address) + 4 * i, 30)))

    def _free_port(self, taken: set[int]) -> int:
        from awgbot.infra import nftguard
        busy = set(nftguard.listen_ports()) | taken
        for _ in range(200):
            p = PORT_LO + secrets.randbelow(PORT_HI - PORT_LO)
            if p not in busy:
                return p
        raise RuntimeError("свободный порт для стороны зоны не нашёлся")

    def reserve_slots_in(self, slots: list) -> dict:
        """`slots` основы → стороны зоны, конфиги, оболочка зоны; тело `slots_ok`."""
        from awgbot.infra import rsvzone
        out = []
        with _LOCK:
            links = self.reserve_guest_links()
            keep: set[str] = set()
            for item in slots or []:
                if not isinstance(item, dict):
                    continue
                slot = int(item.get("slot") or 0)
                gw_pub, psk = str(item.get("pub") or ""), str(item.get("psk") or "")
                if not 1 <= slot <= 253 or not gw_pub or not psk:
                    continue
                key = str(slot)
                keep.add(key)
                rec = links.get(key) or {}
                if not rec.get("priv"):
                    priv, pub = wgkeys.keypair()
                    rec = {"priv": priv, "pub": pub, "port": self._free_port({int(r.get("port") or 0) for r in links.values()}),
                           "cidr": self._rsv_cidr_for(slot), "obf": wgkeys.obfuscation(), "created_at": timeutil.now_iso()}
                rec.update(gw_pub=gw_pub, psk=psk, name=_link_name(slot))
                links[key] = rec
                self._write_zone_link(slot, rec)
                from ..selfhost import own_host
                out.append({"slot": slot, "pub": rec["pub"], "port": int(rec["port"]), "host": own_host(),
                            "cidr": rec["cidr"], "obf": rec["obf"], "chan": self._guest_channel_port()})
            for key in list(links):
                if key not in keep:
                    self._remove_zone_link(int(key), links.pop(key))
            self.rsv_put("rsv_guest_links", links)
        if not out and not links:
            return {"slots": []}
        try:
            rsvzone.shell(self._guest_channel_port())
        except rsvzone.ZoneError as e:
            log.warning("резерв: оболочка зоны не поднялась: %s", e)
        try:
            self._gw_firewall_refresh()
        except Exception as e:                                # noqa: BLE001
            log.warning("резерв: файервол после сторон зоны не перерисован: %s", e)
        log.info("резерв: стороны зоны для слотов основы: %s", ", ".join(str(s["slot"]) for s in out) or "нет")
        return {"slots": out}

    def reserve_zone_shell_ensure(self) -> bool:
        """Зона-оболочка есть, пока есть стороны линков (после перезагрузки хоста
        её никто не поднимал: пространство — не на диске). True — есть."""
        from awgbot.infra import rsvzone
        from awgbot.util import netns
        if not self.reserve_paired() or not self.reserve_guest_links():
            return False
        if netns.exists(netns.path_of(rsvzone.ZONE)):
            return True
        try:
            rsvzone.shell(self._guest_channel_port())
        except rsvzone.ZoneError as e:
            log.warning("резерв: оболочка зоны не поднялась: %s", e)
            return False
        try:
            self._gw_firewall_refresh()
        except Exception as e:                                # noqa: BLE001
            log.warning("резерв: файервол после подъёма зоны не перерисован: %s", e)
        log.info("резерв: оболочка зоны поднята заново (стороны линков: %s)",
                 ", ".join(k for k in self.reserve_guest_links() if k.isdigit()))
        return True

    @staticmethod
    def _guest_channel_port() -> int:
        return int(settings.get_int("app.routing.link_channel_port", 8787) or 8787)

    def _write_zone_link(self, slot: int, rec: dict) -> None:
        from awgbot.infra import rsvzone
        hosts = list(ipaddress.ip_network(rec["cidr"], strict=False).hosts())
        zone_addr, gw_addr = str(hosts[0]), str(hosts[1])
        ka = str(settings.get("app.client_config.keepalive_seconds", config.KEEPALIVE_SECONDS) or "25-35")
        lines = ["[Interface]", f"Address = {zone_addr}/30", f"ListenPort = {int(rec['port'])}", f"PrivateKey = {rec['priv']}"]
        for k in wgkeys.OBF_KEYS:
            if rec.get("obf", {}).get(k):
                lines.append(f"{k} = {rec['obf'][k]}")
        # единственный пир стороны зоны — шлюз: туда уходит и РФ-трафик клиентов основы
        # наружу (таблица РФ-доступа зоны ведёт в этот линк), обратно — ответы
        lines += ["", "[Peer]", f"PublicKey = {rec['gw_pub']}", f"PresharedKey = {rec['psk']}",
                  "AllowedIPs = 0.0.0.0/0", f"PersistentKeepalive = {ka}", ""]
        del gw_addr
        os.makedirs(links_dir(), mode=0o700, exist_ok=True)
        from pathlib import Path
        rsvzone.write_private(Path(links_dir()) / f"{_link_name(slot)}.conf", "\n".join(lines))

    def _remove_zone_link(self, slot: int, rec: dict) -> None:
        try:
            os.unlink(os.path.join(links_dir(), f"{_link_name(slot)}.conf"))
        except OSError:
            pass
        from awgbot.util import netns
        from awgbot.infra import rsvzone
        try:
            netns.run_in(netns.path_of(rsvzone.ZONE), ["ip", "link", "del", _link_name(slot)], timeout=10)
        except Exception:                                     # noqa: BLE001
            pass

    def reserve_zone_links_drop(self) -> None:
        """Удаление пары: стороны зоны и их конфиги — прочь."""
        for key, rec in list(self.reserve_guest_links().items()):
            if key.isdigit():
                self._remove_zone_link(int(key), rec)
        self.rsv_put("rsv_guest_links", None)

    def reserve_zone_link_ports(self) -> list[int]:
        """Порты сторон зоны — открыть во входе хоста (сокеты у хоста)."""
        return [int(r.get("port") or 0) for r in self.reserve_guest_links().values() if int(r.get("port") or 0)]

    def reserve_zone_link_addr(self, slot: int) -> str:
        rec = self.reserve_guest_links().get(str(int(slot))) or {}
        try:
            return str(list(ipaddress.ip_network(rec["cidr"], strict=False).hosts())[0])
        except (KeyError, ValueError, IndexError):
            return ""

    def reserve_zone_link_rx(self, slot: int) -> int | None:
        """Счётчик rx стороны зоны слота — живость шлюза для резерва."""
        from awgbot.util import netns
        from awgbot.infra import rsvzone
        try:
            proc = netns.run_in(netns.path_of(rsvzone.ZONE), ["awg", "show", _link_name(slot), "transfer"], timeout=5)
        except Exception:                                     # noqa: BLE001
            return None
        if proc.returncode != 0:
            return None
        total = 0
        for line in proc.stdout.decode(errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                total += int(parts[1])
        return total
