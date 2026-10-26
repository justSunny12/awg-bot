"""E2E: бонус трафика при исчерпании месячного лимита профиля — выдача в
каждом формате правила «💳 Подписки → 📈 Бонус», выключенный бонус (блок
сразу), повтор в том же месяце, исчерпание уже выданного.

Цена ошибки: бонус не в тех байтах — профиль упирается в лимит раньше или
позже обещанного в уведомлении; выключенный бонус, выдавший «0 ГБ» и
блок тиком позже, шлёт человеку «Добавлено 0 ГБ», а затем «Дополнительная
квота исчерпана», которой не было; повторная выдача в месяце — бесконечная
квота; уведомление, пришедшее дважды, — шум у пользователя и админа.
"""
import re

import pytest

from awgbot.core import config, settings
from awgbot.core.blocks import ClientBlock, DeviceBlock
from awgbot.domain.services import BYTES_PER_GB as GB

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID
UNTIL = r"\d{2}\.\d{2}"                 # дата сброса лимита в текстах


@pytest.fixture()
def rule(monkeypatch):
    """Правило бонуса в conf: rule["v"] = 0 | 50 | "10%" | "10% 50"."""
    conf: dict = {}
    real = settings.get
    monkeypatch.setattr(settings, "get",
                        lambda k, d=None: conf["v"] if k == "limits.traffic_bonus_gb" and "v" in conf
                        else real(k, d))
    return conf


def _over(services, make_active_client, limit_gb, used_gb, tg_id=700, name="Вася"):
    """Профиль с лимитом limit_gb ГБ (дробный — через байты) и расходом used_gb."""
    c = make_active_client(tg_id=tg_id, name=name)
    dc = services.add_device(c.id, "d")
    services.set_client_traffic_limit(c.id, int(limit_gb * GB))
    services.db.add_traffic_bulk([(dc.device_id, int(used_gb * GB), 0)])
    return c, dc


def _texts(notes, tg_id):
    return [n.text for n in notes if n.tg_id == tg_id]


def _blocked(services, c, dc, fake_awg):
    fresh = services.db.get_client(c.id)
    dev = services.db.get_device(dc.device_id)
    return (bool(int(fresh.block_reason) & int(ClientBlock.TRAFFIC_CLIENT)),
            bool(int(dev.block_reason) & int(DeviceBlock.TRAFFIC_CLIENT)),
            dc.address in fake_awg.blocked)


# ── выдача в каждом формате ──────────────────────────────────────────────────

