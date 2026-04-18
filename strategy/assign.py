"""Назначение команд плантациям (см. docs/strategy.md §12)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from api import Command, GameState, Plantation

from . import config as cfg
from .geometry import Coord, chebyshev

ActionKind = Literal["build", "repair"]


@dataclass
class AssignTarget:
    position: Coord
    kind: ActionKind
    priority: int = 5


@dataclass
class AssignContext:
    exit_usage: dict[Coord, int] = field(default_factory=dict)
    used_authors: set[str] = field(default_factory=set)


def compute_signal_range(state: GameState) -> int:
    pu = state.plantation_upgrades
    level = 0
    if pu is not None:
        for t in pu.tiers:
            if t.name == "signal_range":
                level = t.current
                break
    return cfg.DEFAULT_SR + level


def can_reach(
    author: Plantation,
    exit_point: Coord,
    target: Coord,
    state: GameState,
    sr: int,
) -> bool:
    if chebyshev(author.position, exit_point) > sr:
        return False
    if chebyshev(exit_point, target) > state.action_range:
        return False
    return True


def _effective_speed(base: int, usage: int) -> int:
    return max(0, base - usage)


def find_best_assignment(
    target: Coord,
    candidates: list[Plantation],
    all_plants: list[Plantation],
    state: GameState,
    ctx: AssignContext,
    base_speed: int,
    sr: int,
) -> Optional[tuple[Plantation, Coord]]:
    best: Optional[tuple[Plantation, Coord]] = None
    best_score: tuple[int, int] = (10**9, 10**9)

    for author in candidates:
        if author.id in ctx.used_authors:
            continue
        if author.is_isolated:
            continue

        # Автор может использовать себя или любую не-изолированную плантацию как exit.
        for exit_plant in all_plants:
            if exit_plant.is_isolated:
                continue
            exit_pos = exit_plant.position
            if not can_reach(author, exit_pos, target, state, sr):
                continue
            usage = ctx.exit_usage.get(exit_pos, 0)
            if _effective_speed(base_speed, usage) <= 0:
                continue
            # Предпочитаем: меньше usage у exit, короче путь author→target.
            score = (usage, chebyshev(author.position, target))
            if score < best_score:
                best_score = score
                best = (author, exit_pos)

    return best


def assign_commands(
    state: GameState,
    free_plantations: list[Plantation],
    targets: list[AssignTarget],
    cmd: Command,
) -> int:
    """Пишет команды в Command builder. Возвращает число назначенных.
    Цели сортируются по приоритету и распределяются жадно.
    """
    ctx = AssignContext()
    sr = compute_signal_range(state)
    count = 0

    for t in sorted(targets, key=lambda x: x.priority):
        base_speed = cfg.DEFAULT_CS if t.kind == "build" else cfg.DEFAULT_RS
        assignment = find_best_assignment(
            t.position,
            free_plantations,
            state.plantations,
            state,
            ctx,
            base_speed,
            sr,
        )
        if assignment is None:
            continue
        author, exit_point = assignment
        cmd.add_action(author.position, exit_point, t.position)
        ctx.used_authors.add(author.id)
        ctx.exit_usage[exit_point] = ctx.exit_usage.get(exit_point, 0) + 1
        count += 1

    return count
