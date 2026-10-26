"""rekey.py — смена ключей линка на шлюзе: приглашение, отказ, переключение, отсчёт, фиксация по ответу сервера, поиск рабочего конфига."""
from __future__ import annotations

import functools
import json
import os
import threading
import time

from awgbot.core import config, settings
from awgbot.domain import gwcfg
from awgbot.util import wgconf

from .. import base
from ..base import log

SWITCH_SECONDS = 90             # после переключения ждём подписанный ответ сервера столько
SEARCH_SECONDS = 24 * 3600      # сутки после перехода держим второй конфиг и ищем
SEARCH_FIRST = 180              # первый шаг поиска; дальше удваивается
SEARCH_MAX_INTERVAL = 1800      # шаг поиска удваивается до 30 минут
DEAD_CONFIRM_SECONDS = 30       # «линк мёртв» — два замера подряд не ближе этого
DECLINED_KEEP = 24 * 3600       # метка отказа держится столько: повтор того же id — отказ без вопроса
TICK_SWITCHED = 10              # с — такт, пока ждём ответа сервера после переключения
TICK_SEARCH = 30                # с — такт поиска (шаги — по next_try)
AUTO = ("i1", "join")           # link_next без вопроса человеку (возможность rekey2)

# link.txn переписывают такт цикла, такт монитора, link_go, ответ сервера,
# ответ человека — из разных потоков. Один замок на всё; такт его не ждёт.
_RK_LOCK = threading.RLock()


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _RK_LOCK:
            return fn(*args, **kwargs)
    return wrapper


