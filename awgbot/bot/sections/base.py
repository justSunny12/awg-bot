"""Общие типы разделов: контекст действия и описание подтверждения —
отдельным модулем, чтобы модули разделов не зависели от пакета."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery


@dataclass
class Ctx:
    """Контекст действия раздела: нажатие, сервисы, диалог, роль, раздел, ключ, значение."""
    cb: CallbackQuery
    services: Any
    state: FSMContext | None
    br: Any
    sec: str
    key: str
    val: str

    @property
    def hooks(self):
        from . import hooks_for
        return hooks_for(self.br)


@dataclass(frozen=True)
class Confirm:
    """Действие с подтверждением: диспетчер показывает вопрос с ценой и
    кнопками «⬅️ Отмена» (назад в раздел) и label; исполняет run по ключу с «!»."""
    question: Callable[[Ctx], Awaitable[str]]
    label: str
    run: Callable[[Ctx], Awaitable[str | None]]
    danger: bool = True
