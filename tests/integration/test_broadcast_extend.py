"""Объявление с продлением подписки — сервисный слой: план сдвига по типам
подписки, применение одной транзакцией, адресаты только владельцы, шапка и
резерв под неё в лимите текста."""
import datetime

import pytest

from awgbot.bot import texts
from awgbot.core import models
from awgbot.util import timeutil

pytestmark = pytest.mark.integration


def _expire(services, client, iso="2026-09-01T00:00:00+03:00"):
    services.db.update_client_fields(client.id, period_end=iso, status="expired")
    return services.db.get_client(client.id)


def test_plan_active_expired_unlimited(services, make_active_client):
    """Активной — конец + N; истёкшей — сегодня + N (от старого конца плюшка
    была бы пустой); бессрочной — нечего. Порядок — по имени."""
    a = make_active_client("Ксюша", tg_id=8001)
    u = make_active_client("Антон", tg_id=8002, period_kind="never")
    e = _expire(services, make_active_client("Вера", tg_id=8003))
    plan = services.extension_plan([a.id, u.id, e.id], 10)
    assert [x.client.name for x in plan] == ["Антон", "Вера", "Ксюша"]
    by = {x.client.id: x for x in plan}
    assert by[u.id].unlimited and by[u.id].old_end is None
    old = timeutil.parse_iso(a.period_end)
    assert by[a.id].old_end == old and by[a.id].new_end == old + datetime.timedelta(days=10)
    assert not by[a.id].from_now
    assert by[e.id].from_now and by[e.id].old_end == timeutil.parse_iso(e.period_end)
    assert abs((by[e.id].new_end - timeutil.now()).total_seconds() - 10 * 86400) < 5


def test_extend_days_shifts_only_the_right_border(services, make_active_client):
    """Начало и тип периода, долг отсрочки, периодный трафик — не трогаются;
    пороги «истекает через…» сбрасываются; штатное «Подписка продлена до…»
    владельцу не шлётся — эту роль играет шапка."""
    c = make_active_client("Ксюша", tg_id=8010)
    services.db.update_client_fields(c.id, grace_used=1, grace_pending_cut=3 * 86400,
                                     notified_thresholds="10080")
    before = services.db.get_client(c.id)
    plan, notes = services.extend_days([c.id], 7)
    after = services.db.get_client(c.id)
    assert after.period_start == before.period_start and after.period_kind == before.period_kind
    assert timeutil.parse_iso(after.period_end) == timeutil.parse_iso(before.period_end) + datetime.timedelta(days=7)
    assert int(after.grace_pending_cut) == 3 * 86400 and int(after.grace_used) == 1
    assert after.notified_thresholds == set()
    assert notes == [], "владельцу ушло штатное уведомление помимо шапки"
    assert plan[0].new_end == timeutil.parse_iso(after.period_end)


def test_extend_days_reactivates_expired_and_tells_the_holder(services, fake_awg, make_active_client):
    """Истёкшая: статус активна, EXPIRY снят с устройств; держателю переданного
    устройства — «доступ вернулся», владельцу — ничего (шапка скажет)."""
    from awgbot.core.blocks import DeviceBlock
    owner = make_active_client("Вера", tg_id=8020)
    dc = services.add_device(owner.id, "Тел")
    assert services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=8021).ok
    _expire(services, owner)
    services._device_set_block(dc.device_id, DeviceBlock.EXPIRY)
    plan, notes = services.extend_days([owner.id], 7)
    fresh = services.db.get_client(owner.id)
    assert fresh.status == "active" and timeutil.parse_iso(fresh.period_end) > timeutil.now()
    assert int(services.db.get_device(dc.device_id).block_reason) & int(DeviceBlock.EXPIRY) == 0
    assert [n.tg_id for n in notes] == [8021]


def test_extend_days_on_open_admin_pause_moves_the_saved_end(services, make_active_client):
    """На открытой админ-паузе period_end пуст, настоящий конец — сохранённый:
    сдвигаем его, дни проявятся после выхода из паузы."""
    c = make_active_client("Паузный", tg_id=8030)
    saved = c.period_end
    services.db.save_pause(c.id, models.PauseState(active_since=timeutil.now_iso(),
                                                    mode="admin_open", saved_end=saved))
    services.db.update_client_fields(c.id, period_end=None)
    c = services.db.get_client(c.id)
    assert c.effective_period_end == saved
    plan, _ = services.extend_days([c.id], 5)
    assert not plan[0].unlimited and plan[0].old_end == timeutil.parse_iso(saved)
    after = services.db.get_client(c.id)
    assert after.period_end is None, "пауза сломана: конец появился"
    assert timeutil.parse_iso(after.pause_saved_end) == timeutil.parse_iso(saved) + datetime.timedelta(days=5)


