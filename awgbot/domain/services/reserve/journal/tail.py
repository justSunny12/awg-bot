"""
tail.py — часть пакета journal (см. __init__): хвост старой эпохи.

Бывший держатель, узнав, что эпоха ушла (следование после аварийного
открытия у основы; `jstale` у бота основы на резерве после «вернуть без
резерва»), переносит свои неподтверждённые события в `jrn_tail` (infra/db
jrn) и шлёт их держателю (`jtail`, частями, под `K`; повтор раз в
TAIL_RESEND_S до ответа). Держатель перебазирует (rsvjrn.rebase): перенесённое
становится событиями его эпохи и идёт второй стороне обычным порядком, пиры и
блокировки — по итогу. Ответ — `jtail_ok`: числа; если перебазировал бот
основы на резерве (хвост основы после аварии), ещё полный список спорного под
`K` — Т-Ж4 и снимок полного списка делает только основа: база гостя уходит
при `--wipe`.

Уведомления Т-Ж4 (спорное) и Т-Ж5 (всё перенесено) шлёт всегда бот основы:
хвост бота на резерве (С9) — сразу после перебазирования; свой хвост после
аварии — после возврата, отдельным сообщением сразу за Т-РУ5б
(`reserve_tail_notes`, outbound_return `_return_done`). Пока хвост или правки
бота на резерве не пришли — обновление отказывает.
"""
from __future__ import annotations

import time

from awgbot.core import config
from awgbot.util import pairproto, timeutil

from ...gateway_link.common import log
from ...types import Notification, ServiceError
from .ship import EV_BYTES

TAIL_RESEND_S = 60
DISPUTED_MAX = 2000                # в jtail_ok — не больше; сверх — числом `more`, сами — строкой журнала
TAIL_WAIT_REFUSED = ("Жду правки бота на «{name}», сделанные после потери связи, — обновление после того, как "
                     "они придут")


