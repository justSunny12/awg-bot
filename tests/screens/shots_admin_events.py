"""Снимки админа: события без действия человека — уведомления админу и владельцу."""
from __future__ import annotations

import datetime as _dt

from awgbot.core import config

from tests.screens.base import NOW, Shot
from tests.screens.shots_admin_common import KS, LAPTOP, _br_expired, _iso, _mig_ready, _people
from tests.screens.shots_admin_common_gw import _slots


def _ks_expiring(services):
    """Годовая подписка Ксюши кончается через неделю без часа: порог «7 дней»
    только что пересечён, отсрочку она ещё не брала."""
    who = _people(services)
    services.db.update_client_fields(KS, period_start=_iso(NOW - _dt.timedelta(days=358, hours=1)),
                                     period_end=_iso(NOW + _dt.timedelta(days=7) - _dt.timedelta(hours=1)))
    return who


def _gw_file_in_chat(plain: bool):
    """Слот NASPi, в чате админа лежит его файл конфигурации (#40) под
    погасшей карточкой (#39); plain — файл первого применения."""
    def build(services, mp):
        who = _slots(1)(services, mp)
        services.gw_bundle_msg_set(1, config.ADMIN_ID, 40, 39)
        return who
    return build


async def _ev_installed(services, bot):
    from awgbot.bot.handlers.settings import bundle_installed
    await bundle_installed(bot, services, 1)


def _upd_pending(tag: str):
    """Перед рестартом запущено обновление до tag, в чате — «дождись» (#30);
    тело релиза — без сети."""
    def build(services, mp):
        from awgbot.infra import updates
        who = _people(services)
        services.db.set_state("update_pending", tag)
        services.set_update_wait(config.ADMIN_ID, 30)
        mp.setattr(updates, "release_body", lambda t: "- исправлено одно\n- добавлено другое")
        return who
    return build


async def _ev_update_result(services, bot):
    from awgbot.runtime.main import report_update_result
    await report_update_result(bot, services)


async def _ev_update_available(services, bot):
    import types
    from awgbot.runtime.scheduler import notify_update_available
    await notify_update_available(bot, services, types.SimpleNamespace(
        tag="v1.3.0", body="- исправлено одно\n- добавлено другое"))


def _restart_promised(services):
    """Бот перезапускали по кнопке: обещание «вернётся» — сообщение #25."""
    who = _people(services)
    services.set_restart_wait(config.ADMIN_ID, 25)
    return who


async def _ev_restarted(services, bot):
    from awgbot.bot.handlers.admin.panel import restore_panel_after_restart
    await restore_panel_after_restart(bot, services)


async def _ev_zone_adopted(services, bot, prev="Europe/Moscow"):
    """Первый старт после обновления, снявшего ключ timezone: метка шага
    обновления с прежним значением — во временном каталоге."""
    import tempfile
    from pathlib import Path
    from awgbot.runtime import main
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / main.ZONE_ADOPTED_MARKER).write_text(prev + "\n", encoding="utf-8")
        await main._announce_zone(bot, d)


_MERGE = {"line": "same", "empty": False,
          "slots": [{"slot": 1, "case": "same", "name": "NASPi", "label": "", "link_if": "awglink"},
                    {"slot": 2, "case": "rekeyed", "name": "Pi4", "label": "дача", "link_if": "awglink2"},
                    {"slot": 3, "case": "device", "name": "Pi3", "label": "", "link_if": "awglink3"},
                    {"slot": 4, "case": "removed", "name": "Шлюз 4", "label": "", "link_if": "awglink4"},
                    {"slot": 5, "case": "added", "name": "Pi5", "label": "", "link_if": "awglink5"},
                    {"slot": 6, "case": "twin", "name": "Pi6", "label": "", "link_if": "awglink6"}],
          "lost": [{"device": "Телефон", "client": "Иван", "address": "10.8.1.7"}], "blocked": [], "adopt": []}


def _restore_done(services):
    """awg-bot restore отработал и оставил маркер; итог слияния — без шлюзов."""
    who = _people(services)
    services.pop_restore_done = lambda: {"created_at": "2026-09-01T03:00:00+05:00",
                                         "merge": {"line": "same", "slots": [], "lost": []}}
    return who


