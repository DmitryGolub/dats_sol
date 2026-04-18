"""Reactive reflexes — fast safety overrides."""
from __future__ import annotations

from api.models import Command, GameState, Plantation, Position
from strategy.brain.utils import adjacent, chebyshev


class ReflexRules:
    """Emergency overrides with absolute priority over tactical layer."""

    def apply(self, state: GameState, cmd: Command, assigned: set[Position]) -> None:
        """Apply reflexes directly to the command builder and assigned set.
        
        This mutates cmd and assigned in-place.
        """
        hq = next((p for p in state.plantations if p.is_main), None)
        if hq is None:
            return

        own_positions = {p.position for p in state.plantations}
        plant_by_pos = {p.position: p for p in state.plantations}
        max_hp = self._max_hp(state)

        # Reflex 1: HQ HP critically low -> emergency repair
        if hq.hp < max_hp * 0.25:
            self._emergency_repair(hq, state, cmd, assigned, own_positions, plant_by_pos)

        # Reflex 2: HQ terraform progress dangerously high -> emergency relocate
        terraform_by_pos = {c.position: c for c in state.terraformed_cells}
        hq_progress = terraform_by_pos.get(hq.position)
        if hq_progress is not None and hq_progress.terraformation_progress >= 55:
            self._emergency_relocate(hq, state, cmd, assigned, own_positions, plant_by_pos)

        # Reflex 3: Isolated plant with HP > 0 -> emergency reconnect
        for plant in state.plantations:
            if plant.is_isolated and plant.hp > 0 and plant.position not in assigned:
                self._emergency_reconnect(plant, state, cmd, assigned, own_positions, plant_by_pos)

    def _emergency_repair(
        self,
        target: Plantation,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
        plant_by_pos: dict[Position, Plantation],
    ) -> None:
        best = self._best_author(target.position, state, assigned, own_positions)
        if best is not None:
            assigned.add(best)
            if chebyshev(best, target.position) <= state.action_range:
                cmd.repair(best, target.position)
            else:
                exit_point = self._find_exit_point(best, target.position, own_positions, state)
                if exit_point is not None:
                    cmd.repair_via(best, exit_point, target.position)

    def _emergency_relocate(
        self,
        hq: Plantation,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
        plant_by_pos: dict[Position, Plantation],
    ) -> None:
        best_candidate: Position | None = None
        best_score = -1.0
        for nb in adjacent(hq.position):
            if nb not in own_positions:
                continue
            nb_plant = plant_by_pos.get(nb)
            if nb_plant is None or nb_plant.is_isolated:
                continue
            terraform = next((c for c in state.terraformed_cells if c.position == nb), None)
            progress = terraform.terraformation_progress if terraform else 0
            if progress >= 55:
                continue
            beaver_dist = self._nearest_beaver_distance(nb, state.beavers)
            if beaver_dist <= 2:
                continue
            neighbor_count = sum(1 for n in adjacent(nb) if n in own_positions)
            score = nb_plant.hp * 2 + neighbor_count * 20 + (55 - progress) * 4 + beaver_dist * 12
            if score > best_score:
                best_score = score
                best_candidate = nb
        if best_candidate is not None:
            cmd.relocate_main(hq.position, best_candidate)

    def _emergency_reconnect(
        self,
        plant: Plantation,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
        plant_by_pos: dict[Position, Plantation],
    ) -> None:
        # Try to build a neighbor to reconnect, or repair the plant itself
        self._emergency_repair(plant, state, cmd, assigned, own_positions, plant_by_pos)

    def _best_author(
        self,
        target: Position,
        state: GameState,
        assigned: set[Position],
        own_positions: set[Position],
    ) -> Position | None:
        best = None
        best_dist = 999
        for plant in state.plantations:
            if plant.position in assigned or plant.is_isolated or plant.position == target:
                continue
            if chebyshev(plant.position, target) <= state.action_range:
                dist = chebyshev(plant.position, target)
                if dist < best_dist:
                    best = plant.position
                    best_dist = dist
                continue
            exit_point = self._find_exit_point(plant.position, target, own_positions, state)
            if exit_point is not None:
                dist = chebyshev(exit_point, target)
                if dist < best_dist:
                    best = plant.position
                    best_dist = dist
        return best

    def _find_exit_point(
        self,
        author: Position,
        target: Position,
        own_positions: set[Position],
        state: GameState,
    ) -> Position | None:
        best = None
        best_dist = 999
        for pos in own_positions:
            if chebyshev(author, pos) > state.action_range:
                continue
            if chebyshev(pos, target) > state.action_range:
                continue
            dist = chebyshev(pos, target)
            if dist < best_dist:
                best = pos
                best_dist = dist
        return best

    def _nearest_beaver_distance(self, pos: Position, beavers: list) -> int:
        dist = 999
        for b in beavers:
            dist = min(dist, chebyshev(pos, b.position))
        return dist

    def _max_hp(self, state: GameState) -> int:
        hp = 50
        if state.plantation_upgrades:
            for tier in state.plantation_upgrades.tiers:
                if tier.name == "max_hp":
                    hp = 50 + tier.current * 10
                    break
        return hp
