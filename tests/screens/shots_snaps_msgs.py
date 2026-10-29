"""Сообщения со ссылками на снимки и «Скрыть» — как их правит бот (bot/lists/msgs):
прежнее меню с итогом первой строкой при новом меню ниже становится следом из
одного итога с «Скрыть» (retire), меню без итога гаснет; снимок, вытесненный
пятьдесят первым, — ссылка в старом сообщении становится текстом тем же
действием (heal); такт после полуночи (heal.nightly) — ссылки истёкших снимков
текстом и «Скрыть» старше суток прочь, одной правкой на сообщение.

Источники итогов — те же, что в shots_snaps (40 неадресов, 10 подсетей, 15
адресов SSH-доступа, 400 доменов «в туннель»). Поведенческие утверждения —
test_list_msg_retire.py, test_list_snap_hide.py.
"""
from __future__ import annotations

import datetime as _dt

from awgbot.core import config

from tests.screens import base
from tests.screens import shots_snaps as ss
from tests.screens.base import Shot
from tests.screens.shots_admin_common import KS, _br_expired, _fw
from tests.screens.shots_admin_common_gw import _slots
from tests.screens.shots_admin_events import _ev_expiry
from tests.screens.shots_admin_rf import _ks_sites
from tests.screens.shots_client import RF_SITES
from tests.screens.shots_gateway_agent import _shot as _gw_shot
from tests.screens.shots_gateway_common import _TICK_LAN, _ssh

_CARD_N = ("start", f"cl-{KS}-n")                # новое меню ниже: карточка профиля из уведомления


def _restore8_sites(services, mp):
    """Отчёт восстановления на 8 не вставших устройств (одна ссылка на снимок) и три
    своих сайта Ксюши — итог «➕ Сайт» даст новый снимок."""
    who = _ks_sites(services)
    lost = [{"device": f"Телефон {i}", "client": f"Профиль <{i}>", "address": f"10.8.1.{50 + i}"} for i in range(8)]
    services.pop_restore_done = lambda: {"created_at": "2026-09-01T03:00:00+05:00",
                                         "merge": {"line": "same", "slots": [], "lost": lost}}
    return who


async def _fill49(services, bot):
    """Ещё 49 снимков чата админа без сообщений: с отчётом — пятьдесят, следующий
    вытеснит снимок отчёта."""
    for n in range(49):
        services.db.list_snap_keep(config.ADMIN_ID, "admin", f"⚠️ Не добавлено №{n + 2}",
                                   [f"<code>bad{n:02d}..ru</code> — не похоже на адрес"], "")


def _nightly(days: float):
    """Такт после полуночи через days суток после отправки: часы вперёд на время
    такта (снимки и «Скрыть» сверяются с timeutil.now)."""
    async def tick(services, bot):
        import time
        import pytest
        from awgbot.bot.lists.msgs import heal
        from awgbot.util import timeutil
        later = base.NOW + _dt.timedelta(days=days)
        heal._last.clear()                             # темп чата — по часам цикла, не по снимку
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(timeutil, "now", lambda: later)
            mp.setattr(time, "time", lambda: later.timestamp())
            await heal.nightly(bot, services)
    return tick


ADMIN = [
    # ── прежнее меню с итогом при новом меню ниже — след со «Скрыть» ───────
    Shot("adm.snap.trace.sites", role="admin", data=ss._ks_sites,
         steps=[ss._SITES, ss._ADD, ("text", ss.BAD40), _CARD_N],
         title="итог «➕ Сайт» над «📋 Свои сайты» — при новом меню ниже следом из итога и «Скрыть»"),
    Shot("adm.snap.trace.lan", role="admin", data=_slots(1),
         steps=[ss._LAN, ("text", ss.NETS10), ss.snap(new=True)],
         title="итог «🗺 Подсети» — по ссылке «-n» следом, снимок новым сообщением"),
    Shot("adm.snap.trace.fw", role="admin", data=_fw(),
         steps=[*ss._FW_ADD, _CARD_N],
         title="итог «✅ Адреса для SSH-доступа» над «🛡 SSH-доступ» — следом из итога и «Скрыть»"),
    Shot("adm.snap.trace.plain", role="admin", data=ss._ks_sites,
         steps=[("start", ""), ss._SITES, _CARD_N],
         title="меню без итога при новом меню ниже — кнопки сняты, как прежде"),
    # ── вытеснение 51-м снимком — ссылка старого сообщения текстом ─────────
    Shot("adm.snap.evict.edit", role="admin", data=_restore8_sites,
         steps=[("start", ""), ss._RESTORE, ("call", _fill49, "49 снимков"), ss._SITES, ss._ADD,
                ("text", ss.BAD40)],
         title="итог «➕ Сайт» с 51-м снимком — отчёт восстановления правится тем же действием: "
               "«и ещё 3» текстом, «Скрыть» на месте"),
    # ── такт после полуночи ─────────────────────────────────────────────────
    Shot("adm.snap.nightly", role="admin", data=_restore8_sites,
         steps=[("start", ""), ss._RESTORE, ("call", _nightly(31), "такт через 31 день")],
         title="такт через 31 день — отчёт восстановления одной правкой: ссылка текстом, «Скрыть» снята"),
    Shot("adm.snap.nightly.hide", role="admin", data=_br_expired,
         steps=[("call", _ev_expiry, "check_expiry"),
                ("call", _nightly(1), "такт через сутки")],
         title="такт через сутки — у уведомлений остаются кнопки действия, «Скрыть» снята"),
]

CLIENT = [
    Shot("cl.snap.trace.sites", role="client", data=RF_SITES, steps=[*ss._CL_STEPS, ss.snap(new=True)],
         title="итог «➕ Сайт» — по ссылке «-n» следом из итога и «Скрыть»"),
]

GUEST = [
    Shot("gst.snap.trace.sites", role="guest", data=ss._GST, steps=[*ss._GST_STEPS, ss.snap(new=True)],
         title="гость: итог «➕ Сайт» — по ссылке «-n» следом из итога и «Скрыть»"),
]

GATEWAY = [
    _gw_shot("gw.snap.trace.transit", steps=[*ss._GW_LAN, ss.snap(new=True)], tick=_TICK_LAN,
             title="итог «➕ В туннель» — по ссылке «-n» следом из итога и «Скрыть», снимок новым сообщением"),
    _gw_shot("gw.snap.trace.ssh", steps=[*ss._GW_SSH, ss.snap(new=True)], ssh=_ssh(filter=True),
             title="итог «➕ Адрес» SSH-доступа — по ссылке «-n» следом из итога и «Скрыть»"),
]

SHOTS = [*ADMIN, *CLIENT, *GUEST, *GATEWAY]
