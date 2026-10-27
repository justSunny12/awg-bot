"""Шаблон conf/app.yaml: параметры AmneziaWG — в разделе awg.

Бот читает только `awg:` с protocol_id/interface/conf_dir: свежая установка
с другим разделом получила бы параметры, которых бот не видит."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

TEMPLATE = Path(__file__).resolve().parents[2] / "conf" / "app.yaml"


def test_template_has_only_the_awg_section():
    tpl = TEMPLATE.read_text(encoding="utf-8")
    assert not re.search(r"^docker:", tpl, re.M), "в шаблоне остался раздел docker:"
    for key in ("runtime", "container", "app_container", "awg_dir"):
        assert not re.search(rf"^\s*{key}:", tpl, re.M), f"в шаблоне остался ключ {key}"
    awg = yaml.safe_load(tpl)["awg"]
    for key in ("protocol_id", "interface", "conf_dir"):
        assert key in awg, f"в разделе awg шаблона нет ключа {key}"
