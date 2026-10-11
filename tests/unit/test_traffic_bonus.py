"""Unit: бонус трафика при исчерпании (domain/services/traffic/bonus.py) —
разбор ввода «N | N% | N% M | 0», расчёт от месячного лимита профиля,
подписи экрана, кнопки и уведомления админу.

Цена ошибки: разбор, принявший «10» за проценты или «10% 0» за потолок,
молча раздаёт не тот объём всем профилям сразу; процент без потолка в
половину лимита превращает «100%» в удвоение квоты; округление вниз вместо
вверх даёт профилю с лимитом 3 ГБ бонус 0 ГБ — и блок сразу, хотя бонус
включён; «0,5 ГБ» с точкой или хвостом нулей — кривой текст человеку.
"""
import pytest

from awgbot.domain.services.traffic import bonus
from awgbot.domain.services.traffic.bonus import BonusSpec
from awgbot.domain.services.types import BYTES_PER_GB

pytestmark = pytest.mark.unit

GB = BYTES_PER_GB


# ── parse: форматы ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, spec", [
    ("50", BonusSpec("gb", gb=50)),
    ("1", BonusSpec("gb", gb=1)),
    ("100000", BonusSpec("gb", gb=100000)),
    ("0", BonusSpec("off")),
    ("10%", BonusSpec("pct", pct=10)),
    ("1%", BonusSpec("pct", pct=1)),
    ("100%", BonusSpec("pct", pct=100)),
    ("10% 50", BonusSpec("pct_cap", pct=10, cap=50)),
    ("10% 1", BonusSpec("pct_cap", pct=10, cap=1)),
    ("100% 100000", BonusSpec("pct_cap", pct=100, cap=100000)),
    # пробелы по краям и между частями — так набирают на телефоне
    ("  50  ", BonusSpec("gb", gb=50)),
    (" 10 % ", BonusSpec("pct", pct=10)),
    ("10%   50", BonusSpec("pct_cap", pct=10, cap=50)),
    ("\t10% 50\n", BonusSpec("pct_cap", pct=10, cap=50)),
    ("007", BonusSpec("gb", gb=7)),
    # значения conf приходят числом, не строкой: прежнее «100» — 100 ГБ
    (100, BonusSpec("gb", gb=100)),
    (0, BonusSpec("off")),
])
def test_parse_accepts_every_documented_format(raw, spec):
    """Три формата и «0» — ровно те, что обещает приглашение к вводу."""
    assert bonus.parse(raw) == spec, f"{raw!r} разобран не так"


@pytest.mark.parametrize("raw", [
    "", " ", None, "abc", "10 ГБ", "-5", "5.5", "5,5", "1e3",
    "100001",              # за верхней границей ГБ
    "0%", "101%", "1000%",  # процент 1–100
    "10% 0", "10% 100001",  # потолок 1–100000
    "50 10",               # потолок без процента — непонятно, что имелось в виду
    "10%%", "%10", "10% 50 60", "10% -5", "10% abc", "10% 5.5",
    "1234567",             # длинное число не должно проходить
])
def test_parse_rejects_garbage_and_out_of_range(raw):
    """Мусор и выход за границы — ValueError: обработчик переспросит, а не
    запишет в conf то, что потом прочтётся умолчанием."""
    with pytest.raises(ValueError):
        bonus.parse(raw)


@pytest.mark.parametrize("spec", [
    BonusSpec("off"), BonusSpec("gb", gb=50), BonusSpec("pct", pct=10),
    BonusSpec("pct_cap", pct=10, cap=50),
])
def test_stored_value_parses_back_to_the_same_rule(spec):
    """Что записано в conf, то и прочтётся следующим тиком: иначе правка в
    боте живёт до перезапуска кэша, а потом превращается в 100 ГБ."""
    assert bonus.parse(spec.stored()) == spec


def test_stored_keeps_numbers_for_gb_and_off():
    """ГБ и выкл. — числом, как было в conf до процентов (старые чтения
    get_int не ломаются); проценты — строкой."""
    assert bonus.BonusSpec("gb", gb=50).stored() == 50
    assert bonus.BonusSpec("off").stored() == 0
    assert bonus.BonusSpec("pct", pct=10).stored() == "10%"
    assert bonus.BonusSpec("pct_cap", pct=10, cap=50).stored() == "10% 50"


class _Conf:
    """Подставные настройки: только get, как читает bonus.current."""
    def __init__(self, **kv):
        self.kv = kv

    def get(self, key, default=None):
        return self.kv.get(key, default)


@pytest.mark.parametrize("value, spec", [
    (None, BonusSpec("gb", gb=100)),          # ключа нет — умолчание
    (100, BonusSpec("gb", gb=100)),
    (250, BonusSpec("gb", gb=250)),
    (50.0, BonusSpec("gb", gb=50)),            # YAML «50.0» — те же 50 ГБ, не умолчание
    (0.0, BonusSpec("off")),
    (0, BonusSpec("off")),
    ("10%", BonusSpec("pct", pct=10)),
    ("10% 50", BonusSpec("pct_cap", pct=10, cap=50)),
    ("мусор", BonusSpec("gb", gb=100)),        # испорчено руками — 100 ГБ, как прежде
    ("200%", BonusSpec("gb", gb=100)),
    (-1, BonusSpec("gb", gb=100)),
    (50.5, BonusSpec("gb", gb=100)),           # дробные ГБ правило не знает
])
def test_current_reads_conf_and_falls_back_to_100_gb(value, spec):
    """Испорченное значение не выключает бонус (это блок сразу у всех) и не
    роняет тик лимитов — читается умолчанием 100 ГБ."""
    conf = _Conf() if value is None else _Conf(**{bonus.SETTING: value})
    assert bonus.current(conf) == spec


