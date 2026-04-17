from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from api.models import (
    Command,
    EnemyPlantation,
    GameState,
    Plantation,
    Position,
)
from strategy.base import BaseStrategy

log = logging.getLogger("bot.current")

UPGRADE_PRIORITY = [
    "settlement_limit",
    "signal_range",
    "decay_mitigation",
    "max_hp",
    "repair_power",
    "vision_range",
    "earthquake_mitigation",
]

DEFAULT_AR = 2
BE = 5
SE = 5
LODGE_REGEN = 5
TS = 5


def _adjacent(pos: Position) -> list[Position]:
    x, y = pos
    return [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]


def _cheb(a: Position, b: Position) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def _is_reinforced(pos: Position) -> bool:
    return pos[0] % 7 == 0 and pos[1] % 7 == 0


@dataclass
class Context:
    hq: Plantation
    max_hp: int
    signal_range: int
    own_positions: set[Position]
    plant_by_pos: dict[Position, Plantation]
    construction_positions: set[Position]
    enemy_by_pos: dict[Position, EnemyPlantation]
    lodges: list = field(default_factory=list)
    storm_danger: set[Position] = field(default_factory=set)
    terra_by_pos: dict[Position, int] = field(default_factory=dict)
    turns_to_die: dict[Position, int] = field(default_factory=dict)