class CfgRekeyMixin:
    """Переход ключей — два генерала: подтверждение может потеряться ровно в
    момент переключения. Таймеры этого не решают, поэтому решает сервер, а
    шлюз держит оба конфига и ищет рабочий: после переключения без ответа
    сервера (90 с) — назад на прежние и поиск. Поиск: линк мёртв по тому же
    признаку, что у монитора (хендшейк старше app.gateway.handshake_max_age,
    два замера подряд) — пробуем второй конфиг, шаг удваивается до 30 минут;
    сутки вышли — закрываемся, только если линк явно жив, иначе ищем дальше.
    Первый же подписанный ответ сервера закрепляет выбор, а какие ключи
    работают, решает живой конфиг линка, не запись состояния.

    Из ready (согласие дано, link_go не пришёл) поиска нет: сервер на новых
    ключах может оказаться только в 180 с после link_go и сам вернётся на
    прежние раньше, чем шлюз заметит мёртвый линк (порог монитора — 5 мин);
    поиск отсюда уводил бы шлюз на ключи, которых у ВПС уже нет. Вернувшись,
    сервер шлёт link_cancel — ready снимается.

    Состояния (`/etc/awg-gw/link.txn`, переживает перезапуск): asked —
    новый конфиг в rekey.next, ждёт человека; declined — человек отказал
    (метка: сервер, не получивший отказа при лежащем канале, повторит тот
    же id — ответ «отказ» без вопроса); ready — согласие дано, ждём link_go;
    switched — переключились (пишется ДО применения), ждём ответа сервера
    90 с; back — поиск (current: prev | next). msg — сообщение приглашения
    в чате: итог и отзыв правят его."""

    # ── файлы ────────────────────────────────────────────────────────────────

    @staticmethod
    def _rk_path(name: str) -> str:
        from awgbot.infra import gwguard
        return f"{gwguard.GW_ETC}/{name}"

    def rekey_state(self) -> dict:
        try:
            with open(self._rk_path("link.txn"), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _rk_drop_files(self) -> None:
        for name in ("rekey.next", "rekey.prev"):
            try:
                os.unlink(self._rk_path(name))
            except OSError:
                pass

    def _rk_save(self, st: dict) -> None:
        """Пустое состояние — всё прочь; иначе — запись (файлы конфигов не трогаются)."""
        if not st:
            self._rk_drop_files()
            try:
                os.unlink(self._rk_path("link.txn"))
            except OSError:
                pass
            return
        self._cfg_write_private(self._rk_path("link.txn"), json.dumps(st, ensure_ascii=False))

    def _rk_decline_mark(self, rid: str, msg=None) -> None:
        """Отказ — метка без конфигов: повтор того же id получит «отказ»."""
        self._rk_drop_files()
        st = {"id": rid, "state": "declined", "at": time.time()}
        if msg:
            st["msg"] = msg
        self._rk_save(st)

    # ── приглашение ──────────────────────────────────────────────────────────

    @_locked
    def rekey_next_in(self, msg: dict) -> dict:
        """link_next с сервера. Возвращает {"reply": тело link_ack, "ask": bool,
        "replaced": сообщение прежнего приглашения или None}. Тот же id, что
        уже ждёт (сессия упала между asked и ready), — второй раз не
        спрашиваем, отвечаем своим состоянием; тот же id после отказа — отказ.
        `auto` (возможность rekey2): «i1» — переход линка на I1 пользователей
        сервера, «join» — согласие дано кодом сопряжения; человека не
        спрашиваем — сразу ready (проверки ключей те же)."""
        rid = "".join(c for c in str(msg.get("id") or "") if c.isalnum())[:32]
        out = {"reply": None, "ask": False, "replaced": None}
        if not rid:
            return out
        if msg.get("box") and not msg.get("conf"):
            opener = getattr(self, "join_next_open", None)   # сессия сопряжения: conf под ключом сопряжения
            msg = opener(msg) if callable(opener) else msg
            if not msg.get("conf"):
                return {**out, "reply": {"id": rid, "state": "declined",
                                         "error": "новые ключи линка не расшифровались ключом сопряжения"}}
        st = self.rekey_state()
        if st.get("id") == rid:
            state = st.get("state")
            if state == "declined":
                return {**out, "reply": {"id": rid, "state": "declined", "error": ""}}
            if state in ("asked", "ready", "switched"):
                return {**out, "reply": {"id": rid, "state": "asked" if state == "asked" else "ready", "error": ""}}
        if st.get("state") in ("switched", "back"):
            return {**out, "reply": {"id": rid, "state": "declined", "error": "идёт другая смена ключей"}}
        try:
            conf = gwcfg.check_link_rekey(str(msg.get("conf") or ""), self.cfg_link_text())
        except gwcfg.CfgError as e:
            log.warning("gateway: новые ключи линка не прошли проверку: %s", e)
            return {**out, "reply": {"id": rid, "state": "declined", "error": str(e)[:300]}}
        if st.get("state") in ("asked", "ready") and st.get("msg"):
            out["replaced"] = st["msg"]                    # прежнее приглашение — «заменён более новым»
        fp = gwcfg.key_fp(wgconf.pubkey_of(conf.get("Interface", "PrivateKey")), conf.get("Peer", "PublicKey"))
        self._cfg_write_private(self._rk_path("rekey.next"),
                                wgconf.render(conf, "link", stamp=conf.stamp or gwcfg.LINK_CONTRACT))
        auto = str(msg.get("auto") or "")
        if auto in AUTO:
            self._rk_save({"id": rid, "state": "ready", "fp": fp, "at": time.time(), "ready_at": time.time(),
                           "auto": auto})
            log.info("gateway: новые ключи линка без вопроса (%s)", auto)
            return {**out, "reply": {"id": rid, "state": "ready", "error": ""}}
        self._rk_save({"id": rid, "state": "asked", "fp": fp, "at": time.time()})
        return {**out, "reply": {"id": rid, "state": "asked", "error": ""}, "ask": True}

    def rekey_question(self) -> dict | None:
        st = self.rekey_state()
        if st.get("state") != "asked":
            return None
        return {"section": "link", "fp": st.get("fp", ""), "carries": self.carries_traffic()}

    @_locked
    def rekey_mark_msg(self, chat_id: int, message_id: int) -> None:
        """Приглашение показано этим сообщением — итог и отзыв поправят его."""
        st = self.rekey_state()
        if st.get("state") == "asked":
            st["msg"] = [int(chat_id), int(message_id)]
            self._rk_save(st)

    @_locked
    def rekey_answer(self, accept: bool) -> dict | None:
        """Ответ человека: «Применить» — ready, «Отмена» — метка отказа. Тело link_ack или None."""
        st = self.rekey_state()
        if st.get("state") != "asked":
            return None
        if accept:
            st.update(state="ready", ready_at=time.time())
            self._rk_save(st)
            return {"id": st["id"], "state": "ready", "error": ""}
        self._rk_decline_mark(st["id"])
        return {"id": st["id"], "state": "declined", "error": ""}

    @_locked
    def rekey_cancel(self, msg: dict) -> dict | None:
        """link_cancel: сервер отозвал (срок вышел, отменили на ВПС). Было что
        снимать — {"msg": сообщение приглашения или None}, иначе None."""
        st = self.rekey_state()
        if not st.get("id") or st.get("id") != str(msg.get("id") or ""):
            return None
        if st.get("state") == "declined":
            self._rk_save({})
            return None
        if st.get("state") in ("asked", "ready"):
            self._rk_save({})
            return {"msg": st.get("msg"), **({"auto": str(st["auto"])} if st.get("auto") else {})}
        return None

    # ── переключение ─────────────────────────────────────────────────────────

    @_locked
    def rekey_go(self, msg: dict) -> dict:
        """link_go: переключиться на новые ключи. Новый конфиг — стоящий с
        ключами, PSK и обфускацией из приглашения; прежний — в rekey.prev;
        состояние switched пишется ДО применения (перезапуск посреди — тот же
        отсчёт и поиск). Ответ сервер получит в новой сессии. {"ok", "error", "msg"}."""
        st = self.rekey_state()
        if st.get("state") != "ready" or st.get("id") != str(msg.get("id") or ""):
            # ничего не трогали — человеку говорить не о чем, серверу — отказ
            return {"ok": False, "error": "смена ключей не готова", "msg": None, "quiet": True}
        with base._APPLY_LOCK:
            try:
                new = wgconf.parse(base.pathlib_read(self._rk_path("rekey.next")))
                merged = gwcfg.merge_link_keys(self.cfg_link_text(), new, peer_from_new=st.get("auto") == "join")
                prev = self.cfg_link_conf()
                if prev is None:
                    raise gwcfg.CfgError("текущий конфиг линка не прочитан")
                self._cfg_write_private(self._rk_path("rekey.prev"),
                                        wgconf.render(prev, "link", stamp=prev.stamp or gwcfg.LINK_CONTRACT))
                self._cfg_write_private(self._rk_path("rekey.next"),
                                        wgconf.render(merged, "link", stamp=merged.stamp or gwcfg.LINK_CONTRACT))
            except (OSError, wgconf.WgConfError, gwcfg.CfgError) as e:
                self._rk_decline_mark(st["id"], st.get("msg"))
                return {"ok": False, "error": str(e)[:300], "msg": st.get("msg"), "auto": str(st.get("auto") or "")}
            st.update(state="switched", switched_at=time.time())
            self._rk_save(st)
            ok, out = self._rk_link_apply(self._rk_path("rekey.next"))
        if not ok:
            # скрипт вернул прежний конфиг сам — переходу конец, серверу отказ
            log.warning("gateway: новые ключи линка не встали: %s", out[-200:])
            self._rk_decline_mark(st["id"], st.get("msg"))
            return {"ok": False, "error": out.splitlines()[-1] if out else "скрипт отказал", "msg": st.get("msg"),
                    "auto": str(st.get("auto") or "")}
        # срок 90 с — от конца применения: скрипт мог ждать замка обвязки минуты
        st["switched_at"] = time.time()
        self._rk_save(st)
        log.info("gateway: линк переключён на новые ключи, ждём ответа сервера")
        return {"ok": True, "error": "", "msg": st.get("msg")}

    def _rk_apply_file(self, name: str) -> bool:
        path = self._rk_path(name)
        if not os.path.exists(path):
            return False
        with base._APPLY_LOCK:
            ok, out = self._rk_link_apply(path)
        if not ok:
            log.warning("gateway: конфиг линка %s не встал: %s", name, out[-200:])
        return ok

    def _rk_link_apply(self, path: str) -> tuple[bool, str]:
        """--link-apply конфига перехода. Шлюз в сопряжении (юнита обвязки ещё
        нет): подсеть пользователей, соседи и резолвер для проверки скрипта — из
        самого конфига; конфиг по токену — проверкой вида join."""
        from awgbot.infra import gwguard
        extra = {}
        active = getattr(self, "join_active", None)
        if callable(active) and active():
            try:
                extra = gwcfg.join_link_env(base.pathlib_read(path))
            except OSError:
                extra = {}
        return gwguard.link_apply(path, iface_nets=self._cfg_iface_nets(), env_extra=extra)

    # ── ответ сервера ────────────────────────────────────────────────────────

    @_locked
    def rekey_server_answered(self) -> dict:
        """Первый подписанный ответ сервера в новой сессии: выбор закреплён.
        {"done": тело link_done или None, "note": текст или "", "msg": сообщение
        приглашения или None}."""
        st = self.rekey_state()
        state = st.get("state")
        msg = st.get("msg")
        if state not in ("switched", "back"):
            return {"done": None, "note": "", "msg": None}
        on_new = self._rk_live_is_next()
        if on_new is None:                                 # файлы перехода не читаются — по записи состояния
            on_new = state == "switched" or st.get("current") == "next"
        if on_new:
            self._rk_save({})                              # сервер ответил на новых ключах — прежний не нужен
            self.bundle_issued_raise(int(time.time()))     # файлы до смены несут прежние ключи — им отказ
            # без вопроса (auto) человек ничего не начинал — и итог ему без строки
            note = "" if st.get("auto") else "✅ Линк на новых ключах"
            if st.get("auto") == "join" and hasattr(self, "join_linked"):
                self.join_linked()                         # сопряжение: обвязка полным прогоном
            return {"done": {"id": st["id"], "ok": True, "keys": "new"}, "note": note, "msg": msg}
        if state in ("back", "switched"):
            self._rk_save({})
            # на прежних: о возврате человек уже знает (сказано на сроке переключения)
            return {"done": {"id": st["id"], "ok": False, "keys": "old"}, "note": "", "msg": None}
        return {"done": None, "note": "", "msg": None}

    # ── отсчёт и поиск ───────────────────────────────────────────────────────

    def _rk_live_is_next(self) -> bool | None:
        """Живой конфиг линка — новый (rekey.next)? По приватному ключу: запись
        состояния после сбоя посреди применения может врать. None — не знать."""
        try:
            live = wgconf.parse(self.cfg_link_text()).get("Interface", "PrivateKey")
            nxt = wgconf.parse(base.pathlib_read(self._rk_path("rekey.next"))).get("Interface", "PrivateKey")
        except (OSError, wgconf.WgConfError):
            return None
        if not live or not nxt:
            return None
        return live == nxt

    def _rk_link_alive(self) -> bool:
        """Линк явно жив: хендшейк есть и моложе порога монитора."""
        from awgbot.infra import gwguard
        state, age = gwguard.handshake_state(config.GW_LINK_IF)
        return state == "ok" and age is not None and age <= settings.get_int("app.gateway.handshake_max_age", 300)

    def _rk_link_dead(self, st: dict, now: float) -> bool | None:
        """Мёртв ли линк — тем же признаком, что у монитора, и двумя замерами
        подряд (первый ставит dead_seen в состоянии). None — не знаем
        (`awg show` не ответил): ничего не решаем."""
        from awgbot.infra import gwguard
        state, age = gwguard.handshake_state(config.GW_LINK_IF)
        if state == "unknown":
            return None
        dead = state in ("never", "absent") or (age is not None
                                                 and age > settings.get_int("app.gateway.handshake_max_age", 300))
        if not dead:
            st.pop("dead_seen", None)
            return False
        seen = st.get("dead_seen")
        if seen is None:
            st["dead_seen"] = now
            return False
        return now - float(seen) >= DEAD_CONFIRM_SECONDS

    def _rk_start_search(self, st: dict, current: str, now: float) -> None:
        st.update(state="back", current=current, search_until=now + SEARCH_SECONDS,
                  interval=SEARCH_FIRST, next_try=now + SEARCH_FIRST)
        st.pop("dead_seen", None)

    def rekey_tick(self) -> dict:
        """Такт: сроки и поиск. {"note": текст или "", "msg": сообщение
        приглашения или None}. Замок занят (идёт link_go, ответ сервера) —
        такт пропускается, не ждёт."""
        quiet = {"note": "", "msg": None}
        if not _RK_LOCK.acquire(blocking=False):
            return quiet
        try:
            st = self.rekey_state()
            before = json.dumps(st, sort_keys=True)
            res = self._rk_tick(st, time.time())
            if st and json.dumps(st, sort_keys=True) != before and self.rekey_state():
                self._rk_save(st)                          # писать на карту — только изменившееся
            return res
        finally:
            _RK_LOCK.release()

    def _rk_tick(self, st: dict, now: float) -> dict:
        state = st.get("state")
        quiet = {"note": "", "msg": None}
        if state == "declined":
            if now - float(st.get("at") or now) > DECLINED_KEEP:
                self._rk_save({})
            return quiet
        if state in ("asked", "ready"):
            if now - float(st.get("at") or now) > SEARCH_SECONDS:
                # сервер так и не повёл переход (через сутки он снимает его и сам) —
                # снимаем и правим приглашение: иначе в чате висели бы живые кнопки
                self._rk_save({})
                if st.get("auto"):
                    log.info("gateway: смена ключей без вопроса снята — сервер её не повёл")
                    return quiet                           # запроса человек не видел — только журнал
                return {"note": "Запрос отозван сервером AWG", "msg": st.get("msg")}
            return quiet
        if state == "switched" and now - float(st.get("switched_at") or now) > SWITCH_SECONDS:
            # ответа сервера нет — назад на прежние и ищем
            self._rk_apply_file("rekey.prev")
            self._rk_start_search(st, "prev", now)
            self._rk_save(st)
            if st.get("auto"):
                log.warning("gateway: новые ключи линка (без вопроса) не поднялись — вернул прежние")
                return quiet
            return {"note": "⚠️ Новые ключи линка не поднялись — вернул прежние", "msg": st.get("msg")}
        if state == "back":
            if now > float(st.get("search_until") or 0) and self._rk_link_alive():
                self._rk_save({})                          # сутки вышли, линк явно жив — второй конфиг не нужен
                return quiet
            dead = self._rk_link_dead(st, now)
            if dead and now >= float(st.get("next_try") or 0):
                other = "next" if st.get("current") == "prev" else "prev"
                interval = min(int(st.get("interval") or SEARCH_FIRST) * 2, SEARCH_MAX_INTERVAL)
                if self._rk_apply_file(f"rekey.{other}"):
                    st.update(current=other, interval=interval, next_try=now + interval)
                    st.pop("dead_seen", None)
                    log.info("gateway: линк молчит — пробую %s конфиг", "новый" if other == "next" else "прежний")
                else:
                    st["next_try"] = now + int(st.get("interval") or SEARCH_FIRST)   # не дёргать линк каждый такт
        return quiet

    def rekey_tick_every(self) -> int:
        """Сколько секунд до следующего такта перехода; 0 — переход не идёт
        (хватит такта монитора)."""
        state = self.rekey_state().get("state")
        return TICK_SWITCHED if state == "switched" else TICK_SEARCH if state == "back" else 0

    def rekey_pending_section(self) -> list[str]:
        """«link» в очереди вопросов, пока приглашение ждёт человека."""
        return ["link"] if self.rekey_state().get("state") == "asked" else []