def test_extend_days_skips_unlimited(services, make_active_client):
    u = make_active_client("Антон", tg_id=8040, period_kind="never")
    plan, notes = services.extend_days([u.id], 30)
    assert plan[0].unlimited and services.db.get_client(u.id).period_end is None


def test_owners_only_recipients(services, make_active_client):
    """С продлением объявление уходит только владельцам — держатели их
    устройств не получают."""
    import awgbot.core.config as cfg
    c = make_active_client("Один", tg_id=8050)
    dc = services.add_device(c.id, "Тел")
    services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=8051)
    assert services.db.broadcast_recipients_for_clients([c.id], cfg.ADMIN_ID) == [8050, 8051]
    assert services.db.broadcast_recipients_for_clients([c.id], cfg.ADMIN_ID, owners_only=True) == [8050]


def test_header_variants_and_reserve(services, make_active_client):
    """Шапка: активной — «увеличена на N дней» и старая → новая; истёкшей —
    «с текущей даты» и новая; бессрочной — пусто. Резерв в лимите — по самой
    длинной шапке, без тегов."""
    a = make_active_client("Ксюша", tg_id=8060)
    e = _expire(services, make_active_client("Вера", tg_id=8061))
    u = make_active_client("Антон", tg_id=8062, period_kind="never")
    plan = {x.client.id: x for x in services.extension_plan([a.id, e.id, u.id], 1)}
    old, new = timeutil.fmt_date(plan[a.id].old_end), timeutil.fmt_date(plan[a.id].new_end)
    assert texts.extension_header(1, plan[a.id]) == (
        f"<b>Длительность твоей подписки увеличена на 1 день 🙂\n{old} → {new}</b>")
    assert texts.extension_header(5, plan[e.id]) == (
        "<b>К длительности твоей подписки добавлено 5 дней с текущей даты 🙂\n"
        f"Теперь срок подписки: до {timeutil.fmt_date(plan[e.id].new_end)}</b>")
    assert texts.extension_header(5, plan[u.id]) == ""
    assert texts.announcement_text("", "текст") == "текст"
    assert texts.announcement_text("<b>шапка</b>", "") == "<b>шапка</b>"
    assert texts.announcement_text("<b>шапка</b>", "текст") == "<b>шапка</b>\n\nтекст"
    r = texts.extension_reserve()
    assert 70 <= r <= 110, r
    prompt = texts.broadcast_prompt([a], False, extend_days=10)
    assert prompt.startswith("✅ Принято: перед отправкой уведомления подписка адресата будет продлена на <b>10 дней</b>")
    assert f"без них — {4096 - r} символов, с ними — {1024 - r}" in prompt
    assert "адресатов" in texts.broadcast_prompt([a, e], False, extend_days=2)


def test_days_prompt_and_preview_footer(services, make_active_client):
    a = make_active_client("Ксюша", tg_id=8070)
    u = make_active_client("Антон", tg_id=8071, period_kind="never")
    e = _expire(services, make_active_client("Вера", tg_id=8072))
    plan = services.extension_plan([a.id, u.id, e.id], 10)
    p = texts.broadcast_days_prompt(plan)
    assert "Выбранные адресаты:\n• <b>Антон</b> (∞, без продления)\n• <b>Вера</b>\n• <b>Ксюша</b>" in p
    assert p.endswith("На какое количество дней им необходимо продлить подписку? (1–365)")
    solo = texts.broadcast_days_prompt(plan[2:])
    assert "Адресат уведомления — <b>Ксюша</b>." in solo and "ему необходимо" in solo
    pv = texts.broadcast_preview("<b>шапка</b>\n\nтекст", 3, [a, u, e], False, (10, plan))
    assert "Будет отправлено <b>3</b> людям, их подписка будет продлена на <b>10 дней</b>:" in pv
    assert "• <b>Антон</b>: ∞ — без продления" in pv
    by = {x.client.id: x for x in plan}
    assert f"• <b>Вера</b>: ⛔ {timeutil.fmt_date(by[e.id].old_end)} → {timeutil.fmt_date(by[e.id].new_end)}" in pv
    assert f"• <b>Ксюша</b>: {timeutil.fmt_date(by[a.id].old_end)} → {timeutil.fmt_date(by[a.id].new_end)}" in pv
    assert "Получит" not in pv and pv.endswith("Отправляем?")
    one = texts.broadcast_preview("t", 1, [a], False, (3, plan[2:]))
    assert "Будет отправлено <b>1</b> человеку, его подписка будет продлена на <b>3 дня</b>:" in one
    rep = texts.broadcast_report([a, u], False, 1, 1, (10, plan[:1] + plan[2:]))
    assert rep.startswith("✅ Объявление выше доставлено владельцам профилей Ксюша, Антон; "
                          "подписка продлена на 10 дней (Антон — бессрочная, без продления).")
    assert rep.endswith("⚠️ Не доставлено 1 адресату — заблокировали бота или удалили аккаунт; "
                        "подписка ему всё равно продлена.")
