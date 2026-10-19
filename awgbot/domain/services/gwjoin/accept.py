"""accept.py — часть пакета gwjoin (см. __init__): верный код, фиксация и возврат.

Верный код: строка слота — новое устройство «Шлюз N» или своё (новый слот),
замена устройства в слоте, «🔁 Настроить заново» (то же устройство); снимок,
сессия и документ прежнего устройства — вон (gwlink_forget); join_ok —
секреты под SecretBox(Kp); смена ключей `auto=join` (постоянные ключи, PSK,
Jc…H4, I1). Фиксация — hello новым ключом (gateway_link/rekey.py): штампа
сопряжения в конфиге больше нет — карантин снят, `.join-prev` прочь. Итог —
`installed` от агента, уже полного (Т-П9).

Возврат (отмена, срок, третий неверный код) — сразу (Р24): новый слот — линк
снимается (первый слот — новые случайные ключи, как при снятии слота), замена
— `.join-prev` на место; слушатель на адресе закрывается со следующим
`ensure`, файервол перерисовывается.
"""
from __future__ import annotations

import types

from awgbot.core import config
from awgbot.domain.services.types import ServiceError
from awgbot.infra import routing
from awgbot.util import joinsas

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
        if kind in ("new", "replace") and (dev_id == "new" or dev_id is None):
            name = st.get("name") if kind == "new" else ""
            dc = self.add_device(admin.id, name or (self._GW_NEW_NAME if int(slot_id) == 1
                                                    else f"{self._GW_NEW_NAME} {slot_id}"))
            dev_id = dc.device_id
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
        st.update(state="accepted", device=dev.id)
        self._gwjoin_save(slot_id, st)
        try:
            self.gateway_rekey_stage(int(slot_id), auto="join")
        except ServiceError as e:
            log.warning("сопряжение: слот %s — смена ключей после кода не начата: %s", slot_id, e)
        log.info("сопряжение: слот %s — код верный (%s)", slot_id, kind)

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

    def gwjoin_installed(self, slot_id: int) -> dict:
        """Агент подключённого шлюза сказал «установлен» — сопряжение доведено:
        запись снимается, возвращается (для Т-П9); не было — пусто."""
        with _LOCK:
            st = self.gwjoin(slot_id)
            if st.get("state") not in ("linked", "accepted"):
                return {}
            self._gwjoin_save(slot_id, {})
            return st

    def gwjoin_done_info(self, slot_id: int) -> tuple[str, str]:
        """(версия агента из снимка, имя бота шлюза) для Т-П9."""
        snap = self.gwlink_snapshot(int(slot_id)) or {}
        me = self.gw_bot_identity(int(slot_id)) or {}
        return str(snap.get("agent_version") or "").lstrip("v"), str(me.get("username") or "")

    def gwjoin_revert(self, slot_id: int, why: str) -> None:
        """Интерфейс под токен — прочь сразу (Р24): новый слот — линк снят
        (первый — новые случайные ключи), замена — прежние ключи на место."""
        with _LOCK:
            st = self.gwjoin(slot_id)
            if not st:
                return
            self._gwjoin_live_set(slot_id, None)
            self._gwjoin_save(slot_id, {})
        slot = types.SimpleNamespace(id=int(slot_id), link_if=st["link_if"], link_port=int(st["port"]),
                                     link_cidr=st["cidr"])
        try:
            if st.get("kind") == "new":
                if int(slot_id) == 1:
                    self._run_link_script("--rekey", self._slot_env(slot))
                else:
                    self._run_link_script("--rollback", self._slot_env(slot))
                    try:
                        routing.drop_slot_policy(int(slot_id), st["link_if"])
                    except routing.RoutingError:
                        pass
            else:
                self._run_link_script("--join-revert", self._slot_env(slot))
                try:
                    self._ensure_gateway_policy()
                except routing.RoutingError as e:
                    log.warning("сопряжение: политика слота %s не вернулась: %s", slot_id, e)
                self._channel_touch()
        except ServiceError as e:
            log.warning("сопряжение: слот %s — интерфейс под токен не снят: %s", slot_id, e)
        self._gw_firewall_refresh()
        log.info("сопряжение: слот %s — закрыто (%s)", slot_id, why)
