from __future__ import annotations

import logging
from collections import defaultdict

from api.models import Command, GameState, Plantation, Position
from api.helpers import Pathfinder
from strategy.base import BaseStrategy

log = logging.getLogger("bot.current")

UPGRADE_PRIORITY = [
    "settlement_limit",
    "signal_range",
    "decay_mitigation",
    "max_hp",
    "repair_power",
    "vision_range",
]


def _adjacent(pos: Position) -> list[Position]:
    x, y = pos
    return [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]


def _chebyshev(a: Position, b: Position) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def _is_reinforced(pos: Position) -> bool:
    return pos[0] % 7 == 0 and pos[1] % 7 == 0


class CurrentBot(BaseStrategy):
    name = "current"

    def __init__(self) -> None:
        self._mountains: set[Position] = set()
        self._map_size: tuple[int, int] = (0, 0)
        self._reinforced: set[Position] = set()
        self._initialized = False

    def reset(self) -> None:
        self._initialized = False

    def decide(self, state: GameState) -> Command:
        cmd = Command()

        if not state.plantations:
            return cmd

        if not self._initialized:
            self._init(state)

        self._apply_upgrade(state, cmd)

        own_positions = {p.position for p in state.plantations}
        plant_by_pos = {p.position: p for p in state.plantations}
        construction_positions = {c.position for c in state.constructions}

        hq = next((p for p in state.plantations if p.is_main), None)
        if hq is None:
            return cmd

        max_hp = self._get_max_hp(state)
        assigned: set[Position] = set()
        exit_usage: dict[Position, int] = defaultdict(int)

        self._assign_repairs(state, cmd, assigned, exit_usage, plant_by_pos, own_positions, max_hp, hq)
        self._assign_builds(state, cmd, assigned, exit_usage, plant_by_pos, own_positions, construction_positions, hq)
        self._maybe_relocate_hq(state, cmd, hq, own_positions)

        return cmd

    def _init(self, state: GameState) -> None:
        self._mountains = set(state.mountains)
        self._map_size = state.map_size
        w, h = self._map_size
        self._reinforced = set()
        for x in range(0, w, 7):
            for y in range(0, h, 7):
                if (x, y) not in self._mountains:
                    self._reinforced.add((x, y))
        self._initialized = True

    def _get_max_hp(self, state: GameState) -> int:
        if state.plantation_upgrades:
            for t in state.plantation_upgrades.tiers:
                if t.name == "max_hp":
                    return 50 + t.current * 10
        return 50

    def _apply_upgrade(self, state: GameState, cmd: Command) -> None:
        upg = state.plantation_upgrades
        if upg is None or upg.points <= 0:
            return
        tier_map = {t.name: t for t in upg.tiers}
        for name in UPGRADE_PRIORITY:
            tier = tier_map.get(name)
            if tier and tier.current < tier.max:
                cmd.upgrade_plantation(name)
                return

    def _assign_repairs(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        exit_usage: dict[Position, int],
        plant_by_pos: dict[Position, Plantation],
        own_positions: set[Position],
        max_hp: int,
        hq: Plantation,
    ) -> None:
        damaged = sorted(
            [p for p in state.plantations if p.hp < max_hp * 0.7 and not p.is_isolated],
            key=lambda p: (0 if p.is_main else 1, p.hp),
        )

        for target in damaged:
            best_repairer: Position | None = None
            best_exit: Position | None = None
            best_key = (999, 999)

            for p in state.plantations:
                if p.position in assigned or p.is_isolated or p.position == target.position:
                    continue
                exit_point = self._find_exit_point(p.position, target.position, own_positions, state, exit_usage)
                if exit_point is None:
                    continue
                dist = _chebyshev(p.position, target.position)
                key = (exit_usage[exit_point], dist)
                if key < best_key:
                    best_key = key
                    best_repairer = p.position
                    best_exit = exit_point

            if best_repairer is not None and best_exit is not None:
                assigned.add(best_repairer)
                exit_usage[best_exit] += 1
                if best_exit != best_repairer:
                    cmd.repair_via(best_repairer, best_exit, target.position)
                else:
                    cmd.repair(best_repairer, target.position)

    def _assign_builds(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        exit_usage: dict[Position, int],
        plant_by_pos: dict[Position, Plantation],
        own_positions: set[Position],
        construction_positions: set[Position],
        hq: Plantation,
    ) -> None:
        frontier = self._score_frontier(state, own_positions, construction_positions, hq)

        target_assignments: dict[Position, int] = defaultdict(int)

        for target_pos, score in frontier:
            if target_assignments[target_pos] >= 3:
                continue

            best_builder: Position | None = None
            best_exit: Position | None = None
            best_key = (999, 999)

            for p in state.plantations:
                if p.position in assigned or p.is_isolated:
                    continue
                exit_point = self._find_exit_point(p.position, target_pos, own_positions, state, exit_usage)
                if exit_point is None:
                    continue
                dist = _chebyshev(p.position, target_pos)
                key = (exit_usage[exit_point], dist)
                if key < best_key:
                    best_key = key
                    best_builder = p.position
                    best_exit = exit_point

            if best_builder is not None and best_exit is not None:
                assigned.add(best_builder)
                exit_usage[best_exit] += 1
                target_assignments[target_pos] += 1
                if best_exit == best_builder:
                    cmd.build(best_builder, target_pos)
                else:
                    cmd.build_via(best_builder, best_exit, target_pos)

        for p in state.plantations:
            if p.position in assigned or p.is_isolated:
                continue
            chosen_target: Position | None = None
            chosen_exit: Position | None = None
            best_key = (999, 999)
            for target_pos, score in frontier:
                if target_assignments[target_pos] >= 3:
                    continue
                exit_point = self._find_exit_point(p.position, target_pos, own_positions, state, exit_usage)
                if exit_point is None:
                    continue
                key = (exit_usage[exit_point], -score)
                if key < best_key:
                    best_key = key
                    chosen_target = target_pos
                    chosen_exit = exit_point
            if chosen_target is not None and chosen_exit is not None:
                assigned.add(p.position)
                exit_usage[chosen_exit] += 1
                target_assignments[chosen_target] += 1
                if chosen_exit == p.position:
                    cmd.build(p.position, chosen_target)
                else:
                    cmd.build_via(p.position, chosen_exit, chosen_target)

    def _score_frontier(
        self,
        state: GameState,
        own_positions: set[Position],
        construction_positions: set[Position],
        hq: Plantation,
    ) -> list[tuple[Position, float]]:
        w, h = self._map_size
        center = (w // 2, h // 2)
        max_dist = max(w, h)

        candidates: dict[Position, float] = {}

        # Don't let the bot reinforce a construction sitting on (or under) an HQ cell
        # that is close to completing terraformation — HQ dies if the cell hits 100%.
        hq_progress = self._terraform_progress(state, hq.position)

        for con in state.constructions:
            if con.position == hq.position and hq_progress > 80:
                continue
            score = 100.0 + con.progress * 2
            if _is_reinforced(con.position):
                score += 50
            else:
                rx = con.position[0] - con.position[0] % 7
                ry = con.position[1] - con.position[1] % 7
                best_r_dist = 999
                for rfx in (rx, rx + 7):
                    for rfy in (ry, ry + 7):
                        if 0 <= rfx < w and 0 <= rfy < h and (rfx, rfy) not in self._mountains:
                            d = max(abs(con.position[0] - rfx), abs(con.position[1] - rfy))
                            if d < best_r_dist:
                                best_r_dist = d
                if best_r_dist == 1:
                    score += 35
                elif best_r_dist == 2:
                    score += 18
                elif best_r_dist == 3:
                    score += 8
            candidates[con.position] = score

        for pos in own_positions:
            for nb in _adjacent(pos):
                if nb in own_positions or nb in self._mountains or nb in candidates:
                    continue
                if not (0 <= nb[0] < w and 0 <= nb[1] < h):
                    continue

                score = 0.0
                if _is_reinforced(nb):
                    score += 80
                else:
                    rx = nb[0] - nb[0] % 7
                    ry = nb[1] - nb[1] % 7
                    best_r_dist = 999
                    for rfx in (rx, rx + 7):
                        for rfy in (ry, ry + 7):
                            if 0 <= rfx < w and 0 <= rfy < h and (rfx, rfy) not in self._mountains:
                                d = max(abs(nb[0] - rfx), abs(nb[1] - rfy))
                                if d < best_r_dist:
                                    best_r_dist = d
                    if best_r_dist == 1:
                        score += 35
                    elif best_r_dist == 2:
                        score += 18
                    elif best_r_dist == 3:
                        score += 8

                dist_to_center = abs(nb[0] - center[0]) + abs(nb[1] - center[1])
                score += 20 * (1 - dist_to_center / max_dist)

                own_neighbors = sum(1 for n2 in _adjacent(nb) if n2 in own_positions)
                score += own_neighbors * 2
                if own_neighbors >= 3:
                    score -= 15

                candidates[nb] = score

        result = sorted(candidates.items(), key=lambda x: -x[1])
        return result

    def _find_exit_point(
        self,
        author: Position,
        target: Position,
        own_positions: set[Position],
        state: GameState,
        exit_usage: dict[Position, int] | None = None,
    ) -> Position | None:
        if _chebyshev(author, target) <= state.action_range:
            return author

        sr = 3
        if state.plantation_upgrades:
            for t in state.plantation_upgrades.tiers:
                if t.name == "signal_range":
                    sr = 3 + t.current
                    break

        best: Position | None = None
        best_key = (999, 999)

        for pos in own_positions:
            if pos == author:
                continue
            if _chebyshev(author, pos) > sr:
                continue
            if _chebyshev(pos, target) > state.action_range:
                continue
            dist = _chebyshev(pos, target)
            usage = exit_usage[pos] if exit_usage is not None else 0
            key = (usage, dist)
            if key < best_key:
                best = pos
                best_key = key

        return best

    def _terraform_progress(self, state: GameState, pos: Position) -> int:
        for cell in state.terraformed_cells:
            if cell.position == pos:
                return cell.terraformation_progress
        return 0

    def _maybe_relocate_hq(
        self,
        state: GameState,
        cmd: Command,
        hq: Plantation,
        own_positions: set[Position],
    ) -> None:
        if len(state.plantations) < 2:
            return

        hq_progress = self._terraform_progress(state, hq.position)
        hq_neighbors = sum(1 for nb in _adjacent(hq.position) if nb in own_positions)

        # Early return: HQ not in danger and sits on a well-connected cell
        if hq_progress < 80 and hq_neighbors >= 2:
            return

        best_candidate: Position | None = None
        best_score = -1
        best_neighbors = 0

        for nb_pos in _adjacent(hq.position):
            if nb_pos not in own_positions:
                continue
            nb_progress = self._terraform_progress(state, nb_pos)
            if nb_progress >= 95:
                continue
            neighbor_count = sum(1 for n2 in _adjacent(nb_pos) if n2 in own_positions)
            score = (100 - nb_progress) + neighbor_count * 20
            if neighbor_count >= 2:
                score += 100
            if score > best_score:
                best_score = score
                best_candidate = nb_pos
                best_neighbors = neighbor_count

        if best_candidate is None:
            return

        # Below 90%: require a well-connected target (≥2 own neighbors).
        # At 90%+: forced move — HQ dies at 100%, so accept any adjacent own cell.
        if hq_progress < 90 and best_neighbors < 2:
            return

        cmd.relocate_main(hq.position, best_candidate)
