"""Значки строк «🩺 Здоровье» агента (domain/gwchecks.check_mark) и счёт
проблем: ✅ скрыт, 🔴 считается, 🟡 — ожидание (самопочинка, первый замер,
тихие группы), «» — выключенная функция (GwCheck.off): ни строки, ни счёта.
Решение 16.10.2026: статус выключенных функций в здоровье не выводится."""
from awgbot.domain import gwchecks as gc
from awgbot.domain.gateway import GwCheck, GwStatus


def test_marks_by_state():
    assert gc.check_mark(GwCheck("a", True)) == "✅"
    assert gc.check_mark(GwCheck("a", False, "цепочки нет")) == "🔴"
    assert gc.check_mark(GwCheck("a", False, f"упало: {gc.SELF_HEAL_TAIL}")) == "🟡"
    assert gc.check_mark(GwCheck("a", None, gc.FIRST_TICK_DETAIL)) == "🟡"
    assert gc.check_mark(GwCheck("a", None, "нечем проверить")) == "🔴"


def test_quiet_groups_wait_but_do_not_count():
    for group in gc.CHECK_GROUPS_QUIET_UNKNOWN:
        c = GwCheck("a", None, "ждут синхронизации", group=group)
        assert gc.check_mark(c) == "🟡"
        assert gc.health_problems([c]) == []


def test_off_feature_is_neither_line_nor_problem():
    off = GwCheck(gc.CHECK_SSH_PORT, None, "sshd не запущен", group="ssh", off=True)
    bad = GwCheck("b", False, "цепочки нет")
    assert gc.check_mark(off) == ""
    assert gc.health_problems([off, bad]) == [bad]
    # off сильнее любого вердикта: выключенная функция не рисуется даже с ok=False
    assert gc.check_mark(GwCheck("c", False, "x", off=True)) == ""


def test_off_survives_snapshot_roundtrip_and_old_snapshots():
    st = GwStatus(checks=[GwCheck("a", None, "sshd не запущен", group="ssh", off=True)])
    back = GwStatus.from_json(st.to_json())
    assert back.checks[0].off is True
    # снимок прежней версии без поля off — по умолчанию функция включена
    old = GwStatus.from_json('{"checks": [{"name": "a", "ok": null, "detail": "", "group": "ssh"}]}')
    assert old.checks[0].off is False and gc.check_mark(old.checks[0]) == "🟡"
