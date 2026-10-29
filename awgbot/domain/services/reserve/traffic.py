"""
traffic.py — часть пакета reserve (см. __init__): итоги трафика через резерв.
`rsv_traffic` — {"out": накопитель (мои у резерва), "in": накопитель (его у
меня)}; месячный накопитель — util/monthacc; закрытый месяц — в таблицу
`reserve_traffic_monthly`. «in» считает резерв по счётчикам клона в зоне
(раз в минуту, пока serving); «out» — основа по дельтам реплики против
снимка при возврате.
"""
from __future__ import annotations

import subprocess

from awgbot.util import monthacc, timeutil

from ..gateway_link.common import log


class ReserveTrafficMixin:
    def _traffic(self) -> dict:
        return self.rsv_get("rsv_traffic")

    def _traffic_put(self, direction: str, acc: dict) -> None:
        tr = self._traffic()
        prev = tr.get(direction) or {}
        if prev.get("month") and str(acc.get("month") or "") > str(prev["month"]) and (prev.get("rx") or prev.get("tx")):
            self.db.reserve_traffic_archive(str(prev["month"]), direction, int(prev.get("rx") or 0), int(prev.get("tx") or 0))
        tr[direction] = acc
        self.rsv_put("rsv_traffic", tr)

    def reserve_traffic_out_add(self, rx: int, tx: int) -> None:
        """Расход моих пользователей через резерв (дельты реплики) — в итог месяца."""
        month = timeutil.now().strftime("%Y-%m")
        acc = dict(self._traffic().get("out") or {})
        if str(acc.get("month") or "") != month:
            acc = {"month": month, "rx": 0, "tx": 0, "last_rx": 0, "last_tx": 0}
        acc["rx"] = int(acc.get("rx") or 0) + max(0, int(rx))
        acc["tx"] = int(acc.get("tx") or 0) + max(0, int(tx))
        self._traffic_put("out", acc)

    def reserve_traffic_in_tick(self) -> None:
        """Раз в минуту при serving: счётчики клона в зоне → накопитель «in»."""
        if self.reserve_in() not in ("serving", "taking"):
            return
        rx, tx = self._clone_counters()
        if rx is None:
            return
        acc = monthacc.account(self._traffic().get("in"), rx, tx, timeutil.now().strftime("%Y-%m"))
        self._traffic_put("in", acc)

    def reserve_traffic_in_close(self) -> None:
        """Возврат: последний замер и сброс прошлых значений счётчика (клон уходит)."""
        self.reserve_traffic_in_tick()
        acc = dict(self._traffic().get("in") or {})
        if acc:
            acc["last_rx"], acc["last_tx"] = 0, 0
            self._traffic_put("in", acc)

    def _clone_counters(self) -> tuple[int | None, int | None]:
        from awgbot.infra import rsvzone
        from awgbot.util import netns
        st = rsvzone.state()
        ifaces = str(st.get("ifaces") or st.get("iface") or "").split()
        if not ifaces:
            return None, None
        from awgbot.util import probenet
        rx = tx = 0
        for iface in ifaces:                                  # все клоны (окно переезда профилей — два)
            try:
                proc = netns.run_in(netns.path_of(rsvzone.ZONE), ["awg", "show", iface, "dump"], timeout=10)
            except (OSError, subprocess.SubprocessError) as e:
                log.info("резерв: счётчики клона не прочитаны: %s", e)
                return None, None
            if proc.returncode != 0:
                return None, None
            # дамп: pub psk endpoint allowed_ips hs rx tx …; пиры-зонды кворума (адрес из блока) — не трафик
            # пользователей; строка интерфейса (первая) и пиры без рукопожатия (короткий формат) — мимо
            for line in proc.stdout.decode(errors="replace").splitlines():
                f = line.split("\t")
                if len(f) >= 7 and f[4].isdigit() and f[5].isdigit() and f[6].isdigit():
                    if probenet.is_probe(f[3].split(",")[0]):
                        continue
                    rx += int(f[5])
                    tx += int(f[6])
                elif len(f) == 3 and f[1].isdigit() and f[2].isdigit():
                    rx += int(f[1])                           # формат `transfer` (pub rx tx) — без разбора зондов
                    tx += int(f[2])
        return rx, tx
