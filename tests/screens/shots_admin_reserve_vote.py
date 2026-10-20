"""Снимки админа: голосование шлюзов и аварийный код — карточка резерва в Н4
и в демпфере случая 3, карточка основы со шлюзом не на связи с резервом и
со взглядом резерва (случай 3), экран роли админа второго сервера в Н4,
ввод аварийного кода (Т-РН2: приглашение, отмена, неразобранный код, приём
закрыт) и уведомление Т-РУ7б с кнопкой ввода.

Состояние — ключами `rsv_view` и снимком шлюза так, как их оставили бы
такт голосования и канал шлюза; часы заморожены (NOW)."""
from __future__ import annotations

from awgbot.bot.callbacks import PeerCB

from tests.screens.base import NOW, Shot
from tests.screens.shots_admin_common import _iso
from tests.screens.shots_admin_reserve import PEER_ADMIN, RSV, _peer_admin, _rsv

SINCE = _iso(NOW.replace(hour=11, minute=40))
CODE_BAD = "0000-0000-0000-0000-0000-0000-0001"


def _h4(services, mp):
    """Н4: основа и её шлюзы не отвечают резерву, приём кода открыт."""
    from awgbot.util import livestep
    services.__dict__["_reserve_liveness"] = livestep.Liveness(state="down")
    services.rsv_update("rsv_peer_state", session=False, down_since=SINCE)
    services.rsv_put("rsv_pkg_in", {"gen": 3, "fp": "ab" * 8, "share": "cd" * 16, "m_gen": 1, "at": _iso(NOW)})
    services.rsv_put("rsv_view", {"case": "H4", "case_since": SINCE, "voters": {}, "absent": [1],
                                  "code": {"open": True, "since": SINCE, "told": True, "tries": []}})


def _case3(services, mp):
    """Случай 3 у резерва: основа видна, шлюз «не видит» — демпфер идёт."""
    services.rsv_put("rsv_view", {"case": "3", "case_since": SINCE, "voters": {"1": "unseen"},
                                  "rf": {"since": SINCE, "mono": 0.0}})


def _gw_off_reserve(services, mp):
    """Мой шлюз не на связи с резервом (поле rsv снимка) и взгляд резерва — случай 3."""
    services.gwlink_snapshot_in(1, {"rsv": {"seen": "down", "since": SINCE, "gen": 1, "open": False,
                                            "state": "idle", "primary": "ok"}}, 1, True)
    services.rsv_update("rsv_view", peer_case="3", peer_since=SINCE, peer_seen=[], peer_unseen=["NASPi"],
                        peer_at=_iso(NOW))


def _offer(services, bot):
    """Т-РУ7б: такт голосования в Н4 — уведомления обеим сторонам."""
    async def run():
        from awgbot.bot.notifier import send_notifications
        from awgbot.util import livestep
        services.rsv_update("rsv_peer_admin", opened=True)
        services.__dict__["_reserve_liveness"] = livestep.Liveness(state="down")
        services.rsv_update("rsv_peer_state", session=False, down_since=SINCE)
        services.rsv_put("rsv_pkg_in", {"gen": 3, "fp": "ab" * 8, "share": "cd" * 16, "m_gen": 1, "at": _iso(NOW)})
        notes = [n for a in services.reserve_vote_eval() if a[0] == "notify" for n in a[1]]
        await send_notifications(bot, [n for n in notes if n.tg_id == PEER_ADMIN])
    return run()


CODE = PeerCB(action="code")

RESERVE_VOTE = [
    # ── карточки ─────────────────────────────────────────────────────────
    Shot("adm.rsv.card.h4", role="admin", press=[RSV], data=_rsv(ready=True, gw="rsv", paired=True, then=[_h4]),
         title="карточка резерва: Н4 — жду аварийный код админа «Beta»"),
    Shot("adm.rsv.card.case3", role="admin", press=[RSV], data=_rsv(ready=True, gw="rsv", paired=True, then=[_case3]),
         title="карточка резерва: шлюзы «Beta» не видят его, демпфер идёт"),
    Shot("adm.rsv.card.gw_off", role="admin", press=[RSV],
         data=_rsv(ready=True, gw="rsv", paired=True, then=[_gw_off_reserve]),
         title="карточка основы: шлюз не на связи с резервом, резерв видит случай 3"),
    # ── роль админа второго сервера в Н4 ─────────────────────────────────
    Shot("adm.rsv.peer.h4", role="admin", start="", data=_peer_admin(_h4),
         title="админ «Beta»: Н4 — кнопка ввода аварийного кода"),
    Shot("adm.rsv.peer.code", role="admin", steps=[("start", ""), ("press", CODE)], data=_peer_admin(_h4),
         title="админ «Beta»: «🔑 Ввести аварийный код» — приглашение (Т-РН2)"),
    Shot("adm.rsv.peer.code.cancel", role="admin",
         steps=[("start", ""), ("press", CODE), ("press", PeerCB(action="code_cancel"))], data=_peer_admin(_h4),
         title="админ «Beta»: «⬅️ Отмена» — снова экран роли"),
    Shot("adm.rsv.peer.code.bad", role="admin", steps=[("start", ""), ("press", CODE), ("text", CODE_BAD)],
         data=_peer_admin(_h4), title="админ «Beta»: код не разобран — сообщение удалено, попыток осталось 2"),
    Shot("adm.rsv.peer.code.closed", role="admin", steps=[("start", ""), ("press", CODE)], data=_peer_admin(),
         title="админ «Beta»: кнопка кода вне Н4 — всплывашка"),
    Shot("adm.rsv.peer.offer", role="admin", start="", call=_offer, data=_peer_admin(),
         title="админ «Beta»: Т-РУ7б с кнопкой ввода кода"),
]
