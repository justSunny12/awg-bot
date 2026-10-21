"""Снимки админа: «🛟 Резерв» — переезд кнопкой и возврат (карточка по
состояниям, диалог перевода в ручном и автоматическом режиме, живое сообщение
хода, возврат клиентов к основе у резерва, «🌐 Запись DNS» и ввод токена,
строка главной, диалог возврата в боте основы на резерве, уведомление о
записи не туда с кнопкой).

Запись DNS и авторитетный сервер — заглушки с постоянными адресами из
документационных диапазонов; токены — заглушки.
"""
from __future__ import annotations

from awgbot.bot.callbacks import RsvCB

from tests.screens.base import NOW, Shot
from tests.screens.shots_admin_common import _iso
from tests.screens.shots_admin_reserve import OWN_IP, RSV, _away, _guest, _peer_down, _rsv, _serving

DNS = RsvCB(action="dns")
FOREIGN = "198.51.100.77"


class _Provider:
    id, human = "stub", "stub"

    def set(self, cfg, ip):
        return None

    def check(self, cfg):
        return OWN_IP

    def read(self, cfg):
        return OWN_IP


def _automatic(services, mp):
    """Провайдер Cloudflare с токеном-заглушкой; записи проверены в 11:55."""
    from awgbot.util import dnsapi
    services.reserve_dns_set_provider("cloudflare")
    services.reserve_dns_set_token("DUMMY-token")
    services.rsv_update("rsv_dns", current=OWN_IP, checked_at=_iso(NOW.replace(minute=55)), check_ok=True)
    mp.setattr(dnsapi, "provider_for", lambda cfg: _Provider())


def _provider_stub(services, mp):
    from awgbot.util import dnsapi
    mp.setattr(dnsapi, "provider_for", lambda cfg: _Provider())


def _leaving(step: str):
    def build(services, mp):
        services.reserve_set_out("leaving", kind="button", started_at=_iso(NOW), target_ip="198.51.100.7",
                                 record="vpn.example.org", gen=3, pkg_at=_iso(NOW.replace(minute=2)),
                                 up_at=_iso(NOW.replace(minute=3)), msg=[])
        services.rsv_update("rsv_out", step=step)
    return build


def _returning(services, mp):
    services.reserve_set_out("returning", kind="button", step="merge")


def _relay_after_return(services, mp):
    services.reserve_set_out("home", relay_until=_iso(NOW.replace(day=22, hour=14, minute=5)), returned_at=_iso(NOW))


def _foreign_record(services, bot):
    """Надзор записи: она указывает на чужой адрес, переезда не было."""
    async def run():
        import types
        from awgbot.bot.notifier import send_notifications
        from awgbot.domain.services.reserve import dns as dnsmod
        from awgbot.util import dnsapi
        services.rsv_update("rsv_record_watch", mono=-1e9)
        orig = dnsapi.read_authoritative
        dnsapi.read_authoritative = lambda name: [FOREIGN]
        dnsmod.random = types.SimpleNamespace(uniform=lambda a, b: 1.0)
        try:
            notes = services.reserve_record_watch_tick()
        finally:
            dnsapi.read_authoritative = orig
        await send_notifications(bot, notes)
    return run()


def _move_done(services, bot):
    """Такт автомата после перевода: клиенты у резерва — живое сообщение хода
    становится итогом с кнопкой назад в раздел."""
    async def run():
        from awgbot.bot.handlers.settings.reserve import move as move_ui
        services.rsv_update("rsv_out", state="away", step="", taken_at=_iso(NOW), last_switch=_iso(NOW))
        await move_ui.refresh_live(bot, services)
    return run()


def _await_merge(services, mp):
    """Клиенты возвращены без резерва (С9): жду его реплику."""
    services.reserve_set_out("home", await_merge=True, kind="dead")


def _paired(*then):
    return _rsv(ready=True, gw="rsv", paired=True, then=list(then))


