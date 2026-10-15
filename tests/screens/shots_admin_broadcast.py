"""Снимки админа: объявление — адресаты, дни продления, превью, отправка и отчёт."""
from __future__ import annotations

from awgbot.bot.callbacks import BroadcastCB, PresetCB

from tests.screens.base import Shot
from tests.screens.shots_admin_common import BR, FOREVER, KS, LAPTOP, PT, _bc_marks, _bc_ready, _quiet


BROADCAST = [
    # ── 1.5 объявление ──────────────────────────────────────────────────────
    Shot("adm.bc", role="admin", press=[BroadcastCB(action="pick")], data=_bc_ready, title="объявление: кому"),
    Shot("adm.bc.empty", role="admin", press=[BroadcastCB(action="pick")], data=_quiet,
         title="объявление: профилей нет"),
    Shot("adm.bc.all", role="admin", press=[BroadcastCB(action="pick"), BroadcastCB(action="all")],
         data=_bc_ready, title="отмечены все"),
    Shot("adm.bc.ext", role="admin", press=[BroadcastCB(action="pick"), BroadcastCB(action="ext")],
         data=_bc_ready, title="с продлением подписки"),
    Shot("adm.bc.next", role="admin",
         press=[BroadcastCB(action="pick"), BroadcastCB(action="all"), BroadcastCB(action="next")],
         data=_bc_ready, title="текст объявления: приглашение"),
    Shot("adm.bc.next.ext", role="admin",
         press=[BroadcastCB(action="pick"), BroadcastCB(action="ext"), BroadcastCB(action="all"),
                BroadcastCB(action="next")], data=_bc_ready, title="с продлением: дни"),
    Shot("adm.bc.days", role="admin",
         press=[BroadcastCB(action="pick"), BroadcastCB(action="ext"), BroadcastCB(action="all"),
                BroadcastCB(action="next"), PresetCB(kind="bc_days", val=7)], data=_bc_ready,
         title="с продлением: текст"),
    Shot("adm.bc.days.other", role="admin",
         press=[BroadcastCB(action="pick"), BroadcastCB(action="ext"), BroadcastCB(action="all"),
                BroadcastCB(action="next"), PresetCB(kind="bc_days", val=-1)], data=_bc_ready,
         title="с продлением: свои дни"),
    Shot("adm.bc.preview", role="admin",
         press=[BroadcastCB(action="pick"), BroadcastCB(action="all"), BroadcastCB(action="next")],
         text="В субботу с 02:00 до 03:00 — <b>работы на сервере</b>", data=_bc_ready, title="превью"),
    Shot("adm.bc.preview.ext", role="admin",
         press=[BroadcastCB(action="pick"), BroadcastCB(action="ext"), BroadcastCB(action="all"),
                BroadcastCB(action="next"), PresetCB(kind="bc_days", val=7)],
         text="Неделя в подарок — спасибо, что с нами", data=_bc_ready, title="превью с продлением"),
    Shot("adm.bc.cancel", role="admin",
         press=[BroadcastCB(action="pick"), BroadcastCB(action="all"), BroadcastCB(action="cancel")],
         data=_bc_ready, title="отмена — главная"),
]


_BC_TEXT = [("press", BroadcastCB(action="pick")), ("press", BroadcastCB(action="all")),
            ("press", BroadcastCB(action="next")), ("text", "В субботу с 02:00 до 03:00 — работы на сервере")]
_BC_EXT_TEXT = [("press", BroadcastCB(action="pick")), ("press", BroadcastCB(action="ext")),
                ("press", BroadcastCB(action="all")), ("press", BroadcastCB(action="next")),
                ("press", PresetCB(kind="bc_days", val=7)), ("text", "Неделя в подарок — спасибо, что с нами")]


# ── шаг 3: диалоги до итога ─────────────────────────────────────────────────
DIALOGS = [
    Shot("adm.bc.preview.markup", role="admin",
         steps=_BC_TEXT[:3] + [("text", "В субботу — работы на сервере",
                                [{"type": "bold", "offset": 12, "length": 17}])],
         data=_bc_ready, title="превью с разметкой Telegram"),
    Shot("adm.bc.send", role="admin", steps=_BC_TEXT + [("press", BroadcastCB(action="send"))],
         data=_bc_ready, title="разослано: объявление и отчёт"),
    Shot("adm.bc.send.ext", role="admin", steps=_BC_EXT_TEXT + [("press", BroadcastCB(action="send"))],
         data=_bc_ready, title="разослано с продлением"),
]


# ── шаг 5: объявление — адресаты, дни, превью и отчёт — ветки из e2e ───────

FRIEND_TG = 2050                         # гость, которому Ксюша отдала «Ноутбук»


def _bc_holders(services):
    """Ксюша делится «Ноутбуком» с гостем: объявление уходит и ему."""
    who = _bc_ready(services)
    res = services.activate_friend(services.make_device_friendly(LAPTOP), tg_id=FRIEND_TG)
    assert res.ok, res.reason
    return who


