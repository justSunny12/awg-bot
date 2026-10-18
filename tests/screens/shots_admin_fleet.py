"""Снимки набора обновлений основного бота: раздел «⬆️ Обновления» во всех
состояниях (Т-У1), диалог (Т-У3, оба варианта), живое сообщение хода и
отката (Т-У4), итог (Т-У5), набор откатился (Т-У6), откат не прошёл (Т-У13),
уведомления догона и отката сервера (Т-У7, Т-У7б, Т-У8), строка главной во
время хода, строка карточки слота; лимит — раздел с 15 слотами.

Мир набора — настоящий: выпуски лежат в базе так, как их сохраняет проверка
обновлений, шлюзы — слоты с сессией канала (как её открывает hello); ход и
состояние шлюзов — записями домена. Сети нет: GitHub — подставной список.
"""
from __future__ import annotations

from awgbot.bot.callbacks import GwSlotCB, Menu, SetCB, UpdateCB
from awgbot.core import config

from tests.screens.base import NOW, Shot
from tests.screens.shots_admin_common import ADM, _iso, _people

V0, V1 = "v1.2.3", "v1.3.0"               # выпуск набора (VERSION каталога) и цель
SEEN = _iso(NOW.replace(day=12))          # «не на связи с 12.09»


def _release(tag: str, *, main: bool = True, gw: bool = True, needs_gw: str = ""):
    from awgbot.infra import updates
    roles: dict = {}
    if main:
        roles["main"] = {"requires": "1.2.0", **({"needs_gw": needs_gw} if needs_gw else {})}
    if gw:
        roles["gw"] = {"requires": "1.2.0"}
    meta = {"schema": 1, "version": tag[1:], "awg_gen": 1, "roles": roles}
    return updates.Release(tag=tag, version=updates.parse_version(tag),
                           body="- исправлено одно\n- добавлено другое", asset_url=f"https://api/assets/{tag}",
                           sha256="ab" * 32, title="", meta=updates.meta.check(meta, tag))


def _fleet(*, target: str | None = V1, main: bool = True, gw: bool = True, needs_gw: str = "",
           gws=(("NASPi", "1.2.3", True, ""), ("Pi4", "1.2.3", False, "офис")), caps=("cfg1", "upd1"),
           run: dict | None = None, slots: dict | None = None, server: str = "", body: str = "",
           routing: bool = False):
    """Мир набора: выпуски (V0 и target), шлюзы (имя, версия агента, на связи,
    подпись), ход и состояния шлюзов. server — версия сервера после шага."""
    def build(services, mp):
        from awgbot.infra import awglock, updates
        who = _people(services)
        if routing:
            from tests.screens.shots_admin_common_gw import _routing
            _routing(services, mp)
        releases = [_release(V0)] + ([_release(target, main=main, gw=gw, needs_gw=needs_gw)] if target else [])
        if body and target:
            import dataclasses
            releases[-1] = dataclasses.replace(releases[-1], body=body)
        services._update_store(releases, {})
        mp.setattr(updates, "list_releases", lambda **_: list(releases))
        mp.setattr(awglock, "generation", lambda: 1)
        services.apply_update = lambda r, hold=False: None
        from awgbot.domain.fleet import order as fleet_order
        mp.setattr(fleet_order, "_spawn", lambda argv, unit: None)
        for i, (name, ver, online, label) in enumerate(gws, start=1):
            dev = services.add_device(ADM, name)
            services.db.gateway_add(dev.device_id, "awglink" if i == 1 else f"awglink{i}", 443 + i,
                                    f"10.99.{i}.0/30", slot_id=i)
            if label:
                services.db.gateway_update(i, label=label)
            services.gwlink_snapshot_in(i, {"agent_version": ver}, 1, True)
            if online:
                services.gwlink_session_opened(i, ver, 2, list(caps))
            else:
                services.db.set_state(f"gwlink_caps_{i}", ",".join(caps))
                services.db.set_state(f"gwlink_seen_{i}", SEEN)
        if server:
            mp.setattr(config, "INSTALLED_VERSION", server)
        if run is not None:
            services.fleet_run_set({"id": "a1b2c3d4", "tag": target or V0, "prev": V0, "why": "button",
                                    "at": _iso(NOW), "digest": "", "failed_on": "", "reason": "",
                                    "revert_failed": [], "msg": [config.ADMIN_ID, 1], "undo": 0, **run})
        for slot, st in (slots or {}).items():
            services.fleet_slot_set(slot, {"id": f"b{slot}b2c3d4", "run": "a1b2c3d4", "target": target or V0,
                                           "prev": V0, "hold": True, "at": _iso(NOW), **st})
        return who
    return build


