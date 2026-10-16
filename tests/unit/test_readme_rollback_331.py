"""Рецепт отката агента 3.3.1 → 3.3.0 в README: порядок шагов. Агент 3.3.0
прогоняет обвязку на первом же такте — юнит и файлы доводятся до установки
3.3.0 при остановленном агенте, таблица awg_lan снимается после, dnsmasq
перезапускается (строки nftset= читает только при старте). Снимается
вместе с разделом со следующим сдвигом минимума."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _recipe() -> str:
    text = (ROOT / "docs" / "README-bot.md").read_text(encoding="utf-8")
    sec = text.split("**Откат 3.3.1 → 3.3.0.**", 1)[1]
    return sec.split("```sh\n", 1)[1].split("```", 1)[0]


def test_the_unit_and_files_are_fixed_before_the_330_agent_starts():
    r = _recipe()
    assert r.index("awg-bot stop") < r.index("inet#awg_home#") < r.index("awg-bot update")
    assert r.index("awg-bot stop") < r.index("HOME_SUBNETS=") < r.index("awg-bot update")
    assert r.index("rm -f /etc/awg-gw/lan.nft") < r.index("awg-bot update")
    assert r.index("awg-bot update") < r.index("nft delete table inet awg_lan")


def test_dnsmasq_and_the_plumbing_are_restarted_last():
    r = _recipe().rstrip().splitlines()
    assert r[-1].strip() == "sudo systemctl restart awg-link-gw dnsmasq", r[-1]


def test_the_roadmap_no_longer_promises_the_first_run_file_fixes_the_rollback():
    road = (ROOT / "docs" / "ROADMAP.md").read_text(encoding="utf-8")
    assert "только вместе с\n  повторным применением файла первого применения" not in road
