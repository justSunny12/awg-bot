"""
tail.py — часть пакета journal (см. __init__): хвост старой эпохи.

Бывший держатель, узнав, что эпоха ушла (следование после аварийного
открытия у основы; `jstale` у бота основы на резерве после «вернуть без
резерва»), откладывает свои неподтверждённые события в `jrn_tail` (infra/db
jrn, состояние `hold`); когда точка расхождения F известна — откатывает их в
базе и пирах (`reserve_tail_finalize`, состояние `wait`) и шлёт держателю
(`jtail`: записи вида «хвост» под `K` частями, повтор раз в TAIL_RESEND_S до
ответа).

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

«Вернуть без резерва» (С9): правки бота на резерве приходят хвостом при
возврате и перебазируются здесь при получении — c9. Строки Т-Ж4 по записям
спорного и снимок всего списка — tail_notes.

Уведомления Т-Ж4 (спорное) и Т-Ж5 (всё перенесено) шлёт всегда бот основы:
хвост бота на резерве, пришедший `jtail`, — сразу после перебазирования; С9 —
после возврата (`reserve_c9_notes`); свой хвост после аварии — после
возврата, отдельным сообщением сразу за Т-РУ5б (`reserve_tail_notes`). Пока
свой хвост не перенесён — возврат и обновление ждут, но не дольше
TAIL_WAIT_S: дальше не держит, и после возврата основа переносит его сама
поверх своей базы (`reserve_tail_self`, вариант Т-Ж5 «self»). Пока ждём правки
бота на резерве («вернуть без резерва») — обновление отказывает.
"""
from __future__ import annotations

import json
import time

from awgbot.core import config
from awgbot.util import pairproto, timeutil
from awgbot.util.rsvjrn import schema_too_old

from ...gateway_link.common import log
from ...types import Notification, ServiceError
from ..state import HOSTING
from .ship import EV_BYTES, epoch_sane

TAIL_RESEND_S = 60
TAIL_SELF_TELL = 5                # неудач переноса своего хвоста подряд — до уведомления админу
TAIL_WAIT_S = 10 * 60             # свой хвост без ответа держателя дольше — возврат не держит, после него хвост переносит основа
DISPUTED_MAX = 2000                # в jtail_ok — не больше; сверх — числом `more`, сами — строкой журнала
C9_WAIT_REFUSED = ("Жду правки бота на «{name}», сделанные после потери связи, — {what} после них; "
                   "он не вернётся — удали пару в «🛟 Резерв»")
OWN_TAIL_REFUSED = "Правки, сделанные здесь перед переездом на «{name}», ещё переносятся туда — {what} после этого"


def own_tail_blocks_raw(db) -> bool:
    """Свой хвост не перенесён и ждём не дольше TAIL_WAIT_S — по одной базе (и для
    сторожа терминала tools/rsvbusy, без сервисов). Ошибка базы — наружу: «хвоста нет»
    по замку пустило бы обновление при живом хвосте."""
    try:
        rows = db.jrn_tail_rows()
    except Exception as e:                                    # noqa: BLE001
        if schema_too_old(e):
            return False
        raise
    if not rows:
        return False
    raw = db.get_state("rsv_tail") or ""
    try:
        meta_t = json.loads(raw) if raw.startswith("{") else {}
        return timeutil.elapsed(timeutil.parse_iso(str(meta_t.get("at") or ""))) <= TAIL_WAIT_S
    except ValueError:                                        # отметки нет или она битая — не держит
        return False


def c9_wait_raw(db) -> bool:
    """Ждём правки бота на резерве после «вернуть без резерва» (`rsv_out.await_merge`) —
    по одной базе (и для сторожа терминала tools/rsvbusy): одно правило с ботом."""
    raw = db.get_state("rsv_out") or ""
    try:
        out = json.loads(raw) if raw.startswith("{") else {}
    except ValueError:
        return False
    return bool(isinstance(out, dict) and out.get("await_merge"))