async def _show(services, bot):
    """Такт набора показал своё: живое сообщение хода — по состоянию."""
    from awgbot.bot.handlers.fleet import fleet_show
    await fleet_show(bot, services)


async def _tick(services, bot):
    """Такт набора: что ушло бы шлюзам и что сказать админу (уведомления)."""
    from awgbot.bot.handlers.fleet import fleet_show
    out = services.fleet_tick()
    await fleet_show(bot, services, out)


async def _ack(services, bot, slot: int = 2, state: str = "failed", error: str = "GitHub не ответил"):
    from awgbot.bot.handlers.fleet import fleet_show
    st = services.fleet_slot(slot)
    out = services.fleet_ack(slot, {"id": st["id"], "state": state, "error": error})
    await fleet_show(bot, services, out)


async def _catchup_failed(services, bot):
    await _ack(services, bot, 2, "failed", "GitHub не ответил")


async def _catchup_done(services, bot):
    services.gwlink_session_closed(2)
    services.gwlink_session_opened(2, "1.3.0", 2, ["cfg1", "upd1"])
    await _ack(services, bot, 2, "done", "")


async def _server_rolled_back(services, bot):
    import json
    import os
    from awgbot.bot.handlers.fleet import fleet_show
    with open(os.path.join(os.path.dirname(str(config.DB_PATH)), "update-rolled-back"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"version": "1.3.0", "prev": "1.2.3"}))
    await fleet_show(bot, services, services.fleet_on_start())


def _rolled_mark(services, mp):
    """Ход шёл, сторож сервера вернул прежнюю версию: метка отката — в каталоге
    базы (свой временный каталог снимка)."""
    import tempfile
    who = _fleet(run={"state": "running", "steps": [["main", V1]], "undo": 1})(services, mp)
    mp.setattr(config, "DB_PATH", f"{tempfile.mkdtemp(prefix='awg-shot-')}/bot.db")
    return who


def _catchup(slot_state=None):
    """Ход зафиксирован, «Pi4» вернулся на связь отставшим — у него догон."""
    st = {2: {"run": "", "hold": False, "state": "sent", "why": "catchup", **(slot_state or {})}}
    return _fleet(gws=(("NASPi", "1.3.0", True, ""), ("Pi4", "1.2.3", True, "офис")), server="1.3.0",
                  run={"state": "done", "steps": [["main", V1], ["gw", 1, V1]]}, slots=st)


def _scan_failed(services, mp):
    """Проверка обновлений не дошла до списка выпусков (сеть)."""
    who = _fleet(target=None)(services, mp)
    services.update_scan_checked = lambda: (None, True)
    return who


def _blocked(services, mp):
    """Цель есть, но ставить её сейчас нельзя: идёт переезд поколения."""
    who = _fleet()(services, mp)
    services.update_block_reason = lambda release=None: "идёт переезд на поколение 2"
    return who


def _skipped(services, mp):
    """Цель есть, а v1.4.0 пропущен: метаданные выпуска не прочитаны."""
    who = _fleet()(services, mp)
    services.update_skipped = lambda: [("v1.4.0", "sha256 метаданных релиза не совпал")]
    return who


