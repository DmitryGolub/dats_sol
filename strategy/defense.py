"""Планирование защиты ЦУ (см. docs/strategy.md §7-8)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from api import GameState, Plantation

from . import config as cfg
from .diagnose import Diagnosis, UrgencyLevel
from .geometry import Coord, chebyshev, in_bounds, ortho_neighbors


@dataclass
class DefensePlan:
    cu_repairers: list[Plantation] = field(default_factory=list)
    relocate_cu_to: Optional[Coord] = None
    old_cu_repairers: list[Plantation] = field(default_factory=list)
    reserved: set[str] = field(default_factory=set)


def plan_defense(state: GameState, diag: Diagnosis) -> DefensePlan:
    plan = DefensePlan()

    if diag.cu is None or diag.urgency == UrgencyLevel.NORMAL:
        return plan

    shields = diag.shields
    non_cu = [
        p for p in state.plantations
        if not p.is_main and not p.is_isolated
    ]

    if diag.urgency == UrgencyLevel.EMERGENCY:
        if shields:
            best_shield = max(shields, key=lambda s: s.hp)
            plan.relocate_cu_to = best_shield.position
            # Остальные плантации чинят старое место ЦУ после переноса.
            others = [p for p in non_cu if p.id != best_shield.id]
            others.sort(key=lambda p: chebyshev(p.position, diag.cu.position))
            plan.old_cu_repairers = others[:4]
            plan.reserved = {best_shield.id} | {p.id for p in plan.old_cu_repairers}
            return plan
        # Нет щитов — все свободные чинят ЦУ.
        non_cu.sort(key=lambda p: chebyshev(p.position, diag.cu.position))
        plan.cu_repairers = non_cu[:6]
        plan.reserved = {p.id for p in plan.cu_repairers}
        return plan

    # Сортировка щитов по hp возрастанию — чинят самые здоровые (больше HP → больше эффективность ремонта).
    shields_sorted = sorted(shields, key=lambda s: -s.hp)

    if diag.urgency == UrgencyLevel.HEAVY_REPAIR:
        plan.cu_repairers = shields_sorted[:4]
    elif diag.urgency == UrgencyLevel.LIGHT_REPAIR:
        plan.cu_repairers = shields_sorted[:2]

    plan.reserved = {p.id for p in plan.cu_repairers}
    return plan


def missing_shield_positions(state: GameState, cu: Plantation) -> list[Coord]:
    """Куда бы поставить щит, чтобы получить 2 щита с противоположных сторон."""
    occupied = (
        {p.position for p in state.plantations}
        | {c.position for c in state.constructions}
    )
    x, y = cu.position
    left, right = (x - 1, y), (x + 1, y)
    up, down = (x, y - 1), (x, y + 1)

    def buildable(pos: Coord) -> bool:
        return (
            in_bounds(pos, state.map_size)
            and pos not in state.mountains
            and pos not in occupied
        )

    # Приоритет: замкнуть ось, где уже есть щит.
    if left in occupied and buildable(right):
        return [right]
    if right in occupied and buildable(left):
        return [left]
    if up in occupied and buildable(down):
        return [down]
    if down in occupied and buildable(up):
        return [up]

    candidates = [p for p in (left, right, up, down) if buildable(p)]
    return candidates[:2]
