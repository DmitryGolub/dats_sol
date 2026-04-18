"""HeadfulBot — FactoryBot engine + Caterpillar HQ policy.

Build/repair uses proven FactoryBot logic, but:
- HQ relocates aggressively like a caterpillar head
- Build targets get vector-boost toward movement direction
- Tail repairs are suppressed to save resources for head
- Death/lodge memory steers colony away from danger
"""
from __future__ import annotations

from collections import defaultdict

from api.models import Command, GameState, Position
from strategy.bots.benchmarks import BenchmarkFactoryBot
from strategy.brain.spatial import InfluenceMap
from strategy.brain.temporal import HorizonPredictor
from strategy.brain.strategic import StateMachine
from strategy.brain.utils import adjacent, chebyshev, is_reinforced


class HeadfulBot(BenchmarkFactoryBot):
    name = "bench_headful"
    lodge_commit_turn = 180
    late_lodge_bias = 0.5
    branch_penalty = 6.0
    dense_bonus = 28.0

    def __init__(self) -> None:
        super().__init__()
        self._spatial = InfluenceMap(0, 0)
        self._strategic = StateMachine()
        self._temporal = HorizonPredictor()
        self._known_lodges: set[Position] = set()
        self._last_plant_positions: set[Position] = set()
        self._death_positions: list[Position] = []
        self._relocate_cooldown: int = 0

    def reset(self) -> None:
        super().reset()
        self._known_lodges.clear()
        self._last_plant_positions.clear()
        self._death_positions.clear()
        self._relocate_cooldown = 0
        self._spatial = InfluenceMap(0, 0)

    def _init(self, state: GameState) -> None:
        super()._init(state)
        self._spatial = InfluenceMap(state.map_size[0], state.map_size[1])

    def _apply_upgrade(self, state: GameState, cmd: Command) -> None:
        upgrades = state.plantation_upgrades
        if upgrades is None or upgrades.points <= 0:
            return
        tier_map = {tier.name: tier for tier in upgrades.tiers}
        plant_count = len(state.plantations)
        limit = self._plantation_limit(state)

        for name in ["signal_range", "repair_power", "decay_mitigation"]:
            tier = tier_map.get(name)
            if tier and tier.current < tier.max:
                cmd.upgrade_plantation(name)
                return

        if plant_count >= limit - 6 or state.turn_no >= 160:
            tier = tier_map.get("settlement_limit")
            if tier and tier.current < tier.max:
                cmd.upgrade_plantation("settlement_limit")
                return

        for name in ["max_hp", "vision_range", "beaver_damage_mitigation", "earthquake_mitigation"]:
            tier = tier_map.get(name)
            if tier and tier.current < tier.max:
                cmd.upgrade_plantation(name)
                return

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

        # 1. Upgrade
        self._apply_upgrade(state, cmd)

        # 2. Update awareness
        # self._spatial.update(state)  # disabled: not used by caterpillar logic, very expensive
        for beaver in state.beavers:
            self._known_lodges.add(beaver.position)
        current_plants = {p.position for p in state.plantations}
        if self._last_plant_positions:
            for pos in self._last_plant_positions - current_plants:
                self._death_positions.append(pos)
        self._last_plant_positions = current_plants

        # 3. Movement vector
        vec_x, vec_y = self._movement_vector(state, own_positions, hq)

        # 4. Repair (factory base, but suppress tail repairs)
        assigned: set[Position] = set()
        self._assign_repairs_caterpillar(state, cmd, assigned, own_positions, hq, terraform_by_pos, vec_x, vec_y)

        # 5. Lodge attacks
        self._assign_lodge_attacks(state, cmd, assigned, own_positions, hq)

        # 6. Build (factory base + vector boost + death penalty)
        self._assign_builds_caterpillar(state, cmd, assigned, own_positions, hq, terraform_by_pos, vec_x, vec_y)

        # 7. Leapfrog HQ relocate
        self._maybe_relocate_hq_caterpillar(state, cmd, own_positions, plant_by_pos, hq, terraform_by_pos, vec_x, vec_y)

        if self._relocate_cooldown > 0:
            self._relocate_cooldown -= 1

        return cmd

    # ------------------------------------------------------------------
    #  Movement Vector
    # ------------------------------------------------------------------
    def _movement_vector(
        self,
        state: GameState,
        own_positions: set[Position],
        hq,
    ) -> tuple[float, float]:
        hq_pos = hq.position
        vec_x, vec_y = 0.0, 0.0

        # Pull: nearest unreached reinforced
        best_reinf = None
        best_dist = 999
        for x in range(0, self._map_size[0], 7):
            for y in range(0, self._map_size[1], 7):
                pos = (x, y)
                if pos in own_positions or pos in self._mountains:
                    continue
                d = chebyshev(hq_pos, pos)
                if d < best_dist:
                    best_dist = d
                    best_reinf = pos
        if best_reinf:
            vec_x += (best_reinf[0] - hq_pos[0]) * 2.0
            vec_y += (best_reinf[1] - hq_pos[1]) * 2.0

        # Push: away from death cluster
        recent = self._death_positions[-20:]
        if len(recent) >= 2:
            cx = sum(p[0] for p in recent) // len(recent)
            cy = sum(p[1] for p in recent) // len(recent)
            vec_x += (hq_pos[0] - cx) * 1.5
            vec_y += (hq_pos[1] - cy) * 1.5

        # Push: away from known lodges
        for lodge_pos in self._known_lodges:
            dx = hq_pos[0] - lodge_pos[0]
            dy = hq_pos[1] - lodge_pos[1]
            dist = max(1, chebyshev(hq_pos, lodge_pos))
            vec_x += dx / dist * 3.0
            vec_y += dy / dist * 3.0

        mag = (vec_x * vec_x + vec_y * vec_y) ** 0.5
        if mag > 0:
            vec_x /= mag
            vec_y /= mag
        return vec_x, vec_y

    def _dot_with_vector(self, pos: Position, ref: Position, vec_x: float, vec_y: float) -> float:
        return (pos[0] - ref[0]) * vec_x + (pos[1] - ref[1]) * vec_y

    # ------------------------------------------------------------------
    #  Caterpillar Repair (suppress tail, save resources for head)
    # ------------------------------------------------------------------
    def _assign_repairs_caterpillar(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
        hq,
        terraform_by_pos: dict[Position, object],
        vec_x: float,
        vec_y: float,
    ) -> None:
        max_hp = 50
        if state.plantation_upgrades:
            for tier in state.plantation_upgrades.tiers:
                if tier.name == "max_hp":
                    max_hp = 50 + tier.current * 10
                    break

        for plant in state.plantations:
            if plant.position in assigned or plant.is_isolated:
                continue

            # HQ — always repair
            if plant.is_main:
                if plant.hp < max_hp * 0.5:
                    self._commit_repair(plant.position, state, cmd, assigned, own_positions)
                continue

            cell = terraform_by_pos.get(plant.position)
            progress = cell.terraformation_progress if cell else 0
            dot = self._dot_with_vector(plant.position, hq.position, vec_x, vec_y)

            # Tail: far behind AND high progress — let it die unless critical bridge
            is_tail = (dot < -3) and (progress > 70)
            if is_tail:
                if not self._is_critical(plant.position, own_positions, hq.position):
                    continue
                if plant.hp >= max_hp * 0.2:
                    continue

            # Normal repair
            threshold = 0.40
            if self._is_critical(plant.position, own_positions, hq.position):
                threshold = 0.60

            if plant.hp < max_hp * threshold:
                self._commit_repair(plant.position, state, cmd, assigned, own_positions)

    def _commit_repair(
        self,
        target: Position,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
    ) -> None:
        best = None
        best_exit = None
        best_dist = 999
        for plant in state.plantations:
            if plant.position in assigned or plant.is_isolated or plant.position == target:
                continue
            exit_point = self._best_factory_exit(plant.position, target, own_positions, state)
            if exit_point is None:
                continue
            dist = chebyshev(exit_point, target)
            if dist < best_dist:
                best = plant.position
                best_exit = exit_point
                best_dist = dist
        if best is not None:
            assigned.add(best)
            if best_exit == best:
                cmd.repair(best, target)
            else:
                cmd.repair_via(best, best_exit, target)

    # ------------------------------------------------------------------
    #  Caterpillar Build (factory engine + vector boost + penalties)
    # ------------------------------------------------------------------
    def _assign_builds_caterpillar(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
        hq,
        terraform_by_pos: dict[Position, object],
        vec_x: float,
        vec_y: float,
    ) -> None:
        available = [p for p in state.plantations if p.position not in assigned and not p.is_isolated]
        if not available:
            return

        construction_by_pos = {c.position: c for c in state.constructions}
        targets = self._factory_targets(state, own_positions, hq, terraform_by_pos)

        # Apply caterpillar boosts / penalties
        safe_neighbors = self._count_safe_hq_neighbors(hq, own_positions, state)
        boosted = []
        for pos, score in targets:
            dot = self._dot_with_vector(pos, hq.position, vec_x, vec_y)
            dist_hq = chebyshev(pos, hq.position)

            # Head direction bonus
            if dot > 0:
                score += 180.0 + dot * 25.0
            elif dot < -3:
                score -= 120.0

            # HQ escape
            if dist_hq <= 1:
                if safe_neighbors < 2:
                    score += 350.0
                else:
                    score += 150.0
            elif dist_hq == 2:
                score += 60.0

            # Death cluster penalty
            recent = self._death_positions[-20:]
            if len(recent) >= 2:
                death_center = (
                    sum(p[0] for p in recent) // len(recent),
                    sum(p[1] for p in recent) // len(recent),
                )
                if chebyshev(pos, death_center) <= 6:
                    score -= 80.0

            # Lodge penalty
            for lodge_pos in self._known_lodges:
                ld = chebyshev(pos, lodge_pos)
                if ld <= 2:
                    score -= 300.0
                elif ld <= 4:
                    score -= 80.0

            boosted.append((pos, score))

        boosted.sort(key=lambda item: -item[1])

        used_exits: dict[Position, int] = defaultdict(int)
        limit = self._plantation_limit(state)
        current_count = len(state.plantations)
        overbuild = 4 if state.turn_no < 120 else 5 if state.turn_no < 280 else 3
        immediate_budget = max(1, limit + overbuild - current_count)
        staged_count = len(state.constructions)
        births_committed = 0
        early_spread = state.turn_no < 70 and current_count < 6

        for target_pos, priority in boosted:
            if not available:
                break
            if target_pos in self._mountains:
                continue
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
                score -= chebyshev(exit_point, target_pos) * 4
                score += self._factory_mass(plant.position, own_positions, terraform_by_pos) * 0.01
                candidates.append((score, plant.position, exit_point, eff))
            if not candidates:
                continue
            candidates.sort(reverse=True)

            if early_spread and progress <= 0:
                spread_take = min(len(candidates), 2 if current_count >= 3 else 1)
                committed = []
                for _, author, exit_point, _ in candidates[:spread_take]:
                    eff = max(0, self._construction_speed(state) - used_exits[exit_point])
                    if eff <= 0:
                        continue
                    committed.append((author, exit_point, eff))
                    used_exits[exit_point] += 1
                if committed:
                    committed_authors = {a for a, _, _ in committed}
                    available = [p for p in available if p.position not in committed_authors]
                    for author, exit_point, _ in committed:
                        assigned.add(author)
                        if exit_point == author:
                            cmd.build(author, target_pos)
                        else:
                            cmd.build_via(author, exit_point, target_pos)
                    staged_count += 1
                    continue

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
            if immediate and births_committed >= immediate_budget:
                for _, exit_point, _ in committed:
                    used_exits[exit_point] -= 1
                committed = []
                immediate = False
            if not immediate:
                allow_fresh_stage = progress <= 0 and (current_count < 12 or staged_count < max(4, 10))
                if (progress <= 0 and not allow_fresh_stage) or staged_count >= 15:
                    for _, exit_point, _ in committed:
                        used_exits[exit_point] -= 1
                    continue
                for _, exit_point, _ in committed:
                    used_exits[exit_point] -= 1
                committed = []
                total = 0
                for _, author, exit_point, _ in candidates:
                    if progress > 0 and len(committed) >= 3:
                        break
                    if progress <= 0 and len(committed) >= 2 and current_count >= 12:
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

            committed_authors = {a for a, _, _ in committed}
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

    # ------------------------------------------------------------------
    #  Leapfrog HQ Relocate
    # ------------------------------------------------------------------
    def _maybe_relocate_hq_caterpillar(
        self,
        state: GameState,
        cmd: Command,
        own_positions: set[Position],
        plant_by_pos: dict[Position, object],
        hq,
        terraform_by_pos: dict[Position, object],
        vec_x: float,
        vec_y: float,
    ) -> None:
        if self._relocate_cooldown > 0:
            return

        hq_progress = 0
        for cell in state.terraformed_cells:
            if cell.position == hq.position:
                hq_progress = cell.terraformation_progress
                break

        beaver_dist = self._nearest_beaver_distance(hq.position, state.beavers)
        safe_neighbors = self._count_safe_hq_neighbors(hq, own_positions, state)

        must_relocate = (
            hq_progress >= 20
            or beaver_dist <= 2
            or safe_neighbors < 1
            or hq.hp < 25
        )
        should_relocate = (
            state.turn_no % 10 == 0
            and safe_neighbors < 2
        )

        if not must_relocate and not should_relocate:
            return

        best = None
        best_score = -1.0
        for nb in adjacent(hq.position):
            if nb not in own_positions:
                continue
            nb_plant = plant_by_pos.get(nb)
            if nb_plant is None or nb_plant.is_isolated:
                continue
            nb_cell = terraform_by_pos.get(nb)
            progress = nb_cell.terraformation_progress if nb_cell else 0
            if progress >= 30:
                continue
            beaver_dist_nb = self._nearest_beaver_distance(nb, state.beavers)
            if beaver_dist_nb <= 2:
                continue

            direction_bonus = self._dot_with_vector(nb, hq.position, vec_x, vec_y) * 20.0
            neighbor_count = sum(1 for n in adjacent(nb) if n in own_positions)
            score = (30 - progress) * 8 + neighbor_count * 15 + nb_plant.hp * 2 + direction_bonus

            if score > best_score:
                best_score = score
                best = nb

        if best is not None:
            cmd.relocate_main(hq.position, best)
            self._relocate_cooldown = 3

    # ------------------------------------------------------------------
    #  Lodge Attacks
    # ------------------------------------------------------------------
    def _assign_lodge_attacks(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
        hq,
    ) -> None:
        lodges = list(self._known_lodges) + [b.position for b in state.beavers]
        if not lodges:
            return

        for lodge_pos in set(lodges):
            min_dist = min((chebyshev(lodge_pos, op) for op in own_positions), default=999)
            if min_dist > 8:
                continue

            attackers: list[tuple[Position, Position]] = []
            for plant in state.plantations:
                if plant.position in assigned or plant.is_isolated:
                    continue
                exit_point = self._best_factory_exit(plant.position, lodge_pos, own_positions, state)
                if exit_point is None:
                    continue
                attackers.append((plant.position, exit_point))

            if not attackers:
                continue

            max_attackers = 3 if min_dist <= 4 else 2 if min_dist <= 6 else 1
            for author, exit_point in attackers[:max_attackers]:
                assigned.add(author)
                if exit_point == author:
                    cmd.attack_beaver(author, lodge_pos)
                else:
                    cmd.attack_beaver_via(author, exit_point, lodge_pos)

    # ------------------------------------------------------------------
    #  Helpers
    # ------------------------------------------------------------------
    def _count_safe_hq_neighbors(self, hq, own_positions: set[Position], state: GameState) -> int:
        safe = 0
        for nb in adjacent(hq.position):
            if nb not in own_positions:
                continue
            cell = next((c for c in state.terraformed_cells if c.position == nb), None)
            progress = cell.terraformation_progress if cell else 0
            if progress >= 55:
                continue
            beaver_dist = 999
            for b in state.beavers:
                beaver_dist = min(beaver_dist, chebyshev(nb, b.position))
            if beaver_dist <= 2:
                continue
            safe += 1
        return safe

    def _nearest_beaver_distance(self, pos: Position, beavers: list) -> int:
        dist = 999
        for b in beavers:
            dist = min(dist, chebyshev(pos, b.position))
        return dist
