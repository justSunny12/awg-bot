"""Снимки админа: дополнения резерва — карточка основы с «здесь ещё N, там M»
и строкой «мало свободной памяти», кнопка «🛰 Для «Beta»» у общего админа
обоих серверов и экран роли разделом его «🛟 Резерв», экран роли админа
второго сервера со строкой памяти и пулом Telegram, «👤 Меню клиента» у
админа второго сервера, который ещё и клиент этого.

Состояние — ключами `rsv_peer_state` (как после `st` с необязательными
полями) и файлом пула; часы заморожены (NOW)."""
from __future__ import annotations

from awgbot.bot.callbacks import RsvCB

from tests.screens.base import NOW, Shot
from tests.screens.shots_admin_common import _client, _iso
from tests.screens.shots_admin_reserve import PEER_ADMIN, RSV, _away, _peer_admin, _rsv


def _paired(*then):
    return _rsv(ready=True, gw="rsv", paired=True, then=list(then))


def _there(services, mp):
    """У резерва на связи 12 устройств моих клиентов, здесь — 7."""
    services.rsv_update("rsv_peer_state", there=12)
    mp.setattr(services, "_online_count", lambda: 7)


def _low_mem(services, mp):
    services.rsv_update("rsv_peer_state", mem_free=300)


def _self_admin(services, mp):
    """Один человек — админ обоих серверов."""
    from awgbot.core import config
    services.db.reserve_peer_update(peer_admin_id=config.ADMIN_ID)
    mp.setattr(services, "server_ok", lambda: True)


def _role_extras(services, mp):
    """Мало памяти у этого сервера и пул Telegram с двумя выученными сетями."""
    from awgbot.domain.services.reserve import tgpool
    mp.setattr(services, "reserve_mem_free_mb", lambda: 250)
    tgpool._write({"announced": {"62041": ["149.154.160.0/20", "91.108.4.0/22"]}, "fetched_at": _iso(NOW),
                   "learned": [{"prefix": "203.0.113.0/24"}, {"prefix": "198.51.100.0/24"}],
                   "pool": ["203.0.113.0/24", "198.51.100.0/24"]})


def _peer_is_client(services, mp):
    from awgbot.domain.services.reserve import tgpool
    tgpool.pool_path().unlink(missing_ok=True)          # файл пула общий для снимков процесса — без выученных
    _client(services, "Админ Beta", PEER_ADMIN, devices=("Телефон",))


RESERVE_STAGE7 = [
    Shot("adm.rsv.card.away.there", role="admin", press=[RSV], data=_paired(_away, _there),
         title="карточка основы: клиенты у резерва — здесь ещё 7, там 12"),
    Shot("adm.rsv.card.lowmem", role="admin", press=[RSV], data=_paired(_low_mem),
         title="карточка основы: у «Beta» мало свободной памяти"),
    Shot("adm.rsv.card.self_admin", role="admin", press=[RSV], data=_paired(_self_admin),
         title="карточка: админ обоих серверов — кнопка «🛰 Для «Beta»»"),
    Shot("adm.rsv.card.role", role="admin", press=[RSV, RsvCB(action="role")], data=_paired(_self_admin),
         title="экран роли разделом «🛟 Резерв» — с «назад» вместо «Скрыть»"),
    Shot("adm.rsv.peer.extras", role="admin", start="", data=_peer_admin(_role_extras),
         title="админ «Beta»: мало памяти здесь, пул Telegram с выученными сетями"),
    Shot("adm.rsv.peer.client", role="admin", start="", data=_peer_admin(_peer_is_client),
         title="админ «Beta» — ещё и клиент здесь: «👤 Меню клиента»"),
]