RESERVE_MOVE = [
    # ── карточка по состояниям ───────────────────────────────────────────
    Shot("adm.rsv.card.auto", role="admin", press=[RSV], data=_paired(_automatic),
         title="карточка: провайдер с токеном, запись проверена"),
    Shot("adm.rsv.card.leaving", role="admin", press=[RSV], data=_paired(_leaving("record")),
         title="карточка: клиенты переезжают, жду запись"),
    Shot("adm.rsv.card.away", role="admin", press=[RSV], data=_paired(_away),
         title="карточка: мои клиенты у резерва"),
    Shot("adm.rsv.card.returning", role="admin", press=[RSV], data=_paired(_returning),
         title="карточка: клиенты возвращаются"),
    Shot("adm.rsv.card.relay", role="admin", press=[RSV], data=_paired(_relay_after_return),
         title="карточка: после возврата, пересылка хвоста у резерва"),
    Shot("adm.rsv.card.serving", role="admin", press=[RSV], data=_paired(_serving),
         title="карточка: клиенты резерва у меня — «↩️ Вернуть клиентов «Beta» к нему»"),
    # ── перевод кнопкой (Т-Р10, Т-Р10б) ─────────────────────────────────
    Shot("adm.rsv.move", role="admin", press=[RSV, RsvCB(action="move")], data=_paired(),
         title="«🔀 Перевести»: диалог, запись меняет человек"),
    Shot("adm.rsv.move.auto", role="admin", press=[RSV, RsvCB(action="move")], data=_paired(_automatic),
         title="«🔀 Перевести»: диалог, запись меняется сама"),
    Shot("adm.rsv.move.refused", role="admin", press=[RSV, RsvCB(action="move")], data=_paired(_peer_down),
         title="«🔀 Перевести» при недоступном резерве — всплывашка"),
    Shot("adm.rsv.move.yes", role="admin", press=[RSV, RsvCB(action="move"), RsvCB(action="move_yes")],
         data=_paired(), title="перевод начат: живое сообщение хода"),
    Shot("adm.rsv.move.done", role="admin",
         steps=[("press", RSV), ("press", RsvCB(action="move")), ("press", RsvCB(action="move_yes")),
                ("call", _move_done, "reserve_out_tick")], data=_paired(),
         title="перевод состоялся: живое сообщение хода — итог с кнопкой в раздел"),
    Shot("adm.rsv.move.twice", role="admin",
         press=[RSV, RsvCB(action="move"), RsvCB(action="move_yes"), RsvCB(action="move_yes")], data=_paired(),
         title="повторное подтверждение перевода — всплывашка"),
    # ── вернуть клиентов к основе (Т-Р12) ───────────────────────────────
    Shot("adm.rsv.evict", role="admin", press=[RSV, RsvCB(action="evict")], data=_paired(_serving),
         title="«↩️ Вернуть клиентов «Beta» к нему»: диалог"),
    Shot("adm.rsv.evict.yes", role="admin", press=[RSV, RsvCB(action="evict"), RsvCB(action="evict_yes")],
         data=_paired(_serving), title="просьба о возврате отправлена"),
    Shot("adm.rsv.evict.none", role="admin", press=[RSV, RsvCB(action="evict")], data=_paired(),
         title="вернуть некого — всплывашка"),
    # ── вернуть клиентов без резерва (С9) ───────────────────────────────
    Shot("adm.rsv.card.force", role="admin", press=[RSV], data=_paired(_away, _peer_down),
         title="карточка: мои клиенты у резерва, он не на связи 20 минут — «⚠️ Вернуть клиентов без «Beta»»"),
    Shot("adm.rsv.force", role="admin", press=[RSV, RsvCB(action="force")], data=_paired(_away, _peer_down),
         title="«⚠️ Вернуть клиентов без «Beta»»: диалог, запись меняет человек"),
    Shot("adm.rsv.force.auto", role="admin", press=[RSV, RsvCB(action="force")],
         data=_paired(_automatic, _away, _peer_down), title="«⚠️ Вернуть клиентов без «Beta»»: диалог, запись вернётся сама"),
    Shot("adm.rsv.force.refused", role="admin", press=[RSV, RsvCB(action="force")], data=_paired(_away),
         title="«⚠️ Вернуть без резерва» при резерве на связи — всплывашка"),
    Shot("adm.rsv.card.await_merge", role="admin", press=[RSV], data=_paired(_await_merge),
         title="карточка: клиенты возвращены без резерва, жду его реплику"),
    # ── запись DNS (Т-Р16) ──────────────────────────────────────────────
    Shot("adm.rsv.dns.ip", role="admin", press=[RSV, DNS], data=_rsv(host="203.0.113.9", ready=True),
         title="«🌐 Запись DNS» при адресе-IP: записи нет"),
    Shot("adm.rsv.dns.manual", role="admin", press=[RSV, DNS], data=_paired(),
         title="«🌐 Запись DNS»: вручную"),
    Shot("adm.rsv.dns.auto", role="admin", press=[RSV, DNS], data=_paired(_automatic),
         title="«🌐 Запись DNS»: Cloudflare, токен задан"),
    Shot("adm.rsv.dns.prov", role="admin", press=[RSV, DNS, RsvCB(action="dns_prov")], data=_paired(),
         title="провайдер переключён циклом: Cloudflare без токена"),
    Shot("adm.rsv.dns.token", role="admin", press=[RSV, DNS, RsvCB(action="dns_prov"), RsvCB(action="dns_token")],
         data=_paired(), title="«✏️ Токен»: приглашение"),
    Shot("adm.rsv.dns.token.manual", role="admin", press=[RSV, DNS, RsvCB(action="dns_token")], data=_paired(),
         title="«✏️ Токен» при провайдере «вручную» — всплывашка"),
    Shot("adm.rsv.dns.token.ok", role="admin",
         steps=[("press", RSV), ("press", DNS), ("press", RsvCB(action="dns_prov")),
                ("press", RsvCB(action="dns_token")), ("text", "DUMMY-token")], data=_paired(_provider_stub),
         title="токен принят: сообщение удалено, запись проверена"),
    Shot("adm.rsv.dns.token.bad", role="admin",
         steps=[("press", RSV), ("press", DNS), ("press", RsvCB(action="dns_prov")),
                ("press", RsvCB(action="dns_token")), ("text", "два слова")], data=_paired(_provider_stub),
         title="токен не похож на токен — переспрос"),
    Shot("adm.rsv.dns.check", role="admin", press=[RSV, DNS, RsvCB(action="dns_check")], data=_paired(),
         title="«🧪 Проверить»: запись прочитана"),
    # ── главная со строкой Т-Р17 ────────────────────────────────────────
    Shot("adm.rsv.main.away", role="admin", start="", data=_paired(_away), title="главная: мои клиенты у резерва"),
    Shot("adm.rsv.main.leaving", role="admin", start="", data=_paired(_leaving("record")),
         title="главная: клиенты переезжают"),
    Shot("adm.rsv.main.serving", role="admin", start="", data=_paired(_serving),
         title="главная: у меня клиенты резерва"),
    Shot("adm.rsv.main.relay", role="admin", start="", data=_paired(_relay_after_return),
         title="главная: возврат состоялся, идёт пересылка"),
    Shot("adm.rsv.main.down", role="admin", start="", data=_paired(_peer_down), title="главная: резерв недоступен"),
    # ── бот основы на резерве: возврат (Т-Р11) ──────────────────────────
    Shot("adm.rsv.guest.back", role="admin", press=[RSV, RsvCB(action="back")], data=_guest(alive=True),
         title="резерв: «↩️ Вернуть клиентов» — диалог"),
    Shot("adm.rsv.guest.back.early", role="admin", press=[RSV, RsvCB(action="back")], data=_guest(),
         title="резерв: основа ещё не в строю — всплывашка"),
    # ── уведомление Т-РУ16 ──────────────────────────────────────────────
    Shot("adm.rsv.ev.record_foreign", role="admin", call=("reserve_record_watch_tick", _foreign_record),
         data=_paired(), title="запись указывает не сюда: уведомление с кнопкой «↩️ Вернуть запись»"),
]
