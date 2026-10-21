"""
merge_ops.py — часть пакета reserve (см. __init__): слияние реплики при возврате
клиентов — выделено из outbound (автомат там, здесь — работа с базами).

`_return_merge`: реплика под K → `rep-<gen>.db`, база слияния `base-<gen>.db`
(финальная реплика передачи или снимок пакета; нет — копия живой базы и
слияние без удалений), трёхстороннее слияние (backup/merge/reserve), пиры
клона и блокировки по итогу, учёт трафика через резерв, отчёт Т-РУ5б.
"""
from __future__ import annotations

import json
import os

from awgbot.core import config
from awgbot.util.rsvpkg import seal

from ..types import ServiceError
from ..gateway_link.common import log


class ReserveMergeOpsMixin:
    def _return_merge(self, blob: bytes, gen: int) -> dict:
        from awgbot.domain.backup.merge import reserve as merge_mod
        from awgbot.infra import awg
        snap = seal.snapshot_open(self.reserve_k(), blob)
        if not isinstance(snap, dict) or not snap.get("db"):
            raise ServiceError("реплика не того вида")
        base_dir = os.path.join(str(config.DATA_DIR), "reserve", "out")
        snap_gen = int(snap.get("pkg_gen") or 0)             # поколение пакета, с которого стартовал гость
        if snap_gen and (not gen or not os.path.exists(os.path.join(base_dir, f"base-{gen}.db"))):
            gen = snap_gen
        base = os.path.join(base_dir, f"base-{gen}.db")
        no_base = not os.path.exists(base)
        if no_base:
            # снимка нет (основа восстановлена из копии, снимки в неё не входят): база слияния —
            # копия живой базы, удалённое на резерве не выводится, строки реплики — главнее
            log.warning("резерв: снимка base-%s.db нет — слияние без удалений по копии живой базы", gen)
            os.makedirs(base_dir, mode=0o700, exist_ok=True)
            base = os.path.join(base_dir, f"base-{gen}.fallback.db")
            self._copy_db(self._db_path(), base)
        rep = os.path.join(base_dir, f"rep-{gen}.db")
        with open(rep, "wb") as f:
            f.write(bytes.fromhex(str(snap["db"])))
        os.chmod(rep, 0o600)
        try:
            removed = [] if no_base else self._pubkeys_removed(base, rep)
            before = self._merge_watch_snapshot(base)
            rpt = merge_mod.merge(self._db_path(), base, rep, admin_tg=int(config.ADMIN_ID), no_delete=no_base)
            warnings = self._merge_watch_lines(before)
            if no_base:
                warnings.insert(0, "снимка для слияния не было — удалённое на резерве не выводилось")
            # пиры — до сверки: устройства с резерва должны попасть в конфиг раньше,
            # чем сверка посчитает их пропавшими
            added = 0
            conf_pubs: dict[str, set] = {}                   # пиры по каждому интерфейсу: двойники живут на втором
            for d in self.db.list_all_devices():
                if not d.public_key or getattr(d, "is_gateway", 0):
                    continue
                iface = awg.iface_of(d.iface)
                if iface not in conf_pubs:
                    try:
                        conf_pubs[iface] = {p["public_key"] for p in awg.show_dump(iface)}
                    except Exception as e:                    # noqa: BLE001 — интерфейса нет (окно переезда закрыто)
                        log.info("резерв: пиры %s не прочитаны: %s", iface, e)
                        conf_pubs[iface] = set()
                if d.public_key not in conf_pubs[iface]:
                    try:
                        awg.add_peer(d.public_key, d.preshared_key or "", d.address, awg.iface_of(d.iface))
                        added += 1
                    except Exception as e:                    # noqa: BLE001
                        log.warning("резерв: пир %s не добавлен: %s", d.name, e)
            for pub in removed:
                try:
                    awg.remove_peer(pub)
                except Exception as e:                        # noqa: BLE001
                    log.warning("резерв: пир снят с ошибкой: %s", e)
            try:
                self.reconcile_blocks(True)
            except Exception as e:                            # noqa: BLE001
                log.warning("резерв: блокировки после слияния: %s", e)
            self._return_traffic(base, rep)
            appended = getattr(rpt, "appended", {}) or {}
            report = {"clients": int(rpt.inserted.get("clients", 0)), "devices": int(rpt.inserted.get("devices", 0)),
                      "extensions": int(rpt.updated.get("client_subscription", 0)),
                      "blocks": int(appended.get("client_block_histories", 0)),   # записи журнала блокировок с резерва
                      "peers_added": added, "peers_removed": len(removed),
                      "lost": [self._lost_line(x) for x in rpt.lost[:5]] + warnings[:5]}
            log.info("резерв: слияние: %s", json.dumps(report, ensure_ascii=False))
            return report
        finally:
            for p in (rep, base if no_base else ""):
                try:
                    if p:
                        os.unlink(p)
                except OSError:
                    pass

    @staticmethod
    def _copy_db(src: str, dst: str) -> None:
        import sqlite3
        s_con = sqlite3.connect(src)
        try:
            d_con = sqlite3.connect(dst)
            try:
                s_con.backup(d_con)
            finally:
                d_con.close()
        finally:
            s_con.close()
        os.chmod(dst, 0o600)

    @staticmethod
    def _merge_watch_snapshot(base: str) -> dict:
        """Что в снимке стоило отдельного взгляда: Telegram у профилей, устройства
        профиля админа — правки этого с резерва не должны пройти незаметно."""
        import sqlite3
        con = sqlite3.connect(base)
        try:
            tg = {int(r[0]): (r[1], r[2]) for r in con.execute("SELECT id, tg_id, name FROM clients")}
            dev = {int(r[0]) for r in con.execute("SELECT id FROM devices")}
        except sqlite3.Error:
            return {}
        finally:
            con.close()
        return {"tg": tg, "dev": dev}

    def _merge_watch_lines(self, before: dict) -> list[str]:
        """Строки Т-РУ5б: «сменён Telegram у профиля …» (в снимке был непустой),
        «новое устройство у профиля админа …»."""
        if not before:
            return []
        lines: list[str] = []
        try:
            for c in self.db.list_clients() if hasattr(self.db, "list_clients") else []:
                old = before["tg"].get(int(c.id))
                if old and old[0] and c.tg_id != old[0]:
                    lines.append(f"⚠️ У профиля «{c.name}» на «{self.reserve_peer_name()}» сменён Telegram-аккаунт "
                                 f"({old[0]} → {c.tg_id}) — проверь, что это сделал ты")
            admin = self.db.get_client_by_tg(int(config.ADMIN_ID)) if hasattr(self.db, "get_client_by_tg") else None
            if admin is not None:
                for d in self.db.list_all_devices():
                    if int(getattr(d, "client_id", 0) or 0) == int(admin.id) and int(d.id) not in before["dev"]:
                        lines.append(f"⚠️ У профиля админа появилось устройство «{d.name}», заведённое на "
                                     f"«{self.reserve_peer_name()}» — проверь, что это твоё")
        except Exception as e:                                # noqa: BLE001
            log.info("резерв: сверка правок резерва не прошла: %s", e)
        return lines

    @staticmethod
    def _pubkeys_removed(base: str, rep: str) -> list[str]:
        import sqlite3
        def pubs(path):
            con = sqlite3.connect(path)
            try:
                return {r[0] for r in con.execute("SELECT public_key FROM devices WHERE public_key IS NOT NULL")}
            finally:
                con.close()
        return sorted(pubs(base) - pubs(rep))

    def _lost_line(self, lost: dict) -> str:
        from awgbot.domain.notices import reserve as rn
        return rn.lost_device(str(lost.get("name") or "?"), str(lost.get("owner") or "?"), str(lost.get("taken_by") or "?"),
                              str(lost.get("taken_owner") or "?"), self.reserve_peer_name())

    def _return_traffic(self, base: str, rep: str) -> None:
        """Расход через резерв: сумма дельт счётчиков реплики против снимка."""
        import sqlite3
        try:
            def totals(path):
                con = sqlite3.connect(path)
                try:
                    row = con.execute("SELECT COALESCE(SUM(traffic_rx_month),0), COALESCE(SUM(traffic_tx_month),0) "
                                      "FROM device_traffic").fetchone()
                    return int(row[0]), int(row[1])
                finally:
                    con.close()
            brx, btx = totals(base)
            rrx, rtx = totals(rep)
            self.reserve_traffic_out_add(max(0, rrx - brx), max(0, rtx - btx))
        except Exception as e:                                # noqa: BLE001
            log.info("резерв: расход через резерв не посчитан: %s", e)
