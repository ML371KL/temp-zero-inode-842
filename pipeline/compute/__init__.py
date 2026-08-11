"""Вычислительные слои «MOEX Radar» (docs/CONTRACT.md §4).

panel  — дневная панель сигналов из стора (лаги доступности, один календарь);
core   — слой 1, месячный композит z-скоров (ядро);
states — слой 2, машина состояний и сигналы второго ряда;
health — скользящий IC ядра: работает модель или уже нет.

monitors.py сюда намеренно НЕ импортируется: тайлы мониторинга живут своей жизнью
и их падение не должно ронять расчётный слой.
"""

from .panel import build_panel, PanelError
from .core import compute_core, monthly_frame
from .states import compute_states
from .health import compute_health

__all__ = ["build_panel", "PanelError", "compute_core", "monthly_frame",
           "compute_states", "compute_health"]