class ReserveTailMixin:

    # ── отправка (бывший держатель) ─────────────────────────────────────────
    def reserve_tail_pending(self) -> bool:
        try:
            return bool(self.db.jrn_tail_rows())
        except Exception:                                     # noqa: BLE001 — база старше журнала
            return False

    def _tail_parts(self) -> list[dict]:
        """Хвост частями `jtail` (записи под K, как события)."""
        from awgbot.util.rsvjrn import event
        rows = self.db.jrn_tail_rows()
        if not rows:
            return []
        k, line, meta = self.reserve_k(), self.db.jrn_line(), self.db.jrn_meta()
        groups: list[list[bytes]] = [[]]
        size = 0
        for ev in rows:
            r = event.seal_event(k, line, ev)
            if groups[-1] and size + len(r) > EV_BYTES:
                groups.append([])
                size = 0
            groups[-1].append(r)
            size += len(r)
        e = max(ev.e for ev in rows)
        return [{"line": line, "e": e, "F": int(meta["fork_i"]), "part": n, "parts": len(groups),
                 "blob": b"".join(g).hex()} for n, g in enumerate(groups)]

    def reserve_tail_out(self) -> list[tuple]:
        """Такт: хвост держателю — когда он на связи; повтор, пока нет ответа."""
        if not self.reserve_tail_pending():
            return []
        now = time.monotonic()
        if now - float(self.__dict__.get("_tail_sent_at") or 0) < TAIL_RESEND_S:
            return []
        if getattr(self, "guest", False):
            send = "guest_send"                               # гость: по локальному каналу боту резерва
        else:
            peer = self.reserve_peer_state()
            if not self.reserve_online() or str(peer.get("inn") or "") not in ("taking", "serving", "releasing"):
                return []
            send = "send"
        parts = self._tail_parts()
        self.__dict__["_tail_sent_at"] = now
        self.db.jrn_tail_mark("sent")
        log.info("резерв: хвост эпохи %s — %d событий держателю (частей: %d)", parts[0]["e"],
                 len(self.db.jrn_tail_rows()), len(parts))
        return [(send, "jtail", p) for p in parts]

    def reserve_tail_finalize(self, fork: int) -> None:
        """Точка расхождения стала известна: хвост — откатить в базе и пиры за ним, к отправке."""
        try:
            if not self.db.jrn_tail_held():
                return
            n = self.db.jrn_tail_finalize(int(fork))
        except Exception as e:                                # noqa: BLE001
            log.warning("резерв: хвост не подготовлен: %s", e)
            return
        if n:
            self.reserve_tail_undo_peers()

    def reserve_tail_undo_peers(self) -> None:
        """После ухода в следование хвост откачен в базе — и пиры за ним: заведённые хвостом
        устройства — с интерфейса, удалённые — обратно, сменившие ключ — прежним ключом."""
        from awgbot.infra import awg
        for ev in reversed(self.db.jrn_tail_rows(("wait",))):
            if ev.t != "devices":
                continue
            c, o = ev.c or {}, ev.o or {}
            try:
                if ev.op == "I" and c.get("public_key"):
                    awg.remove_peer(str(c["public_key"]), awg.iface_of(c.get("iface")))
                elif ev.op == "D" and o.get("public_key") and o.get("address"):
                    awg.add_peer(str(o["public_key"]), str(o.get("preshared_key") or ""), str(o["address"]),
                                 awg.iface_of(o.get("iface")))
                elif ev.op == "U" and o.get("public_key") and c.get("public_key") != o.get("public_key"):
                    awg.remove_peer(str(c.get("public_key") or ""), awg.iface_of(c.get("iface")))
                    awg.add_peer(str(o["public_key"]), str(o.get("preshared_key") or ""), str(o.get("address") or ""),
                                 awg.iface_of(o.get("iface")))
            except Exception as e:                            # noqa: BLE001
                log.info("резерв: пир хвоста не откачен: %s", e)

    # ── приём (держатель) ───────────────────────────────────────────────────
    def reserve_tail_in(self, body: dict) -> list[tuple]:
        """Часть хвоста второй стороны: целиком — перебазировать, ответить `jtail_ok`;
        повтор уже перебазированного — тот же ответ."""
        from awgbot.util.rsvjrn import event
        line, e, fork = str(body["line"]), int(body["e"]), int(body["F"])
        part, parts = int(body["part"]), int(body["parts"])
        if not 0 <= part < parts <= pairproto.MAX_CHUNKS:
            return []
        meta = self.db.jrn_meta()
        if meta["role"] != "holder":
            log.info("резерв: хвост эпохи %s пришёл, а держатель не здесь — пропущен", e)
            return []
        key = f"{e}:{fork}"
        done = self.__dict__.setdefault("_tail_done", {})
        if key in done:
            return list(done[key])
        acc = self.__dict__.setdefault("_tail_acc", {}).setdefault(key, [None] * parts)
        if len(acc) != parts:
            acc = self.__dict__["_tail_acc"][key] = [None] * parts
        acc[part] = str(body["blob"])
        if any(p is None for p in acc):
            return []
        self.__dict__["_tail_acc"].pop(key, None)
        try:
            k = self.reserve_k()
            evs = [event.open_event(k, r, line) for r in event.split(bytes.fromhex("".join(acc)))]
        except (ValueError, event.RecordError) as ex:
            log.warning("резерв: хвост эпохи %s не открылся: %s", e, ex)
            return []
        before = self.db.jrn_last_i()
        rpt = self.db.jrn_rebase(evs)
        self._tail_side_effects(before)
        log.info("резерв: хвост эпохи %s: %d событий, перенесено %d, спорных %d", e, len(evs), rpt.moved, len(rpt.disputed))
        if rpt.disputed:
            log.info("резерв: хвост эпохи %s: спорные: %s", e, rpt.disputed)
        guest = bool(getattr(self, "guest", False))
        acts: list[tuple] = []
        reply = {"line": line, "e": e, "F": fork, "moved": rpt.moved, "disputed_n": len(rpt.disputed),
                 "more": max(0, len(rpt.disputed) - DISPUTED_MAX)}
        send = "guest_send" if guest else "send"
        if guest:
            # хвост основы — Т-Ж4 делает основа: ей полный список спорного под K
            from awgbot.util.rsvpkg import seal
            blob = seal.snapshot_seal(k, {"v": 1, "line": line, "e": e, "F": fork,
                                          "disputed": rpt.disputed[:DISPUTED_MAX], "more": reply["more"]}).hex()
            chunks = pairproto.chunks(blob)
            acts += [(send, "jtail_ok", {**reply, "part": n, "parts": len(chunks), "blob": c})
                     for n, c in enumerate(chunks)]
        else:
            acts.append((send, "jtail_ok", {**reply, "part": 0, "parts": 1, "blob": ""}))
            acts.append(("notify", self._tail_notes("c9", rpt.moved, rpt.disputed, len(rpt.disputed))))
        done[key] = [a for a in acts if a[0] != "notify"]
        return acts

    def _tail_side_effects(self, before: int) -> None:
        """Перенесённое хвостом — события моей эпохи после before: пиры и блокировки по ним."""
        from awgbot.infra import awg
        evs = self.db.jrn_events_after(before, 100000)
        blocks = False
        for ev in evs:
            if ev.t == "devices":
                self._follow_peer(awg, ev)
                self._follow_peer_gone(awg, ev)
            blocks = blocks or "block_reason" in (ev.c or {})
        if blocks:
            try:
                self.reconcile_blocks(True)
            except Exception as e:                            # noqa: BLE001
                log.warning("резерв: блокировки после хвоста не наложены: %s", e)

    # ── ответ (бывший держатель) ────────────────────────────────────────────
    def reserve_tail_ok_in(self, body: dict) -> list[tuple]:
        """`jtail_ok`: хвост перебазирован — снять его; у основы (хвост после аварии) —
        список спорного до отправки Т-Ж4 после возврата."""
        part, parts = int(body.get("part") or 0), int(body.get("parts") or 1)
        if not 0 <= part < parts <= pairproto.MAX_CHUNKS:
            return []
        if getattr(self, "guest", False):
            self.db.jrn_tail_drop()
            log.info("гость: хвост перенесён основой (%s), спорных %s", body.get("moved"), body.get("disputed_n"))
            return []
        key = f"ok:{body.get('e')}:{body.get('F')}"
        acc = self.__dict__.setdefault("_tail_acc", {}).setdefault(key, [None] * parts)
        if len(acc) != parts:
            acc = self.__dict__["_tail_acc"][key] = [None] * parts
        acc[part] = str(body.get("blob") or "")
        if any(p is None for p in acc):
            return []
        self.__dict__["_tail_acc"].pop(key, None)
        disputed: list[dict] = []
        more = int(body.get("more") or 0)
        blob = "".join(acc)
        if blob:
            from awgbot.util.rsvpkg import seal
            try:
                obj = seal.snapshot_open(self.reserve_k(), bytes.fromhex(blob))
            except (ValueError, seal.PackageError) as e:
                log.warning("резерв: список спорного хвоста не открылся: %s", e)
                obj = {}
            ok = isinstance(obj, dict) and str(obj.get("line") or "") == self.db.jrn_line() \
                and int(obj.get("e") or -1) == int(body.get("e") or -2) and int(obj.get("F") or -1) == int(body.get("F") or -2)
            if ok:
                disputed = [d for d in (obj.get("disputed") or []) if isinstance(d, dict)][:DISPUTED_MAX]
                more = int(obj.get("more") or 0)
            elif obj:
                log.warning("резерв: список спорного хвоста — не к этому хвосту, отброшен")
        self.rsv_update("rsv_out", tail_report={"variant": "crash", "moved": int(body.get("moved") or 0),
                                                "disputed": disputed, "total": int(body.get("disputed_n") or 0),
                                                "more": more, "at": timeutil.now_iso()})
        self.db.jrn_tail_drop()
        log.info("резерв: хвост этой базы перенесён на «%s»: %s, спорных %s", self.reserve_peer_name(),
                 body.get("moved"), body.get("disputed_n"))
        return []

    # ── уведомления (только основа) ─────────────────────────────────────────
    def _tail_notes(self, variant: str, moved: int, disputed: list[dict], total: int) -> list[Notification]:
        from awgbot.domain.notices import reserve as rn
        name = self.reserve_peer_name()
        if not disputed and not total:
            if not moved:
                return []
            return [Notification(tg_id=int(config.ADMIN_ID), text=rn.tail_moved(variant, name, moved), silent=True)]
        lines = [self._tail_line(d, variant) for d in disputed[:rn.TAIL_LINES_MAX]]
        return [Notification(tg_id=int(config.ADMIN_ID), text=rn.tail_disputed(variant, name, lines, max(total, len(disputed))),
                             silent=True)]

    def _tail_line(self, d: dict, variant: str) -> str:
        from awgbot.domain.notices import reserve as rn
        from awgbot.util.textfmt.base import profile_link
        import html
        bot = str(getattr(self, "bot_username", "") or "")
        client = None
        try:
            if d.get("client_id") is not None:
                client = self.db.get_client(int(d["client_id"]))
        except Exception:                                     # noqa: BLE001
            client = None
        profile = profile_link(client, bot) if client is not None else html.escape(str(d.get("name") or "?"), quote=False)
        device = ""
        if d.get("table") == "devices":
            device = html.escape(str(d.get("name") or ((d.get("tail") or {}).get("name") if isinstance(d.get("tail"), dict)
                                                         else "") or "?"), quote=False)
        taken = ""
        tb = d.get("taken_by") or {}
        if tb:
            owner = None
            try:
                owner = self.db.get_client(int(tb.get("client_id") or 0))
            except Exception:                                 # noqa: BLE001
                owner = None
            taken = f"«{html.escape(str(tb.get('name') or '?'), quote=False)}»" + \
                (f" профиля {profile_link(owner, bot)}" if owner is not None else "")
        return rn.tail_line(d, profile, device, taken, self.reserve_peer_name(), variant)

    def reserve_tail_notes(self) -> list[Notification]:
        """После возврата: Т-Ж4/Т-Ж5 по хвосту этой базы, перебазированному ботом на резерве."""
        rep = self.rsv_get("rsv_out").get("tail_report") or {}
        if not rep:
            return []
        self.rsv_update("rsv_out", tail_report=None)
        return self._tail_notes("crash", int(rep.get("moved") or 0), list(rep.get("disputed") or []),
                                int(rep.get("total") or 0))

    # ── отказ обновления ────────────────────────────────────────────────────
    def reserve_refuse_update_if_waiting(self) -> None:
        """Пока ждём правки бота на резерве («вернуть без резерва») или мой хвост не
        перенесён — обновление отказывает: выпуск без этого пути их бы не принял."""
        if not self.reserve_paired():
            return
        if self.rsv_get("rsv_out").get("await_merge") or self.reserve_tail_pending():
            raise ServiceError(TAIL_WAIT_REFUSED.format(name=self.reserve_peer_name()))
