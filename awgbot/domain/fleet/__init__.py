"""
fleet/ — набор обновлений (только основной бот): сервер AWG и шлюзы живут на
одном выпуске — выпуске сервера; одна кнопка обновляет всех (концепт «единые
обновления»).

Части: state — ход `fleet_target` и состояние шлюза `gwupd_<слот>`; plan —
выпуск набора, версии ролей в выпуске, цель кнопки, участники и план; order —
ход вперёд: старт, ступень сервера, очередь шлюзов, ответы, фиксация; rollback
— откат набора обратным ходу порядком, откат сервера сторожем, догон и
следование вне хода, повторы; restore — восстановление бэкапа = переход на
выпуск бэкапа: проверка выпуска и digest, строка плана, скачивание.

Шлюзу сервер называет только тег (`upd`); проверки и цепочку ступеней агент
делает сам (domain/gateway/upd.py). Ход атомарен (Р21): не обновился любой
доступный шлюз — назад весь набор; недоступный никого не держит и догоняет
по `hello`.
"""
from __future__ import annotations

from .state import FleetStateMixin
from .plan import FleetPlanMixin, role_target
from .order import FleetOrderMixin, FleetOut
from .rollback import FleetRollbackMixin
from .restore import FleetRestoreMixin


class FleetMixin(FleetStateMixin, FleetPlanMixin, FleetOrderMixin, FleetRollbackMixin, FleetRestoreMixin):
    """Набор обновлений — подмешивается в Services основного бота."""


__all__ = ["FleetMixin", "FleetOut", "role_target"]
