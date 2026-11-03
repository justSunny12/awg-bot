"""Цифры уровней стенда (tests/lab/pairnode.py): быстрый уровень (LAB_FAST=1)
сжимает кворум и «здоров» настройками узлов поверх conf/reserve.yaml, полный —
умолчания продукта. Обе связки обязаны проходить `Timing.check` — ту же
проверку, что у сервера при чтении reserve.yaml.

Цена ошибки: правка LAB_FAST_DIGITS или умолчаний reserve.yaml, разорвавшая
связку (окно «да» короче 1,4·P + 15, up короче W + down + запас), видна только
на ВМ: `pairnode digits` выходит с SystemExit, и каждый сценарий стенда падает
уже на `eval` цифр — целый прогон стенда впустую вместо одного красного теста.
"""
from __future__ import annotations

import pytest

from tests.lab import pairnode

pytestmark = pytest.mark.unit


def test_the_fast_lab_digits_pass_the_server_timing_check(monkeypatch):
    monkeypatch.setenv("LAB_FAST", "1")
    t, healthy = pairnode.lab_timing()
    q = pairnode.LAB_FAST_DIGITS["quorum"]
    # условие: цифры быстрого уровня действительно легли поверх reserve.yaml — иначе
    # проверялись бы умолчания продукта, и тест был бы зелёным при любой правке
    assert (t.probe_s, t.yes_window_s, t.up_s) == (q["probe_seconds"], q["yes_window_seconds"], q["up_seconds"]), \
        f"быстрые цифры не наложены: P {t.probe_s}, W {t.yes_window_s}, up {t.up_s}"
    assert healthy == pairnode.LAB_FAST_DIGITS["healthy_seconds"], healthy
    assert t.check() == "", f"быстрый уровень стенда не проходит Timing.check: {t.check()}"


def test_the_full_lab_level_is_the_product_defaults_and_passes_the_check(monkeypatch):
    monkeypatch.delenv("LAB_FAST", raising=False)
    t, _healthy = pairnode.lab_timing()
    assert t.probe_s != pairnode.LAB_FAST_DIGITS["quorum"]["probe_seconds"] or \
        t.up_s != pairnode.LAB_FAST_DIGITS["quorum"]["up_seconds"], "без LAB_FAST наложены цифры быстрого уровня"
    assert t.check() == "", f"умолчания reserve.yaml не проходят Timing.check: {t.check()}"
