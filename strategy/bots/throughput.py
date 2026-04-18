"""Бот с приоритетом throughput (production-tuned).

Подклассы BenchmarkFactoryBot, но:
- ниже пороги repair (тратим меньше ходов на починку),
- включена фаза sabotage против вражеских плантаций,
- включены атаки на логова бобров (lodge_commit_turn снижен),
- детектим шторм / потерю темпа → расширяем build pipeline,
- HQ релоцируется раньше.
"""

from __future__ import annotations

from collections import defaultdict

from api.models import Command, GameState, Plantation, Position, TerraformCell
from strategy.bots.benchmarks import (
    BenchmarkFactoryBot,
    _adjacent,
    _chebyshev,
    _is_reinforced,
)


class ThroughputBot(BenchmarkFactoryBot):
    name = "throughput"

    # heal less, build more
    repair_threshold = 0.28
    critical_repair_threshold = 0.45

    # reactivate lodge attacks (base: 999 disabled)
    lodge_commit_turn = 50
    late_lodge_bias = 0.4

    # sabotage tunables
    sabotage_max_authors = 2
    sabotage_hp_ceiling = 18

    # recovery / storm tunables
    recovery_drop_threshold = 3
    recovery_hold_turns = 25
    storm_hold_turns = 8

    # HQ relocation gate (base: progress<40, hp>=18)
    proactive_hq_progress_gate = 25
    proactive_hq_hp_gate = 26

    def __init__(self) -> None:
        super().__init__()
        self._prev_plant_count: int = 0
        self._recovery_until_turn: int = -1
        self._storm_until_turn: int = -1
        self._storm_cells: set[Position] = set()

    def reset(self) -> None:
        super().reset()
        self._prev_plant_count = 0
        self._recovery_until_turn = -1
        self._storm_until_turn = -1
        self._storm_cells = set()

    # ------------------------------------------------------------------ decide

    def decide(self, state: GameState) -> Command:
        cmd = Command()
        if not state.plantations:
            return cmd
        if not self._initialized:
            self._init(state)

        hq = next((p for p in state.plantations if p.is_main), None)
        if hq is None:
            return cmd

        own_positions = {p.position for p in state.plantations}
        plant_by_pos = {p.position: p for p in state.plantations}
        terraform_by_pos = {cell.position: cell for cell in state.terraformed_cells}

        self._apply_upgrade(state, cmd)
        self._update_recovery_state(state, own_positions)

        assigned: set[Position] = set()
        self._assign_repairs_factory(state, cmd, assigned, own_positions, hq, terraform_by_pos)
        self._assign_sabotage(state, cmd, assigned, own_positions, hq, terraform_by_pos)
        self._assign_lodge_finishes(state, cmd, assigned, own_positions, hq)
        self._assign_builds_factory(state, cmd, assigned, own_positions, hq, terraform_by_pos)
        self._maybe_relocate_hq_factory(state, cmd, own_positions, plant_by_pos, hq, terraform_by_pos)
        return cmd

    # --------------------------------------------------------- recovery/storm

    def _update_recovery_state(self, state: GameState, own_positions: set[Position]) -> None:
        turn = state.turn_no
        count = len(state.plantations)

        if count + self.recovery_drop_threshold <= self._prev_plant_count:
            self._recovery_until_turn = turn + self.recovery_hold_turns

        storm_cells: set[Position] = set()
        storm_active = False
        for m in state.meteo_forecasts:
            if m.kind == "sandstorm":
                center = m.position
                radius = m.radius if m.radius is not None else 3
                if center is not None:
                    for dx in range(-(radius + 1), radius + 2):
                        for dy in range(-(radius + 1), radius + 2):
                            storm_cells.add((center[0] + dx, center[1] + dy))
                if m.next_position is not None:
                    nx, ny = m.next_position
                    for dx in range(-(radius + 1), radius + 2):
                        for dy in range(-(radius + 1), radius + 2):
                            storm_cells.add((nx + dx, ny + dy))
            elif m.kind == "earthquake":
                turns_until = m.turns_until if m.turns_until is not None else 999
                if turns_until <= 3:
                    storm_active = True

        if storm_cells & own_positions:
            storm_active = True

        self._storm_cells = storm_cells
        if storm_active:
            self._storm_until_turn = turn + self.storm_hold_turns

        self._prev_plant_count = count

    def _in_recovery(self, turn: int) -> bool:
        return turn <= self._recovery_until_turn

    def _in_storm(self, turn: int) -> bool:
        return turn <= self._storm_until_turn

    # --------------------------------------------------------------- sabotage

    def _assign_sabotage(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
        hq: Plantation,
        terraform_by_pos: dict[Position, TerraformCell],
    ) -> None:
        if self._in_recovery(state.turn_no):
            return
        if not state.enemy_plantations:
            return

        targets = sorted(
            [e for e in state.enemy_plantations if e.hp <= self.sabotage_hp_ceiling],
            key=lambda e: (e.hp, _chebyshev(e.position, hq.position)),
        )
        if not targets:
            return

        authors_used = 0
        for ep in targets:
            if authors_used >= self.sabotage_max_authors:
                break
            best_author: Position | None = None
            best_exit: Position | None = None
            best_score = -1.0
            for plant in state.plantations:
                if plant.position in assigned or plant.is_isolated:
                    continue
                exit_point = self._best_factory_exit(plant.position, ep.position, own_positions, state)
                if exit_point is None:
                    continue
                mass = self._factory_mass(plant.position, own_positions, terraform_by_pos)
                score = 100 - _chebyshev(exit_point, ep.position) * 4 + mass * 0.01
                if score > best_score:
                    best_author = plant.position
                    best_exit = exit_point
                    best_score = score
            if best_author is None or best_exit is None:
                continue
            assigned.add(best_author)
            authors_used += 1
            if best_exit == best_author:
                cmd.sabotage(best_author, ep.position)
            else:
                cmd.sabotage_via(best_author, best_exit, ep.position)

    # ---------------------------------------------------------------- repairs

    def _assign_repairs_factory(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
        hq: Plantation,
        terraform_by_pos: dict[Position, TerraformCell],
    ) -> None:
        if not self._in_storm(state.turn_no) or not self._storm_cells:
            super()._assign_repairs_factory(state, cmd, assigned, own_positions, hq, terraform_by_pos)
            return

        # во время шторма чиним только то, что не попадает в зону удара
        saved_positions = [p for p in state.plantations if p.position in self._storm_cells and not p.is_main]
        if not saved_positions:
            super()._assign_repairs_factory(state, cmd, assigned, own_positions, hq, terraform_by_pos)
            return

        doomed = {p.position for p in saved_positions}
        filtered_plantations = [p for p in state.plantations if p.position not in doomed]
        if not filtered_plantations:
            return
        filtered_state = _replace_plantations(state, filtered_plantations)
        super()._assign_repairs_factory(filtered_state, cmd, assigned, own_positions, hq, terraform_by_pos)

    # ----------------------------------------------------------------- builds

    def _assign_builds_factory(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
        hq: Plantation,
        terraform_by_pos: dict[Position, TerraformCell],
    ) -> None:
        available = [p for p in state.plantations if p.position not in assigned and not p.is_isolated]
        if not available:
            return

        construction_by_pos = {c.position: c for c in state.constructions}
        targets = self._factory_targets(state, own_positions, hq, terraform_by_pos)

        in_storm = self._in_storm(state.turn_no) and self._storm_cells
        if in_storm:
            targets = [(pos, pr) for pos, pr in targets if pos not in self._storm_cells]

        used_exits: dict[Position, int] = defaultdict(int)
        limit = self._plantation_limit(state)
        current_count = len(state.plantations)

        overbuild_allowance = 16 if state.turn_no < 120 else 12 if state.turn_no < 280 else 8
        if self._in_recovery(state.turn_no) or self._in_storm(state.turn_no):
            overbuild_allowance += 6
        immediate_budget = max(1, limit + overbuild_allowance - current_count)
        pipeline_cap = max(12, current_count * 2 // 3 + 12)
        staged_count = len(state.constructions)
        births_committed = 0

        in_recovery = self._in_recovery(state.turn_no)

        for target_pos, priority in targets:
            if not available:
                break
            progress = construction_by_pos.get(target_pos).progress if target_pos in construction_by_pos else 0
            required = max(0, 50 - progress)
            candidates: list[tuple[float, Position, Position, int]] = []
            for plant in available:
                exit_point = self._best_factory_exit(plant.position, target_pos, own_positions, state)
                if exit_point is None:
                    continue
                eff = max(0, self._construction_speed(state) - used_exits[exit_point])
                if eff <= 0:
                    continue
                score = priority
                if exit_point == plant.position:
                    score += 25
                score -= _chebyshev(exit_point, target_pos) * 4
                score += self._factory_mass(plant.position, own_positions, terraform_by_pos) * 0.01
                candidates.append((score, plant.position, exit_point, eff))
            if not candidates:
                continue
            candidates.sort(reverse=True)

            committed: list[tuple[Position, Position, int]] = []
            total = 0
            for _, author, exit_point, _ in candidates:
                eff = max(0, self._construction_speed(state) - used_exits[exit_point])
                if eff <= 0:
                    continue
                committed.append((author, exit_point, eff))
                used_exits[exit_point] += 1
                total += eff
                if total >= required:
                    break

            immediate = total >= required
            if immediate and births_committed >= immediate_budget and not in_recovery:
                for _, exit_point, _ in committed:
                    used_exits[exit_point] -= 1
                committed = []
                immediate = False
            if not immediate:
                allow_fresh_stage = progress <= 0 and (
                    current_count < 18 or staged_count < max(6, pipeline_cap // 2)
                )
                if (progress <= 0 and not allow_fresh_stage) or staged_count >= pipeline_cap:
                    for _, exit_point, _ in committed:
                        used_exits[exit_point] -= 1
                    continue
                for _, exit_point, _ in committed:
                    used_exits[exit_point] -= 1
                committed = []
                total = 0
                for _, author, exit_point, _ in candidates:
                    if progress > 0 and len(committed) >= 5:
                        break
                    if progress <= 0 and len(committed) >= 4 and current_count >= 18:
                        break
                    eff = max(0, self._construction_speed(state) - used_exits[exit_point])
                    if eff <= 0:
                        continue
                    committed.append((author, exit_point, eff))
                    used_exits[exit_point] += 1
                    total += eff
                    if progress > 0 and total >= max(1, required // 2):
                        break
                    if progress <= 0 and total >= max(5, required // 3):
                        break
                if not committed:
                    continue

            committed_authors = {author for author, _, _ in committed}
            available = [p for p in available if p.position not in committed_authors]
            for author, exit_point, _ in committed:
                assigned.add(author)
                if exit_point == author:
                    cmd.build(author, target_pos)
                else:
                    cmd.build_via(author, exit_point, target_pos)
            if immediate:
                births_committed += 1
            elif progress <= 0:
                staged_count += 1

    # ------------------------------------------------------------ HQ relocate

    def _maybe_relocate_hq_factory(
        self,
        state: GameState,
        cmd: Command,
        own_positions: set[Position],
        plant_by_pos: dict[Position, Plantation],
        hq: Plantation,
        terraform_by_pos: dict[Position, TerraformCell],
    ) -> None:
        if len(state.plantations) < 2:
            return
        hq_progress = (
            terraform_by_pos.get(hq.position).terraformation_progress
            if hq.position in terraform_by_pos
            else 0
        )
        hq_beaver_dist = self._nearest_beaver_distance(hq.position, state.beavers)
        if (
            hq_progress < self.proactive_hq_progress_gate
            and hq.hp >= self.proactive_hq_hp_gate
            and hq_beaver_dist > 2
        ):
            return

        best_candidate: Position | None = None
        best_score = -1_000.0
        for nb_pos in _adjacent(hq.position):
            nb = plant_by_pos.get(nb_pos)
            if nb is None or nb.is_isolated:
                continue
            progress = (
                terraform_by_pos.get(nb_pos).terraformation_progress
                if nb_pos in terraform_by_pos
                else 0
            )
            neighbor_count = sum(1 for n in _adjacent(nb_pos) if n in own_positions)
            score = 0.0
            score += max(0, 70 - progress) * 4.5
            score += neighbor_count * 20
            score += nb.hp * 1.5
            score += self._factory_mass(nb_pos, own_positions, terraform_by_pos) * 0.08
            score += self._nearest_beaver_distance(nb_pos, state.beavers) * 8
            if _is_reinforced(nb_pos):
                score -= 35
            if progress >= 85:
                score -= 120
            if score > best_score:
                best_score = score
                best_candidate = nb_pos
        if best_candidate is not None:
            cmd.relocate_main(hq.position, best_candidate)


def _replace_plantations(state: GameState, plantations: list[Plantation]) -> GameState:
    """Вернуть копию GameState с заменённым списком plantations."""
    from dataclasses import replace

    return replace(state, plantations=plantations)