def _restore_done_gw(services):
    """Маркер с итогом по каждому шлюзу и устройством, которое не встало."""
    who = _people(services)
    services.pop_restore_done = lambda: {"created_at": "2026-10-05T14:00:00+03:00", "merge": _MERGE}
    return who


def _restore_failed(services):
    """Копия из чата не прошла до остановки сервиса: маркер отказа, сервис поднят."""
    who = _people(services)
    services.pop_restore_failed_info = lambda: {"failed": "бэкап не зашифрован"}
    services.pop_restore_done = lambda: None
    return who


def _restore_done_release(services):
    """Восстановление с переходом на выпуск бэкапа (Т-В3): строка версии."""
    who = _people(services)
    services.pop_restore_done = lambda: {"created_at": "2026-09-01T03:00:00+05:00", "version": "1.2.0",
                                         "merge": {"line": "same", "slots": [], "lost": []}}
    return who


def _restore_failed_release(services):
    """Переход на выпуск бэкапа не удался: прежний код назад (Т-В3)."""
    who = _people(services)
    services.pop_restore_failed_info = lambda: {"failed": "слияние базы не удалось", "release": "1.2.0",
                                                "from": "1.3.0"}
    services.pop_restore_done = lambda: None
    return who


async def _ev_restore_done(services, bot):
    from awgbot.bot.handlers.restore import report_restore_result
    await report_restore_result(bot, services)


def _restore_wait(empty: bool, at: str):
    """Ожидание выхода шлюзов на связь после восстановления: с отметки."""
    def build(services):
        import json
        who = _people(services)
        services.db.set_state("restore_wait", json.dumps(
            {"at": at, "empty": empty, "slots": {"2": {"name": "Pi4", "label": "дача", "link_if": "awglink2"}}}))
        return who
    return build


async def _ev_wait_hello(services, bot):
    from awgbot.bot.notifier import send_notifications
    await send_notifications(bot, services.restore_wait_hello(2))


async def _ev_wait_tick(services, bot):
    from awgbot.bot.notifier import send_notifications
    await send_notifications(bot, services.restore_wait_tick())


async def _ev_expiry(services, bot):
    """Задача сроков, как её собирает планировщик: уведомления об истечении
    с кнопкой отсрочки (kb.grace_offer)."""
    from awgbot.bot import keyboards as kb
    from awgbot.bot.notifier import send_notifications
    from awgbot.core import settings
    notes = services.check_expiry()
    for n in notes:
        if getattr(n, "grace_offer_client_id", 0):
            n.reply_markup = kb.grace_offer(n.grace_offer_client_id, settings.get_int("grace.grace_days", 14))
    await send_notifications(bot, notes)


async def _ev_added_by_admin(services, bot):
    """Уведомление владельцу о новом устройстве от админа (kb.added_by_admin)."""
    from awgbot.bot import keyboards as kb
    from awgbot.bot import texts
    from awgbot.bot.notifier import notify_one
    used, limit = services.device_quota(KS)
    await notify_one(bot, 2001, texts.reassign_recipient_notice("Ноутбук", used, limit),
                     reply_markup=kb.added_by_admin(LAPTOP))


