"""rekey.py — смена ключей линка на ВПС: стадия, ответы шлюза, переключение, фиксация по hello новым ключом, сроки, возврат на старте."""
from __future__ import annotations

import json
import secrets
import threading
from typing import Optional

from awgbot.core import config, settings
from awgbot.domain import gwcfg
from awgbot.domain.services.types import Notification, ServiceError
from awgbot.util import timeutil, wgconf

from . import slotkeys
from .common import log

ASK_HOURS = 24          # без решения человека дольше — переход снимается
SWITCH_SECONDS = 180    # после link_go — ждём hello новым ключом столько, иначе прежние
DECLINED_HOURS = 24     # отказ шлюза держится строкой карточки столько

# Переходы слота (commit по таймеру, отказ шлюза, срок, старт) зовутся из
# разных потоков: скрипт линка не должен идти двумя режимами сразу.
_REKEY_LOCK = threading.RLock()


class RekeyMixin:
    """Ключи линка меняются тем же устройством, каналом: новые ключи обеих
    сторон, PSK и обфускация; порт, адреса /30 и Endpoint — те же. Состояние
    слота (`gwlink_rekey_<слот>`): staged — новые конфиги в .next, ушёл
    link_next; asked — шлюз спросил человека; switched — ушёл link_go
    (committed — скрипт переключил ВПС); hello слота новым ключом —
    переход зафиксирован, состояние стёрто; срок вышел — прежние ключи;
    declined — шлюз отказал (строка карточки сутки). Фиксирует hello новым
    ключом: он сам доказывает, что шлюз на новых ключах и путь до агента
    есть, — и фиксируется ДО первого слова сервера в этой сессии.

    Шлюзовая сторона получает I1 клиентского интерфейса (linki1.py) при любой
    смене ключей. `auto` — смена без вопроса в чате бота шлюза (агенту с
    возможностью `rekey2`): «i1» — переход живого линка на I1 (rekey_i1.py),
    «join» — согласие уже дано кодом сопряжения. Сменой с `auto` админ не
    управлял: итог без звука, отказы — строкой карточки после трёх
    (rekey_i1.py), а не уведомлением на каждый."""
    _REKEY_KEY = "gwlink_rekey"

    # ── состояние ────────────────────────────────────────────────────────────

    def gateway_rekey(self, slot_id: int) -> dict:
        raw = self.db.get_state(self._gw_slot_key(self._REKEY_KEY, slot_id)) or ""
        try:
            data = json.loads(raw) if raw.startswith("{") else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _rekey_save(self, slot_id: int, st: dict) -> None:
        self.db.set_state(self._gw_slot_key(self._REKEY_KEY, slot_id),
                          json.dumps(st, ensure_ascii=False) if st else "")

    def gateway_rekey_active(self, slot_id: int) -> bool:
        return self.gateway_rekey(slot_id).get("state") in ("staged", "asked", "switched")

    _REKEY_CANCEL_KEY = "gwlink_rekey_cancel"

    def _rekey_cancel_queue(self, slot_id: int, rid: str) -> None:
        """link_cancel — в очередь на ближайшую сессию: сервер вернул прежние
        (срок, отказ commit, старт, сутки без решения), а шлюз, у которого
        link_go потерялся, иначе сутки держал бы «согласие дано»."""
        if rid:
            self.db.set_state(self._gw_slot_key(self._REKEY_CANCEL_KEY, slot_id), rid)

    def gateway_rekey_cancel_peek(self, slot_id: int) -> str:
        """id отзыва, ждущий доставки, или пусто (очередь не трогается)."""
        return self.db.get_state(self._gw_slot_key(self._REKEY_CANCEL_KEY, slot_id)) or ""

    def gateway_rekey_cancel_take(self, slot_id: int) -> str:
        """id отзыва из очереди — и снять его (для разовых проверок; доставка
        идёт через peek/done, чтобы неушедший отзыв не выпадал)."""
        rid = self.gateway_rekey_cancel_peek(slot_id)
        self.gateway_rekey_cancel_done(slot_id, rid)
        return rid

    def gateway_rekey_cancel_done(self, slot_id: int, rid: str) -> None:
        """Отзыв ушёл в сокет — снять из очереди, если там всё ещё он (новая
        стадия могла стереть или заменить)."""
        key = self._gw_slot_key(self._REKEY_CANCEL_KEY, slot_id)
        if rid and (self.db.get_state(key) or "") == rid:
            self.db.set_state(key, "")

    def _rekey_restarted(self, slot_id: int) -> None:
        """Линк ВПС переподнят (коммит, возврат): маршруты через интерфейс
        ушли вместе с ним — политику слота заново сразу, не тактом живости."""
        try:
            self._ensure_gateway_policy()
        except Exception as e:                            # noqa: BLE001
            log.warning("ключи линка: политика слота %s не доведена: %s", slot_id, e)

    def _rekey_paths(self, gw) -> tuple[str, str]:
        """(сторона ВПС .next, сторона шлюза .next)."""
        return f"{config.AWG_DIR}/{gw.link_if}.conf.next", f"/root/gw-{gw.link_if}.conf.next"

    def _rekey_env(self, gw, i1: str = "") -> dict:
        env = {**self._slot_env(gw), **self._lan_env(gw),
               "LINK_KEEPALIVE": str(settings.get("app.client_config.keepalive_seconds", config.KEEPALIVE_SECONDS))}
        if i1:
            env["LINK_I1"] = i1                           # только --rekey-stage его пишет, и только шлюзу
        return env

    def _rekey_run(self, gw, mode: str, i1: str = "") -> Optional[str]:
        """Режим скрипта линка; отказ — причина словами, успех — None."""
        try:
            self._run_link_script(mode, self._rekey_env(gw, i1))
            return None
        except ServiceError as e:
            log.warning("ключи линка: слот %s, %s: %s", gw.id, mode, e)
            return str(e)

    # ── начало ───────────────────────────────────────────────────────────────

    def gateway_rekey_stage(self, slot_id: int, auto: str = "") -> dict:
        """«🔑 Сменить» (auto пусто) или смена без вопроса (auto: i1 | join):
        новые ключи в .next обеих сторон, живые не тронуты; дальше link_next
        уходит шлюзу (сейчас, если на связи, иначе при подключении).
        Возвращает состояние."""
        with _REKEY_LOCK:                                 # двойное «🔑 Сменить» — одна стадия
            return self._rekey_stage(slot_id, auto)

    def _rekey_i1_for(self, gw, auto: str) -> str:
        """I1 шлюзовой стороне: клиентского интерфейса; не прочитан — ручная
        смена оставляет стоящий у линка (не снимать I1 случайно), смена ради
        I1 — отказ (менять не на что)."""
        ok, i1 = self.gateway_client_i1()
        if ok:
            return i1
        if auto == "i1":
            raise ServiceError("I1 клиентского интерфейса не прочитан")
        return self.gateway_link_i1(gw)[1]

    def _rekey_stage(self, slot_id: int, auto: str = "") -> dict:
        gw = self._gw_slot(slot_id)
        if self.gwlink_agent_outdated(gw.id):
            raise ServiceError("Нужен агент шлюза v3.3.0")
        if self.gateway_rekey_active(gw.id):
            raise ServiceError("Смена ключей уже идёт")
        joining = getattr(self, "gwjoin", None)
        if auto != "join" and callable(joining) and joining(gw.id).get("state") in ("waiting", "connected",
                                                                                     "accepted", "closing"):
            # слот в сопряжении (замена, «Настроить заново»): ключи линка ведёт оно (ui-8)
            raise ServiceError("Идёт подключение шлюза командой — дождись итога или отмени его")
        for other in self.db.gateways():
            # сопряжение (join) — новый линк, трафика ещё не несёт: чужая смена ему не помеха
            if auto != "join" and other.id != gw.id and self.gateway_rekey_active(other.id):
                raise ServiceError(f"Идёт смена ключей у {self._gw_display(other)} — дождись итога")
        i1 = self._rekey_i1_for(gw, auto)
        err = self._rekey_run(gw, "--rekey-stage", i1)
        if err:
            raise ServiceError(f"Новые ключи не сгенерированы: {err}")
        body = self.gateway_rekey_next_body(gw.id, stage=True)
        if body is None:
            self._rekey_run(gw, "--rekey-drop")
            raise ServiceError("Новые ключи не прочитаны")
        st = {"id": secrets.token_hex(6), "state": "staged", "fp": body["fp"],
              "at": timeutil.to_iso(timeutil.now()), "auto": auto if auto in ("i1", "join") else "", "i1": i1}
        self._rekey_save(gw.id, st)
        self.db.set_state(self._gw_slot_key(self._REKEY_CANCEL_KEY, gw.id), "")   # прежний отзыв — не о нём
        log.info("ключи линка: слот %s — новые ключи готовы (%s)", gw.id, st["fp"])
        return st

    def gateway_rekey_next_body(self, slot_id: int, stage: bool = False) -> Optional[dict]:
        """Тело link_next: конфиг шлюзовой стороны с новыми ключами (чистый
        текст со штампом), отпечаток пары. Не staged/asked — None."""
        gw = self._gw_slot(slot_id)
        st = self.gateway_rekey(slot_id)
        if not stage and st.get("state") not in ("staged", "asked"):
            return None
        vps_next, gw_next = self._rekey_paths(gw)
        try:
            with open(gw_next, encoding="utf-8") as f:
                gconf = wgconf.parse(f.read())
            with open(vps_next, encoding="utf-8") as f:
                vconf = wgconf.parse(f.read())
        except (OSError, wgconf.WgConfError) as e:
            log.warning("ключи линка: слот %s, .next не прочитан: %s", slot_id, e)
            return None
        gconf.comments = []
        fp = gwcfg.key_fp(vconf.get("Peer", "PublicKey"), gconf.get("Peer", "PublicKey"))
        out = {"id": st.get("id", ""), "conf": wgconf.render(gconf, "link", stamp=gwcfg.LINK_CONTRACT), "fp": fp}
        # без вопроса в чате бота шлюза; сопряжение — агент 3.4.0 и новее
        # (join1) заведомо, а возможностей у сессии сопряжения нет
        if st.get("auto") == "join" or (st.get("auto") and "rekey2" in self.gwlink_agent_caps(slot_id)):
            out["auto"] = st["auto"]
        return out

    @staticmethod
    def _rekey_notes(st: dict, notes: list, *, done: bool = False) -> list:
        """Уведомления перехода: смена без вопроса (auto) — итог без звука,
        отказы молча (о них скажет строка карточки после трёх)."""
        if not st.get("auto"):
            return notes
        if not done:
            return []
        for n in notes:
            n.silent = True
        return notes

    # ── ответы шлюза ─────────────────────────────────────────────────────────

    def gateway_rekey_ack_in(self, slot_id: int, body: dict) -> tuple[str, list[Notification]]:
        """link_ack: asked — шлюз спросил человека; ready — можно переключать
        (возвращает "go"); declined — снято, а если ВПС уже переключался
        (link_apply на шлюзе отказал) — сразу прежние ключи, не через 3 мин.
        (действие, уведомления)."""
        with _REKEY_LOCK:
            st = self.gateway_rekey(slot_id)
            if not st or st.get("id") != str(body.get("id") or ""):
                return "", []
            state = str(body.get("state") or "")
            gw = self._gw_slot(slot_id)
            if state == "asked" and st.get("state") == "staged":
                st["state"] = "asked"
                self._rekey_save(slot_id, st)
                return "", []
            if state == "ready" and st.get("state") in ("staged", "asked"):
                return "go", []
            if state != "declined" or st.get("state") not in ("staged", "asked", "switched"):
                return "", []
            error = " ".join(str(body.get("error") or "").split())[:200]
            if st.get("state") == "switched":
                committed = bool(st.get("committed"))
                self._rekey_run(gw, "--rekey-revert" if committed else "--rekey-drop")
                self._rekey_save(slot_id, {"state": "declined", "at": timeutil.to_iso(timeutil.now()), "error": error,
                                           "auto": st.get("auto") or ""})
                if committed:
                    self._rekey_restarted(slot_id)
                text = f"⚠️ {self._gw_link(gw)}: шлюз не поднял новые ключи линка" + (f": {_esc(error)}" if error else "")
                return "", self._rekey_notes(st, [Notification(config.ADMIN_ID, text + " — вернул прежние")])
            self._rekey_run(gw, "--rekey-drop")
            self._rekey_save(slot_id, {"state": "declined", "at": timeutil.to_iso(timeutil.now()), "error": error,
                                           "auto": st.get("auto") or ""})
            text = (f"🔑 {self._gw_link(gw)}: смену ключей линка отклонили в чате бота шлюза" if not error else
                    f"⚠️ {self._gw_link(gw)}: шлюз не принял новые ключи линка: {_esc(error)}")
            return "", self._rekey_notes(st, [Notification(config.ADMIN_ID, text)])

    def gateway_rekey_go(self, slot_id: int) -> None:
        """link_go ушёл в сокет: дальше — переключение ВПС и срок 180 с."""
        with _REKEY_LOCK:
            st = self.gateway_rekey(slot_id)
            if st.get("state") not in ("staged", "asked"):
                return
            st.update(state="switched", go_at=timeutil.to_iso(timeutil.now()), committed=False)
            self._rekey_save(slot_id, st)

    def gateway_rekey_commit(self, slot_id: int) -> tuple[bool, list[Notification]]:
        """Через секунды после link_go: живые конфиги → .prev, .next → живые,
        линк ВПС переподнят, политика слота заново. Не вышло — прежние ключи.
        (переключился ли, уведомления): переключился — сессию слота слушатель
        рвёт, чтобы шлюз пришёл новым hello."""
        with _REKEY_LOCK:
            gw = self._gw_slot(slot_id)
            st = self.gateway_rekey(slot_id)
            if st.get("state") != "switched" or st.get("committed"):
                return False, []
            err = self._rekey_run(gw, "--rekey-commit")
            if err:
                self._rekey_run(gw, "--rekey-revert")    # .next прочь; скрипт уже вернул прежние, если подменял
                self._rekey_save(slot_id, {})
                self._rekey_cancel_queue(slot_id, str(st.get("id") or ""))
                self._rekey_restarted(slot_id)
                return False, self._rekey_notes(st, [Notification(
                    config.ADMIN_ID, f"⚠️ {self._gw_link(gw)}: новые ключи линка не поднялись — вернул прежние")])
            st["committed"] = True
            self._rekey_save(slot_id, st)
            self._rekey_restarted(slot_id)
            return True, []

    def gateway_rekey_hello(self, slot_id: int, session_key: bytes | None = None) -> dict:
        """Сессия слота открылась (hello подписан ключом session_key — тем, что
        слушатель вывел из /root/gw-<линк>.conf при приёме коннекта). Зовётся
        ПЕРВЫМ шагом hello, до любого слова сервера: шлюз закрепляет выбор по
        первому слову, и решение сервера к этому моменту обязано быть записано.
        {"notes", "hold": закрыть сессию молча (ВПС ещё не переключился, или
        hello подписан прежним ключом, или переход занят — шлюз придёт снова),
        "rekeyed": переход зафиксирован}."""
        none = {"notes": [], "hold": False, "rekeyed": False}
        if self.gateway_rekey(slot_id).get("state") != "switched":
            return none                                   # быстрый путь: замок не нужен
        if not _REKEY_LOCK.acquire(timeout=5):
            return {**none, "hold": True}                 # commit или возврат идут — пусть шлюз придёт снова
        try:
            st = self.gateway_rekey(slot_id)
            if st.get("state") != "switched":
                return none
            if not st.get("committed"):
                return {**none, "hold": True}
            gw = self._gw_slot(slot_id)
            if session_key is not None:
                from awgbot.util import gwlink
                try:
                    current = gwlink.channel_key(self._link_privkey(gw))
                except Exception:                         # noqa: BLE001
                    current = None
                if current != session_key:
                    # ключ сессии прочитан до подмены файла — это hello прежним ключом
                    return {**none, "hold": True}
            self._rekey_run(gw, "--rekey-drop")
            self._rekey_save(slot_id, {})
        finally:
            _REKEY_LOCK.release()
        log.info("ключи линка: слот %s на новых ключах", slot_id)
        self._backup_event(gw.id, "rekey")                # бэкап со старыми ключами больше не годится
        if st.get("auto") == "join" and hasattr(self, "gwjoin_linked"):
            self.gwjoin_linked(slot_id)                   # сопряжение: карантин снят, .join-prev прочь
            # итог — сообщением подключения («код верный» → «шлюз подключён»), не «ключи сменены» (ui-7)
            return {"notes": [], "hold": False, "rekeyed": True}
        notes = [Notification(config.ADMIN_ID, f"🔑 {self._gw_link(gw)}: ключи линка сменены")]
        return {"notes": self._rekey_notes(st, notes, done=True), "hold": False, "rekeyed": True}

    def gateway_rekey_done_in(self, slot_id: int, body: dict) -> None:
        """link_done — подтверждение шлюза для журнала: фиксирует hello."""
        log.info("ключи линка: слот %s сообщил итог перехода: %s, ключи %s", slot_id,
                 "ok" if body.get("ok") else "отказ", str(body.get("keys") or "")[:8])

    # ── сроки и старт ────────────────────────────────────────────────────────

    def gateway_rekey_tick(self) -> tuple[list[tuple[int, str]], list[Notification]]:
        """Такт живости: ([(слот, id) для link_cancel], уведомления).
        Сутки без решения — снято; 180 с после link_go без hello новым ключом —
        прежние ключи (шлюз прежние найдёт сам — поиск); отказ шлюза — строка
        карточки сутки."""
        cancel: list[tuple[int, str]] = []
        notes: list[Notification] = []
        now = timeutil.now()
        for gw in self.db.gateways():
            with _REKEY_LOCK:
                st = self.gateway_rekey(gw.id)
                state = st.get("state")
                if state in ("staged", "asked", "declined"):
                    if _age(now, st.get("at", ""), 0) <= (DECLINED_HOURS if state == "declined" else ASK_HOURS) * 3600:
                        continue
                    self._rekey_save(gw.id, {})
                    if state == "declined":
                        continue
                    self._rekey_run(gw, "--rekey-drop")
                    cancel.append((gw.id, str(st.get("id") or "")))
                    self._rekey_cancel_queue(gw.id, str(st.get("id") or ""))   # нет сессии — уйдёт при подключении
                    notes += self._rekey_notes(st, [Notification(
                        config.ADMIN_ID, f"🔑 {self._gw_link(gw)}: смена ключей линка отменена — за сутки её не подтвердили")])
                elif state == "switched" and _age(now, st.get("go_at", ""), SWITCH_SECONDS + 1) > SWITCH_SECONDS:
                    self._rekey_run(gw, "--rekey-revert")
                    self._rekey_save(gw.id, {})
                    self._rekey_cancel_queue(gw.id, str(st.get("id") or ""))
                    self._rekey_restarted(gw.id)
                    notes += self._rekey_notes(st, [Notification(
                        config.ADMIN_ID, f"⚠️ {self._gw_link(gw)}: новые ключи линка не поднялись за 3 мин — вернул прежние")])
        return cancel, notes

    def gateway_rekey_startup(self) -> list[Notification]:
        """Старт бота посреди переключения: что стало со шлюзом, сервер знать не
        может — прежние ключи на место (шлюз найдёт их поиском). Один раз за
        процесс: зовут и старт слушателя, и первый такт живости — кто раньше."""
        notes: list[Notification] = []
        with _REKEY_LOCK:
            if self.__dict__.get("_rekey_boot_done"):
                return notes
            self.__dict__["_rekey_boot_done"] = True
            for gw in self.db.gateways():
                st = self.gateway_rekey(gw.id)
                if st.get("state") == "switched":
                    self._rekey_run(gw, "--rekey-revert")
                    self._rekey_save(gw.id, {})
                    self._rekey_cancel_queue(gw.id, str(st.get("id") or ""))
                    self._rekey_restarted(gw.id)
                    notes += self._rekey_notes(st, [Notification(
                        config.ADMIN_ID, f"⚠️ {self._gw_link(gw)}: перезапуск бота посреди смены ключей линка — вернул прежние")])
        return notes

    def gateway_rekey_slots(self) -> list[tuple[int, str]]:
        """Шлюзы, которым смену ключей можно предложить каналом (агент не ниже
        3.3.0): [(слот, «имя (подпись)»)] — для «🔧 Сервис → 🔑 Ключи линка»."""
        return [(gw.id, self._gw_display(gw)) for gw in self.db.gateways()
                if gw.device_id and not self.gwlink_agent_outdated(gw.id)]

    def _backup_event(self, slot_id: int, kind: str) -> None:
        """Отметка «нужна свежая копия» (services/backups); отказ отметки
        событие не ломает."""
        mark = getattr(self, "backup_event_mark", None)
        if mark is None:
            return
        try:
            mark(kind, slot_id)
        except Exception as e:                            # noqa: BLE001
            log.warning("бэкап по событию %s не отмечен: %s", kind, e)

    def gateway_rekey_forget(self, slot_id: int) -> None:
        """Переход и очередь отмен слота — по реестру slotkeys."""
        slotkeys.forget(self.db, slot_id, slotkeys.names(part="rekey"))

    def gateway_rekey_abort(self, gw) -> None:
        """Ключи линка слота меняются другим путём (замена устройства, файл):
        переход, если шёл, снимается вместе с .next/.prev — иначе возврат по
        сроку поставил бы прежние ключи поверх нового устройства."""
        with _REKEY_LOCK:
            st = self.gateway_rekey(gw.id)
            if not st:
                return
            self._rekey_run(gw, "--rekey-drop")
            self._rekey_save(gw.id, {})
        log.info("ключи линка: слот %s — переход снят заменой ключей", gw.id)


def _age(now, iso: str, default: float) -> float:
    try:
        return timeutil.elapsed(timeutil.parse_iso(iso), now)
    except (ValueError, TypeError):
        return default


def _esc(text: str) -> str:
    import html
    return html.escape(text, quote=False)
