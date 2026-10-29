"""Прежнее меню гаснет (lists/msgs/retire) — через настоящий диспетчер ролей и
посредник на сессии: меню с итогом первой строкой при новом меню ниже
становится следом из одного итога с «Скрыть»; без итога — гаснет, как
прежде; меню, у которого «Скрыть» не дожила бы до такта; правка не удалась.

Цена ошибки: меню с итогом, погашенное целиком, — экран «Свои сайты» висит в
чате без кнопок, и итог («не добавлено …») тонет в нём; след без «Скрыть» —
убрать его нельзя; «Скрыть» на следе, которому к такту будет больше 47 ч 30 мин,
— после 48 часов нажатие не удаляет; «Скрыть» снята со следа раньше — сообщение
приходится удалять руками; ссылка итога без «-n» — нажатая из следа,
она перерисовала бы живое меню ниже; меню без итога, ставшее «следом», — в
чате пустое сообщение; отказ правки, оставивший кнопки, — два живых меню.
"""
from __future__ import annotations

import json

import pytest

from awgbot.bot import links
from awgbot.bot.callbacks import DelDeviceCB
from tests.screens import harness
from tests.screens import shots_snaps as ss
from tests.screens.base import Shot
from tests.screens.harness import take
from tests.screens.shots_admin_common import KS, LAPTOP, _fw, _people
from tests.screens.shots_admin_common_gw import _slots
from tests.screens.shots_client import RF_SITES
from tests.screens.shots_gateway_common import _TICK_LAN, _ssh

pytestmark = pytest.mark.screens

HIDE = [[("Скрыть", "hd")]]
CARD_N = ("start", f"cl-{KS}-n")                  # новое меню ниже: карточка профиля из уведомления
# меню, которое бот знает как живое (указатель меню в базе): #1 снимка — нажатое
# сообщение, которого бот не отправлял, поэтому сначала главная
MAIN = ("start", "")
DEL = ("press", DelDeviceCB(device_id=LAPTOP, stage="confirm"))


def _rows(markup):
    if markup is None:
        return None
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


def _num(head: str) -> int:
    return int(head.split("#", 1)[1].split()[0])


def _menu_before(rec: harness.Record) -> tuple[int, str]:
    """Живое меню перед последним шагом: номер и текст, с которым оно стояло."""
    last = len(rec.everything) - len(rec.calls)
    shown = [c for c in rec.everything[:last]
             if c.body is not None and harness.is_menu(c.markup) and c.head.startswith(("+ send #", "= edit #"))
             and "→ чат" not in c.head]
    assert shown, "до последнего шага не было живого меню"
    return _num(shown[-1].head), shown[-1].body


def _gw(id_: str, steps, **opts) -> Shot:
    from tests.screens.shots_gateway_agent import _shot
    return _shot(id_, steps=tuple(steps), **opts)


# итог на живом меню («➕ Сайт» трёх ролей, «🗺 Подсети», «➕ Адрес» обоих ботов, «➕ В туннель»,
# итог на месте) → новое меню ниже по ссылке «-n»
CASES = {
    "admin.sites": Shot("adm.rt.sites", role="admin", data=ss._ks_sites,
                        steps=[ss._SITES, ss._ADD, ("text", ss.BAD40), ss.snap(new=True)]),
    "client.sites": Shot("cl.rt.sites", role="client", data=RF_SITES, steps=[*ss._CL_STEPS, ss.snap(new=True)]),
    "guest.sites": Shot("gst.rt.sites", role="guest", data=ss._GST, steps=[*ss._GST_STEPS, ss.snap(new=True)]),
    "admin.lan": Shot("adm.rt.lan", role="admin", data=_slots(1), steps=[ss._LAN, ("text", ss.NETS10),
                                                                          ss.snap(new=True)]),
    "admin.ssh": Shot("adm.rt.fw", role="admin", data=_fw(), steps=[*ss._FW_ADD, ss.snap(new=True)]),
    "gateway.ssh": _gw("gw.rt.ssh", [*ss._GW_SSH, ss.snap(new=True)], ssh=_ssh(filter=True)),
    "gateway.transit": _gw("gw.rt.transit", [*ss._GW_LAN, ss.snap(new=True)], tick=_TICK_LAN),
    "admin.in_place": Shot("adm.rt.dev", role="admin", data=_people,
                           steps=[MAIN, DEL, CARD_N]),
}


