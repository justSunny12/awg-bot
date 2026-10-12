"""Сверка версий ролей в выборе цели: `#gw_needs_main_X` (агенту нужен сервер
AWG не ниже X) и `#main_needs_gw_X` (основному боту нужны шлюзы не ниже X).
Каждая роль читает только свою строку; хотфикс условий не несёт; условие идёт
по цепочке от установленного до цели; допустимый ниже ждущего предлагается,
ждущий — в waiting_target."""
from __future__ import annotations

from awgbot.infra import updates


def _r(tag: str, body: str = "") -> updates.Release:
    return updates.Release(tag=tag, version=updates.parse_version(tag), body=body,
                           asset_url=f"https://api/assets/{tag}", sha256="a" * 64)


def _peer(have: tuple):
    """Другая сторона версии have: условие need выполнено, если have ≥ need."""
    return lambda need: have >= need


# ── разбор ────────────────────────────────────────────────────────────────────

def test_each_role_reads_only_its_own_line():
    rel = _r("v3.3.1", "#requires_main_3.3.0\n#requires_gw_3.3.0\n"
                       "#gw_needs_main_3.3.1\n#main_needs_gw_3.3.0\n#awg_gen2")
    assert rel.needs("gateway") == (3, 3, 1), "агент читает #gw_needs_main_"
    assert rel.needs("client") == (3, 3, 0), "основной бот читает #main_needs_gw_"
    assert rel.needs("") == (3, 3, 0)
    only_gw = _r("v3.3.1", "#requires_gw_3.3.0\n#gw_needs_main_3.3.0")
    assert only_gw.needs("client") is None, "строка агента основному боту ничего не говорит"
    only_main = _r("v3.3.1", "#requires_main_3.3.0\n#main_needs_gw_3.3.0")
    assert only_main.needs("gateway") is None, "строка основного агенту ничего не говорит"


def test_no_line_means_no_condition():
    assert _r("v3.3.1", "#requires_main_3.2.0\n#awg_gen2").needs("client") is None
    assert _r("v3.3.1", "").needs("gateway") is None


def test_a_hotfix_carries_no_condition_even_if_the_line_is_there():
    rel = _r("v3.3.1.1", "#requires_gw_3.3.0\n#gw_needs_main_3.3.1")
    assert rel.needs("gateway") is None


def test_a_four_digit_version_in_the_line_is_not_a_condition():
    """Версия в строке — три цифры; «3.3.0.1» не читается как «3.3.0»."""
    assert _r("v3.3.1", "#gw_needs_main_3.3.0.1").needs("gateway") is None
    assert _r("v3.3.1", "#gw_needs_main_v3.3.0").needs("gateway") == (3, 3, 0)


def test_the_draft_name_is_not_read():
    assert _r("v3.3.1", "#needs_main_3.3.0\n#needs_gw_3.3.0").needs("gateway") is None
    assert _r("v3.3.1", "#needs_main_3.3.0\n#needs_gw_3.3.0").needs("client") is None


# ── выбор цели ───────────────────────────────────────────────────────────────

_GW = [_r("v3.3.0", "#requires_gw_3.2.0"),
       _r("v3.3.1", "#requires_gw_3.2.0\n#gw_needs_main_3.3.1"),
       _r("v3.3.2", "#requires_gw_3.2.0")]


def test_without_peer_the_choice_is_as_before():
    assert updates.pick_target((3, 2, 1), _GW, "gateway").tag == "v3.3.2"
    assert updates.waiting_target((3, 2, 1), _GW, "gateway") is None


def test_met_condition_lets_the_newest_through():
    assert updates.pick_target((3, 2, 1), _GW, "gateway", peer=_peer((3, 3, 1))).tag == "v3.3.2"
    assert updates.waiting_target((3, 2, 1), _GW, "gateway", peer=_peer((3, 3, 1))) is None


