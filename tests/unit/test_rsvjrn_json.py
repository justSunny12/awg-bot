"""Проверка JSON-функций SQLite для журнала данных резерва (util/rsvjrn.json_ok).

Триггеры захвата собирают событие через `json_object`: на сборке SQLite без
JSON они падали бы на каждой записи решения. Цена ошибки: проверка, которая
ответит «есть» без функций, — направление объявит журнал, и бот перестанет
сохранять профили; проверка, которая падает сама, — старт бота валится на
хосте со старой библиотекой вместо того, чтобы идти прежним путём.
"""
from __future__ import annotations

import sqlite3

import pytest

from awgbot.util import rsvjrn

pytestmark = pytest.mark.unit


class _NoJson:
    """Соединение SQLite без JSON-функций: любой вызов json_* — «нет такой функции»."""

    def execute(self, sql, *args):
        if "json_" in sql:
            raise sqlite3.OperationalError("no such function: json_object")
        raise AssertionError(f"неожиданный запрос: {sql}")


class _OddJson:
    """Функция есть, но отвечает не по стандарту (иной формат вывода)."""

    def execute(self, sql, *args):
        class _R:
            def fetchone(self):
                return ('{"a": 1}',)
        return _R()


def test_a_real_sqlite_has_json():
    conn = sqlite3.connect(":memory:")
    try:
        assert rsvjrn.json_ok(conn) is True, "в сборке Python нет JSON-функций SQLite — журнал не заработает"
    finally:
        conn.close()


def test_a_sqlite_without_json_is_reported_not_raised():
    assert rsvjrn.json_ok(_NoJson()) is False


def test_a_json_function_with_another_output_is_not_trusted():
    assert rsvjrn.json_ok(_OddJson()) is False
