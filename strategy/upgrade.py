"""Выбор апгрейда (см. docs/strategy.md §9)."""

from __future__ import annotations

from typing import Optional

from api import GameState

from . import config as cfg


def choose_upgrade(state: GameState) -> Optional[str]:
    pu = state.plantation_upgrades
    if pu is None or pu.points <= 0:
        return None

    current: dict[str, int] = {t.name: t.current for t in pu.tiers}
    caps: dict[str, int] = {t.name: t.max for t in pu.tiers}

    for name, target in cfg.UPGRADE_PRIORITY:
        cur = current.get(name, 0)
        cap = caps.get(name, target)
        if cur < min(target, cap):
            return name

    return None
