"""Снимки админа: подключение шлюза командой — сообщение подключения во всех
состояниях (ожидание, подключился, заново, с двух адресов, неверный код,
попытки кончились, код верный, готово, отменено, срок вышел, не актуально),
замена устройства и её отмена, «🔁 Настроить заново», строки карточки и
«Шлюзов» во время сопряжения (Т-П1…Т-П18).

Сеть шлюза не трогается: «шлюз подключился» — запись сопряжения и код в
памяти процесса так, как их положил бы слушатель канала по hello
сопряжения, и событие хода тем же крючком, что у слушателя. Токен и id
сопряжений — заглушки построителя (shots_admin_common_gw._routing).
"""
from __future__ import annotations

from awgbot.bot.callbacks import GwJoinCB, GwMarkCB, GwSlotCB, SetCB

from tests.screens.base import Shot
from tests.screens.shots_admin_common_gw import TOKEN2, _gw, _slots

CODE = "K7F2QX"
J1 = f"{1:016x}"                    # id первого сопряжения снимка
J2 = f"{2:016x}"


def _token2(services, mp):
    services._shot_tokens[2] = TOKEN2


def _kit(services, mp):
    """Сервер не из выпуска (своя сборка): выпуска на GitHub нет — кит."""
    _token2(services, mp)
    mp.setattr(services, "_gwjoin_kit", lambda: True)


def _joined(slot: int, *addrs: str, hosts: dict | None = None):
    """Шлюз подключился к сопряжению слота с адресов addrs (последний — живая
    сессия) — как это записал бы слушатель канала; код — в памяти."""
    async def step(services, bot):
        st = services.gwjoin(slot)
        st["endpoints"] = list(addrs)
        st["hosts"] = dict(hosts or {})
        st["sessions"] = len(addrs)
        st["state"] = "connected"
        services._gwjoin_save(slot, st)
        services._gwjoin_live_set(slot, {"code": CODE, "kp": bytes(32), "sid": "s1"})
    return step


def _event(slot: int, event: str, *addrs: str, hosts: dict | None = None):
    """Событие хода сопряжения тем же крючком, что у слушателя канала."""
    async def step(services, bot):
        from awgbot.bot.handlers.settings import on_join_event
        if addrs:
            await _joined(slot, *addrs, hosts=hosts)(services, bot)
        await on_join_event(bot, services, event, services.gwjoin(slot), {})
    return step


def _expire(slot: int):
    """Час вышел: такт сопряжений закрывает и правит сообщение (Т-П11)."""
    async def step(services, bot):
        from awgbot.bot.handlers.settings import on_join_event
        from awgbot.runtime import linkserver
        st = services.gwjoin(slot)
        until = st["expires"]
        st["expires"] = st["issued"]                     # часы снимка стоят: срок — уже в прошлом
        services._gwjoin_save(slot, st)
        for closed in services.gwjoin_tick():
            await linkserver.join_close(services, int(closed["slot"]), "expired")
            await on_join_event(bot, services, "expired", {**closed, "expires": until}, {})
    return step


def _done(slot: int):
    """Агент подключённого шлюза сказал «установлен» (Т-П9)."""
    async def step(services, bot):
        from awgbot.bot.handlers.settings import on_join_event
        snap = services.gwlink_snapshot(slot) or {}
        services.gwlink_snapshot_in(slot, {**snap, "agent_version": "1.2.3"}, 1, True)
        st = services.gwjoin_installed(slot)
        await on_join_event(bot, services, "done", st, {})
    return step


def _only(slot: int, event: str, **extra):
    """Событие хода как есть, состояние записи не трогается (ui-6, такт)."""
    async def step(services, bot):
        from awgbot.bot.handlers.settings import on_join_event
        await on_join_event(bot, services, event, services.gwjoin(slot), extra)
    return step


def _accepted(slot: int):
    """Код верный — запись accepted, как после gwjoin_code (без смены ключей)."""
    async def step(services, bot):
        st = services.gwjoin(slot)
        st["state"] = "accepted"
        services._gwjoin_save(slot, st)
    return step