class ReserveTailMixin:

    # ── бывший держатель: подготовка и отправка ─────────────────────────────
    def reserve_tail_pending(self) -> bool:
        """Свой хвост готов и не перенесён (отложенный до точки расхождения — не в счёт).
        Ошибка базы — наружу: «хвоста нет» по замку осушило бы гостя при живом хвосте."""
        try:
            return bool(self.db.jrn_tail_rows())
        except Exception as e:                                # noqa: BLE001
            if schema_too_old(e):
                return False
            raise

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
            self.rsv_put("rsv_tail", {"e": e, "F": fork, "at": timeutil.now_iso()})
            self.reserve_tail_undo_peers()

    def reserve_tail_undo_peers(self) -> None:
        """Хвост откачен в базе — и пиры за ним: заведённые хвостом устройства — с
        интерфейса, удалённые — обратно, сменившие ключ — прежним ключом."""
        from awgbot.infra import awg
        for ev in reversed(self.db.jrn_tail_rows()):
            if ev.t != "devices":
                continue
            c, o = ev.c or {}, ev.o or {}
            try:
                if ev.op == "I" and c.get("public_key"):
                    awg.remove_peer(str(c["public_key"]), awg.iface_of(c.get("iface")))
                elif ev.op == "D" and o.get("public_key") and o.get("address"):
                    awg.add_peer(str(o["public_key"]), str(o.get("preshared_key") or ""), str(o["address"]),
                                 awg.iface_of(o.get("iface")))
                elif ev.op == "U" and o.get("public_key") and any(
                        k in c and c.get(k) != o.get(k) for k in ("public_key", "address", "iface", "preshared_key")):
                    awg.remove_peer(str(c.get("public_key") or o["public_key"]), awg.iface_of(c.get("iface") or o.get("iface")))
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
        """Такт: хвост держателю — когда он на связи; повтор, пока нет ответа (отметка
        отправки — в памяти: повтор после перезапуска не вредит)."""
        if not self.reserve_tail_pending():
            return []
        now = time.monotonic()
        if now - float(self.__dict__.get("_tail_sent_at") or 0) < TAIL_RESEND_S:
            return []
        if getattr(self, "guest", False):
            send = "guest_send"                               # гость: по локальному каналу боту резерва
        else:
            peer = self.reserve_peer_state()
            if not self.reserve_online() or str(peer.get("inn") or "") not in HOSTING:
                return []
            send = "send"
        parts = self._tail_parts()
        if not parts:
            return []
        self.__dict__["_tail_sent_at"] = now
        log.info("резерв: хвост эпохи %s после %s — держателю (частей: %d)", parts[0]["e"], parts[0]["F"], len(parts))
        return [(send, "jtail", p) for p in parts]

    def reserve_tail_blocks(self) -> bool:
        """Свой хвост не перенесён и ждём не дольше TAIL_WAIT_S; дольше — не держит: после
        возврата основа перенесёт его сама (`reserve_tail_self`)."""
        return own_tail_blocks_raw(self.db)

    def reserve_tail_settle(self) -> list[tuple]:
        """Такт дома: держатель здесь, а свой хвост так и не перенесён — эпоху взяли без
        возврата («вернуть без резерва», снятый зависший вебхук, резерв вернулся пустым),
        и бот на резерве его уже не примет (ему — `jstale`). Перенести здесь, как после
        возврата; не вышло — повтор раз в TAIL_RESEND_S, после TAIL_SELF_TELL неудач —
        админу. В отъезде и возврате — свой путь (`_return_done`)."""
        if getattr(self, "guest", False) or self.reserve_out() != "home" or not self.reserve_tail_pending():
            return []
        if self.db.jrn_meta()["role"] != "holder":
            return []
        now = time.monotonic()
        if now < float(self.__dict__.get("_tail_self_next") or 0):
            return []
        self.__dict__["_tail_self_next"] = now + TAIL_RESEND_S
        notes = self.reserve_tail_self()
        if not self.reserve_tail_pending():
            self.reserve_tail_keys_clear()
        return [("notify", notes)] if notes else []

    def reserve_tail_self(self) -> list[Notification]:
        """Держатель здесь, а свой хвост бот на резерве так и не принял (после возврата или
        при становлении держателем без возврата) — перенести самой поверх своей базы;
        Т-Ж4/Т-Ж5 — как от него. Не вышло — хвост остаётся, зовущий повторит."""
        from awgbot.domain.notices import reserve_tail as rt
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
            fails = int(self.__dict__.get("_tail_self_fails") or 0) + 1
            self.__dict__["_tail_self_fails"] = fails
            log.error("резерв: свой хвост не перенесён (%d): %s", fails, ex)
            if fails == TAIL_SELF_TELL:                        # один раз на эпизод: дальше — строки журнала
                return [Notification(tg_id=int(config.ADMIN_ID), text=rt.tail_self_failed(self.reserve_peer_name(), str(ex)))]
            return []
        self.__dict__.pop("_tail_self_fails", None)
        self._tail_side_effects(before)
        self.db.jrn_keys_restore()                            # вставки хвоста — без закрытого ключа: он отложен
        self.db.jrn_tail_drop()
        self.rsv_put("rsv_tail", None)
        log.info("резерв: свой хвост: %d событий, перенесено %d, спорных %d, пропущено %d", len(evs), rpt.moved,
                 len(rpt.disputed), rpt.skipped)
        if rpt.disputed:
            log.info("резерв: свой хвост: спорные: %s", rpt.disputed)
            return self._tail_notes("crash", rpt.moved, rpt.disputed, len(rpt.disputed))
        return self._tail_notes("self", rpt.moved, [], 0)

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

    def _tail_assemble(self, slot: str, key: list, part: int, parts: int, blob: str) -> str | None:
        """Части одной сборки (ключ — эпоха, F, число частей; другой ключ — сборка заново):
        все на месте — склеенный hex, иначе None."""
        acc = self.__dict__.get(slot)
        if not acc or acc["key"] != key:
            acc = self.__dict__[slot] = {"key": key, "parts": [None] * parts}
        acc["parts"][part] = blob
        if any(p is None for p in acc["parts"]):
            return None
        self.__dict__.pop(slot, None)
        return "".join(acc["parts"])

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
        blob = self._tail_assemble("_tail_acc_in", [e, fork, parts], part, parts, str(body["blob"]))
        if blob is None:
            return []
        try:
            k = self.reserve_k()
            evs = [event.open_event(k, r, line, tail=True) for r in event.split(bytes.fromhex(blob))]
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
            rpt = self.db.jrn_rebase(evs, before_commit=lambda conn, r: self._tail_done_write(conn, e, fork, r, guest, before))
        except Exception as ex:                                # noqa: BLE001 — sqlite3.OperationalError и прочее
            log.error("резерв: хвост эпохи %s не перенесён: %s", e, ex)
            return []
        self._tail_side_effects(before)
        log.info("резерв: хвост эпохи %s: %d событий, перенесено %d, спорных %d, пропущено %d", e, len(evs), rpt.moved,
                 len(rpt.disputed), rpt.skipped)
        if rpt.disputed:
            log.info("резерв: хвост эпохи %s: спорные: %s", e, rpt.disputed)
        acts = self._tail_reply(self.rsv_get("rsv_tail_done"))
        if not guest:
            acts.append(("notify", self._tail_notes("c9", rpt.moved, rpt.disputed, len(rpt.disputed))))
        return acts

    def _tail_done_write(self, conn, e: int, fork: int, rpt, guest: bool, before: int) -> None:
        """В транзакции перебазирования: отметка «хвост этой эпохи перенесён» и ответ на повтор;
        `range` — номера событий моей эпохи, порождённых переносом (подряд после before): основа
        по ним не считает свои же правки «сделанными на резерве» в Т-РУ5б; `at` — для ручной
        диагностики (sqlite3), код его не читает."""
        meta_e = int(conn.execute("SELECT epoch FROM jrn_meta WHERE id = 1").fetchone()[0])
        row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'jrn_events'").fetchone()
        last_i = int(row[0]) if row and row[0] is not None else before
        done = {"epoch": meta_e, "e": e, "F": fork, "moved": rpt.moved, "disputed_n": len(rpt.disputed),
                "more": max(0, len(rpt.disputed) - DISPUTED_MAX), "at": timeutil.now_iso(),
                "range": [before + 1, last_i] if last_i > before else []}
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
                                                 "disputed": list(done.get("disputed") or []), "more": reply["more"],
                                                 "range": list(done.get("range") or [])}).hex()
        chunks = pairproto.chunks(blob)
        return [(send, "jtail_ok", {**reply, "part": n, "parts": len(chunks), "blob": c}) for n, c in enumerate(chunks)]

    def _tail_side_effects(self, before: int, report: bool = False) -> None:
        """Перенесённое хвостом — события моей эпохи после before: пиры и блокировки по ним,
        пересчёт сроков по итогу; report — это правки бота на резерве (С9): они идут и в
        сводку эпохи Т-РУ5б со строками «проверь глазами», как при следовании."""
        from awgbot.infra import awg
        evs = self.db.jrn_events_after(before, 100000)
        if report:
            self._follow_after(evs, self._follow_watch())
        else:
            blocks = False
            for ev in evs:
                if ev.t == "devices":
                    self._follow_peer(awg, ev)
                    self._follow_peer_gone(awg, ev)
                blocks = blocks or "block_reason" in (ev.c or {})
            if blocks:
                try:
                    self.reconcile_blocks(True)
                except Exception as e:                        # noqa: BLE001
                    log.warning("резерв: блокировки после хвоста не наложены: %s", e)
        self._tail_recheck(evs)

    def _tail_recheck(self, evs: list) -> None:
        """После перебазирования — пересчёт по итогу (концепт §3.7): продление из хвоста легло
        поверх истечения у держателя (колонки разные — спора нет) — подписка снова активна, бит
        EXPIRY снят с профиля и устройств. Истечение в обратную сторону делает задание держателя."""
        from awgbot.core.blocks import ClientBlock, DeviceBlock
        from awgbot.core.enums import SubStatus
        ids: set[int] = set()
        for ev in evs:
            if ev.t in ("clients", "client_subscription", "client_quota"):
                ids.add(int((ev.k or {}).get("client_id") or (ev.k or {}).get("id") or 0))
            elif ev.t == "devices":
                ids.add(int(((ev.c or {}).get("client_id") or (ev.o or {}).get("client_id")) or 0))
        now = timeutil.now()
        for cid in sorted(i for i in ids if i):
            cl = self.db.get_client(cid)
            if cl is None or cl.status != SubStatus.EXPIRED or not cl.period_end:
                continue
            try:
                if timeutil.parse_iso(cl.period_end) <= now:
                    continue
            except ValueError:
                continue
            self.db.update_client_fields(cid, status=SubStatus.ACTIVE)
            self._client_clear_block(cid, ClientBlock.EXPIRY)
            for dev in self.db.list_devices(cid):
                self._device_clear_block(int(dev.id), DeviceBlock.EXPIRY)
            log.info("резерв: после переноса хвоста подписка профиля %s снова активна (срок до %s)", cid, cl.period_end)

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
        blob = self._tail_assemble("_tail_acc_ok", [body.get("e"), body.get("F"), parts], part, parts,
                                   str(body.get("blob") or ""))
        if blob is None:
            return []
        try:
            obj = event.open_obj(self.reserve_k(), bytes.fromhex(blob))
        except (ValueError, event.RecordError) as e:
            log.warning("резерв: список спорного хвоста не открылся (%s) — хвост не снят", e)
            return []
        if str(obj.get("line") or "") != self.db.jrn_line() or obj.get("e") != body.get("e") or obj.get("F") != body.get("F"):
            log.warning("резерв: список спорного — не к этому хвосту — хвост не снят")
            return []
        disputed = [d for d in (obj.get("disputed") or []) if isinstance(d, dict)][:DISPUTED_MAX]
        rng = [int(x) for x in (obj.get("range") or []) if isinstance(x, int)][:2]
        # отчёт — до Т-Ж4 после возврата: всё спорное (не больше DISPUTED_MAX) и общее число
        self.rsv_update("rsv_out", tail_report={"moved": int(body.get("moved") or 0), "disputed": disputed,
                                                "total": int(body.get("disputed_n") or 0)},
                        tail_skip=rng if len(rng) == 2 else None)   # мой же хвост вернётся его событиями — не «с резерва»
        self.db.jrn_tail_drop()
        self.rsv_put("rsv_tail", None)
        log.info("резерв: хвост этой базы перенесён на «%s»: %s, спорных %s", self.reserve_peer_name(),
                 body.get("moved"), body.get("disputed_n"))
        return []

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

    # ── отказы ──────────────────────────────────────────────────────────────
    def reserve_refuse_update_if_waiting(self) -> None:
        """Пока ждём правки бота на резерве («вернуть без резерва») или мой хвост не
        перенесён — обновление отказывает: выпуск без этого пути их бы не принял. Тот же
        порядок — у сторожа терминала (tools/rsvbusy)."""
        if not self.reserve_paired():
            return
        if self.reserve_tail_blocks():
            raise ServiceError(OWN_TAIL_REFUSED.format(name=self.reserve_peer_name(), what="обновление"))
        if c9_wait_raw(self.db):
            raise ServiceError(C9_WAIT_REFUSED.format(name=self.reserve_peer_name(), what="обновление"))

    def reserve_tail_return_why(self) -> str:
        """Возврат ждёт перенос моего хвоста (держатель на резерве его ещё не принял)."""
        if self.reserve_tail_blocks():
            return OWN_TAIL_REFUSED.format(name=self.reserve_peer_name(), what="возврат")
        return ""
