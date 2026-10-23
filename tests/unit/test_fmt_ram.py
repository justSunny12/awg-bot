"""Unit: строка памяти «занято/всего» для панелей — `ram_pair`
(awgbot/bot/texts/fmt.py).

Занято — МБ целыми до 1024, дальше ГБ с двумя значащими цифрами дроби без
хвостовых нулей; всего — МБ до 900, с 900 — ГБ целыми (планка круглая: у
платы «1 ГБ» ядро видит ~976 МБ). Ошибка здесь — админ читает «478 МБ/976 МБ»
и ищет, куда делись 48 МБ, или «1,50/8 ГБ» рядом с «1,5 ГБ» трафика.
"""

import pytest

from awgbot.bot.texts.fmt import ram_pair

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("used, total, want", [
    (478, 976, "478 МБ/1 ГБ"),        # плата на 1 ГБ: ядро видит 976 МБ — всего «1 ГБ»
    (1340, 7812, "1,31/8 ГБ"),        # оба в ГБ — единица один раз, занято с двумя значащими
    (1536, 8192, "1,5/8 ГБ"),         # хвостовой ноль не выводим
    (300, 512, "300 МБ/512 МБ"),      # маленький сервер: всего меньше 900 МБ — единицы у каждого
    (2100, 3800, "2,05/4 ГБ"),        # 3800 МБ — «4 ГБ», не «3,71»
    (0, 7812, "0 МБ/8 ГБ"),           # ничего не занято — «0 МБ», не пусто
])
def test_ram_pair_examples(used, total, want):
    assert ram_pair(used, total) == want


@pytest.mark.parametrize("total, want_total", [
    (899, "899 МБ"),                  # до 900 — МБ как есть
    (900, "1 ГБ"),                    # с 900 — ГБ целыми
    (1023, "1 ГБ"),
    (1024, "1 ГБ"),
    (1536, "2 ГБ"),                   # 1,5 ГБ планок не бывает — округляем до целого
])
def test_total_switches_to_whole_gigabytes_from_900_mb(total, want_total):
    assert ram_pair(100, total) == f"100 МБ/{want_total}"


@pytest.mark.parametrize("used, want_used", [
    (1023, "1023 МБ"),                # ниже границы — МБ целыми
    (1024, "1"),                      # ровно гигабайт — целое без дроби
    (1536, "1,5"),                    # хвостовой ноль долой
    (1341, "1,31"),                   # 1,3096 → две значащие
])
def test_used_switches_to_gigabytes_from_1024_mb(used, want_used):
    assert ram_pair(used, 7812) == f"{want_used}/8 ГБ"


def test_used_in_gigabytes_keeps_its_unit_when_total_is_in_megabytes():
    """Ветка «занято в ГБ, всего в МБ» — единицы у каждого, не «1,5/512 МБ»."""
    assert ram_pair(1536, 512) == "1,5 ГБ/512 МБ"