@pytest.mark.parametrize("value, limit_gb, given, amount, note", [
    (50, 100, 50 * GB, "50 ГБ", ""),
    ("10%", 100, 10 * GB, "10 ГБ", " (10% лимита)"),
    ("10%", 1, GB // 2, "0,5 ГБ", " (10% лимита, не более половины)"),
    ("80%", 10, 5 * GB, "5 ГБ", " (80% лимита, не более половины)"),
    ("20%", 3, 1 * GB, "1 ГБ", " (20% лимита)"),
    ("10% 50", 1000, 50 * GB, "50 ГБ", " (10% лимита, не более 50 ГБ)"),
    ("10% 50", 100, 10 * GB, "10 ГБ", " (10% лимита)"),
])
def test_bonus_is_granted_by_the_rule_and_announced_once(services, fake_awg, make_active_client, rule,
                                                         value, limit_gb, given, amount, note):
    """Первое исчерпание в месяце: bonus_bytes — ровно по правилу, доступ не
    тронут, пользователю — сколько добавлено и когда лимит сбросится, админу —
    сколько выдано и откуда число. Второй тик — ни новой выдачи, ни
    повторных уведомлений."""
    rule["v"] = value
    c, dc = _over(services, make_active_client, limit_gb, limit_gb + 0.001)
    notes = services.check_traffic_limits()
    fresh = services.db.get_client(c.id)
    assert fresh.bonus_granted_month == 1
    assert int(fresh.bonus_bytes) == given, f"выдано {int(fresh.bonus_bytes) / GB} ГБ вместо {given / GB}"
    assert _blocked(services, c, dc, fake_awg) == (False, False, False), "бонус выдан — доступ должен остаться"

    cli = _texts(notes, 700)
    assert len(cli) == 1, cli
    assert re.fullmatch(
        rf"📈 Месячный лимит исчерпан\. Добавлено {re.escape(amount)} до конца месяца — "
        rf"разово, больше в этом месяце квота не вырастет\. Лимит сбросится {UNTIL}", cli[0]), cli[0]
    adm = _texts(notes, ADMIN)
    assert len(adm) == 1, adm
    assert adm[0].startswith("📈 ") and adm[0].endswith(
        f": лимит исчерпан — выдано {amount}{note} до конца месяца"), adm[0]
    assert "Вася" in adm[0] and "разово" not in adm[0], adm[0]

    again = services.check_traffic_limits()
    assert int(services.db.get_client(c.id).bonus_bytes) == given, "бонус выдан второй раз в месяце"
    assert not [t for t in _texts(again, 700) + _texts(again, ADMIN) if t.startswith("📈")], \
        f"повтор уведомления о бонусе: {again}"


@pytest.mark.parametrize("value", ["10%", "10% 50", 5])
def test_no_80_percent_warning_right_after_the_bonus(services, fake_awg, make_active_client, rule, value):
    """Только что пришло «Месячный лимит исчерпан. Добавлено 10 ГБ» — тиком
    позже не должно прийти «Израсходовано ~80% месячного лимита»: лимит уже
    пройден, а маленький бонус (проценты) делает такое предупреждение
    правилом, а не редкостью."""
    rule["v"] = value
    _over(services, make_active_client, 100, 100.001)
    services.check_traffic_limits()
    again = services.check_traffic_limits()
    assert not _texts(again, 700), f"после бонуса — предупреждение о ~80% лимита: {_texts(again, 700)}"


def test_admin_bonus_notice_respects_its_toggle(services, fake_awg, make_active_client, rule, monkeypatch):
    """Тумблер «Бонусный объём» выключен — админ молчит, пользователю текст уходит."""
    rule["v"] = "10%"
    real = settings.get_bool
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: False
                        if k == "notifications.client_events.bonus" else real(k, d))
    _over(services, make_active_client, 100, 101)
    notes = services.check_traffic_limits()
    assert _texts(notes, 700) and not _texts(notes, ADMIN), notes


def test_profile_without_a_limit_gets_no_bonus(services, fake_awg, make_active_client, rule):
    """Безлимитный профиль: ни бонуса, ни уведомлений при любом расходе."""
    rule["v"] = "10%"
    c = make_active_client(tg_id=701)
    dc = services.add_device(c.id, "d")
    services.db.add_traffic_bulk([(dc.device_id, 5000 * GB, 0)])
    notes = services.check_traffic_limits()
    fresh = services.db.get_client(c.id)
    assert fresh.bonus_granted_month == 0 and int(fresh.bonus_bytes) == 0
    assert not _texts(notes, 701)


def test_below_the_limit_nothing_is_granted(services, fake_awg, make_active_client, rule):
    rule["v"] = 50
    c, _ = _over(services, make_active_client, 100, 99)
    services.check_traffic_limits()
    assert services.db.get_client(c.id).bonus_granted_month == 0


# ── бонус выключен ───────────────────────────────────────────────────────────