EVENTS = [
    # ── события без действия человека ───────────────────────────────────────
    Shot("adm.ev.gw_installed", role="admin", call=("bundle_installed", _ev_installed),
         data=_gw_file_in_chat(True), title="A146: шлюз настроен"),
    Shot("adm.ev.upd_available", role="admin", call=("notify_update_available", _ev_update_available),
         data=_people, title="доступна новая версия"),
    Shot("adm.ev.upd_done", role="admin", call=("report_update_result", _ev_update_result),
         data=_upd_pending("v1.2.3"), title="обновлён после рестарта"),
    Shot("adm.ev.upd_failed", role="admin", call=("report_update_result", _ev_update_result),
         data=_upd_pending("v1.3.0"), title="обновление не применилось"),
    Shot("adm.ev.restarted", role="admin", call=("restore_panel_after_restart", _ev_restarted),
         data=_restart_promised, title="бот вернулся после перезапуска"),
    Shot("adm.ev.restore_done", role="admin", call=("report_restore_result", _ev_restore_done),
         data=_restore_done, title="восстановление из копии завершено"),
    Shot("adm.ev.restore_done.gw", role="admin", call=("report_restore_result", _ev_restore_done),
         data=_restore_done_gw, title="восстановление: итог по каждому шлюзу, устройство не встало"),
    Shot("adm.ev.restore_failed", role="admin", call=("report_restore_result", _ev_restore_done),
         data=_restore_failed, title="копия из чата не восстановлена — ничего не изменилось"),
    Shot("adm.ev.restore_done.release", role="admin", call=("report_restore_result", _ev_restore_done),
         data=_restore_done_release, title="Т-В3: восстановление с переходом на выпуск бэкапа"),
    Shot("adm.ev.restore_failed.release", role="admin", call=("report_restore_result", _ev_restore_done),
         data=_restore_failed_release, title="Т-В3: переход на выпуск бэкапа не удался"),
    Shot("adm.ev.restore_back", role="admin", call=("restore_wait_hello", _ev_wait_hello),
         data=_restore_wait(False, "2026-09-15T11:55:00+05:00"), title="шлюз на связи после восстановления"),
    Shot("adm.ev.restore_lost", role="admin", call=("restore_wait_tick", _ev_wait_tick),
         data=_restore_wait(False, "2026-09-15T11:40:00+05:00"),
         title="шлюз не вышел на связь за 15 минут: непустой сервер"),
    Shot("adm.ev.restore_lost.empty", role="admin", call=("restore_wait_tick", _ev_wait_tick),
         data=_restore_wait(True, "2026-09-15T11:40:00+05:00"),
         title="шлюз не вышел на связь за 15 минут: новый сервер"),
    Shot("adm.ev.zone_adopted", role="admin", call=("_announce_zone", _ev_zone_adopted), data=_people,
         title="первый старт после обновления: ключ пояса снят — время в поясе сервера, прежний был другим"),
    Shot("adm.ev.expired", role="admin", call=("check_expiry", _ev_expiry), data=_br_expired,
         title="подписка истекла — «⏱ Продлить» админу"),
    Shot("adm.ev.grace_offer", role="admin", call=("check_expiry", _ev_expiry), data=_ks_expiring,
         title="подписка истекает — владельцу кнопка отсрочки"),
    Shot("adm.ev.added_by_admin", role="admin", call=("notify_one", _ev_added_by_admin), data=_people,
         title="владельцу — новое устройство от админа"),
]


def _new_generation(available: bool):
    """Поставка привезла поколение ядра новее применённого; available —
    второй интерфейс под переезд уже поднят."""
    def build(services, mp):
        from awgbot.infra import awglock
        who = _mig_ready(services, mp) if available else _people(services)
        mp.setattr(awglock, "generation", lambda: 2)
        mp.setattr(awglock, "applied_generation", lambda: 1)
        return who
    return build


async def _ev_migration_needed(services, bot):
    from awgbot.runtime.main import _notify_migration_needed
    await _notify_migration_needed(bot, services)


def _dns_public(services, mp):
    """Ядро на хосте, DNS клиентов публичный, решения о своём резолвере нет."""
    from awgbot.core import settings
    who = _people(services)
    mp.setattr(config, "AWG_RUNTIME", "host")
    settings.set_value("app.client_config.dns1", "1.1.1.1")
    settings.set_value("app.client_config.dns2", "8.8.8.8")
    return who


async def _ev_private_dns_offer(services, bot):
    from awgbot.runtime.main import _notify_private_dns_offer
    await _notify_private_dns_offer(bot, services)


# ── шаг 5: построители клавиатур без снимка ─────────────────────────────────
GENERATION = [
    Shot("adm.ev.generation_pending", role="admin", call=("notify_migration_needed", _ev_migration_needed),
         data=_new_generation(False), title="новое поколение ядра: интерфейс под переезд не поднят"),
    Shot("adm.ev.migration_needed", role="admin", call=("notify_migration_needed", _ev_migration_needed),
         data=_new_generation(True), title="новое поколение ядра: нужен переезд"),
    Shot("adm.ev.private_dns_offer", role="admin", call=("notify_private_dns_offer", _ev_private_dns_offer),
         data=_dns_public, title="предложение своего DNS-резолвера"),
]
