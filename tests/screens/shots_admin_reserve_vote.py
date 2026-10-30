"""Снимки админа: окно аварийного кода резерва — карточка резерва с открытым
окном (голосов для переезда не хватает), экран роли админа второго сервера с
открытым окном, ввод аварийного кода (Т-РН2: приглашение, отмена,
неразобранный код, приём закрыт) и уведомление Т-К21 с кнопкой ввода; ничья
кворума (Т-К5, Т-К5б) с клавиатурами.

Состояние — ключами `rsv_view` так, как их оставил бы кандидат кворума;
уведомление Т-К21 — настоящим `reserve_code_window_set`; часы заморожены (NOW)."""
from __future__ import annotations

from awgbot.bot.callbacks import PeerCB

from tests.screens.base import NOW, Shot
from tests.screens.shots_admin_common import _iso
from tests.screens.shots_admin_reserve import PEER_ADMIN, RSV, _peer_admin, _rsv

SINCE = _iso(NOW.replace(hour=11, minute=40))
CODE_BAD = "0000-0000-0000-0000-0000-0000-0001"


def _h4(services, mp):
    """Окно кода кандидата открыто: «Beta» не вижу, голосов для переезда не хватает."""
    from awgbot.util import livestep
    services.__dict__["_reserve_liveness"] = livestep.Liveness(state="down")
    services.rsv_update("rsv_peer_state", session=False, down_since=SINCE)
    services.rsv_put("rsv_pkg_in", {"gen": 3, "fp": "ab" * 8, "share": "cd" * 16, "m_gen": 1, "at": _iso(NOW)})
    services.rsv_put("rsv_view", {"code": {"open": True, "since": SINCE, "told": True, "tries": [], "episode": SINCE,
                                           "by": "quorum"}})


def _offer(services, bot):
    """Т-К21: кандидат открыл окно кода — уведомление админу «Beta» с кнопкой ввода."""
    async def run():
        from awgbot.bot.notifier import send_notifications
        services.rsv_update("rsv_peer_admin", opened=True)
        services.rsv_put("rsv_pkg_in", {"gen": 3, "fp": "ab" * 8, "share": "cd" * 16, "m_gen": 1, "at": _iso(NOW)})
        notes = [n for a in services.reserve_code_window_set(True, since=SINCE, n=1, v=3, t=2) if a[0] == "notify"
                 for n in a[1]]
        await send_notifications(bot, [n for n in notes if n.tg_id == PEER_ADMIN])
    return run()


CODE = PeerCB(action="code")

RESERVE_VOTE = [
    # ── карточки ─────────────────────────────────────────────────────────
    Shot("adm.rsv.card.h4", role="admin", press=[RSV], data=_rsv(ready=True, gw="rsv", paired=True, then=[_h4]),
         title="карточка резерва: окно кода — голосов не хватает, жду аварийный код админа «Beta»"),
    # ── роль админа второго сервера при открытом окне кода ───────────────
    Shot("adm.rsv.peer.h4", role="admin", start="", data=_peer_admin(_h4),
         title="админ «Beta»: окно кода открыто — кнопка ввода аварийного кода"),
    Shot("adm.rsv.peer.code", role="admin", steps=[("start", ""), ("press", CODE)], data=_peer_admin(_h4),
         title="админ «Beta»: «🔑 Ввести аварийный код» — приглашение (Т-РН2)"),
    Shot("adm.rsv.peer.code.cancel", role="admin",
         steps=[("start", ""), ("press", CODE), ("press", PeerCB(action="code_cancel"))], data=_peer_admin(_h4),
         title="админ «Beta»: «⬅️ Отмена» — снова экран роли"),
    Shot("adm.rsv.peer.code.bad", role="admin", steps=[("start", ""), ("press", CODE), ("text", CODE_BAD)],
         data=_peer_admin(_h4), title="админ «Beta»: код не разобран — сообщение удалено, попыток осталось 2"),
    Shot("adm.rsv.peer.code.closed", role="admin", steps=[("start", ""), ("press", CODE)], data=_peer_admin(),
         title="админ «Beta»: кнопка кода при закрытом окне — всплывашка"),
    Shot("adm.rsv.peer.offer", role="admin", start="", call=_offer, data=_peer_admin(),
         title="админ «Beta»: Т-К21 с кнопкой ввода кода"),
]


# ── ничья кворума (4.2.0): уведомления Т-К5 лидеру и Т-К5б кандидату с клавиатурами ──
def _tie_leader(services, bot):
    """Т-К5: ничья о моём сервере — клавиатура quorum_tie (переезд кнопкой и раздел);
    настоящий отправитель — quorum/tie.py (reserve_quorum_leader_tie_tick)."""
    async def run():
        from awgbot.core import config
        from awgbot.bot.notifier import send_notifications
        from awgbot.domain.notices import reserve as rn
        from awgbot.domain.services import Notification
        text = rn.quorum_tie_leader("Alpha", "Beta", SINCE, ["Beta", "Шлюз 3"], ["Шлюз 1", "Шлюз 2"],
                                    {"hs": 37, "flow": True, "at": SINCE})
        await send_notifications(bot, [Notification(tg_id=int(config.ADMIN_ID), text=text, force_sound=True, keyboard="quorum_tie",
                                                    keyboard_arg="Beta")])
    return run()


def _tie_candidate(services, bot):
    """Т-К5б: ничья о втором сервере глазами кандидата — клавиатура quorum_tie_cand (раздел)."""
    async def run():
        from awgbot.core import config
        from awgbot.bot.notifier import send_notifications
        from awgbot.domain.notices import reserve as rn
        from awgbot.domain.services import Notification
        text = rn.quorum_tie_candidate("Beta", SINCE, ["шлюз 3 «Beta»"], ["Alpha", "шлюз 1 «Beta»", "шлюз 2 «Beta»"],
                                       {"hs": 37, "flow": True, "at": SINCE}, "шлюз 3 «Beta»", True)
        await send_notifications(bot, [Notification(tg_id=int(config.ADMIN_ID), text=text, force_sound=True, keyboard="quorum_tie_cand")])
    return run()


RESERVE_VOTE += [
    Shot("adm.rsv.tie.leader", role="admin", start="", call=_tie_leader, data=_rsv(ready=True, gw="rsv", paired=True),
         title="ничья о моём сервере: Т-К5 с кнопками переезда и раздела"),
    Shot("adm.rsv.tie.cand", role="admin", start="", call=_tie_candidate, data=_rsv(ready=True, gw="rsv", paired=True),
         title="ничья о «Beta» глазами кандидата: Т-К5б с кнопкой раздела"),
]