def test_off_blocks_at_once_with_its_own_texts(services, fake_awg, make_active_client, rule):
    """«0»: исчерпал лимит — блок всех устройств в тот же тик, без выдачи
    «0 ГБ»; тексты — про лимит, а не про «дополнительную квоту», которой не
    было. Повторный тик — тишина."""
    rule["v"] = 0
    c, dc = _over(services, make_active_client, 10, 11)
    notes = services.check_traffic_limits()
    fresh = services.db.get_client(c.id)
    assert fresh.bonus_granted_month == 0 and int(fresh.bonus_bytes) == 0, "при выкл. бонус не выдаётся"
    assert _blocked(services, c, dc, fake_awg) == (True, True, True), "доступ должен быть приостановлен сразу"
    cli = _texts(notes, 700)
    assert len(cli) == 1 and re.fullmatch(
        rf"🔴 Месячный лимит исчерпан\. Доступ всех устройств приостановлен до {UNTIL}", cli[0]), cli
    adm = _texts(notes, ADMIN)
    assert len(adm) == 1 and adm[0].startswith("🔴 ") and adm[0].endswith(
        ": лимит исчерпан — доступ приостановлен") and "Вася" in adm[0], adm
    assert not any("квота" in t for t in cli + adm), "про доп. квоту при выключенном бонусе — неправда"

    again = services.check_traffic_limits()
    assert not _texts(again, 700) and not _texts(again, ADMIN), f"повтор уведомлений: {again}"


def test_enabling_the_bonus_after_an_off_block_does_not_promise_traffic_to_a_blocked_profile(
        services, fake_awg, make_active_client, rule):
    """Бонус был выключен — профиль заблокирован; админ включил «10%» в том
    же месяце. Следующий тик не должен сообщать пользователю «Добавлено N ГБ»,
    оставив доступ приостановленным: либо бонус выдан и доступ вернулся,
    либо обещания нет."""
    rule["v"] = 0
    c, dc = _over(services, make_active_client, 10, 10.5)
    services.check_traffic_limits()
    rule["v"] = "10%"
    notes = services.check_traffic_limits()
    promised = any("Добавлено" in t for t in _texts(notes, 700))
    blocked = _blocked(services, c, dc, fake_awg)
    assert not (promised and any(blocked)), (
        f"пользователю обещан бонус ({_texts(notes, 700)}), а доступ приостановлен: {blocked}")


# ── исчерпание выданного ─────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [50, "10%", "10% 50", 0])
def test_exhausted_bonus_blocks_with_the_former_texts(services, fake_awg, make_active_client, rule, value):
    """Бонус уже выдан и тоже исчерпан — прежние тексты про доп. квоту при
    любом правиле, в том числе если правило за месяц переключили на «0»."""
    rule["v"] = value
    c, dc = _over(services, make_active_client, 10, 10.5)
    services.db.update_client_fields(c.id, bonus_granted_month=1, bonus_bytes=0)
    notes = services.check_traffic_limits()
    assert _blocked(services, c, dc, fake_awg) == (True, True, True)
    cli = _texts(notes, 700)
    assert len(cli) == 1 and re.fullmatch(
        rf"🔴 Дополнительная квота исчерпана\. Доступ всех устройств приостановлен до {UNTIL}", cli[0]), cli
    adm = _texts(notes, ADMIN)
    assert len(adm) == 1 and adm[0].endswith(": исчерпан лимит и доп. квота — доступ приостановлен"), adm
    assert int(services.db.get_client(c.id).bonus_bytes) == 0, "второй бонус в месяце"


def test_full_month_path_bonus_then_block(services, fake_awg, make_active_client, rule):
    """Сквозь месяц: лимит → бонус «10%» (1 ГБ при лимите 10) → расход сверх
    лимита и бонуса → блок с прежними текстами, по одному разу."""
    rule["v"] = "10%"
    c, dc = _over(services, make_active_client, 10, 10.2)
    first = services.check_traffic_limits()
    assert int(services.db.get_client(c.id).bonus_bytes) == GB
    assert _blocked(services, c, dc, fake_awg) == (False, False, False)
    assert any("Добавлено 1 ГБ" in t for t in _texts(first, 700))
    services.db.add_traffic_bulk([(dc.device_id, GB, 0)])           # 11,2 > 11
    second = services.check_traffic_limits()
    assert _blocked(services, c, dc, fake_awg) == (True, True, True)
    assert [t[:33] for t in _texts(second, 700)] == ["🔴 Дополнительная квота исчерпана."], second
