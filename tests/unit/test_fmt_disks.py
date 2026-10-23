"""Unit: строки носителей для панелей — `disk_pair` и `disk_lines`
(awgbot/bot/texts/fmt.py).

ГБ — целыми, ТБ (от 1024 ГБ) — два знака через запятую; один носитель —
«диск 10/200 ГБ», несколько — по два в строку с номерами. Ошибка здесь —
админ читает «1720 ГБ» рядом с «1,68 ТБ» и не понимает, одного ли это
порядка числа, или строка панели не влезает в ширину экрана телефона.
"""

import pytest

from awgbot.bot.texts.fmt import disk_lines, disk_pair

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("used, total, want", [
    (10.0, 200.0, "10/200 ГБ"),             # оба в ГБ — единица один раз, в конце
    (10.5, 200.0, "10/200 ГБ"),             # 10,5 → «10»: банковское округление Python, дробей ГБ нет
    (0.4, 931.5, "0/932 ГБ"),               # почти пустой — «0», не «0,4»
    (100.0, 1720.3, "100 ГБ/1,68 ТБ"),      # всего в ТБ, занято ещё в ГБ — единицы у каждого
    (1228.8, 1720.3, "1,20/1,68 ТБ"),       # оба в ТБ — единица один раз
    (1024.0, 1024.0, "1,00/1,00 ТБ"),       # граница: ровно 1024 ГБ — уже ТБ
    (1023.4, 2048.0, "1023 ГБ/2,00 ТБ"),    # чуть ниже границы — ещё ГБ
    (0.0, 0.0, "0/0 ГБ"),                   # нули не ломают формат
])
def test_disk_pair_shows_gb_whole_and_tb_with_two_decimals(used, total, want):
    assert disk_pair(used, total) == want


def test_single_disk_is_one_line_without_a_number():
    assert disk_lines([[10.4, 200.0, "/"]]) == ["диск 10/200 ГБ"]


def test_several_disks_go_two_per_line_and_are_numbered():
    """Три носителя — две строки: «1 · 2» и «3». Номера нужны, потому что
    точки монтирования на панели не показываем (/srv/dev-disk-by-uuid-… не
    влезает), а различать носители по порядку человеку надо."""
    disks = [[10.0, 200.0, "/"], [100.0, 1720.3, "/srv/a"], [1228.8, 1720.3, "/srv/b"]]
    assert disk_lines(disks) == [
        "диск 1 - 10/200 ГБ · диск 2 - 100 ГБ/1,68 ТБ",
        "диск 3 - 1,20/1,68 ТБ",
    ]


def test_four_disks_fill_exactly_two_lines():
    disks = [[1, 2, "/"], [3, 4, "/a"], [5, 6, "/b"], [7, 8, "/c"]]
    assert disk_lines(disks) == ["диск 1 - 1/2 ГБ · диск 2 - 3/4 ГБ", "диск 3 - 5/6 ГБ · диск 4 - 7/8 ГБ"]


@pytest.mark.parametrize("disks", [None, [], [[None, None, "/"]], [{"used_gb": None, "total_gb": 5}]])
def test_no_usable_disks_means_no_lines(disks):
    """Старый снимок без списка или элементы без чисел — строк нет вовсе:
    панель не рисует «диск None/None»."""
    assert disk_lines(disks) == []


def test_dict_elements_are_read_like_lists():
    """`read_disks()` отдаёт словари, снимок в БД и канал — списки; обе формы
    дают одну и ту же строку."""
    as_dicts = [{"mount": "/", "used_gb": 10.4, "total_gb": 200.0},
                {"mount": "/srv", "used_gb": 1105.2, "total_gb": 1720.3}]
    as_lists = [[10.4, 200.0, "/"], [1105.2, 1720.3, "/srv"]]
    assert disk_lines(as_dicts) == disk_lines(as_lists) == ["диск 1 - 10/200 ГБ · диск 2 - 1,08/1,68 ТБ"]


def test_elements_without_numbers_are_skipped_but_the_rest_is_numbered_in_order():
    """Один носитель без чисел посреди списка пропускается, нумерация идёт по
    оставшимся: «диск 1 · диск 2», а не «диск 1 · диск 3»."""
    disks = [[10.0, 200.0, "/"], [None, None, "/x"], [5.0, 50.0, "/y"]]
    assert disk_lines(disks) == ["диск 1 - 10/200 ГБ · диск 2 - 5/50 ГБ"]
