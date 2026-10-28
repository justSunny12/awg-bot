"""
tail.py — часть пакета journal (см. __init__): хвост старой эпохи.

Бывший держатель, узнав, что эпоха ушла (следование после аварийного
открытия у основы; `jstale` у бота основы на резерве после «вернуть без
резерва»), откладывает свои неподтверждённые события в `jrn_tail` (infra/db
jrn); когда точка расхождения F известна — откатывает их в базе и пирах
(`reserve_tail_finalize`) и шлёт держателю (`jtail`: записи вида «хвост» под
`K` частями, повтор раз в TAIL_RESEND_S до ответа).

Держатель принимает хвост, только пока он ему положен: F — его собственная
точка взятия эпохи (`jrn_meta.fork_i`), события — эпохи меньше его и с
номером после F, хвост в этой эпохе ещё не переносился. Перебазирует
(rsvjrn.rebase): перенесённое становится событиями его эпохи и идёт второй
стороне обычным порядком, пиры и блокировки — по итогу; отметка «перенесён»
(`rsv_tail_done`) пишется той же транзакцией — повтор хвоста получает тот же
ответ, а не второе перебазирование. Ответ — `jtail_ok`: числа; если
перебазировал бот основы на резерве (хвост основы после аварии), ещё полный
список спорного под `K` (добитый до ступени) — Т-Ж4 и снимок списка делает
только основа: база гостя уходит при `--wipe`. Бывший держатель снимает хвост
только по ответу на свой хвост (эпоха и F те же; у основы — с открывшимся
списком).

Уведомления Т-Ж4 (спорное) и Т-Ж5 (всё перенесено) шлёт всегда бот основы:
хвост бота на резерве (С9) — сразу после перебазирования; свой хвост после
аварии — после возврата, отдельным сообщением сразу за Т-РУ5б
(`reserve_tail_notes`). Пока свой хвост не перенесён — возврат и обновление
ждут (не дольше TAIL_WAIT_S: дальше хвост снимается строкой журнала); пока
ждём правки бота на резерве («вернуть без резерва») — обновление отказывает.
"""
from __future__ import annotations

import json
import time

from awgbot.core import config
from awgbot.util import pairproto, timeutil

from ...gateway_link.common import log
from ...types import Notification, ServiceError
from .ship import EV_BYTES, epoch_sane

TAIL_RESEND_S = 60
TAIL_WAIT_S = 10 * 60             # свой хвост без ответа держателя дольше — возврат не держит, хвост — строкой журнала
DISPUTED_MAX = 2000                # в jtail_ok — не больше; сверх — числом `more`, сами — строкой журнала
TAIL_WAIT_REFUSED = ("Жду правки бота на «{name}», сделанные после потери связи, — обновление после того, как "
                     "они придут")
OWN_TAIL_REFUSED = "Правки, сделанные здесь перед переездом на «{name}», ещё переносятся туда — {what} после этого"