def test_chain_holds_a_later_release_without_its_own_condition():
    """v3.3.2 сам условий не несёт, но цепочка до него идёт через v3.3.1 с
    условием: без сервера 3.3.1 недопустимы оба — предлагается v3.3.0."""
    peer = _peer((3, 3, 0))
    assert updates.pick_target((3, 2, 1), _GW, "gateway", peer=peer).tag == "v3.3.0"
    assert updates.waiting_target((3, 2, 1), _GW, "gateway", peer=peer).tag == "v3.3.2"
    assert updates.chain_need((3, 2, 1), _GW, _GW[-1], "gateway") == (3, 3, 1)


def test_a_hotfix_does_not_lead_past_its_base_with_a_condition():
    """Хотфикс X.Y.Z.1 без условий после X.Y.Z с условием — тоже ждёт: иначе
    он увёл бы через голову базы."""
    rel = [_r("v3.3.0", "#requires_gw_3.2.0"),
           _r("v3.3.1", "#requires_gw_3.2.0\n#gw_needs_main_3.3.1"),
           _r("v3.3.1.1", "#requires_gw_3.2.0")]
    peer = _peer((3, 3, 0))
    assert updates.pick_target((3, 2, 1), rel, "gateway", peer=peer).tag == "v3.3.0"
    assert updates.waiting_target((3, 2, 1), rel, "gateway", peer=peer).tag == "v3.3.1.1"


def test_a_condition_already_passed_is_not_in_the_chain():
    """Установленный уже выше релиза с условием — условие позади."""
    peer = _peer((3, 2, 0))
    assert updates.pick_target((3, 3, 1), _GW, "gateway", peer=peer).tag == "v3.3.2"


def test_nothing_admissible_means_no_target_and_the_newest_waits():
    rel = [_r("v3.3.1", "#requires_gw_3.2.0\n#gw_needs_main_3.3.1"), _r("v3.3.2", "#requires_gw_3.2.0")]
    peer = _peer((3, 2, 1))
    assert updates.pick_target((3, 3, 0), rel, "gateway", peer=peer) is None
    assert updates.waiting_target((3, 3, 0), rel, "gateway", peer=peer).tag == "v3.3.2"


def test_lowering_by_minimum_keeps_the_condition():
    """Понижение по минимуму не ломает условие: цепочка пониженной цели —
    часть цепочки допустимой."""
    rel = [_r("v3.2.0", "#requires_main_3.1.0"),
           _r("v3.3.0", "#requires_main_3.2.0"),
           _r("v3.3.1", "#requires_main_3.2.0\n#main_needs_gw_3.3.0"),
           _r("v3.4.0", "#requires_main_3.3.1\n#main_needs_gw_3.4.0")]
    peer = _peer((3, 3, 0))
    # допустимый новейший — v3.3.1 (v3.4.0 ждёт шлюзы 3.4.0); минимум 3.2.0 → v3.2.0
    assert updates.pick_target((3, 1, 0), rel, "client", peer=peer).tag == "v3.2.0"
    assert updates.waiting_target((3, 1, 0), rel, "client", peer=peer).tag == "v3.4.0"
    # с 3.2.0 — сразу на допустимый v3.3.1
    assert updates.pick_target((3, 2, 0), rel, "client", peer=peer).tag == "v3.3.1"


def test_releases_of_the_other_role_do_not_carry_conditions_into_the_chain():
    """Релиз только для агента с #gw_needs_main_ основному боту не адресован —
    в его цепочку не входит."""
    rel = [_r("v3.3.1", "#requires_gw_3.2.0\n#gw_needs_main_3.3.1"),
           _r("v3.3.2", "#requires_main_3.2.0")]
    assert updates.pick_target((3, 3, 0), rel, "client", peer=_peer((0, 0, 0))).tag == "v3.3.2"


def test_generation_ceiling_still_applies_with_a_peer():
    rel = [_r("v3.3.1", "#requires_gw_3.2.0\n#awg_gen1"), _r("v3.4.0", "#requires_gw_3.2.0\n#awg_gen2")]
    assert updates.pick_target((3, 3, 0), rel, "gateway", max_generation=1,
                               peer=_peer((9, 9, 9))).tag == "v3.3.1"
