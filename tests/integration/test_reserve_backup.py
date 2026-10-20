"""Бэкап и линк пары (domain/backup/archive.py: backup_extra).

Конфиг линка пары `awg/awgpair.conf` едет в копии, только когда пара
записана: без него восстановленный сервер не выйдет на связь с резервом;
конфиг под приглашение (до кода, карантин) — не пара, ему в копии не место.

Цена ошибки: копия без конфига пары — после восстановления резерв мёртв до
нового приглашения, а админ уверен, что он есть; копия с конфигом
приглашения — восстановление поднимает линк под чужое, давно истёкшее
приглашение.
"""
from __future__ import annotations

import pytest

from tests.integration import _reserve_common as rc

pytestmark = pytest.mark.integration
rsv = rc.rsv


def _names(services) -> dict[str, bytes]:
    return dict(services.backup_extra())


def test_the_pair_link_config_goes_into_the_backup_with_the_pair(services, rsv):
    rc.pair_as_inviter(services, rsv)
    extra = _names(services)
    assert "awg/awgpair.conf" in extra, sorted(extra)
    assert extra["awg/awgpair.conf"] == rsv.conf.read_bytes(), "в копии не тот конфиг пары"


def test_no_pair_no_pair_config_in_the_backup(services, rsv):
    rsv.conf.write_text("[Interface]\nPrivateKey = DUMMY\n", encoding="utf-8")
    assert "awg/awgpair.conf" not in _names(services), "конфиг без пары уехал в копию"


def test_an_invitation_is_not_a_pair_for_the_backup(services, rsv):
    services.reserve_invite_issue()
    assert rsv.conf.exists()
    assert "awg/awgpair.conf" not in _names(services), "конфиг под приглашение уехал в копию"


def test_a_pair_whose_config_is_gone_does_not_break_the_backup(services, rsv):
    rc.pair_row(services)
    assert "awg/awgpair.conf" not in _names(services)