def _go_offline(services, mp):
    from awgbot.infra import updates
    who = _fleet()(services, mp)

    def boom(**_):
        raise updates.UpdateError("сеть недоступна")
    mp.setattr(updates, "list_releases", boom)
    return who


def _many(services, mp):
    """15 слотов в разделе — лимит сообщения Telegram."""
    names = tuple((f"Шлюз номер {i} с длинным именем", "1.2.3", i % 2 == 0, f"площадка {i}") for i in range(1, 16))
    return _fleet(gws=names)(services, mp)


_RUN_FWD = {"state": "running", "steps": [["main", V1]], "undo": 1}
_RUN_BACK = {"state": "rolling_back", "steps": [["main", V1], ["gw", 1, V1]], "undo": 2, "failed_on": 2,
             "reason": "обновленный агент не вышел на связь за 10 минут"}


FLEET = [
    # ── раздел ───────────────────────────────────────────────────────────────
    Shot("adm.set.upd", role="admin", press=[SetCB(sec="upd")],
         data=_fleet(target=None, gws=(("NASPi", "1.2.3", True, ""), ("Pi4", "1.2.3", True, "офис"))),
         title="обновления: набор актуален — сервер и шлюзы одной строкой"),
    Shot("adm.set.upd.found", role="admin", press=[SetCB(sec="upd")], data=_fleet(),
         title="обновления: доступна v1.3.0 — строки хостов, шлюз не на связи, список изменений"),
    Shot("adm.set.upd.main_only", role="admin", press=[SetCB(sec="upd")], data=_fleet(gw=False),
         title="обновления: выпуск только для сервера — шлюзы остаются"),
    Shot("adm.set.upd.agents", role="admin", press=[SetCB(sec="upd")],
         data=_fleet(gws=(("NASPi", "9.9.9", True, ""), ("Pi4", "1.2.3.7", True, "")), target=None),
         title="обновления: агент новее набора вернётся к нему, своя сборка — команда не дойдёт"),
    Shot("adm.set.upd.old_agent", role="admin", press=[SetCB(sec="upd")],
         data=_fleet(gws=(("NASPi", "1.2.3", True, ""),), caps=("cfg1",)),
         title="обновления: агент 3.3.x командой не обновляется — последний раз в чате бота шлюза"),
    Shot("adm.set.upd.catchup_failed", role="admin", press=[SetCB(sec="upd")],
         data=_catchup({"state": "failed", "final": True, "tries": 3, "error": "GitHub не ответил"}),
         title="обновления: догон не удался трижды — строка ⚠️ и «🔁 Повторить»"),
    Shot("adm.set.upd.running", role="admin", press=[SetCB(sec="upd")],
         data=_fleet(run=_RUN_FWD, server="1.3.0", slots={1: {"state": "sent"}}),
         title="обновления: идёт ход — кнопки обновления нет"),
    Shot("adm.set.upd.rolled_back", role="admin", press=[SetCB(sec="upd")],
         data=_fleet(run={"state": "rolled_back", "steps": [["main", V1]], "failed_on": 1,
                          "reason": "GitHub не ответил"}, slots={1: {"state": "failed", "error": "GitHub не ответил"}}),
         title="обновления: набор откатился — причина и «🔁 Повторить»"),
    Shot("adm.set.upd.failed", role="admin", press=[SetCB(sec="upd")], data=_scan_failed,
         title="обновления: проверка не удалась"),
    Shot("adm.set.upd.blocked", role="admin", press=[SetCB(sec="upd")], data=_blocked,
         title="обновления: цель есть, но сейчас недоступна — строка, а не кнопка"),
    Shot("adm.set.upd.skipped", role="admin", press=[SetCB(sec="upd")], data=_skipped,
         title="обновления: цель есть, v1.4.0 пропущен — метаданные не прочитаны"),
    Shot("adm.set.upd.huge", role="admin", press=[SetCB(sec="upd")],
         data=_fleet(body="\n".join(f"- пункт номер {i} с подробным текстом" for i in range(150))),
         title="обновления: список изменений обрезан со ссылкой на релиз"),
    Shot("adm.set.upd.many", role="admin", press=[SetCB(sec="upd")], data=_many,
         title="обновления: 15 слотов — сообщение в пределе Telegram"),
    Shot("adm.set.upd.notify", role="admin", press=[SetCB(sec="upd", act="toggle", key="notify")],
         data=_fleet(), title="уведомления об обновлениях выключены"),
    Shot("adm.set.upd.cycle", role="admin", press=[SetCB(sec="upd", act="cycle", key="updates.poll_schedule")],
         data=_fleet(target=None), title="расписание проверки"),
    Shot("adm.set.upd.retry", role="admin", press=[SetCB(sec="upd", act="do", key="retry")],
         data=_fleet(run={"state": "rolled_back", "steps": [], "failed_on": "main", "reason": "сеть"}),
         title="«🔁 Повторить» в разделе — ход заново, живое сообщение"),
    # ── диалог ───────────────────────────────────────────────────────────────
    Shot("adm.upd.install", role="admin", press=[UpdateCB(action="install", tag=V1)], data=_fleet(),
         title="«⬆️ Обновить до v1.3.0» — диалог: порядок, атомарность, шлюзы не на связи"),
    Shot("adm.upd.install.needs_gw", role="admin", press=[UpdateCB(action="install", tag=V1)],
         data=_fleet(needs_gw="1.3.0", gws=(("NASPi", "1.2.3", True, ""),)),
         title="диалог: серверу нужны новые шлюзы — сначала шлюзы"),
    Shot("adm.upd.install.changed", role="admin", press=[UpdateCB(action="install", tag="v1.2.9")],
         data=_fleet(), title="«⬆️ Обновить»: цель пересчитана другой — alert, диалога нет"),
    Shot("adm.upd.install.old_button", role="admin", press=["upd:install"], data=_fleet(),
         title="«⬆️ Обновить» прежнего образца без тега — alert"),
    Shot("adm.upd.install.running", role="admin", press=[UpdateCB(action="install", tag=V1)],
         data=_fleet(run=_RUN_FWD), title="«⬆️ Обновить», когда ход уже идёт — alert"),
    Shot("adm.upd.cancel", role="admin", press=[UpdateCB(action="cancel")], data=_fleet(),
         title="«⬅️ Отмена» в диалоге из уведомления — сообщение прочь"),
    Shot("adm.upd.go", role="admin", press=[UpdateCB(action="go", tag=V1)], data=_fleet(),
         title="«⬆️ Обновить» — живое сообщение хода без звука"),
    Shot("adm.upd.go.offline", role="admin", press=[UpdateCB(action="go", tag=V1)], data=_go_offline,
         title="«⬆️ Обновить»: GitHub не ответил — своя всплывашка, хода нет"),
    # ── живое сообщение ──────────────────────────────────────────────────────
    Shot("adm.fleet.live", role="admin", call=("такт набора", _show),
         data=_fleet(run=_RUN_FWD, server="1.3.0", slots={1: {"state": "accepted"}}),
         title="ход: сервер обновлён, шлюз обновляется, второй не на связи"),
    Shot("adm.fleet.rolling_back", role="admin", call=("такт набора", _show),
         data=_fleet(run=_RUN_BACK, server="1.3.0", slots={1: {"state": "reverting"}},
                     gws=(("NASPi", "1.3.0", True, ""), ("Pi4", "1.2.3", False, "офис"))),
         title="откат: шлюз возвращается, сервер вернётся последним"),
    Shot("adm.fleet.done", role="admin", call=("такт набора", _show),
         data=_fleet(run={"state": "done", "steps": [["main", V1], ["gw", 1, V1]], "undo": 2}, server="1.3.0",
                     slots={1: {"state": "done"}}, gws=(("NASPi", "1.3.0", True, ""), ("Pi4", "1.2.3", False, "офис"))),
         title="итог: обновлено, шлюз не на связи обновится позже — «⬅️ В меню»"),
    Shot("adm.fleet.rolled_back", role="admin", call=("такт набора", _show),
         data=_fleet(run={"state": "rolled_back", "steps": [["main", V1], ["gw", 1, V1]], "failed_on": 2,
                          "reason": "обновленный агент не вышел на связь за 10 минут"},
                     slots={1: {"state": "reverted"}, 2: {"state": "rolled_back",
                                                          "error": "обновленный агент не вышел на связь за 10 минут"}},
                     gws=(("NASPi", "1.2.3", True, ""), ("Pi4", "1.2.3", True, "офис"), ("Дача", "1.2.3", False, ""))),
         title="набор откатился: причина и шлюз, «🔁 Повторить» · «⬅️ В меню»"),
    Shot("adm.fleet.rollback_failed", role="admin", call=("такт набора", _show),
         data=_fleet(run={"state": "rollback_failed", "steps": [["main", V1], ["gw", 1, V1]], "failed_on": 2,
                          "revert_failed": [1]},
                     slots={1: {"state": "revert_failed", "error": "прежней версии нет"}},
                     gws=(("NASPi", "1.3.0", True, ""),)),
         title="откат не прошёл: шлюз остался на новой, команд нет до «🔁 Повторить»"),
    # ── уведомления ──────────────────────────────────────────────────────────
    Shot("adm.ev.fleet.catchup", role="admin", call=("догон", _catchup_done), data=_catchup(),
         title="шлюз вышел на связь и обновлён — без звука"),
    Shot("adm.ev.fleet.catchup_failed", role="admin", call=("догон", _catchup_failed), data=_catchup(),
         title="догон не удался — когда повтор"),
    Shot("adm.ev.fleet.server_rolled_back", role="admin", call=("старт после отката", _server_rolled_back),
         data=_rolled_mark,
         title="сторож вернул прежнюю версию сервера — Т-У8"),
    Shot("adm.ev.fleet.tick", role="admin", call=("такт набора", _tick),
         data=_fleet(run={"state": "running", "steps": [], "undo": 0}),
         title="такт хода: ступень сервера — живое сообщение «обновляется»"),
    # ── главная и карточка слота ─────────────────────────────────────────────
    Shot("adm.main.upd_run", role="admin", start="", data=_fleet(run=_RUN_FWD, server="1.3.0",
                                                                slots={1: {"state": "sent"}}),
         title="главная: идёт ход — «🟡 Обновление до v1.3.0: N из M»"),
    Shot("adm.fleet.card", role="admin", press=[GwSlotCB(action="card", slot=2)],
         data=_fleet(routing=True, gws=(("NASPi", "1.3.0", True, ""), ("Pi4", "1.2.3", False, "офис")),
                     server="1.3.0", run={"state": "done", "steps": [["main", V1], ["gw", 1, V1]]}), title="карточка слота: шлюз не на связи обновится, когда выйдет на связь"),
    Shot("adm.upd.menu", role="admin", press=[UpdateCB(action="menu")], data=_people,
         title="«В меню» на итоге обновления"),
    Shot("adm.upd.mute", role="admin", press=[UpdateCB(action="mute")], data=_people,
         title="«Не уведомлять» на уведомлении"),
    Shot("adm.upd.retry", role="admin", press=[UpdateCB(action="retry")],
         data=_fleet(run={"state": "rolled_back", "steps": [], "failed_on": "main", "reason": "сеть"}),
         title="«🔁 Повторить» на итоге — новый ход, итог прошлого остаётся в истории"),
]

__all__ = ["FLEET", "Menu"]
