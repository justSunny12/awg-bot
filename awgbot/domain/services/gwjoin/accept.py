"""accept.py — часть пакета gwjoin (см. __init__): верный код, фиксация и возврат.

Верный код: строка слота — новое устройство «Шлюз N» или своё (новый слот),
замена устройства в слоте, «🔁 Настроить заново» (то же устройство); снимок,
сессия и документ прежнего устройства — вон (gwlink_forget); join_ok —
секреты под SecretBox(Kp); смена ключей `auto=join` (постоянные ключи, PSK,
Jc…H4, I1). Фиксация — hello новым ключом (gateway_link/rekey.py): штампа
сопряжения в конфиге больше нет — карантин снят, `.join-prev` прочь. Итог —
`join_done` от агента, уже полного (или `installed` нового агента), — «шлюз подключён».

Возврат (отмена, срок, третий неверный код) — сразу: новый слот — линк
снимается (первый слот — новые случайные ключи, как при снятии слота), замена
— `.join-prev` на место; слушатель на адресе закрывается со следующим
`ensure`, файервол перерисовывается.
"""
from __future__ import annotations

import types
from typing import Optional

from awgbot.core import config
from awgbot.domain.services.types import ServiceError
from awgbot.infra import routing
from awgbot.util import joinsas, timeutil

from ..gateway_link.common import log
from .state import _LOCK


