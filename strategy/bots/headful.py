"""HeadfulBot — FactoryBot base + layered stateful HQ planning."""
from __future__ import annotations

from api.models import Command, GameState, Position
from strategy.bots.benchmarks import BenchmarkFactoryBot
from strategy.brain.spatial import InfluenceMap
from strategy.brain.temporal import HorizonPredictor
from strategy.brain.strategic import StateMachine
from strategy.brain.utils import adjacent, chebyshev


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
        self._head_plan: list[Position] = []
        self._known_lodges: set[Position] = set()
        self._last_plant_positions: set[Position] = set()
        self._death_positions: list[Position] = []
        self._bridge_plan: list[Position] = []
        self._cluster_seeds: list[Position] = []

    def reset(self) -> None:
        super().reset()
        self._head_plan.clear()
        self._known_lodges.clear()
        self._last_plant_positions.clear()
        self._death_positions.clear()
        self._bridge_plan.clear()
        self._cluster_seeds.clear()
        self._spatial = InfluenceMap(0, 0)

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

        # Settlement limit earlier than factory default
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

        # 1. Upgrade (factory logic)
        self._apply_upgrade(state, cmd)

        # 2. Update spatial and temporal
        self._spatial.update(state)
        predictions = self._temporal.predict(state)

        # 2.5 Remember visible lodges and plant deaths
        for beaver in state.beavers:
            self._known_lodges.add(beaver.position)
        current_plants = {p.position for p in state.plantations}
        if self._last_plant_positions:
            for pos in self._last_plant_positions - current_plants:
                self._death_positions.append(pos)
        self._last_plant_positions = current_plants

        # 3. Strategic phase evaluation
        phase, weights = self._strategic.evaluate(state, predictions, own_positions, hq)

        # 4. Update head plan (HQ succession)
        self._update_head_plan(state, own_positions, hq)

        # 4.5 Update bridge / cluster plan
        self._update_bridge_plan(state, own_positions)

        # 5. Repair (factory logic)
        assigned: set[Position] = set()
        self._assign_repairs_factory(state, cmd, assigned, own_positions, hq, terraform_by_pos)

        # 6. Lodge attack — aggressive, does not require one-shot kill
        self._assign_lodge_attacks_headful(state, cmd, assigned, own_positions, hq)

        # 7. Build — use factory base, but with head-aware target injection
        self._assign_builds_headful(state, cmd, assigned, own_positions, hq, terraform_by_pos, phase, weights)

        # 8. HQ relocation — factory base + proactive head-based relocate
        self._maybe_relocate_hq_headful(state, cmd, own_positions, plant_by_pos, hq, terraform_by_pos, predictions)

        return cmd

    def _update_head_plan(
        self,
        state: GameState,
        own_positions: set[Position],
        hq: object,
    ) -> None:
        if hq is None:
            self._head_plan.clear()
            return
        current_hq = hq.position
        if self._head_plan and self._head_plan[0] == current_hq:
            pass
        else:
            self._head_plan = [current_hq]
        if len(self._head_plan) < 3:
            self._extend_head_plan(state, own_positions, self._head_plan)

    def _extend_head_plan(
        self,
        state: GameState,
        own_positions: set[Position],
        plan: list[Position],
    ) -> None:
        terraform_by_pos = {c.position: c for c in state.terraformed_cells}
        plant_by_pos = {p.position: p for p in state.plantations}
        max_hp = 50
        if state.plantation_upgrades:
            for tier in state.plantation_upgrades.tiers:
                if tier.name == "max_hp":
                    max_hp = 50 + tier.current * 10
                    break
        last = plan[-1]
        visited = set(plan)
        queue = [last]
        while queue and len(plan) < 5:
            current = queue.pop(0)
            candidates: list[tuple[float, Position]] = []
            for nb in adjacent(current):
                if nb in visited or nb not in own_positions:
                    continue
                plant = plant_by_pos.get(nb)
                if plant is None or plant.is_isolated:
                    continue
                cell = terraform_by_pos.get(nb)
                progress = cell.terraformation_progress if cell else 0
                if progress >= 40:
                    continue
                beaver_dist = self._nearest_beaver_distance(nb, state.beavers)
                if beaver_dist <= 2:
                    continue
                neighbor_count = sum(1 for n in adjacent(nb) if n in own_positions)
                hp_ratio = plant.hp / max(1, max_hp)
                score = (40 - progress) * 5 + neighbor_count * 15 + hp_ratio * 30 + beaver_dist * 8
                candidates.append((score, nb))
            candidates.sort(reverse=True)
            for score, pos in candidates[:1]:
                plan.append(pos)
                visited.add(pos)
                queue.append(pos)
                break
            if not candidates:
                break

    def _update_bridge_plan(
        self,
        state: GameState,
        own_positions: set[Position],
    ) -> None:
        """Plan a bridge to a distant high-value seed to create satellite clusters."""
        # Clean up completed bridge cells
        self._bridge_plan = [p for p in self._bridge_plan if p not in own_positions]
        self._cluster_seeds = [p for p in self._cluster_seeds if p not in own_positions]

        # Only plan new bridge if we have enough plants and no active bridge
        plant_count = len(state.plantations)
        if self._bridge_plan or plant_count < 8:
            return
        if state.turn_no < 50 or state.turn_no % 80 != 0:
            return

        # Find best distant seed (reinforced or high value, away from lodges)
        best_seed = None
        best_score = -1.0
        for x in range(0, self._map_size[0], 3):
            for y in range(0, self._map_size[1], 3):
                pos = (x, y)
                if pos in own_positions or pos in self._mountains:
                    continue
                dist_to_cluster = min(chebyshev(pos, op) for op in own_positions)
                if dist_to_cluster < 5 or dist_to_cluster > 14:
                    continue
                score = self._spatial.value_heatmap.get(pos, 0.0)
                # Strongly prefer reinforced
                if chebyshev(pos, (pos[0] // 7 * 7, pos[1] // 7 * 7)) == 0:
                    score += 300.0
                # Penalize proximity to known lodges
                for lodge_pos in self._known_lodges:
                    if chebyshev(pos, lodge_pos) <= 5:
                        score -= 300.0
                if score > best_score:
                    best_score = score
                    best_seed = pos

        if best_seed is None:
            return

        # Find nearest own plant and plan first step towards seed
        nearest = min(own_positions, key=lambda op: chebyshev(op, best_seed))
        # Greedy step towards seed
        dx = 0
        dy = 0
        if best_seed[0] > nearest[0]:
            dx = 1
        elif best_seed[0] < nearest[0]:
            dx = -1
        if best_seed[1] > nearest[1]:
            dy = 1
        elif best_seed[1] < nearest[1]:
            dy = -1

        step = (nearest[0] + dx, nearest[1] + dy)
        if step in own_positions or step in self._mountains:
            # try orthogonal alternatives
            alt_steps = [
                (nearest[0] + dx, nearest[1]),
                (nearest[0], nearest[1] + dy),
                (nearest[0] - dx, nearest[1] + dy),
                (nearest[0] + dx, nearest[1] - dy),
            ]
            for alt in alt_steps:
                if alt not in own_positions and alt not in self._mountains:
                    if 0 <= alt[0] < self._map_size[0] and 0 <= alt[1] < self._map_size[1]:
                        step = alt
                        break

        if step not in own_positions and step not in self._mountains:
            self._bridge_plan = [step, best_seed]
            self._cluster_seeds.append(best_seed)

    def _assign_builds_headful(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
        hq: object,
        terraform_by_pos: dict[Position, object],
        phase: str,
        weights,
    ) -> None:
        """Wrap factory build assignment with head-aware priority injection."""
        # If STABILIZE dominates, we may need to override build priorities
        # to focus on HQ escape and head grooming.
        head_boost: dict[Position, float] = {}
        if phase == "stabilize":
            for pos in self._head_plan[1:3]:
                for nb in adjacent(pos):
                    if nb not in own_positions and nb not in self._mountains:
                        head_boost[nb] = head_boost.get(nb, 0.0) + 300.0
            # Boost escape cells around current HQ
            for nb in adjacent(hq.position):
                if nb not in own_positions and nb not in self._mountains:
                    head_boost[nb] = head_boost.get(nb, 0.0) + 220.0

        # Penalty for building near known lodges
        lodge_penalty: dict[Position, float] = {}
        for lodge_pos in self._known_lodges:
            for dx in range(-3, 4):
                for dy in range(-3, 4):
                    pos = (lodge_pos[0] + dx, lodge_pos[1] + dy)
                    dist = chebyshev(pos, lodge_pos)
                    if dist <= 2:
                        lodge_penalty[pos] = lodge_penalty.get(pos, 0.0) - 250.0
                    elif dist <= 4:
                        lodge_penalty[pos] = lodge_penalty.get(pos, 0.0) - 80.0

        # Penalty for building towards death cluster (extrapolated lodge direction)
        death_penalty: dict[Position, float] = {}
        recent_deaths = self._death_positions[-30:]
        if len(recent_deaths) >= 3:
            death_center = (
                sum(p[0] for p in recent_deaths) // len(recent_deaths),
                sum(p[1] for p in recent_deaths) // len(recent_deaths),
            )
            # Vector from HQ to death center
            vec_x = death_center[0] - hq.position[0]
            vec_y = death_center[1] - hq.position[1]
            for pos in own_positions:
                for dx in range(-3, 4):
                    for dy in range(-3, 4):
                        nb = (pos[0] + dx, pos[1] + dy)
                        if nb in own_positions or nb in self._mountains:
                            continue
                        # Dot product with death vector
                        d_x = nb[0] - hq.position[0]
                        d_y = nb[1] - hq.position[1]
                        # If target is in same quadrant as death center, penalize
                        if (vec_x >= 0 and d_x >= 0 or vec_x < 0 and d_x < 0) and (vec_y >= 0 and d_y >= 0 or vec_y < 0 and d_y < 0):
                            dist_to_death = chebyshev(nb, death_center)
                            if dist_to_death <= 6:
                                death_penalty[nb] = death_penalty.get(nb, 0.0) - 120.0
                            elif dist_to_death <= 10:
                                death_penalty[nb] = death_penalty.get(nb, 0.0) - 40.0

        # Merge boosts
        combined_boost: dict[Position, float] = {}
        for pos, val in head_boost.items():
            combined_boost[pos] = combined_boost.get(pos, 0.0) + val
        for pos, val in lodge_penalty.items():
            combined_boost[pos] = combined_boost.get(pos, 0.0) + val
        for pos, val in death_penalty.items():
            combined_boost[pos] = combined_boost.get(pos, 0.0) + val

        # Call factory build logic with injected boost
        self._assign_builds_factory_with_boost(
            state, cmd, assigned, own_positions, hq, terraform_by_pos, combined_boost
        )

    def _assign_builds_factory_with_boost(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
        hq: object,
        terraform_by_pos: dict[Position, object],
        head_boost: dict[Position, float],
    ) -> None:
        """Modified factory build logic that applies head_boost to target scores."""
        available = [p for p in state.plantations if p.position not in assigned and not p.is_isolated]
        if not available:
            return

        from collections import defaultdict
        construction_by_pos = {c.position: c for c in state.constructions}
        targets = self._factory_targets(state, own_positions, hq, terraform_by_pos)

        # Apply head boost
        boosted_targets = []
        for pos, score in targets:
            boosted_targets.append((pos, score + head_boost.get(pos, 0.0)))
        boosted_targets.sort(key=lambda item: -item[1])

        used_exits: dict[Position, int] = defaultdict(int)
        limit = self._plantation_limit(state)
        current_count = len(state.plantations)
        overbuild_allowance = 4 if state.turn_no < 120 else 5 if state.turn_no < 280 else 3
        immediate_budget = max(1, limit + overbuild_allowance - current_count)
        pipeline_cap = max(12, current_count * 2 + 4) if current_count < 10 else max(10, current_count // 2 + 10)
        staged_count = len(state.constructions)
        births_committed = 0
        early_spread = state.turn_no < 70 and current_count < 6

        for target_pos, priority in boosted_targets:
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
                    committed_authors = {author for author, _, _ in committed}
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
                allow_fresh_stage = progress <= 0 and (
                    current_count < 12 or staged_count < max(4, pipeline_cap // 2)
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

    def _maybe_relocate_hq_headful(
        self,
        state: GameState,
        cmd: Command,
        own_positions: set[Position],
        plant_by_pos: dict[Position, object],
        hq: object,
        terraform_by_pos: dict[Position, object],
        predictions,
    ) -> None:
        """Proactive HQ relocation based on head plan and temporal predictions."""
        # First check factory emergency conditions
        terraform_progress = 0
        for cell in state.terraformed_cells:
            if cell.position == hq.position:
                terraform_progress = cell.terraformation_progress
                break
        beaver_dist = self._nearest_beaver_distance(hq.position, state.beavers)
        safe_neighbors = self._count_safe_hq_neighbors(hq, own_positions, state)
        emergency = terraform_progress >= 55 or hq.hp < 25 or beaver_dist <= 2 or safe_neighbors < 1

        # Proactive relocate: if we have a ready successor in head plan
        proactive = False
        best_candidate = None
        if len(self._head_plan) >= 2 and not emergency:
            successor = self._head_plan[1]
            if successor in own_positions:
                succ_plant = plant_by_pos.get(successor)
                if succ_plant is not None and not succ_plant.is_isolated:
                    succ_terraform = terraform_by_pos.get(successor)
                    succ_progress = succ_terraform.terraformation_progress if succ_terraform else 0
                    # Proactive trigger: HQ progress >= 30 and successor is healthy/low progress
                    if terraform_progress >= 30 and succ_progress < 35 and succ_plant.hp >= 30:
                        proactive = True
                        best_candidate = successor

        # Also proactive if safe neighbors are critically low (< 2) and we have any good candidate
        if not emergency and not proactive and safe_neighbors < 2:
            best_score = -1.0
            for nb in adjacent(hq.position):
                if nb not in own_positions:
                    continue
                nb_plant = plant_by_pos.get(nb)
                if nb_plant is None or nb_plant.is_isolated:
                    continue
                nb_cell = terraform_by_pos.get(nb)
                progress = nb_cell.terraformation_progress if nb_cell else 0
                if progress >= 40:
                    continue
                beaver_dist_nb = self._nearest_beaver_distance(nb, state.beavers)
                if beaver_dist_nb <= 2:
                    continue
                score = nb_plant.hp * 2 + (40 - progress) * 4 + beaver_dist_nb * 12
                if score > best_score:
                    best_score = score
                    best_candidate = nb
            if best_candidate is not None:
                proactive = True

        if emergency or proactive:
            if best_candidate is None:
                # Fallback to factory logic
                best_score = -1.0
                for nb in adjacent(hq.position):
                    if nb not in own_positions:
                        continue
                    nb_plant = plant_by_pos.get(nb)
                    if nb_plant is None or nb_plant.is_isolated:
                        continue
                    nb_cell = terraform_by_pos.get(nb)
                    progress = nb_cell.terraformation_progress if nb_cell else 0
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

    def _assign_lodge_attacks_headful(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
        hq: object,
    ) -> None:
        """Aggressively attack beaver lodges that threaten our network.
        Uses remembered lodge positions from previous vision."""
        lodges = list(self._known_lodges) + [b.position for b in state.beavers]
        if not lodges:
            return

        for lodge_pos in set(lodges):
            # Only attack lodges near our network
            min_dist = min(
                (chebyshev(lodge_pos, op) for op in own_positions),
                default=999,
            )
            if min_dist > 8:
                continue

            attackers: list[tuple[Position, Position, int]] = []
            for plant in state.plantations:
                if plant.position in assigned or plant.is_isolated:
                    continue
                exit_point = self._find_exit_point(plant.position, lodge_pos, own_positions, state)
                if exit_point is None:
                    continue
                dmg = 10
                attackers.append((plant.position, exit_point, dmg))

            if not attackers:
                continue

            # Commit 1-3 attackers depending on lodge proximity
            max_attackers = 3 if min_dist <= 4 else 2 if min_dist <= 6 else 1
            for author, exit_point, _ in attackers[:max_attackers]:
                assigned.add(author)
                if exit_point == author:
                    cmd.attack_beaver(author, lodge_pos)
                else:
                    cmd.attack_beaver_via(author, exit_point, lodge_pos)

    def _count_safe_hq_neighbors(self, hq, own_positions: set[Position], state: GameState) -> int:
        safe = 0
        for nb in adjacent(hq.position):
            if nb not in own_positions:
                continue
            cell = next((c for c in state.terraformed_cells if c.position == nb), None)
            progress = cell.terraformation_progress if cell else 0
            if progress >= 55:
                continue
            beaver_dist = self._nearest_beaver_distance(nb, state.beavers)
            if beaver_dist <= 2:
                continue
            safe += 1
        return safe

    def _nearest_beaver_distance(self, pos: Position, beavers: list) -> int:
        dist = 999
        for b in beavers:
            dist = min(dist, chebyshev(pos, b.position))
        return dist
