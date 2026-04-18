"""Диагностика угроз ЦУ (см. docs/strategy.md §6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

from api import GameState, MeteoEvent, Plantation

from . import config as cfg
from .geometry import Coord, chebyshev, ortho_neighbors


class UrgencyLevel(IntEnum):
    NORMAL = 0         # HP > 80%
    LIGHT_REPAIR = 1   # 50-80%
    HEAVY_REPAIR = 2   # 20-50%
    EMERGENCY = 3      # < 20% или ЦУ мертва


@dataclass
class Diagnosis:
    urgency: UrgencyLevel
    cu: Optional[Plantation]
    cu_hp_ratio: float
    cu_mhp: int
    shields: list[Plantation] = field(default_factory=list)
    incoming_earthquake: bool = False
    storm_near_cu: bool = False


def compute_cu_mhp(state: GameState) -> int:
    """Текущий MHP плантаций с учётом апгрейда max_hp."""
    pu = state.plantation_upgrades
    level = 0
    if pu is not None:
        for tier in pu.tiers:
            if tier.name == "max_hp":
                level = tier.current
                break
    return cfg.DEFAULT_MHP + level * 10


def find_main_plantation(state: GameState) -> Optional[Plantation]:
    for p in state.plantations:
        if p.is_main:
            return p
    return None


def find_shields(state: GameState, cu: Plantation) -> list[Plantation]:
    """Плантации ортогонально рядом с ЦУ, не изолированные."""
    by_pos = {p.position: p for p in state.plantations if not p.is_isolated}
    result: list[Plantation] = []
    for pos in ortho_neighbors(cu.position):
        p = by_pos.get(pos)
        if p is not None and not p.is_main:
            result.append(p)
    return result


def has_opposite_shields(shields: list[Plantation], cu: Plantation) -> bool:
    positions = {s.position for s in shields}
    x, y = cu.position
    horizontal = (x - 1, y) in positions and (x + 1, y) in positions
    vertical = (x, y - 1) in positions and (x, y + 1) in positions
    return horizontal or vertical


def has_imminent_earthquake(forecasts: list[MeteoEvent]) -> bool:
    for f in forecasts:
        if f.kind == "earthquake" and f.turns_until is not None and f.turns_until <= 1:
            return True
    return False


def is_storm_near(pos: Coord, forecasts: list[MeteoEvent]) -> bool:
    for f in forecasts:
        if f.kind != "sandstorm":
            continue
        if f.is_forming:
            continue
        if f.position is None:
            continue
        radius = f.radius or 3
        if chebyshev(pos, f.position) < radius + 10:
            return True
    return False


def diagnose(state: GameState) -> Diagnosis:
    cu = find_main_plantation(state)
    mhp = compute_cu_mhp(state)

    if cu is None:
        return Diagnosis(
            urgency=UrgencyLevel.EMERGENCY,
            cu=None,
            cu_hp_ratio=0.0,
            cu_mhp=mhp,
        )

    ratio = cu.hp / mhp if mhp > 0 else 0.0

    if ratio < cfg.CU_HP_THRESHOLD_EMERGENCY:
        urgency = UrgencyLevel.EMERGENCY
    elif ratio < cfg.CU_HP_THRESHOLD_ALL_REPAIR:
        urgency = UrgencyLevel.HEAVY_REPAIR
    elif ratio < cfg.CU_HP_THRESHOLD_SOME_REPAIR:
        urgency = UrgencyLevel.LIGHT_REPAIR
    else:
        urgency = UrgencyLevel.NORMAL

    shields = find_shields(state, cu)

    return Diagnosis(
        urgency=urgency,
        cu=cu,
        cu_hp_ratio=ratio,
        cu_mhp=mhp,
        shields=shields,
        incoming_earthquake=has_imminent_earthquake(state.meteo_forecasts),
        storm_near_cu=is_storm_near(cu.position, state.meteo_forecasts),
    )