class GwJoinAcceptMixin:
    """Верный код, фиксация сопряжения, итог и возврат."""

    def gwjoin_accept(self, slot_id: int) -> None:
        """Строка слота и устройство — по виду сопряжения; затем смена ключей
        без вопроса. Зовёт gwjoin_code под замком сопряжения."""
        st = self.gwjoin(slot_id)
        admin = self.admin_client()
        if not st or admin is None:
            raise ServiceError("сопряжения нет")
        kind = st.get("kind")
        gw = self.db.gateway(slot_id)
        dev_id = st.get("device")
        made = 0
        if kind in ("new", "replace") and (dev_id == "new" or dev_id is None):
            name = st.get("name") if kind == "new" else ""
            dc = self.add_device(admin.id, name or (self._GW_NEW_NAME if int(slot_id) == 1
                                                    else f"{self._GW_NEW_NAME} {slot_id}"))
            dev_id = made = dc.device_id
        dev = self.db.get_device(int(dev_id)) if dev_id not in (None, "new") else None
        if dev is None:
            raise ServiceError("устройство слота не найдено")
        if dev.routing_on:
            self.set_routing_device(dev.id, False)        # шлюзу РФ-доступ не нужен никогда
        if kind == "new":
            gw = self.db.gateway_add(dev.id, st["link_if"], int(st["port"]), st["cidr"], slot_id=int(slot_id))
            self._backup_event(gw.id, "assign")
        elif gw is not None:
            if kind == "replace" and gw.device_id != dev.id:
                self.db.gateway_update(gw.id, device_id=dev.id)
                self._backup_event(gw.id, "replace")
            self.gwlink_forget(gw.id)                     # снимок, сессия, документ прежней установки — вон
        # эфемерный ключ шлюза, чей код ввели: после кода сессия — только с ним
        st.update(state="accepted", device=dev.id, eph=str(self._gwjoin_live(slot_id).get("eph") or ""),
                  accepted_at=timeutil.to_iso(timeutil.now()))
        if made:
            st["made"] = int(made)                        # заведено кодом — возврат его снимет
        token = self.gw_bot_token(int(slot_id))
        if token:
            # токен бота, введённый при подключении (или стоявший у слота), — шлюзу
            # разделом token документа: у новой установки другого пути к нему нет
            from awgbot.domain import gwcfg
            self.db.set_state(self._gw_slot_key(self._TOKEN_PUSH_KEY, int(slot_id)),
                              gwcfg.token_print(token.strip()))
        self._gwjoin_save(slot_id, st)
        try:
            self.gateway_rekey_stage(int(slot_id), auto="join")
        except ServiceError as e:
            log.warning("сопряжение: слот %s — смена ключей после кода не начата: %s", slot_id, e)
        log.info("сопряжение: слот %s — код верный (%s)", slot_id, kind)

    def gwjoin_next_body(self, slot_id: int) -> Optional[dict]:
        """link_next сессии сопряжения после верного кода. Смены ключей нет
        (снята сроком переключения или перезапуском сервера посреди неё,
        join-1) — заводится заново: иначе сопряжение ждало бы до отката."""
        nxt = self.gateway_rekey_next_body(slot_id)
        if nxt or self.gwjoin(slot_id).get("state") != "accepted" or self.gateway_rekey(slot_id):
            return nxt
        try:
            self.gateway_rekey_stage(int(slot_id), auto="join")
        except ServiceError as e:
            log.warning("сопряжение: слот %s — смена ключей после кода не начата заново: %s", slot_id, e)
            return None
        return self.gateway_rekey_next_body(slot_id)

    def gwjoin_ok_body(self, slot_id: int) -> dict:
        """Тело join_ok живой сессии: секреты под ключом сопряжения."""
        live = self._gwjoin_live(slot_id)
        st = self.gwjoin(slot_id)
        if not live.get("kp") or not st:
            return {}
        secrets_ = {"admin_id": int(config.ADMIN_ID), "server_bot": getattr(self, "bot_username", "") or "",
                    "slot": int(slot_id), "name": st.get("name") or ""}
        return {"box": joinsas.seal(live["kp"], secrets_)}

    def gwjoin_linked(self, slot_id: int) -> None:
        """Смена ключей после кода зафиксирована (hello новым ключом): штампа
        нет — карантин снят; прежние ключи замены прочь; маршрут слота."""
        with _LOCK:
            st = self.gwjoin(slot_id)
            if st.get("state") != "accepted":
                return
            gw = self.db.gateway(slot_id)
            if gw is not None and st.get("kind") != "new":
                try:
                    self._run_link_script("--join-drop", self._slot_env(gw))
                except ServiceError as e:
                    log.warning("сопряжение: слот %s — .join-prev не убран: %s", slot_id, e)
            st["state"] = "linked"
            st["linked_at"] = timeutil.to_iso(timeutil.now())
            self._gwjoin_save(slot_id, st)
            self._gwjoin_live_set(slot_id, None)
        self._gw_firewall_refresh()
        try:
            routing.invalidate_self_check()
            self._ensure_gateway_policy()
        except routing.RoutingError as e:
            log.warning("сопряжение: обвязка слота %s не доведена: %s", slot_id, e)
        self._channel_touch()
        log.info("сопряжение: слот %s на постоянных ключах — карантин снят", slot_id)

    def gwjoin_installed(self, slot_id: int, join_id: str = "") -> dict:
        """Агент подключённого шлюза сказал «установлен» или join_done —
        сопряжение доведено: запись снимается, возвращается (для итога «шлюз подключён»); не
        было (или id другого сопряжения) — пусто."""
        with _LOCK:
            st = self.gwjoin(slot_id)
            if st.get("state") not in ("linked", "accepted") or (join_id and st.get("id") != join_id):
                return {}
            self._gwjoin_save(slot_id, {})
            return st

    def gwjoin_done_info(self, slot_id: int) -> tuple[str, str]:
        """(версия агента из снимка, имя бота шлюза) для итога «шлюз подключён»."""
        snap = self.gwlink_snapshot(int(slot_id)) or {}
        me = self.gw_bot_identity(int(slot_id)) or {}
        return str(snap.get("agent_version") or "").lstrip("v"), str(me.get("username") or "")

    def gwjoin_revert(self, slot_id: int, why: str) -> str:
        """Интерфейс под токен — прочь сразу: новый слот — линк снят
        (первый — новые случайные ключи), замена — прежние ключи на место.
        "" — сделано, запись снята; иначе причина отказа: запись остаётся
        «closing», такт сопряжений повторит (quar-4), человеку — честно."""
        with _LOCK:
            st = self.gwjoin(slot_id)
            if not st:
                return ""
            self._gwjoin_live_set(slot_id, None)
            st["state"] = "closing"
            st.setdefault("why", why)
            self._gwjoin_save(slot_id, st)
        slot = types.SimpleNamespace(id=int(slot_id), link_if=st["link_if"], link_port=int(st["port"]),
                                     link_cidr=st["cidr"])
        err = ""
        try:
            if st.get("kind") == "new":
                if self.db.gateway(slot_id) is not None:
                    # код был верный, а смена ключей не встала (срок accepted):
                    # строка слота, заведённая кодом, — прочь вместе с линком
                    self._gwjoin_drop_new_row(slot_id)
                self._gwjoin_drop_made(st)
                if int(slot_id) == 1:
                    self._run_link_script("--rekey", self._slot_env(slot))
                else:
                    self._run_link_script("--rollback", self._slot_env(slot))
                    try:
                        routing.drop_slot_policy(int(slot_id), st["link_if"])
                    except routing.RoutingError:
                        pass
            else:
                if self.gateway_rekey(int(slot_id)):
                    self.gateway_rekey_abort(self.db.gateway(slot_id) or slot)   # смена после кода — снята
                self._run_link_script("--join-revert", self._slot_env(slot))
                gw = self.db.gateway(slot_id)
                prev = int(st.get("prev_device") or 0)
                if gw is not None and prev and gw.device_id != prev and self.db.get_device(prev) is not None:
                    # код был верный (устройство уже заменено), а смена ключей не
                    # встала: линк — прежнему устройству, и слот — ему же
                    self.gwlink_forget(gw.id)
                    self.db.gateway_update(gw.id, device_id=prev)
                self._gwjoin_drop_made(st)
                try:
                    self._ensure_gateway_policy()
                except routing.RoutingError as e:
                    log.warning("сопряжение: политика слота %s не вернулась: %s", slot_id, e)
                self._channel_touch()
        except ServiceError as e:
            err = str(e)
            log.warning("сопряжение: слот %s — интерфейс под токен не снят: %s", slot_id, e)
        if not err:
            with _LOCK:
                if self.gwjoin(slot_id).get("id") == st.get("id"):
                    self._gwjoin_save(slot_id, {})
        self._gw_firewall_refresh()
        log.info("сопряжение: слот %s — %s (%s)", slot_id, "закрыто" if not err else "возврат не удался", why)
        return err

    def _gwjoin_drop_made(self, st: dict) -> None:
        """Устройство «Шлюз N», заведённое верным кодом, — прочь вместе с
        недоведённым сопряжением: в списке админа не остаётся пира-сироты.
        Отказ — в журнал: линк важнее, устройство админ снимет сам."""
        made = int(st.get("made") or 0)
        if not made or self.db.get_device(made) is None or self.db.gateway_by_device(made) is not None:
            return
        try:
            self.remove_device(made)
        except ServiceError as e:
            log.warning("сопряжение: устройство %s, заведённое кодом, не снято: %s", made, e)

    def _gwjoin_drop_new_row(self, slot_id: int) -> None:
        """Строка нового слота, заведённая верным кодом, — прочь (сопряжение не
        доведено); устройство, заведённое кодом, снимает _gwjoin_drop_made."""
        gw = self.db.gateway(slot_id)
        if gw is None:
            return
        self.gateway_rekey_abort(gw)
        self.gwlink_forget(gw.id)
        self._slot_forget(gw.id)
        self.db.gateway_delete(gw.id)