# ── bytes_for ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, limit, expected", [
    # примеры правила
    ("10%", 1 * GB, GB // 2),          # 0,1 → 1 ГБ вверх, но не более половины — 0,5
    ("80%", 10 * GB, 5 * GB),          # 8 ГБ > половины 10 — 5
    ("20%", 3 * GB, 1 * GB),           # 0,6 → 1 ГБ (половина 1,5 не мешает)
    ("10%", 100 * GB, 10 * GB),
    ("10% 50", 1000 * GB, 50 * GB),    # 100 > потолка 50
    ("10% 50", 100 * GB, 10 * GB),     # 10 < потолка
    # края
    ("100%", 10 * GB, 5 * GB),         # «N%» никогда не больше половины лимита
    ("100% 100000", 10 * GB, 10 * GB), # с потолком половина не действует
    ("50%", 10 * GB, 5 * GB),          # ровно половина
    ("1%", 1 * GB, GB // 2),           # 0,01 → 1 → упор в половину
    ("10%", 1536 * 1024 ** 2, 768 * 1024 ** 2),  # лимит 1,5 ГБ: 0,15 → 1 → половина 0,75
    ("15%", 10 * GB, 2 * GB),          # 1,5 → 2 вверх, не вниз
    ("10%", 101 * GB, 11 * GB),        # 10,1 → 11
    ("10% 50", 495 * GB, 50 * GB),     # 49,5 → 50 = потолок
    ("10% 5", 51 * GB, 5 * GB),        # 6 вверх, упор в потолок
    ("50", 1 * GB, 50 * GB),           # ГБ не зависят от лимита
    ("100000", 10 * GB, 100000 * GB),
])
def test_bytes_for_follows_the_rule(raw, limit, expected):
    got = bonus.bytes_for(bonus.parse(raw), limit)
    assert got == expected, f"{raw} при лимите {limit / GB} ГБ: {got / GB} ГБ вместо {expected / GB}"
    assert isinstance(got, int), "байты — целые: идут в bonus_bytes БД"


@pytest.mark.parametrize("raw", ["0", "50", "10%", "10% 50"])
@pytest.mark.parametrize("limit", [0, -1])
def test_profiles_without_a_limit_get_no_bonus(raw, limit):
    """Профиль без лимита трафика бонуса не получает ни в одном формате."""
    assert bonus.bytes_for(bonus.parse(raw), limit) == 0


def test_off_gives_nothing_even_with_a_limit():
    assert bonus.bytes_for(BonusSpec("off"), 100 * GB) == 0


# ── gb_text ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("num, text", [
    (10 * GB, "10 ГБ"),
    (100 * GB, "100 ГБ"),
    (100000 * GB, "100000 ГБ"),
    (GB // 2, "0,5 ГБ"),
    (GB // 4, "0,25 ГБ"),
    (768 * 1024 ** 2, "0,75 ГБ"),
    (1536 * 1024 ** 2, "1,5 ГБ"),
    (GB // 3, "0,33 ГБ"),              # до сотых
    (0, "0 ГБ"),
])
def test_gb_text_uses_comma_and_drops_trailing_zeros(num, text):
    """«0,5 ГБ», а не «0.5 ГБ» или «0,50 ГБ»; целые — без дробной части."""
    assert bonus.gb_text(num) == text


# ── подписи ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, screen, button", [
    ("0", "выкл.", "выкл."),
    ("50", "50 ГБ", "50 ГБ"),
    ("10%", "10% лимита", "10%"),
    ("10% 50", "10% лимита, не более 50 ГБ", "10%, ≤ 50 ГБ"),
])
def test_screen_and_button_texts_per_kind(raw, screen, button):
    spec = bonus.parse(raw)
    assert bonus.screen_text(spec) == screen
    assert bonus.button_text(spec) == button


@pytest.mark.parametrize("raw, limit, note", [
    ("50", 100 * GB, ""),                                  # ГБ — без скобки
    ("0", 100 * GB, ""),
    ("10%", 100 * GB, "10% лимита"),
    ("10%", 1 * GB, "10% лимита, не более половины"),      # 0,1 → 1 вверх, упор в половину — 0,5
    ("80%", 10 * GB, "80% лимита, не более половины"),     # 8 > 5
    ("20%", 3 * GB, "20% лимита"),                         # 0,6 → 1 < половины 1,5
    ("50%", 100 * GB, "50% лимита"),                       # ровно половина — это и есть 50%
    ("10% 50", 1000 * GB, "10% лимита, не более 50 ГБ"),   # упёрлись в потолок
    ("10% 50", 100 * GB, "10% лимита"),                    # до потолка не дошли
    ("10% 50", 500 * GB, "10% лимита"),                    # ровно потолок — это и есть 10%
    ("10% 50", 495 * GB, "10% лимита"),                    # 49,5 → 50 округлением, не потолком
])
def test_admin_note_explains_where_the_number_came_from(raw, limit, note):
    """Скобка у админа: откуда число; «не более M ГБ» и «не более половины» —
    только когда потолок действительно срезал процент, иначе админ ищет
    несуществующий упор."""
    spec = bonus.parse(raw)
    assert bonus.admin_note(spec, bonus.bytes_for(spec, limit), limit) == note