NEW = GwMarkCB(action="new_yes", slot=0)          # «➕ Новое устройство» резервом, токен известен
REPLACE = GwMarkCB(action="new_yes", slot=1)      # замена устройства слота 1 новым


def _new(*steps, title: str, sid: str):
    return Shot(sid, role="admin", steps=[("press", NEW), *steps], data=_gw(1, _token2), title=title)


def _replace(*steps, title: str, sid: str):
    return Shot(sid, role="admin", steps=[("press", REPLACE), *steps], data=_slots(1), title=title)


JOIN = [
    # ── новое подключение: ход по сообщению подключения ──────────────────
    _new(title="Т-П1: команда подключения, жду шлюз", sid="adm.join.wait"),
    _new(("call", _event(2, "connected", "203.0.113.50", hosts={"203.0.113.50": "raspberrypi"}), "подключился"),
         title="Т-П2: шлюз подключился, hostname из hello", sid="adm.join.connected"),
    _new(("call", _event(2, "reconnected", "203.0.113.50", "203.0.113.50"), "заново"),
         title="Т-П2: подключился заново — код сменился", sid="adm.join.reconnected"),
    _new(("call", _event(2, "two", "203.0.113.50", "198.51.100.23",
                         hosts={"203.0.113.50": "raspberrypi", "198.51.100.23": "nas"}), "с двух адресов"),
         title="Т-П3: к слоту подключались с двух адресов", sid="adm.join.two"),
    _new(("press", GwJoinCB(action="code", slot=2, jid=J1)),
         title="«✏️ Ввести код» до подключения шлюза — всплывашка", sid="adm.join.code.early"),
    _new(("call", _joined(2, "203.0.113.50"), "подключился"), ("press", GwJoinCB(action="code", slot=2, jid=J1)),
         title="Т-П4: приглашение кода", sid="adm.join.code.ask"),
    _new(("call", _joined(2, "203.0.113.50"), "подключился"), ("press", GwJoinCB(action="code", slot=2, jid=J1)),
         ("text", "K7F"), title="Т-П5: не тот формат — попытка не считается", sid="adm.join.code.format"),
    _new(("call", _joined(2, "203.0.113.50"), "подключился"), ("press", GwJoinCB(action="code", slot=2, jid=J1)),
         ("text", "AAAAAA"), title="Т-П6: код не совпал", sid="adm.join.code.wrong"),
    _new(("call", _joined(2, "203.0.113.50"), "подключился"), ("press", GwJoinCB(action="code", slot=2, jid=J1)),
         ("text", "AAAAAA"), ("text", "BBBBBB"), ("text", "CCCCCC"),
         title="Т-П7: третий неверный — подключение закрыто сразу", sid="adm.join.code.refused"),
    _new(("call", _joined(2, "203.0.113.50"), "подключился"), ("press", GwJoinCB(action="code", slot=2, jid=J1)),
         ("text", "k7f-2qx"), title="Т-П8: код верный (регистр и дефис не важны)", sid="adm.join.code.ok"),
    _new(("call", _joined(2, "203.0.113.50"), "подключился"), ("press", GwJoinCB(action="code", slot=2, jid=J1)),
         ("text", "K7F2QX"), ("call", _done(2), "агент установлен"),
         title="Т-П9: шлюз подключён — версия агента и бот шлюза", sid="adm.join.done"),
    _new(("press", GwJoinCB(action="cancel", slot=2, jid=J1)),
         title="Т-П10: «⬅️ Отменить» — команда больше не действует", sid="adm.join.cancel"),
    _new(("call", _expire(2), "час вышел"),
         title="Т-П11: срок вышел — правка сообщения и уведомление без звука", sid="adm.join.expired"),
    _new(("press", GwJoinCB(action="code", slot=2, jid="00000000000000ff")),
         title="кнопка чужой (прежней) заявки — всплывашка", sid="adm.join.code.gone"),
    _new(("press", GwJoinCB(action="cancel", slot=2, jid=J1)), ("press", GwJoinCB(action="cancel", slot=2, jid=J1)),
         title="«⬅️ Отменить» второй раз — всплывашка, а не тишина (ui-2)", sid="adm.join.cancel.twice"),
    _new(("call", _joined(2, "203.0.113.50"), "подключился"), ("call", _accepted(2), "код верный"),
         ("call", _only(2, "connected"), "переподключился"),
         title="код уже верный, шлюз переподключился — Т-П8 остаётся (ui-6)", sid="adm.join.accepted.again"),
    _new(("call", _joined(2, "203.0.113.50"), "подключился"), ("call", _accepted(2), "код верный"),
         ("call", _only(2, "stuck"), "час после кода"),
         title="код верный, а ключи не встали за час — откат и уведомление (quar-3)", sid="adm.join.stuck"),
    _new(("call", _joined(2, "203.0.113.50"), "подключился"), ("call", _only(2, "stale"), "сутки без итога"),
         title="на постоянных ключах, а агент не доложил за сутки (quar-7)", sid="adm.join.stale_linked"),
    _new(("call", _only(2, "revert_failed", err="скрипт линка (--rollback) не отработал: занято"), "возврат упал"),
         title="возврат линка не удался — честно, один раз (quar-4)", sid="adm.join.revert_failed"),
    Shot("adm.join.kit", role="admin", steps=[("press", NEW)], data=_gw(1, _kit),
         title="Т-П1: сервер не из выпуска — шлюз ставится китом (flow-12)"),
    Shot("adm.join.list.cancel", role="admin",
         steps=[("press", NEW), ("start", ""), ("press", SetCB(sec="rt")),
                ("press", GwJoinCB(action="cancel", slot=2, jid=J1))],
         data=_gw(1, _token2), title="«Шлюзы»: «⬅️ Отменить подключение» — экран заново, без строки (ui-3)"),
    Shot("adm.join.list", role="admin", steps=[("press", NEW), ("press", SetCB(sec="rt"))],
         data=_gw(1, _token2), title="Т-П15: «Шлюзы» — новый слот в сопряжении строкой"),
    # ── замена устройства и её отмена ─────────────────────────────────────
    _replace(title="Т-П1: замена устройства — команда, «↩️ Отменить замену»", sid="adm.join.replace"),
    _replace(("press", REPLACE), title="Т-П12: новая команда того же слота — прежняя «Не актуально»",
             sid="adm.join.stale"),
    _replace(("press", GwJoinCB(action="cancel", slot=1, jid=J1)),
             title="Т-П14: отменить замену?", sid="adm.join.replace.cancel_ask"),
    _replace(("press", GwJoinCB(action="cancel", slot=1, jid=J1)),
             ("press", GwJoinCB(action="cancel_yes", slot=1, jid=J1)),
             title="Т-П10: замена отменена — линк вернулся прежнему устройству", sid="adm.join.replace.cancel_yes"),
    _replace(("press", GwJoinCB(action="cancel", slot=1, jid=J1)),
             ("press", GwJoinCB(action="cancel_no", slot=1, jid=J1)),
             title="Т-П14: «⬅️ Отмена» — замена идёт дальше", sid="adm.join.replace.cancel_no"),
    _replace(("call", _expire(1), "час вышел"),
             title="Т-П11: замена не завершилась за час", sid="adm.join.replace.expired"),
    _replace(("call", _joined(1, "203.0.113.50"), "подключился"),
             ("press", GwJoinCB(action="code", slot=1, jid=J1)), ("text", "AAAAAA"), ("text", "BBBBBB"),
             ("text", "CCCCCC"), title="Т-П7: попытки кончились — линк вернулся прежнему", sid="adm.join.replace.refused"),
    _replace(("press", GwSlotCB(action="card", slot=1)),
             title="Т-П15: карточка слота в замене устройства", sid="adm.join.replace.card"),
    # ── «🔁 Настроить заново» ─────────────────────────────────────────────
    Shot("adm.rt.same_ask", role="admin", press=[GwMarkCB(action="same", slot=1)], data=_slots(1),
         title="«🔁 Настроить заново»: подтверждение"),
    Shot("adm.rt.same_yes", role="admin", press=[GwMarkCB(action="same_yes", slot=1)], data=_slots(1),
         title="«🔁 Настроить заново»: команда для того же устройства"),
]
