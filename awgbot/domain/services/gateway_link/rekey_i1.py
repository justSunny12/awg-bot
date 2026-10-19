"""rekey_i1.py — I1 живым линкам (§3.10 концепта «единые обновления»):
автоматическая смена ключей `auto=i1`, когда I1 шлюзовой стороны линка
(/root/gw-<линк>.conf) не тот, что у клиентского интерфейса сервера (Р26) —
и когда его там нет, и когда клиентский сменили на хосте.

Условие слота: агент объявил `rekey2`, на связи, смены ключей нет ни у кого,
ход набора обновлений не идёт, сопряжения нет. Попытка — в случайный момент
окна 30 мин…6 ч от того, как слот попал в план; по одному слоту за раз,
резервные раньше активного (следующий ждёт, пока предыдущий не встал).
Не встало (переход снят: отказ, срок, возврат) — прежние ключи, как у любой
смены; повтор через 24 ч ±40 %; после трёх неудач — строка карточки Т-Л1
(повторы идут и дальше, раз в сутки). Цена — секунда без линка на слот.

Состояние слота — `gwlink_i1_<слот>`: {"want": I1, ради которого план,
"next_at", "tries", "rid": id смены ключей, начатой планом}.
"""
from __future__ import annotations

import json
import random
from typing import Optional

from awgbot.core import config
from awgbot.domain.services.types import Notification, ServiceError
from awgbot.util import timeutil

from .common import log

WINDOW_S = (30 * 60, 6 * 3600)     # первая попытка — в случайный момент окна
RETRY_S = 24 * 3600                # повтор после неудачи
RETRY_JITTER = 0.4
TRIES_WARN = 3                     # столько неудач — строка карточки Т-Л1
NONE_TOLD_KEY = "gw_i1_none_told"  # Т-Л2 сказано (один раз, пока у клиентов нет I1)

T_L1 = "⚠️ Новые ключи и обфускация линка не встали трижды — линк на прежних, повторю через сутки"
T_L2 = "⚠️ Линк без I1: у клиентского интерфейса сервера AWG нет строки I1 — линк берёт её оттуда"


def _later(seconds: float) -> str:
    return timeutil.to_iso(timeutil.shift(timeutil.now(), int(seconds)))


def _due(iso: str) -> bool:
    try:
        return timeutil.elapsed(timeutil.parse_iso(iso)) >= 0
    except (ValueError, TypeError):
        return True


class RekeyI1Mixin:
    """План смен ключей ради I1 и строки карточки."""

    _I1_KEY = "gwlink_i1"

    def gateway_i1_plan(self, slot_id: int) -> dict:
        raw = self.db.get_state(self._gw_slot_key(self._I1_KEY, slot_id)) or ""
        try:
            data = json.loads(raw) if raw.startswith("{") else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _i1_plan_save(self, slot_id: int, plan: dict) -> None:
        self.db.set_state(self._gw_slot_key(self._I1_KEY, slot_id), json.dumps(plan, ensure_ascii=False) if plan else "")

    def _i1_blocked(self) -> bool:
        """Переход на I1 не начинать: идёт ход набора обновлений или сопряжение."""
        running = getattr(self, "fleet_running", None)
        if callable(running) and running():
            return True
        joining = getattr(self, "gwjoin_active", None)
        return bool(callable(joining) and joining())

    def gateway_i1_tick(self) -> tuple[list[int], list[Notification]]:
        """Такт живости: ([слот, которому начата смена ключей], уведомления).
        Ничего в сеть — только план и стадия; link_next доставит слушатель."""
        notes: list[Notification] = []
        ok, want = self.gateway_client_i1()
        if not ok:
            return [], notes
        notes += self._i1_none_note(want)
        gws = sorted(self.db.gateways(), key=lambda g: (self._i1_is_active(g), g.id))   # резервные, активный последним
        busy = any(self.gateway_rekey_active(g.id) for g in gws)   # смена ключей идёт (своя или ручная) — ждём итога
        started: list[int] = []
        queued = False                                    # слот раньше по порядку ждёт своей попытки
        for gw in gws:
            plan = self.gateway_i1_plan(gw.id)
            got, cur = self.gateway_link_i1(gw)
            if not got or not gw.device_id:
                continue
            if cur == want:
                if plan:
                    self._i1_plan_save(gw.id, {})
                    if plan.get("rid"):
                        log.info("I1 линка: слот %s на I1 клиентского интерфейса", gw.id)
                continue
            if "rekey2" not in self.gwlink_agent_caps(gw.id):
                continue                                  # агент без смены ключей без вопроса — попыток нет
            if plan.get("want") != want:
                # новый план (или клиентский I1 сменили — прежние неудачи не о нём)
                plan = {"want": want, "next_at": _later(random.uniform(*WINDOW_S)), "tries": 0}
                self._i1_plan_save(gw.id, plan)
            if plan.get("rid") and not self.gateway_rekey_active(gw.id):
                # смена, начатая планом, закончилась, а I1 у линка прежний — не встало
                plan["tries"] = int(plan.get("tries") or 0) + 1
                plan["rid"] = ""
                plan["next_at"] = _later(RETRY_S * random.uniform(1 - RETRY_JITTER, 1 + RETRY_JITTER))
                self._i1_plan_save(gw.id, plan)
                log.info("I1 линка: слот %s — смена ключей не встала (%s), повтор через сутки", gw.id, plan["tries"])
            if not self.gwlink_session(gw.id):
                continue                                  # не на связи — никого не держит
            if queued or busy:
                continue                                  # по одному слоту: следующий ждёт
            queued = True
            if not _due(str(plan.get("next_at") or "")) or self._i1_blocked():
                continue
            try:
                st = self.gateway_rekey_stage(gw.id, auto="i1")
            except ServiceError as e:
                log.info("I1 линка: слот %s — смена ключей не начата: %s", gw.id, e)
                plan["next_at"] = _later(RETRY_S * random.uniform(1 - RETRY_JITTER, 1 + RETRY_JITTER))
                self._i1_plan_save(gw.id, plan)
                continue
            plan["rid"] = st.get("id", "")
            self._i1_plan_save(gw.id, plan)
            started.append(gw.id)
            busy = True
            log.info("I1 линка: слот %s — смена ключей ради I1 начата", gw.id)
        return started, notes

    def _i1_is_active(self, gw) -> bool:
        active = self.active_gateway()
        return active is not None and active.id == gw.id

    def _i1_none_note(self, want: str) -> list[Notification]:
        """Т-Л2 один раз без звука: у клиентского интерфейса нет I1, а линки есть."""
        told = bool(self.db.get_state(NONE_TOLD_KEY))
        if want:
            if told:
                self.db.set_state(NONE_TOLD_KEY, "")
            return []
        if told or not any(gw.device_id for gw in self.db.gateways()):
            return []
        self.db.set_state(NONE_TOLD_KEY, "1")
        return [Notification(config.ADMIN_ID, T_L2, silent=True)]

    def gateway_i1_line(self, slot_id: int) -> Optional[str]:
        """Строка карточки слота: Т-Л1 (трижды не встало); иначе None. Без
        сети. Т-Л2 строкой карточки не бывает: линк без I1, когда его нет и у
        клиентов, — не ошибка, а «как у клиентов»; о нём — одно уведомление."""
        plan = self.gateway_i1_plan(slot_id)
        if int(plan.get("tries") or 0) >= TRIES_WARN:
            return T_L1
        return None