class CurrentBot(BaseStrategy):
    name = "current"

    def __init__(self) -> None:
        self._mountains: set[Position] = set()
        self._map_size: tuple[int, int] = (0, 0)
        self._initialized = False

    def reset(self) -> None:
        self._initialized = False

    def decide(self, state: GameState) -> Command:
        cmd = Command()
        if not state.plantations:
            return cmd
        if not self._initialized:
            self._init(state)

        ctx = self._build_context(state)
        self._apply_upgrade(state, cmd, ctx)

        assigned: set[Position] = set()
        exit_usage: dict[Position, int] = defaultdict(int)

        self._proactive_relocate_hq(state, cmd, ctx)
        self._assign_repairs(state, cmd, assigned, exit_usage, ctx)
        self._assign_builds(state, cmd, assigned, exit_usage, ctx)

        return cmd

    def _init(self, state: GameState) -> None:
        self._mountains = set(state.mountains)
        self._map_size = state.map_size
        self._initialized = True

    def _build_context(self, state: GameState) -> Context:
        own_positions = {p.position for p in state.plantations}
        plant_by_pos = {p.position: p for p in state.plantations}
        construction_positions = {c.position for c in state.constructions}
        enemy_by_pos = {e.position: e for e in state.enemy_plantations}

        hq = next((p for p in state.plantations if p.is_main), state.plantations[0])
        max_hp = self._get_upgrade_val(state, "max_hp", 50, 10)
        signal_range = self._get_upgrade_val(state, "signal_range", 3, 1)

        storm_danger: set[Position] = set()
        for ev in state.meteo_forecasts:
            if ev.kind == "sandstorm":
                for center in (ev.position, ev.next_position):
                    if center is None or ev.radius is None:
                        continue
                    cx, cy = center
                    r = ev.radius
                    for dx in range(-r, r + 1):
                        for dy in range(-r, r + 1):
                            storm_danger.add((cx + dx, cy + dy))

        terra_by_pos: dict[Position, int] = {}
        for cell in state.terraformed_cells:
            terra_by_pos[cell.position] = cell.terraformation_progress

        turns_to_die: dict[Position, int] = {}
        for pos in own_positions:
            progress = terra_by_pos.get(pos, 0)
            remaining_pct = 100 - progress
            if remaining_pct <= 0:
                turns_to_die[pos] = 0
            else:
                turns_to_die[pos] = remaining_pct // TS

        return Context(
            hq=hq,
            max_hp=max_hp,
            signal_range=signal_range,
            own_positions=own_positions,
            plant_by_pos=plant_by_pos,
            construction_positions=construction_positions,
            enemy_by_pos=enemy_by_pos,
            lodges=list(state.beavers),
            storm_danger=storm_danger,
            terra_by_pos=terra_by_pos,
            turns_to_die=turns_to_die,
        )

    def _get_upgrade_val(self, state: GameState, name: str, base: int, per_level: int) -> int:
        if state.plantation_upgrades:
            for t in state.plantation_upgrades.tiers:
                if t.name == name:
                    return base + t.current * per_level
        return base

    def _apply_upgrade(self, state: GameState, cmd: Command, ctx: Context) -> None:
        upg = state.plantation_upgrades
        if upg is None or upg.points <= 0:
            return
        tier_map = {t.name: t for t in upg.tiers}
        for name in UPGRADE_PRIORITY:
            tier = tier_map.get(name)
            if tier and tier.current < tier.max:
                cmd.upgrade_plantation(name)
                return

    def _proactive_relocate_hq(self, state: GameState, cmd: Command, ctx: Context) -> None:
        if len(state.plantations) < 2:
            return

        hq = ctx.hq
        hq_ttd = ctx.turns_to_die.get(hq.position, 999)

        if hq_ttd > 5:
            return

        best: Position | None = None
        best_ttd = -1

        for nb in _adjacent(hq.position):
            if nb not in ctx.own_positions:
                continue
            nb_ttd = ctx.turns_to_die.get(nb, 999)
            if nb_ttd > best_ttd:
                best_ttd = nb_ttd
                best = nb

        if best is not None and best_ttd > hq_ttd:
            cmd.relocate_main(hq.position, best)

    def _assign_repairs(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        exit_usage: dict[Position, int],
        ctx: Context,
    ) -> None:
        damaged = []
        for p in state.plantations:
            if p.is_isolated:
                continue
            ttd = ctx.turns_to_die.get(p.position, 999)
            if ttd < 5:
                continue
            if p.hp < ctx.max_hp * 0.6:
                priority = 0 if p.is_main else 1
                damaged.append((priority, p.hp, p))

        for _, _, target in sorted(damaged):
            worker = self._find_best_worker(target.position, state, assigned, exit_usage, ctx)
            if worker is None:
                continue
            pos, exit_pt = worker
            assigned.add(pos)
            exit_usage[exit_pt] += 1
            if exit_pt == pos:
                cmd.repair(pos, target.position)
            else:
                cmd.repair_via(pos, exit_pt, target.position)

    def _assign_builds(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        exit_usage: dict[Position, int],
        ctx: Context,
    ) -> None:
        frontier = self._score_frontier(state, ctx)
        target_builders: dict[Position, int] = defaultdict(int)

        for target_pos, _score in frontier:
            if target_builders[target_pos] >= 3:
                continue

            worker = self._find_best_worker(target_pos, state, assigned, exit_usage, ctx)
            if worker is None:
                continue

            pos, exit_pt = worker
            assigned.add(pos)
            exit_usage[exit_pt] += 1
            target_builders[target_pos] += 1
            if exit_pt == pos:
                cmd.build(pos, target_pos)
            else:
                cmd.build_via(pos, exit_pt, target_pos)

        for p in state.plantations:
            if p.position in assigned or p.is_isolated:
                continue
            best_target: Position | None = None
            best_exit: Position | None = None
            best_score = -1e9
            for target_pos, score in frontier:
                if target_builders[target_pos] >= 3:
                    continue
                exit_pt = self._find_exit_point(p.position, target_pos, ctx, exit_usage)
                if exit_pt is None:
                    continue
                if score > best_score:
                    best_score = score
                    best_target = target_pos
                    best_exit = exit_pt
            if best_target is not None and best_exit is not None:
                assigned.add(p.position)
                exit_usage[best_exit] += 1
                target_builders[best_target] += 1
                if best_exit == p.position:
                    cmd.build(p.position, best_target)
                else:
                    cmd.build_via(p.position, best_exit, best_target)

    def _score_frontier(
        self,
        state: GameState,
        ctx: Context,
    ) -> list[tuple[Position, float]]:
        w, h = self._map_size
        center = (w // 2, h // 2)
        max_dist = max(w, h)

        candidates: dict[Position, float] = {}

        for con in state.constructions:
            score = 200.0 + con.progress * 3
            if _is_reinforced(con.position):
                score += 80
            candidates[con.position] = score

        for pos, ttd in ctx.turns_to_die.items():
            if ttd <= 12:
                for nb in _adjacent(pos):
                    if nb in ctx.own_positions or nb in self._mountains:
                        continue
                    if not (0 <= nb[0] < w and 0 <= nb[1] < h):
                        continue
                    if nb not in candidates:
                        candidates[nb] = 0
                    candidates[nb] += 40

        for pos in ctx.own_positions:
            for nb in _adjacent(pos):
                if nb in ctx.own_positions or nb in self._mountains or nb in candidates:
                    continue
                if not (0 <= nb[0] < w and 0 <= nb[1] < h):
                    continue
                if nb in ctx.storm_danger:
                    continue

                score = 0.0

                if _is_reinforced(nb):
                    score += 100
                else:
                    for n2 in _adjacent(nb):
                        if 0 <= n2[0] < w and 0 <= n2[1] < h and _is_reinforced(n2):
                            score += 40
                            break

                dist_to_center = abs(nb[0] - center[0]) + abs(nb[1] - center[1])
                score += 20 * (1 - dist_to_center / max_dist)

                own_neighbors = sum(1 for n2 in _adjacent(nb) if n2 in ctx.own_positions)
                if own_neighbors >= 2:
                    score += 15
                if own_neighbors >= 3:
                    score -= 10

                candidates[nb] = score

        return sorted(candidates.items(), key=lambda x: -x[1])

    def _find_best_worker(
        self,
        target: Position,
        state: GameState,
        assigned: set[Position],
        exit_usage: dict[Position, int],
        ctx: Context,
    ) -> tuple[Position, Position] | None:
        best: tuple[Position, Position] | None = None
        best_key = (999, 999)

        for p in state.plantations:
            if p.position in assigned or p.is_isolated:
                continue
            exit_pt = self._find_exit_point(p.position, target, ctx, exit_usage)
            if exit_pt is None:
                continue
            dist = _cheb(p.position, target)
            key = (exit_usage.get(exit_pt, 0), dist)
            if key < best_key:
                best_key = key
                best = (p.position, exit_pt)

        return best

    def _find_exit_point(
        self,
        author: Position,
        target: Position,
        ctx: Context,
        exit_usage: dict[Position, int] | None = None,
    ) -> Position | None:
        if _cheb(author, target) <= DEFAULT_AR:
            return author

        best: Position | None = None
        best_key = (999, 999)

        for pos in ctx.own_positions:
            if pos == author:
                continue
            if _cheb(author, pos) > ctx.signal_range:
                continue
            if _cheb(pos, target) > DEFAULT_AR:
                continue
            dist = _cheb(pos, target)
            usage = exit_usage.get(pos, 0) if exit_usage is not None else 0
            key = (usage, dist)
            if key < best_key:
                best = pos
                best_key = key

        return best
