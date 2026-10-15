"""pick.py — выбор с отложенным применением: отметки экрана живут в FSM-данных,
тумблеры меняют только их, применяет одна кнопка «✅ Применить».

Так устроены «👥 Кому доступен» (settings/sections.py) и РФ-доступ по
устройствам (routing.py): тумблер на экране ничего не включает и никому не
пишет, пока человек не нажал «Применить»; «⬅️ Назад» уводит без сохранения.

Отметки хранятся под одним ключом вместе с областью (scope — «rf:<профиль>»,
«rt_users»): отметки чужого экрана не считаются — кнопка из старого сообщения
другого профиля стартует с сохранённого, а «Применить» без отметок своей
области — устаревшая кнопка. Ключи — строками: хранилище FSM может быть JSON.
"""

from __future__ import annotations

KEY = "pick"


async def load(state, scope: str, saved: dict) -> dict:
    """Отметки экрана scope: из FSM, если они для него, иначе сохранённые.
    Объекты, появившиеся после отметок, — по сохранённому; исчезнувшие —
    не считаются."""
    marks = await pending(state, scope)
    if marks is None:
        return dict(saved)
    return {k: marks.get(k, v) for k, v in saved.items()}


async def pending(state, scope: str):
    """Отметки своей области из FSM или None — их нет или они чужие."""
    if state is None:
        return None
    pick = (await state.get_data()).get(KEY) or {}
    if pick.get("scope") != scope:
        return None
    return {int(k): bool(v) for k, v in (pick.get("marks") or {}).items()}


async def store(state, scope: str, marks: dict) -> None:
    if state is None:                 # вызов без FSM (старые тесты, служебные пути): отметки не держим
        return
    await state.update_data(**{KEY: {"scope": scope, "marks": {str(k): bool(v) for k, v in marks.items()}}})


async def drop(state) -> None:
    """Отметки отслужили (применены или экран ушёл)."""
    if state is not None:
        await state.update_data(**{KEY: None})


def changed(marks: dict, saved: dict) -> dict:
    """Что применить: {id: новое состояние} только там, где отметка отличается."""
    return {k: v for k, v in marks.items() if k in saved and bool(saved[k]) != bool(v)}


def toggle_all(marks: dict) -> dict:
    """«Все …»: ✅ (все отмечены) — снять все, ☑️ — отметить все."""
    on = not (marks and all(marks.values()))
    return {k: on for k in marks}


__all__ = ["KEY", "load", "pending", "store", "drop", "changed", "toggle_all"]