class ReserveTailMixin:

    # ── бывший держатель: подготовка и отправка ─────────────────────────────
    def reserve_tail_pending(self) -> bool:
        """Свой хвост готов и не перенесён (отложенный до точки расхождения — не в счёт)."""
        try:
            return bool(self.db.jrn_tail_rows())
        except Exception:                                     # noqa: BLE001 — база старше журнала
            return False

    def reserve_tail_finalize(self, fork: int) -> None:
        """Точка расхождения стала известна: хвост — откатить в базе и пиры за ним, к отправке.
        F — не ниже подтверждённого и не дальше моего последнего номера старой эпохи (иначе
        это не моя точка: сломанный резерв или чужое слово)."""
        try:
            if not self.db.jrn_tail_held():
                return
            meta = self.db.jrn_meta()
            held = self.db.jrn_tail_rows(("hold",))
            top = max((ev.i for ev in held), default=0)
            fork = int(fork)
            try:                                              # у резерва моих номеров бывает больше (пропало питание)
                keep_last = int((self.reserve_peer_state().get("jrn_keep") or {}).get("last") or 0)
            except (TypeError, ValueError):
                keep_last = 0
            if not int(meta["acked"]) <= fork <= max(top, int(meta["acked"]), keep_last):
                log.warning("резерв: точка расхождения %s вне моих номеров (подтверждено %s, хвост по %s) — пропущена",
                            fork, meta["acked"], top)
                return
            n = self.db.jrn_tail_finalize(fork)
        except Exception as e:                                # noqa: BLE001
            log.warning("резерв: хвост не подготовлен: %s", e)
            return
        if n:
            e = max((ev.e for ev in held), default=0)
            self.rsv_put("rsv_tail", {"e": e, "F": fork, "at": timeutil.now_iso(), "mono": time.monotonic()})
            self.reserve_tail_undo_peers()

    def reserve_tail_undo_peers(self) -> None:
        """Хвост откачен в базе — и пиры за ним: заведённые хвостом устройства — с
        интерфейса, удалённые — обратно, сменившие ключ — прежним ключом."""
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

    def _tail_parts(self) -> list[dict]:
        """Хвост частями `jtail` (записи вида «хвост» под K); больше MAX_CHUNKS частей — первые."""
        from awgbot.util.rsvjrn import event
        rows = self.db.jrn_tail_rows()
        meta_t = self.rsv_get("rsv_tail")
        if not rows or meta_t.get("F") is None:
            return []
        k, line = self.reserve_k(), self.db.jrn_line()
        groups: list[list[bytes]] = [[]]
        size = 0
        for ev in rows:
            r = event.seal_event(k, line, ev, tail=True)
            if groups[-1] and size + len(r) > EV_BYTES:
                if len(groups) >= pairproto.MAX_CHUNKS:
                    log.error("резерв: хвост больше %d частей — уходит только начало", pairproto.MAX_CHUNKS)
                    break
                groups.append([])
                size = 0
            groups[-1].append(r)
            size += len(r)
        return [{"line": line, "e": int(meta_t.get("e") or 0), "F": int(meta_t["F"]), "part": n, "parts": len(groups),
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
        if not parts:
            return []
        self.__dict__["_tail_sent_at"] = now
        self.db.jrn_tail_mark("sent")
        log.info("резерв: хвост эпохи %s после %s — держателю (частей: %d)", parts[0]["e"], parts[0]["F"], len(parts))
        return [(send, "jtail", p) for p in parts]

    def reserve_tail_blocks(self) -> bool:
        """Свой хвост не перенесён и ждём не дольше TAIL_WAIT_S; дольше — не держит: после
        возврата основа перенесёт его сама (`reserve_tail_self`)."""
        if not self.reserve_tail_pending():
            return False
        meta_t = self.rsv_get("rsv_tail")
        try:
            return timeutil.elapsed(timeutil.parse_iso(str(meta_t.get("at") or ""))) <= TAIL_WAIT_S
        except ValueError:
            return False

    def reserve_tail_self(self) -> list[Notification]:
        """После возврата (держатель — здесь) свой хвост, который бот на резерве так и не
        принял, — перенести самой поверх своей базы; Т-Ж4/Т-Ж5 — как от него."""
        if not self.reserve_tail_pending() or self.db.jrn_meta()["role"] != "holder":
            return []
        evs = self.db.jrn_tail_rows()
        meta_t = self.rsv_get("rsv_tail")
        log.warning("резерв: хвост эпохи %s (%d событий) бот на «%s» не принял — переношу здесь",
                    meta_t.get("e"), len(evs), self.reserve_peer_name())
        before = self.db.jrn_last_i()
        try:
            rpt = self.db.jrn_rebase(evs)
        except Exception as ex:                                # noqa: BLE001
            log.error("резерв: свой хвост не перенесён: %s", ex)
            return []
        self._tail_side_effects(before)
        self.db.jrn_keys_restore()                            # вставки хвоста — без закрытого ключа: он отложен
        self.db.jrn_tail_drop()
        self.rsv_put("rsv_tail", None)
        if rpt.disputed:
            log.info("резерв: свой хвост: спорные: %s", rpt.disputed)
        if not rpt.disputed:
            return self._tail_notes("self", rpt.moved, [], 0)
        return self._tail_notes("crash", rpt.moved, rpt.disputed, len(rpt.disputed))

    # ── держатель: приём ────────────────────────────────────────────────────
    def _tail_expected(self, e: int, fork: int) -> str:
        """Пусто — хвост с эпохой e после F мне положен; иначе причина отказа."""
        meta = self.db.jrn_meta()
        if meta["role"] != "holder":
            return "держатель не здесь"
        if int(fork) != int(meta["fork_i"]):
            return f"точка {fork}, а моя — {meta['fork_i']}"
        if not (int(e) < int(meta["epoch"]) and epoch_sane(int(meta["epoch"]), int(e))):
            return f"эпоха хвоста {e} при моей {meta['epoch']}"
        return ""

    def reserve_tail_in(self, body: dict) -> list[tuple]:
        """Часть хвоста второй стороны: целиком — перебазировать, ответить `jtail_ok`;
        повтор уже перенесённого (в этой эпохе) — тот же ответ."""
        from awgbot.util.rsvjrn import event
        line, e, fork = str(body["line"]), int(body["e"]), int(body["F"])
        part, parts = int(body["part"]), int(body["parts"])
        if not 0 <= part < parts <= pairproto.MAX_CHUNKS:
            log.warning("резерв: часть хвоста %s из %s — вне пределов", part, parts)
            return []
        why = self._tail_expected(e, fork)
        if why:
            log.warning("резерв: хвост эпохи %s после %s не принят: %s", e, fork, why)
            return []
        meta = self.db.jrn_meta()
        done = self.rsv_get("rsv_tail_done")
        if done.get("epoch") is not None and int(done["epoch"]) == int(meta["epoch"]):
            # повтор: перенесён — тот же ответ (на последнюю часть повтора, не на каждую)
            return self._tail_reply(done) if part == parts - 1 else []
        key = [e, fork, parts]
        acc = self.__dict__.get("_tail_acc_in")
        if not acc or acc["key"] != key:
            acc = self.__dict__["_tail_acc_in"] = {"key": key, "parts": [None] * parts}   # одна сборка
        acc["parts"][part] = str(body["blob"])
        if any(p is None for p in acc["parts"]):
            return []
        self.__dict__.pop("_tail_acc_in", None)
        try:
            k = self.reserve_k()
            evs = [event.open_event(k, r, line, tail=True) for r in event.split(bytes.fromhex("".join(acc["parts"])))]
        except (ValueError, event.RecordError) as ex:
            log.warning("резерв: хвост эпохи %s не открылся: %s", e, ex)
            return []
        bad = [ev.i for ev in evs if ev.e >= int(meta["epoch"]) or ev.i <= fork]
        if bad:
            log.warning("резерв: хвост эпохи %s — события не хвоста (%s…) — не принят", e, bad[:5])
            return []
        before = self.db.jrn_last_i()
        guest = bool(getattr(self, "guest", False))
        try:
            rpt = self.db.jrn_rebase(evs, before_commit=lambda conn, r: self._tail_done_write(conn, e, fork, r, guest))
        except Exception as ex:                                # noqa: BLE001 — sqlite3.OperationalError и прочее
            log.error("резерв: хвост эпохи %s не перенесён: %s", e, ex)
            return []
        self._tail_side_effects(before)
        log.info("резерв: хвост эпохи %s: %d событий, перенесено %d, спорных %d", e, len(evs), rpt.moved, len(rpt.disputed))
        if rpt.disputed:
            log.info("резерв: хвост эпохи %s: спорные: %s", e, rpt.disputed)
        acts = self._tail_reply(self.rsv_get("rsv_tail_done"))
        if not guest:
            acts.append(("notify", self._tail_notes("c9", rpt.moved, rpt.disputed, len(rpt.disputed))))
        return acts

    def _tail_done_write(self, conn, e: int, fork: int, rpt, guest: bool) -> None:
        """В транзакции перебазирования: отметка «хвост этой эпохи перенесён» и ответ на повтор."""
        meta_e = int(conn.execute("SELECT epoch FROM jrn_meta WHERE id = 1").fetchone()[0])
        done = {"epoch": meta_e, "e": e, "F": fork, "moved": rpt.moved, "disputed_n": len(rpt.disputed),
                "more": max(0, len(rpt.disputed) - DISPUTED_MAX), "at": timeutil.now_iso()}
        if guest:
            done["disputed"] = rpt.disputed[:DISPUTED_MAX]     # основе — полным списком под K
        conn.execute("INSERT INTO server_state (key, value) VALUES ('rsv_tail_done', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(done, ensure_ascii=False),))

    def _tail_reply(self, done: dict) -> list[tuple]:
        from awgbot.util.rsvjrn import event
        guest = bool(getattr(self, "guest", False))
        send = "guest_send" if guest else "send"
        reply = {"line": self.db.jrn_line(), "e": int(done["e"]), "F": int(done["F"]), "moved": int(done["moved"]),
                 "disputed_n": int(done["disputed_n"]), "more": int(done.get("more") or 0)}
        if not guest:
            return [(send, "jtail_ok", {**reply, "part": 0, "parts": 1, "blob": ""})]
        blob = event.seal_obj(self.reserve_k(), {"v": 1, "line": reply["line"], "e": reply["e"], "F": reply["F"],
                                                 "disputed": list(done.get("disputed") or []), "more": reply["more"]}).hex()
        chunks = pairproto.chunks(blob)
        return [(send, "jtail_ok", {**reply, "part": n, "parts": len(chunks), "blob": c}) for n, c in enumerate(chunks)]

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

    # ── бывший держатель: ответ ─────────────────────────────────────────────
    def reserve_tail_ok_in(self, body: dict) -> list[tuple]:
        """`jtail_ok` на мой хвост (эпоха и F — те же): снять его; у основы (хвост после
        аварии) — только с открывшимся списком спорного, он — до Т-Ж4 после возврата."""
        from awgbot.util.rsvjrn import event
        part, parts = int(body.get("part") or 0), int(body.get("parts") or 1)
        if not 0 <= part < parts <= pairproto.MAX_CHUNKS:
            return []
        meta_t = self.rsv_get("rsv_tail")
        mine = (meta_t.get("e"), meta_t.get("F"))
        if not self.reserve_tail_pending() or mine != (body.get("e"), body.get("F")):
            log.info("резерв: ответ на хвост %s/%s — не на мой (%s/%s) — пропущен", body.get("e"), body.get("F"), *mine)
            return []
        if getattr(self, "guest", False):
            self.db.jrn_tail_drop()
            self.rsv_put("rsv_tail", None)
            log.info("гость: хвост перенесён основой (%s), спорных %s", body.get("moved"), body.get("disputed_n"))
            return []
        key = [body.get("e"), body.get("F"), parts]
        acc = self.__dict__.get("_tail_acc_ok")
        if not acc or acc["key"] != key:
            acc = self.__dict__["_tail_acc_ok"] = {"key": key, "parts": [None] * parts}
        acc["parts"][part] = str(body.get("blob") or "")
        if any(p is None for p in acc["parts"]):
            return []
        self.__dict__.pop("_tail_acc_ok", None)
        try:
            obj = event.open_obj(self.reserve_k(), bytes.fromhex("".join(acc["parts"])))
        except (ValueError, event.RecordError) as e:
            log.warning("резерв: список спорного хвоста не открылся (%s) — хвост не снят", e)
            return []
        if str(obj.get("line") or "") != self.db.jrn_line() or obj.get("e") != body.get("e") or obj.get("F") != body.get("F"):
            log.warning("резерв: список спорного — не к этому хвосту — хвост не снят")
            return []
        disputed = [d for d in (obj.get("disputed") or []) if isinstance(d, dict)][:DISPUTED_MAX]
        self.rsv_update("rsv_out", tail_report={"variant": "crash", "moved": int(body.get("moved") or 0),
                                                "disputed": disputed, "total": int(body.get("disputed_n") or 0),
                                                "more": int(obj.get("more") or 0), "at": timeutil.now_iso()})
        self.db.jrn_tail_drop()
        self.rsv_put("rsv_tail", None)
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
        total = max(total, len(disputed))
        lines = [self._tail_line(d, variant) for d in disputed[:rn.TAIL_LINES_MAX]]
        more = ""
        if total > rn.TAIL_LINES_MAX:
            # весь список — снимком в базе бота основы (переживает --wipe резерва), хвост — ссылкой на него
            from awgbot.util import listlink
            from awgbot.util.textfmt.tail import more_line
            rows = [self._tail_line(d, variant) for d in disputed]
            key = self.db.list_snap_keep(int(config.ADMIN_ID), "admin", rn.tail_snap_title(variant, name), rows,
                                         new=True, n=total, missing=total - len(rows))
            if key:
                more = more_line(total - len(lines), str(getattr(self, "bot_username", "") or ""),
                                 listlink.build("snap", key=key))
        return [Notification(tg_id=int(config.ADMIN_ID), text=rn.tail_disputed(variant, name, lines, total, more),
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
        device = ""
        if client is not None:
            profile = profile_link(client, bot)
        else:                                                 # владелец не определён: у строки профиля — её имя
            profile = "" if d.get("table") == "devices" else html.escape(str(d.get("name") or "?"), quote=False)
        if d.get("table") == "devices":
            tail_name = (d.get("tail") or {}).get("name") if isinstance(d.get("tail"), dict) else ""
            device = html.escape(str(d.get("name") or tail_name or "?"), quote=False)
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

    # ── отказы ──────────────────────────────────────────────────────────────
    def reserve_refuse_update_if_waiting(self) -> None:
        """Пока ждём правки бота на резерве («вернуть без резерва») или мой хвост не
        перенесён — обновление отказывает: выпуск без этого пути их бы не принял."""
        if not self.reserve_paired():
            return
        if self.reserve_tail_blocks():
            raise ServiceError(OWN_TAIL_REFUSED.format(name=self.reserve_peer_name(), what="обновление"))
        if self.rsv_get("rsv_out").get("await_merge"):
            raise ServiceError(TAIL_WAIT_REFUSED.format(name=self.reserve_peer_name()))

    def reserve_tail_return_why(self) -> str:
        """Возврат ждёт перенос моего хвоста (держатель на резерве его ещё не принял)."""
        if self.reserve_tail_blocks():
            return OWN_TAIL_REFUSED.format(name=self.reserve_peer_name(), what="возврат")
        return ""

    # ── «вернуть без резерва» (С9): правки бота на резерве — хвостом ─────────
    def reserve_c9_start(self, guest_e: int) -> None:
        """Основа взяла эпоху без резерва: события бота на резерве эпохи guest_e после моей
        точки взятия эпохи — хвост; они придут его же записями (живой гость при возврате или
        журнал данных гостя с диска резерва) и перебазируются здесь при получении."""
        fork = int(self.db.jrn_meta()["fork_i"])
        self.rsv_put("rsv_c9", {"e": int(guest_e), "F": fork, "upto": fork, "moved": 0, "disputed": [], "total": 0,
                                "at": timeutil.now_iso()})
        log.warning("резерв: правки бота на «%s» эпохи %s после %s — хвостом при встрече", self.reserve_peer_name(),
                    guest_e, fork)

    def reserve_disk_waiting(self) -> bool:
        """Гостя у резерва нет, но его журнал на диске новее того, что здесь уже есть: в С9 —
        перенесённого хвостом, в следовании — применённого (возврат заберёт его, не «пусто»)."""
        keep = self.reserve_peer_state().get("jrn_keep") or {}
        try:
            gj_e, gj_last = int(keep.get("gj_e") or -1), int(keep.get("gj_last") or 0)
        except (TypeError, ValueError):
            return False
        c9 = self.rsv_get("rsv_c9")
        if c9:
            return gj_e == int(c9.get("e") or -2) and gj_last > int(c9.get("upto") or 0)
        if self.reserve_following():
            return gj_last > int(self.db.jrn_meta()["applied"])
        return False

    def reserve_c9_tail_in(self, body: dict, c9: dict) -> list[tuple]:
        """Записи бота на резерве эпохи, которую я сменил: строго подряд после перенесённого —
        перебазировать (держатель — я), подтвердить ему; итог — к Т-Ж4/Т-Ж5 после возврата."""
        from awgbot.util.rsvjrn import event
        line, e = str(body["line"]), int(body["e"])
        try:
            evs = [event.open_event(self.reserve_k(), r, line) for r in event.split(bytes.fromhex(str(body["blob"])))]
        except (ValueError, event.RecordError) as ex:
            log.warning("резерв: правки бота на «%s» не открылись: %s", self.reserve_peer_name(), ex)
            return []
        upto = int(c9.get("upto") or 0)
        evs = [ev for ev in evs if ev.i > upto and ev.e == e]
        if not evs:
            return [("send", "jack", {"line": line, "e": e, "i": upto})]
        if [ev.i for ev in evs] != list(range(upto + 1, upto + 1 + len(evs))):
            return [("send", "jneed", {"line": line, "from": upto + 1})]
        before = self.db.jrn_last_i()

        def mark(conn, rpt) -> None:
            row = conn.execute("SELECT value FROM server_state WHERE key = 'rsv_c9'").fetchone()
            try:
                cur = json.loads(row[0]) if row and row[0] else {}
            except ValueError:
                cur = {}
            if not cur:
                return                                        # возврат уже снял ожидание — не воскрешать
            cur.update(upto=max(int(cur.get("upto") or 0), evs[-1].i), moved=int(cur.get("moved") or 0) + rpt.moved,
                       total=int(cur.get("total") or 0) + len(rpt.disputed),
                       disputed=(list(cur.get("disputed") or []) + rpt.disputed)[:DISPUTED_MAX])
            conn.execute("INSERT INTO server_state (key, value) VALUES ('rsv_c9', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(cur, ensure_ascii=False),))
        try:
            rpt = self.db.jrn_rebase(evs, before_commit=mark)
        except Exception as ex:                                # noqa: BLE001
            log.error("резерв: правки бота на «%s» не перенесены: %s", self.reserve_peer_name(), ex)
            return []
        self._tail_side_effects(before)
        log.info("резерв: правки бота на «%s» %s…%s: перенесено %d, спорных %d", self.reserve_peer_name(), evs[0].i,
                 evs[-1].i, rpt.moved, len(rpt.disputed))
        if rpt.disputed:
            log.info("резерв: правки бота на «%s»: спорные: %s", self.reserve_peer_name(), rpt.disputed)
        return [("send", "jack", {"line": line, "e": e, "i": evs[-1].i})]

    def reserve_c9_notes(self) -> list[Notification]:
        """После возврата: Т-Ж4/Т-Ж5 по правкам бота на резерве, перенесённым сюда."""
        c9 = self.rsv_get("rsv_c9")
        if not c9:
            return []
        self.rsv_put("rsv_c9", None)
        return self._tail_notes("c9", int(c9.get("moved") or 0), list(c9.get("disputed") or []),
                                int(c9.get("total") or 0))

    def reserve_tail_keys_clear(self) -> None:
        """После возврата: отложенные ключи устройств, которые так и не вернулись, — прочь
        (строкой журнала: эти устройства — среди спорного Т-Ж4)."""
        raw = self.db.get_state("rsv_tail_keys") or ""
        if not raw:
            return
        try:
            n = len(json.loads(raw))
        except ValueError:
            n = 0
        if n:
            log.warning("резерв: закрытые ключи %d устройств хвоста не понадобились — устройства не вернулись", n)
        self.db.set_state("rsv_tail_keys", "")
