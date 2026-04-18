"""Генерация целей для стройки (см. docs/strategy.md §11)."""

from __future__ import annotations

from dataclasses import dataclass

from api import GameState, Plantation

from . import config as cfg
from .geometry import (
    Coord,
    chebyshev,
    in_bounds,
    is_boosted,
)


@dataclass(frozen=True)
class BuildTarget:
    position: Coord
    priority: int   # меньше = важнее (0 щит, 1 ×7, 2 мост, 3 любая)
    reason: str


def compute_reachable_cells(state: GameState) -> set[Coord]:
    """Клетки в AR от любой нашей не-изолированной плантации."""
    ar = state.action_range
    w, h = state.map_size
    result: set[Coord] = set()
    for p in state.plantations:
        if p.is_isolated:
            continue
        x, y = p.position
        for dx in range(-ar, ar + 1):
            for dy in range(-ar, ar + 1):
                pos = (x + dx, y + dy)
                if 0 <= pos[0] < w and 0 <= pos[1] < h and pos not in state.mountains:
                    result.add(pos)
    return result


def _blocked_positions(state: GameState) -> set[Coord]:
    return (
        set(state.mountains)
        | {p.position for p in state.plantations}
        | {c.position for c in state.constructions}
        | {e.position for e in state.enemy_plantations}
    )


def generate_build_targets(state: GameState, cu: Plantation) -> list[BuildTarget]:
    blocked = _blocked_positions(state)
    reachable = compute_reachable_cells(state)
    targets: list[BuildTarget] = []

    # Приоритет 1: ×7 клетки в радиусе [MIN..MAX] от ЦУ.
    for cell in reachable:
        if cell in blocked:
            continue
        if not is_boosted(cell):
            continue
        d = chebyshev(cell, cu.position)
        if cfg.X7_SEARCH_RADIUS_MIN <= d <= cfg.X7_SEARCH_RADIUS_MAX:
            targets.append(BuildTarget(cell, priority=1, reason=f"x7_r{d}"))

    # Приоритет 2: мост в сторону ближайшей ×7, которая пока недостижима.
    bridge = _find_bridge_to_nearest_x7(state, cu, blocked, reachable)
    if bridge is not None:
        targets.append(BuildTarget(bridge, priority=2, reason="bridge"))

    # Приоритет 3: любая достижимая клетка в разумном радиусе.
    if cfg.FALLBACK_ANY_CELL and not any(t.priority == 1 for t in targets):
        for cell in reachable:
            if cell in blocked:
                continue
            d = chebyshev(cell, cu.position)
            if d == 0 or d > cfg.X7_SEARCH_RADIUS_MAX:
                continue
            targets.append(BuildTarget(cell, priority=3, reason=f"fill_r{d}"))

    targets.sort(
        key=lambda t: (
            t.priority,
            0 if is_boosted(t.position) else 1,
            chebyshev(t.position, cu.position),
        )
    )
    return targets


def _find_bridge_to_nearest_x7(
    state: GameState,
    cu: Plantation,
    blocked: set[Coord],
    reachable: set[Coord],
) -> Coord | None:
    """Ищем ×7 клетку вне reachable в радиусе VR*3 и тянем к ней мост."""
    w, h = state.map_size
    best_x7: Coord | None = None
    best_dist = 10_000
    radius = cfg.X7_SEARCH_RADIUS_MAX * 3
    cx, cy = cu.position
    for dx in range(-radius, radius + 1, cfg.BOOSTED_CELL_MODULO):
        for dy in range(-radius, radius + 1, cfg.BOOSTED_CELL_MODULO):
            pos = (cx + dx, cy + dy)
            if not (0 <= pos[0] < w and 0 <= pos[1] < h):
                continue
            if not is_boosted(pos):
                continue
            if pos in reachable or pos in blocked:
                continue
            d = chebyshev(pos, cu.position)
            if d < best_dist:
                best_dist = d
                best_x7 = pos

    if best_x7 is None:
        return None

    candidates = [
        c for c in reachable
        if c not in blocked and chebyshev(c, best_x7) < best_dist
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: chebyshev(c, best_x7))
