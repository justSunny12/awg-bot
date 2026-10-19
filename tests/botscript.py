"""awg-bot.sh одним текстом: части из install/lib (их awg-bot.sh читает при
разборе строками `. "$SELF_DIR/install/lib/<имя>.sh"`) склеены на месте этих
строк. Тесты берут функции по тексту и гоняют копии скрипта во временных
каталогах — без каталога lib рядом; склейка повторяет то, что видит bash."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "awg-bot.sh"
LIB_DIR = ROOT / "install" / "lib"

_SOURCE = re.compile(r'^\. "\$SELF_DIR/install/lib/([a-z_]+\.sh)"\n', re.M)


def flat() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    out, n = _SOURCE.subn(lambda m: (LIB_DIR / m.group(1)).read_text(encoding="utf-8"), text)
    libs = sorted(p.name for p in LIB_DIR.glob("*.sh"))
    assert n == len(libs), f"awg-bot.sh читает не все части install/lib: {libs}"
    return out
