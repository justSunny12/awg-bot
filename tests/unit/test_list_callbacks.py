"""
Колбэки экрана «весь список»: ListCB (bot/callbacks) и листание его страниц
PageCB с back=ListCB — упаковка в 64 байта Telegram и разбор обратно.

Цена ошибки: упаковка длиннее 64 байт — aiogram роняет построение всей
клавиатуры (ValueError), и экран списка с листанием перестаёт открываться у
всех с длинным списком; префикс, совпавший с чужим колбэком, отдаёт нажатие
«⬅️ Назад» или «След. страница ▶️» не тому обработчику.
"""
from __future__ import annotations

import inspect

import pytest
from aiogram.filters.callback_data import CallbackData

from awgbot.bot import callbacks, lists
from awgbot.bot.callbacks import ListCB, PageCB

pytestmark = pytest.mark.unit

_LONGEST_KIND = "abcdefghij"          # вид — до 10 букв (грамматика ссылок)
_KEY = "k3x9a2bq7d"                    # ключ снимка — 10 знаков


def _size(cb) -> int:
    return len(cb.pack().encode())


@pytest.mark.parametrize("kind", [_LONGEST_KIND, "snap", "gwnets", "sshunres"])
def test_the_longest_list_callback_fits_64_bytes_and_unpacks_back(kind):
    cb = ListCB(kind=kind, ref=10 ** 6, key=_KEY, new=1)
    assert _size(cb) <= 64, cb.pack()
    assert ListCB.unpack(cb.pack()) == cb


def test_a_page_of_the_longest_list_fits_64_bytes_and_carries_its_list_back():
    """Кнопка листания несёт колбэк экрана целиком: после нажатия диспетчер
    отдаёт его заново — тот же вид, слот, ключ и метка «из уведомления»."""
    back = ListCB(kind=_LONGEST_KIND, ref=10 ** 6, key=_KEY, new=1)
    page = PageCB(screen=lists.screen_name(_LONGEST_KIND), ref=10 ** 6, page=99, back=back.pack())
    assert _size(page) <= 64, page.pack()
    got = PageCB.unpack(page.pack())
    assert ListCB.unpack(got.back) == back, got.back


def test_the_documented_widest_snapshot_page_fits():
    """Самый длинный реальный случай описания: снимок с ключом и меткой."""
    back = ListCB(kind="snap", ref=0, key=_KEY, new=1).pack()
    assert back == "ls:snap:0:k3x9a2bq7d:1", back
    page = PageCB(screen="ls_snap", ref=0, page=1, back=back).pack()
    assert page == "pg|ls_snap|0|1|ls:snap:0:k3x9a2bq7d:1" and len(page.encode()) <= 64, page


def test_the_list_prefix_is_not_taken_by_another_callback():
    """Префикс «ls» — только у ListCB; совпадение отдало бы нажатие чужому фильтру."""
    owners = [name for name, obj in inspect.getmembers(callbacks, inspect.isclass)
              if issubclass(obj, CallbackData) and obj is not CallbackData
              and obj.__prefix__ == ListCB.__prefix__]
    assert owners == ["ListCB"], owners


def test_list_callback_defaults_pack_short():
    """Вид без слота и снимка — самый частый: «ls:peernets:0::0»."""
    assert ListCB(kind="peernets").pack() == "ls:peernets:0::0"