@pytest.mark.parametrize("case", sorted(CASES))
async def test_a_menu_with_a_note_becomes_a_trace_of_the_note_with_hide(tmp_path, frozen, fakes, case):
    rec = await take(CASES[case], tmp_path, fakes)
    mid, text = _menu_before(rec)
    note = text.split("\n\n", 1)[0]
    assert note != text, f"{case}: у меню нет итога первой строкой — сценарий не о том"
    heads = [c.head for c in rec.calls]
    assert f"- markup off #{mid}" not in heads, f"{case}: меню с итогом погашено целиком: {heads}"
    edits = [c for c in rec.calls if c.head == f"= edit #{mid}"]
    assert len(edits) == 1, f"{case}: прежнее меню не стало следом: {heads}"
    assert edits[0].body == links.mark_new(note), f"{case}: след — не один итог (с «-n» у ссылок):\n{edits[0].body}"
    assert "list-snap-" not in note or "-n\"" in edits[0].body, f"{case}: ссылка итога без «-n»"
    assert _rows(edits[0].markup) == HIDE, f"{case}: на следе не одна «Скрыть»: {_rows(edits[0].markup)}"
    assert heads.index(f"= edit #{mid}") < max(i for i, h in enumerate(heads) if h.startswith("+ send #")), \
        f"{case}: новое меню пришло раньше, чем прежнее стало следом"
    assert mid not in rec.notes, f"{case}: у следа в записи остался итог — следующее меню погасит его снова"


async def test_a_menu_without_a_note_is_dimmed_as_before(tmp_path, frozen, fakes):
    rec = await take(Shot("adm.rt.plain", role="admin", data=ss._ks_sites, steps=[MAIN, ss._SITES, CARD_N]),
                     tmp_path, fakes)
    mid, text = _menu_before(rec)
    heads = [c.head for c in rec.calls]
    assert f"- markup off #{mid}" in heads, f"меню без итога не погашено: {heads}"
    assert f"= edit #{mid}" not in heads, "меню без итога переписано — в чате пустой след"


def _age(hours: float):
    """Записи сообщений чата — отправлены hours назад (меню давно висит)."""
    async def age(services, bot):
        from awgbot.infra.db import listsnap
        from awgbot.util import timeutil
        import datetime as _dt
        for r in services.db.list_msg_all():
            r["sent"] = timeutil.to_iso(timeutil.now() - _dt.timedelta(hours=hours))
            services.db.set_state(f"{listsnap.MSG_PREFIX}{r['chat']}:{r['mid']}", json.dumps(r, ensure_ascii=False))
    return age


@pytest.mark.parametrize("hours, markup", [(23.5, HIDE), (30, HIDE), (35, HIDE), (35.5, None), (72, None)])
async def test_a_menu_whose_hide_would_not_survive_to_the_tick_becomes_a_trace_without_buttons(
        tmp_path, frozen, fakes, hours, markup):
    """Снимок в 12:00, такт — завтра 00:06 (через 12 ч 06 мин): «Скрыть» ставится, пока
    к такту сообщению меньше 47 ч 30 мин (35 ч сейчас), и не ставится с 35 ч 30 мин."""
    shot = Shot(f"adm.rt.old.{hours}", role="admin", data=_people, steps=[
        MAIN, DEL, ("call", _age(hours), "сутки спустя"), CARD_N])
    rec = await take(shot, tmp_path, fakes)
    mid, _ = _menu_before(rec)
    edits = [c for c in rec.calls if c.head == f"= edit #{mid}"]
    assert len(edits) == 1 and edits[0].body == "✅ Устройство «Ноутбук» удалено", [c.head for c in rec.calls]
    assert _rows(edits[0].markup) == markup, \
        f"меню {hours} ч: «Скрыть» {'нет' if markup else 'есть'} — {_rows(edits[0].markup)}"


class _NoEdit(harness.StubSession):
    """Telegram отказывает в правке текста прежнего меню («message can't be edited»)."""

    refuse_mid = 0

    async def make_request(self, bot, method, timeout=None):
        from aiogram.methods import EditMessageText
        if isinstance(method, EditMessageText) and int(method.message_id or 0) == self.refuse_mid:
            self.calls.append(harness.Call(f"! отказ правки #{self.refuse_mid}"))
            body = {"ok": False, "error_code": 400, "description": "Bad Request: message can't be edited"}
            return self.check_response(bot=bot, method=method, status_code=400, content=json.dumps(body)).result
        return await super().make_request(bot, method, timeout)


async def test_a_failed_edit_to_the_note_dims_the_menu_as_before(tmp_path, frozen, fakes, monkeypatch):
    async def refuse(services, bot):
        bot.session.refuse_mid = bot.session.nav
    monkeypatch.setattr(harness, "StubSession", _NoEdit)
    rec = await take(Shot("adm.rt.refused", role="admin", data=_people, steps=[
        MAIN, DEL, ("call", refuse, "отказ"), CARD_N]),
        tmp_path, fakes)
    mid, _ = _menu_before(rec)
    heads = [c.head for c in rec.calls]
    assert f"! отказ правки #{mid}" in heads and f"- markup off #{mid}" in heads, \
        f"правка до итога не удалась — кнопки не сняты (два живых меню): {heads}"
    assert heads.index(f"! отказ правки #{mid}") < heads.index(f"- markup off #{mid}")

