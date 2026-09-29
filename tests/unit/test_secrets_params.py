"""
Боевые параметры argon2id — по исходнику: autouse-фикстура прогона подменяет
их на MIN, и обычный import показал бы подмену, а не то, что уедет на хост.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "awgbot" / "util" / "secrets_util.py"


def test_passphrase_kdf_is_argon2id_moderate_in_production():
    """MODERATE — сотни мс и ~256 МБ на фразу: заметно дороже брутфорса, и
    терпимо для разовых операций (копия раз в месяц, восстановление руками)."""
    from nacl import pwhash
    spec = importlib.util.spec_from_file_location("secrets_util_fresh", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._OPS == pwhash.argon2id.OPSLIMIT_MODERATE
    assert mod._MEM == pwhash.argon2id.MEMLIMIT_MODERATE