def _bc_undelivered(services, mp):
    """Как _bc_holders, но Боря заблокировал бота: доставка ему не проходит
    (остальным — настоящей рассылкой)."""
    from awgbot.bot.handlers.admin import broadcast as bc
    who = _bc_holders(services)
    real = bc.broadcast

    async def broadcast(bot, tg_ids, text, photos=(), by_tg=None):
        ok, failed = await real(bot, [t for t in tg_ids if t != 2002], text, photos, by_tg)
        return ok, failed + ([2002] if 2002 in tg_ids else [])
    mp.setattr(bc, "broadcast", broadcast)
    return who


_BC = [("press", BroadcastCB(action="pick"))]
_BC_EXT = _BC + [("press", BroadcastCB(action="ext"))]
_BC_KS = [("press", BroadcastCB(action="tgl", ref=KS))]
_BC_NEXT = [("press", BroadcastCB(action="next"))]
_BC_TYPED = [("press", PresetCB(kind="bc_days", val=-1)), ("text", "10")]
_BC_SEND = [("text", "В субботу с 02:00 до 03:00 — работы на сервере"), ("press", BroadcastCB(action="send"))]


BRANCHES = [
    Shot("adm.bc.partial", role="admin", steps=_BC + _BC_KS, data=_bc_ready,
         title="отмечены не все — «Выбрать все» остаётся ☑️"),
    Shot("adm.bc.by_hand", role="admin",
         steps=_BC + [("press", BroadcastCB(action="tgl", ref=c)) for c in (BR, KS, PT)], data=_bc_ready,
         title="все отмечены по одному — «✅ Выбрать все»"),
    Shot("adm.bc.next.none", role="admin", steps=_BC + _BC_NEXT, data=_bc_ready,
         title="«Далее» без адресатов — всплывашка"),
    Shot("adm.bc.blank", role="admin", steps=_BC_TEXT[:3] + [("text", "   ")], data=_bc_ready,
         title="пустое сообщение вместо объявления — переспрос с кнопкой"),
    Shot("adm.bc.preview.one", role="admin",
         steps=_BC + _BC_KS + _BC_NEXT + [("text", "В субботу с 02:00 до 03:00 — работы на сервере")],
         data=_bc_ready, title="превью одному профилю — адресат по имени"),
    Shot("adm.bc.send.one", role="admin", steps=_BC + _BC_KS + _BC_NEXT + _BC_SEND, data=_bc_ready,
         title="разослано одному профилю — отчёт"),
    Shot("adm.bc.send.holders", role="admin", steps=_BC + _BC_KS + _BC_NEXT + _BC_SEND, data=_bc_holders,
         title="разослано профилю и тем, с кем он делится устройствами"),
    Shot("adm.bc.send.undelivered", role="admin",
         steps=_BC + [("press", BroadcastCB(action="all"))] + _BC_NEXT + _BC_SEND, data=_bc_undelivered,
         title="разослано всем, держателям тоже; одному не доставлено"),
    Shot("adm.bc.next.unlimited", role="admin",
         steps=_BC_EXT + [("press", BroadcastCB(action="tgl", ref=FOREVER))] + _BC_NEXT, data=_bc_marks,
         title="с продлением отмечены одни бессрочные — продлевать некого"),
    Shot("adm.bc.next.ext.mixed", role="admin",
         steps=_BC_EXT + _BC_KS + [("press", BroadcastCB(action="tgl", ref=FOREVER))] + _BC_NEXT,
         data=_bc_marks, title="с продлением: дни, бессрочный — «без продления»"),
    Shot("adm.bc.days.one", role="admin",
         steps=_BC_EXT + _BC_KS + _BC_NEXT + [("press", PresetCB(kind="bc_days", val=7))], data=_bc_ready,
         title="с продлением одному профилю: приглашение к тексту"),
    Shot("adm.bc.days.bad", role="admin",
         steps=_BC_EXT + _BC_KS + _BC_NEXT + [("press", PresetCB(kind="bc_days", val=-1)), ("text", "400")],
         data=_bc_ready, title="свои дни продления вне границ — переспрос"),
    Shot("adm.bc.days.typed", role="admin", steps=_BC_EXT + _BC_KS + _BC_NEXT + _BC_TYPED, data=_bc_ready,
         title="свои дни продления — приглашение к тексту с кнопкой"),
    Shot("adm.bc.preview.ext.one", role="admin",
         steps=_BC_EXT + _BC_KS + _BC_NEXT + _BC_TYPED + [("text", "Спасибо за терпение")], data=_bc_ready,
         title="превью с продлением одному профилю на свои дни"),
    Shot("adm.bc.send.ext.marks", role="admin",
         steps=_BC_EXT + [("press", BroadcastCB(action="all"))] + _BC_NEXT
         + [("press", PresetCB(kind="bc_days", val=7)), ("text", "Спасибо за терпение"),
            ("press", BroadcastCB(action="send"))],
         data=_bc_marks, title="разослано с продлением: истёкшему — с текущей даты, бессрочному — без"),
]
