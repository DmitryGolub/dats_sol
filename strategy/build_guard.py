"""Защита от самосноса при стройке (см. docs/strategy.md §10)."""

from __future__ import annotations

from typing import Optional

from api import GameState, Plantation

from . import config as cfg
from .memory import BotMemory


def compute_settlement_limit(state: GameState) -> int:
    pu = state.plantation_upgrades
    level = 0
    if pu is not None:
        for t in pu.tiers:
            if t.name == "settlement_limit":
                level = t.current
                break
    return cfg.DEFAULT_SETTLEMENT_LIMIT + level


def find_main(state: GameState) -> Optional[Plantation]:
    for p in state.plantations:
        if p.is_main:
            return p
    return None


def can_build_safely(
    state: GameState,
    memory: BotMemory,
    shield_ids: set[str],
) -> tuple[bool, str]:
    """Разрешено ли строить сейчас без риска снести ЦУ или щит."""
    limit = compute_settlement_limit(state)
    total = len(state.plantations) + len(state.constructions)

    if total < limit - cfg.KEEP_SLOTS_FREE:
        return True, "ok_below_limit"

    oldest = memory.get_oldest_plantation(state)
    if oldest is None:
        return False, "unknown_oldest"

    cu = find_main(state)
    if cu is not None and oldest.id == cu.id:
        return False, "oldest_is_cu"

    if oldest.id in shield_ids:
        return False, "oldest_is_shield"

    return True, "ok_oldest_is_buffer"
