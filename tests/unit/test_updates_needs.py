"""Сверка версий ролей в выборе цели: условия на другую роль — только в
метаданных релиза (`roles.gw.needs_main` — агенту нужен сервер AWG не ниже,
`roles.main.needs_gw` — основному боту нужны шлюзы не ниже). Каждая роль
читает только своё поле; хотфикс условий не несёт; условие идёт по цепочке
от установленного до цели; допустимый ниже ждущего предлагается, ждущий — в
waiting_target. Хэштеги в теле условий не дают."""
from __future__ import annotations

from awgbot.infra import updates


def _r(tag: str, main: str | None = None, gw: str | None = None, *, needs_gw: str | None = None,
       needs_main: str | None = None, gen: int = 1, body: str = "") -> updates.Release:
    """Релиз с метаданными: main / gw — минимум роли (есть — адресован)."""
    roles = {}
    if main is not None:
        roles["main"] = {"requires": main, **({"needs_gw": needs_gw} if needs_gw else {})}
    if gw is not None:
        roles["gw"] = {"requires": gw, **({"needs_main": needs_main} if needs_main else {})}
    meta = {"schema": 1, "version": tag.lstrip("v"), "awg_gen": gen, "roles": roles}
    return updates.Release(tag=tag, version=updates.parse_version(tag), body=body,
                           asset_url=f"https://api/assets/{tag}", sha256="a" * 64,
                           meta=updates.meta.check(meta, tag))


def _peer(have: tuple):
    """Другая сторона версии have: условие need выполнено, если have ≥ need."""
    return lambda need: have >= need


# ── разбор ────────────────────────────────────────────────────────────────────

def test_each_role_reads_only_its_own_field():
    rel = _r("v3.3.1", "3.3.0", "3.3.0", needs_gw="3.3.0", needs_main="3.3.1")
    assert rel.needs("gateway") == (3, 3, 1), "агент читает roles.gw.needs_main"
    assert rel.needs("client") == (3, 3, 0), "основной бот читает roles.main.needs_gw"
    assert rel.needs("") == (3, 3, 0)
    only_gw = _r("v3.3.1", gw="3.3.0", needs_main="3.3.0")
    assert only_gw.needs("client") is None, "условие агента основному боту ничего не говорит"
    only_main = _r("v3.3.1", "3.3.0", needs_gw="3.3.0")
    assert only_main.needs("gateway") is None, "условие основного агенту ничего не говорит"


def test_no_field_means_no_condition():
    assert _r("v3.3.1", "3.2.0").needs("client") is None
    assert _r("v3.3.1", gw="3.2.0").needs("gateway") is None


def test_a_hotfix_carries_no_condition():
    """У хотфикса needs_* в метаданных — ошибка проверки; и без неё условия
    у X.Y.Z.P не читаются."""
    import pytest
    with pytest.raises(updates.MetaError):
        _r("v3.3.1.1", gw="3.3.0", needs_main="3.3.1")
    assert _r("v3.3.1.1", gw="3.3.0").needs("gateway") is None


def test_hashtags_in_the_body_are_not_conditions():
    """Перекрёстные условия — только в метаданных: черновые хэштеги концепта
    в теле не читаются ни с метаданными, ни без них."""
    body = "#gw_needs_main_3.3.0\n#main_needs_gw_3.3.0\n#needs_main_3.3.0"
    assert _r("v3.3.1", "3.3.0", "3.3.0", body=body).needs("gateway") is None
    bare = updates.Release(tag="v3.3.1", version=(3, 3, 1), body="#requires_gw_3.3.0\n" + body,
                           asset_url=None, sha256=None)
    assert bare.needs("gateway") is None and bare.needs("client") is None


# ── выбор цели ───────────────────────────────────────────────────────────────

_GW = [_r("v3.3.0", gw="3.2.0"),
       _r("v3.3.1", gw="3.2.0", needs_main="3.3.1"),
       _r("v3.3.2", gw="3.2.0")]


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
    rel = [_r("v3.3.0", gw="3.2.0"),
           _r("v3.3.1", gw="3.2.0", needs_main="3.3.1"),
           _r("v3.3.1.1", gw="3.2.0")]
    peer = _peer((3, 3, 0))
    assert updates.pick_target((3, 2, 1), rel, "gateway", peer=peer).tag == "v3.3.0"
    assert updates.waiting_target((3, 2, 1), rel, "gateway", peer=peer).tag == "v3.3.1.1"


def test_a_condition_already_passed_is_not_in_the_chain():
    """Установленный уже выше релиза с условием — условие позади."""
    peer = _peer((3, 2, 0))
    assert updates.pick_target((3, 3, 1), _GW, "gateway", peer=peer).tag == "v3.3.2"


def test_nothing_admissible_means_no_target_and_the_newest_waits():
    rel = [_r("v3.3.1", gw="3.2.0", needs_main="3.3.1"), _r("v3.3.2", gw="3.2.0")]
    peer = _peer((3, 2, 1))
    assert updates.pick_target((3, 3, 0), rel, "gateway", peer=peer) is None
    assert updates.waiting_target((3, 3, 0), rel, "gateway", peer=peer).tag == "v3.3.2"


def test_lowering_by_minimum_keeps_the_condition():
    """Понижение по минимуму не ломает условие: цепочка пониженной цели —
    часть цепочки допустимой."""
    rel = [_r("v3.2.0", "3.1.0"),
           _r("v3.3.0", "3.2.0"),
           _r("v3.3.1", "3.2.0", needs_gw="3.3.0"),
           _r("v3.4.0", "3.3.1", needs_gw="3.4.0")]
    peer = _peer((3, 3, 0))
    # допустимый новейший — v3.3.1 (v3.4.0 ждёт шлюзы 3.4.0); минимум 3.2.0 → v3.2.0
    assert updates.pick_target((3, 1, 0), rel, "client", peer=peer).tag == "v3.2.0"
    assert updates.waiting_target((3, 1, 0), rel, "client", peer=peer).tag == "v3.4.0"
    # с 3.2.0 — сразу на допустимый v3.3.1
    assert updates.pick_target((3, 2, 0), rel, "client", peer=peer).tag == "v3.3.1"


def test_releases_of_the_other_role_do_not_carry_conditions_into_the_chain():
    """Релиз только для агента с needs_main основному боту не адресован — в
    его цепочку не входит."""
    rel = [_r("v3.3.1", gw="3.2.0", needs_main="3.3.1"),
           _r("v3.3.2", "3.2.0")]
    assert updates.pick_target((3, 3, 0), rel, "client", peer=_peer((0, 0, 0))).tag == "v3.3.2"


def test_generation_ceiling_still_applies_with_a_peer():
    rel = [_r("v3.3.1", gw="3.2.0", gen=1), _r("v3.4.0", gw="3.2.0", gen=2)]
    assert updates.pick_target((3, 3, 0), rel, "gateway", max_generation=1,
                               peer=_peer((9, 9, 9))).tag == "v3.3.1"
