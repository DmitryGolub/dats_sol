from __future__ import annotations

from collections import defaultdict

from api.models import Command, GameState, Plantation, Position, TerraformCell
from strategy.base import BaseStrategy


def _adjacent(pos: Position) -> list[Position]:
    x, y = pos
    return [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]


def _chebyshev(a: Position, b: Position) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def _manhattan(a: Position, b: Position) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _is_reinforced(pos: Position) -> bool:
    return pos[0] % 7 == 0 and pos[1] % 7 == 0


class _BenchmarkBase(BaseStrategy):
    name = "bench_base"
    upgrade_priority: list[str] = [
        "settlement_limit",
        "signal_range",
        "repair_power",
        "decay_mitigation",
        "max_hp",
        "vision_range",
        "beaver_damage_mitigation",
        "earthquake_mitigation",
    ]
    repair_threshold = 0.65
    critical_repair_threshold = 0.85
    center_weight = 18.0
    reinforced_bonus = 90.0
    reinforced_ring_1_bonus = 42.0
    reinforced_ring_2_bonus = 20.0
    branch_penalty = 18.0
    branch_distance_penalty = 3.0
    leaf_beaver_penalty = 36.0
    inner_beaver_penalty = 110.0
    bridge_bonus = 18.0
    dense_bonus = 12.0
    lodge_commit_turn = 340
    late_lodge_bias = 1.0

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

        hq = next((p for p in state.plantations if p.is_main), None)
        if hq is None:
            return cmd

        own_positions = {p.position for p in state.plantations}
        plant_by_pos = {p.position: p for p in state.plantations}

        self._apply_upgrade(state, cmd)

        assigned: set[Position] = set()
        self._assign_repairs(state, cmd, assigned, own_positions, hq)
        self._assign_lodge_finishes(state, cmd, assigned, own_positions, hq)
        self._assign_builds(state, cmd, assigned, own_positions, hq)
        self._maybe_relocate_hq(state, cmd, own_positions, plant_by_pos, hq)
        return cmd

    def _init(self, state: GameState) -> None:
        self._mountains = set(state.mountains)
        self._map_size = state.map_size
        self._initialized = True

    def _apply_upgrade(self, state: GameState, cmd: Command) -> None:
        upgrades = state.plantation_upgrades
        if upgrades is None or upgrades.points <= 0:
            return
        tier_map = {tier.name: tier for tier in upgrades.tiers}
        for name in self.upgrade_priority:
            tier = tier_map.get(name)
            if tier and tier.current < tier.max:
                cmd.upgrade_plantation(name)
                return

    def _assign_repairs(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
        hq: Plantation,
    ) -> None:
        max_hp = 50
        if state.plantation_upgrades:
            for tier in state.plantation_upgrades.tiers:
                if tier.name == "max_hp":
                    max_hp = 50 + tier.current * 10
                    break

        critical = {
            p.position
            for p in state.plantations
            if self._is_critical(p.position, own_positions, hq.position)
        }
        damaged = sorted(
            [
                p for p in state.plantations
                if not p.is_isolated and (
                    p.hp < max_hp * self.repair_threshold
                    or (p.position in critical and p.hp < max_hp * self.critical_repair_threshold)
                )
            ],
            key=lambda p: (
                0 if p.is_main else 1,
                0 if p.position in critical else 1,
                self._nearest_beaver_distance(p.position, state.beavers),
                p.hp,
            ),
        )

        for target in damaged:
            if target.position in assigned:
                continue
            best_author: Position | None = None
            best_exit: Position | None = None
            best_dist = 999
            for plant in state.plantations:
                if plant.position in assigned or plant.is_isolated or plant.position == target.position:
                    continue
                if _chebyshev(plant.position, target.position) <= state.action_range:
                    dist = _chebyshev(plant.position, target.position)
                    if dist < best_dist:
                        best_author = plant.position
                        best_exit = plant.position
                        best_dist = dist
                        continue
                exit_point = self._find_exit_point(plant.position, target.position, own_positions, state)
                if exit_point is not None:
                    dist = _chebyshev(exit_point, target.position)
                    if dist < best_dist:
                        best_author = plant.position
                        best_exit = exit_point
                        best_dist = dist
            if best_author is None or best_exit is None:
                continue
            assigned.add(best_author)
            if best_exit == best_author:
                cmd.repair(best_author, target.position)
            else:
                cmd.repair_via(best_author, best_exit, target.position)

    def _assign_lodge_finishes(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
        hq: Plantation,
    ) -> None:
        if not state.beavers:
            return
        for beaver in sorted(state.beavers, key=lambda b: (b.hp, _chebyshev(hq.position, b.position))):
            attackers = self._collect_attackers(state, assigned, own_positions, beaver.position)
            if not attackers:
                continue
            total_damage = sum(dmg for _, _, dmg in attackers)
            hq_threat = _chebyshev(hq.position, beaver.position) <= 2
            if not hq_threat and (state.turn_no < self.lodge_commit_turn or total_damage < beaver.hp):
                continue
            committed = 0
            for author, exit_point, dmg in attackers:
                if committed >= beaver.hp and not hq_threat:
                    break
                assigned.add(author)
                committed += dmg
                if exit_point == author:
                    cmd.attack_beaver(author, beaver.position)
                else:
                    cmd.attack_beaver_via(author, exit_point, beaver.position)

    def _collect_attackers(
        self,
        state: GameState,
        assigned: set[Position],
        own_positions: set[Position],
        target: Position,
    ) -> list[tuple[Position, Position, int]]:
        candidates: list[tuple[int, int, Position, Position]] = []
        for plant in state.plantations:
            if plant.position in assigned or plant.is_isolated:
                continue
            exit_point = self._find_exit_point(plant.position, target, own_positions, state)
            if exit_point is None or _chebyshev(exit_point, target) > state.action_range:
                continue
            danger = 0 if _chebyshev(plant.position, target) <= 2 else 1
            candidates.append((_chebyshev(exit_point, target), danger, plant.position, exit_point))
        candidates.sort()

        exit_usage: defaultdict[Position, int] = defaultdict(int)
        attacks: list[tuple[Position, Position, int]] = []
        for _, _, author, exit_point in candidates:
            damage = max(0, 5 - exit_usage[exit_point])
            if damage <= 0:
                continue
            exit_usage[exit_point] += 1
            attacks.append((author, exit_point, damage))
        return attacks

    def _assign_builds(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
        hq: Plantation,
    ) -> None:
        frontier = self._score_frontier(state, own_positions, hq)
        per_target: dict[Position, int] = defaultdict(int)
        for target_pos, _ in frontier:
            if per_target[target_pos] >= 3:
                continue
            best_author: Position | None = None
            best_exit: Position | None = None
            best_dist = 999
            for plant in state.plantations:
                if plant.position in assigned or plant.is_isolated:
                    continue
                if _chebyshev(plant.position, target_pos) <= state.action_range:
                    dist = _chebyshev(plant.position, target_pos)
                    if dist < best_dist:
                        best_author = plant.position
                        best_exit = plant.position
                        best_dist = dist
                        continue
                exit_point = self._find_exit_point(plant.position, target_pos, own_positions, state)
                if exit_point is not None:
                    dist = _chebyshev(exit_point, target_pos)
                    if dist < best_dist:
                        best_author = plant.position
                        best_exit = exit_point
                        best_dist = dist
            if best_author is None or best_exit is None:
                continue
            assigned.add(best_author)
            per_target[target_pos] += 1
            if best_exit == best_author:
                cmd.build(best_author, target_pos)
            else:
                cmd.build_via(best_author, best_exit, target_pos)

    def _score_frontier(
        self,
        state: GameState,
        own_positions: set[Position],
        hq: Plantation,
    ) -> list[tuple[Position, float]]:
        width, height = self._map_size
        center = (width // 2, height // 2)
        max_dist = max(width, height)
        beaver_positions = [b.position for b in state.beavers]
        candidates: dict[Position, float] = {}

        for con in state.constructions:
            own_neighbors = sum(1 for n in _adjacent(con.position) if n in own_positions)
            score = 100 + con.progress * 2.5
            score += self._cell_shape_score(con.position, own_neighbors, own_positions, hq.position, center, max_dist)
            score += self._beaver_penalty(con.position, own_neighbors, beaver_positions, state.turn_no)
            candidates[con.position] = score

        for pos in own_positions:
            for nb in _adjacent(pos):
                if nb in own_positions or nb in self._mountains or not (0 <= nb[0] < width and 0 <= nb[1] < height):
                    continue
                own_neighbors = sum(1 for n in _adjacent(nb) if n in own_positions)
                score = self._cell_shape_score(nb, own_neighbors, own_positions, hq.position, center, max_dist)
                score += self._beaver_penalty(nb, own_neighbors, beaver_positions, state.turn_no)
                if nb not in candidates or score > candidates[nb]:
                    candidates[nb] = score

        return sorted(candidates.items(), key=lambda item: -item[1])

    def _cell_shape_score(
        self,
        pos: Position,
        own_neighbors: int,
        own_positions: set[Position],
        hq_pos: Position,
        center: Position,
        max_dist: int,
    ) -> float:
        score = self.reinforced_bonus if _is_reinforced(pos) else self._reinforced_ring_bonus(pos)
        score += self.center_weight * (1 - _manhattan(pos, center) / (max_dist * 2))
        if own_neighbors >= 2:
            score += self.bridge_bonus + own_neighbors * self.dense_bonus
        else:
            score -= self.branch_penalty + max(0, _manhattan(pos, hq_pos) - 4) * self.branch_distance_penalty
        if self._is_critical(pos, own_positions, hq_pos):
            score -= self.branch_penalty * 0.7
        return score

    def _reinforced_ring_bonus(self, pos: Position) -> float:
        rx = pos[0] - pos[0] % 7
        ry = pos[1] - pos[1] % 7
        best = 999
        for rfx in (rx, rx + 7):
            for rfy in (ry, ry + 7):
                if (rfx, rfy) in self._mountains:
                    continue
                if 0 <= rfx < self._map_size[0] and 0 <= rfy < self._map_size[1]:
                    best = min(best, _chebyshev(pos, (rfx, rfy)))
        if best == 1:
            return self.reinforced_ring_1_bonus
        if best == 2:
            return self.reinforced_ring_2_bonus
        return 0.0

    def _beaver_penalty(
        self,
        pos: Position,
        own_neighbors: int,
        beaver_positions: list[Position],
        turn_no: int,
    ) -> float:
        penalty = 0.0
        for bp in beaver_positions:
            dist = _chebyshev(pos, bp)
            if dist <= 2:
                penalty -= self.inner_beaver_penalty
                break
            if dist <= 4:
                penalty -= self.leaf_beaver_penalty if own_neighbors <= 1 else self.leaf_beaver_penalty * 0.5
        if turn_no >= self.lodge_commit_turn:
            penalty *= self.late_lodge_bias
        return penalty

    def _find_exit_point(
        self,
        author: Position,
        target: Position,
        own_positions: set[Position],
        state: GameState,
    ) -> Position | None:
        if _chebyshev(author, target) <= state.action_range:
            return author

        signal_range = 3
        if state.plantation_upgrades:
            for tier in state.plantation_upgrades.tiers:
                if tier.name == "signal_range":
                    signal_range = 3 + tier.current
                    break

        best: Position | None = None
        best_dist = 999
        for pos in own_positions:
            if pos == author or _chebyshev(author, pos) > signal_range or _chebyshev(pos, target) > state.action_range:
                continue
            dist = _chebyshev(pos, target)
            if dist < best_dist:
                best = pos
                best_dist = dist
        return best

    def _nearest_beaver_distance(self, pos: Position, beavers: list) -> int:
        if not beavers:
            return 999
        return min(_chebyshev(pos, b.position) for b in beavers)

    def _is_critical(self, pos: Position, own_positions: set[Position], hq_pos: Position) -> bool:
        if pos == hq_pos:
            return True
        neighbor_count = sum(1 for n in _adjacent(pos) if n in own_positions)
        if neighbor_count <= 1:
            return False
        axis_x = any((pos[0] + dx, pos[1]) in own_positions for dx in (-1, 1))
        axis_y = any((pos[0], pos[1] + dy) in own_positions for dy in (-1, 1))
        return not (axis_x and axis_y)

    def _maybe_relocate_hq(
        self,
        state: GameState,
        cmd: Command,
        own_positions: set[Position],
        plant_by_pos: dict[Position, Plantation],
        hq: Plantation,
    ) -> None:
        if len(state.plantations) < 2:
            return
        terraform_progress = 0
        for cell in state.terraformed_cells:
            if cell.position == hq.position:
                terraform_progress = cell.terraformation_progress
                break
        if terraform_progress < 70 and hq.hp >= 25 and self._nearest_beaver_distance(hq.position, state.beavers) > 2:
            return
        best_candidate: Position | None = None
        best_score = -1.0
        for nb_pos in _adjacent(hq.position):
            if nb_pos not in own_positions:
                continue
            nb = plant_by_pos.get(nb_pos)
            if nb is None or nb.is_isolated:
                continue
            neighbor_count = sum(1 for n in _adjacent(nb_pos) if n in own_positions)
            beaver_dist = self._nearest_beaver_distance(nb_pos, state.beavers)
            score = nb.hp * 2 + neighbor_count * 10 + beaver_dist * 8
            if self._is_critical(nb_pos, own_positions, hq.position):
                score -= 10
            if score > best_score:
                best_score = score
                best_candidate = nb_pos
        if best_candidate is not None:
            cmd.relocate_main(hq.position, best_candidate)


class BenchmarkReinforcedBot(_BenchmarkBase):
    name = "bench_reinf"
    center_weight = 14.0
    reinforced_bonus = 110.0
    reinforced_ring_1_bonus = 54.0
    reinforced_ring_2_bonus = 22.0
    branch_penalty = 12.0
    branch_distance_penalty = 2.0
    inner_beaver_penalty = 95.0
    late_lodge_bias = 0.7


class BenchmarkStableBot(_BenchmarkBase):
    name = "bench_stable"
    repair_threshold = 0.72
    critical_repair_threshold = 0.92
    center_weight = 22.0
    branch_penalty = 22.0
    branch_distance_penalty = 3.8
    bridge_bonus = 24.0
    dense_bonus = 15.0
    inner_beaver_penalty = 120.0
    leaf_beaver_penalty = 44.0


class BenchmarkOverdriveBot(_BenchmarkBase):
    name = "bench_overdrive"
    upgrade_priority = [
        "settlement_limit",
        "repair_power",
        "signal_range",
        "decay_mitigation",
        "max_hp",
        "vision_range",
        "beaver_damage_mitigation",
        "earthquake_mitigation",
    ]
    repair_threshold = 0.52
    critical_repair_threshold = 0.72
    center_weight = 10.0
    reinforced_bonus = 130.0
    reinforced_ring_1_bonus = 60.0
    reinforced_ring_2_bonus = 24.0
    branch_penalty = 8.0
    branch_distance_penalty = 1.0
    inner_beaver_penalty = 70.0
    leaf_beaver_penalty = 18.0
    lodge_commit_turn = 280
    late_lodge_bias = 0.5


class BenchmarkMillionBot(_BenchmarkBase):
    name = "bench_million"
    upgrade_priority = [
        "settlement_limit",
        "repair_power",
        "signal_range",
        "decay_mitigation",
        "max_hp",
        "vision_range",
        "beaver_damage_mitigation",
        "earthquake_mitigation",
    ]
    repair_threshold = 0.54
    critical_repair_threshold = 0.82
    center_weight = 10.0
    reinforced_bonus = 150.0
    reinforced_ring_1_bonus = 75.0
    reinforced_ring_2_bonus = 28.0
    branch_penalty = 10.0
    branch_distance_penalty = 1.1
    leaf_beaver_penalty = 20.0
    inner_beaver_penalty = 72.0
    bridge_bonus = 20.0
    dense_bonus = 10.0
    lodge_commit_turn = 420
    late_lodge_bias = 0.45

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
        productive_mass = self._compute_productive_mass(own_positions, terraform_by_pos)

        self._apply_upgrade(state, cmd)

        assigned: set[Position] = set()
        self._assign_repairs_million(state, cmd, assigned, own_positions, hq, terraform_by_pos, productive_mass)
        self._assign_lodge_finishes(state, cmd, assigned, own_positions, hq)
        self._assign_builds_million(state, cmd, assigned, own_positions, hq, terraform_by_pos, productive_mass)
        self._maybe_relocate_hq_million(state, cmd, own_positions, plant_by_pos, hq, productive_mass, terraform_by_pos)
        return cmd

    def _phase(self, turn_no: int) -> str:
        if turn_no < 140:
            return "expand"
        if turn_no < 360:
            return "cycle"
        return "harvest"

    def _apply_upgrade(self, state: GameState, cmd: Command) -> None:
        upgrades = state.plantation_upgrades
        if upgrades is None or upgrades.points <= 0:
            return
        phase = self._phase(state.turn_no)
        orders = {
            "expand": ["settlement_limit", "repair_power", "signal_range", "decay_mitigation", "max_hp", "vision_range", "beaver_damage_mitigation", "earthquake_mitigation"],
            "cycle": ["settlement_limit", "signal_range", "decay_mitigation", "repair_power", "max_hp", "vision_range", "beaver_damage_mitigation", "earthquake_mitigation"],
            "harvest": ["decay_mitigation", "repair_power", "settlement_limit", "max_hp", "signal_range", "vision_range", "beaver_damage_mitigation", "earthquake_mitigation"],
        }
        tier_map = {tier.name: tier for tier in upgrades.tiers}
        for name in orders[phase]:
            tier = tier_map.get(name)
            if tier and tier.current < tier.max:
                cmd.upgrade_plantation(name)
                return

    def _assign_repairs_million(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
        hq: Plantation,
        terraform_by_pos: dict[Position, TerraformCell],
        productive_mass: dict[Position, float],
    ) -> None:
        max_hp = 50
        if state.plantation_upgrades:
            for tier in state.plantation_upgrades.tiers:
                if tier.name == "max_hp":
                    max_hp = 50 + tier.current * 10
                    break
        damaged = sorted(
            [
                p for p in state.plantations
                if not p.is_isolated and (
                    p.hp < max_hp * self.repair_threshold
                    or (
                        (self._is_critical(p.position, own_positions, hq.position) or productive_mass.get(p.position, 0.0) >= 220)
                        and p.hp < max_hp * self.critical_repair_threshold
                    )
                )
            ],
            key=lambda p: (
                0 if p.is_main else 1,
                -productive_mass.get(p.position, 0.0),
                self._nearest_beaver_distance(p.position, state.beavers),
                p.hp,
            ),
        )

        for target in damaged:
            if target.position in assigned:
                continue
            best_author: Position | None = None
            best_exit: Position | None = None
            best_dist = 999
            for plant in state.plantations:
                if plant.position in assigned or plant.is_isolated or plant.position == target.position:
                    continue
                if _chebyshev(plant.position, target.position) <= state.action_range:
                    dist = _chebyshev(plant.position, target.position)
                    if dist < best_dist:
                        best_author = plant.position
                        best_exit = plant.position
                        best_dist = dist
                        continue
                exit_point = self._find_exit_point(plant.position, target.position, own_positions, state)
                if exit_point is not None:
                    dist = _chebyshev(exit_point, target.position)
                    if dist < best_dist:
                        best_author = plant.position
                        best_exit = exit_point
                        best_dist = dist
            if best_author is None or best_exit is None:
                continue
            assigned.add(best_author)
            if best_exit == best_author:
                cmd.repair(best_author, target.position)
            else:
                cmd.repair_via(best_author, best_exit, target.position)

    def _assign_builds_million(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
        hq: Plantation,
        terraform_by_pos: dict[Position, TerraformCell],
        productive_mass: dict[Position, float],
    ) -> None:
        frontier = self._score_frontier_million(state, own_positions, hq, terraform_by_pos, productive_mass)
        per_target: dict[Position, int] = defaultdict(int)
        for target_pos, score in frontier:
            cap = self._builder_cap(target_pos, terraform_by_pos.get(target_pos), state.turn_no, score)
            if per_target[target_pos] >= cap:
                continue
            best_author: Position | None = None
            best_exit: Position | None = None
            best_dist = 999
            for plant in state.plantations:
                if plant.position in assigned or plant.is_isolated:
                    continue
                if _chebyshev(plant.position, target_pos) <= state.action_range:
                    dist = _chebyshev(plant.position, target_pos)
                    if dist < best_dist:
                        best_author = plant.position
                        best_exit = plant.position
                        best_dist = dist
                        continue
                exit_point = self._find_exit_point(plant.position, target_pos, own_positions, state)
                if exit_point is not None:
                    dist = _chebyshev(exit_point, target_pos)
                    if dist < best_dist:
                        best_author = plant.position
                        best_exit = exit_point
                        best_dist = dist
            if best_author is None or best_exit is None:
                continue
            assigned.add(best_author)
            per_target[target_pos] += 1
            if best_exit == best_author:
                cmd.build(best_author, target_pos)
            else:
                cmd.build_via(best_author, best_exit, target_pos)

    def _score_frontier_million(
        self,
        state: GameState,
        own_positions: set[Position],
        hq: Plantation,
        terraform_by_pos: dict[Position, TerraformCell],
        productive_mass: dict[Position, float],
    ) -> list[tuple[Position, float]]:
        width, height = self._map_size
        center = (width // 2, height // 2)
        max_dist = max(width, height)
        beaver_positions = [b.position for b in state.beavers]
        phase = self._phase(state.turn_no)
        candidates: dict[Position, float] = {}

        for con in state.constructions:
            own_neighbors = sum(1 for n in _adjacent(con.position) if n in own_positions)
            score = 120.0 + con.progress * 3.5
            score += self._million_cell_score(
                pos=con.position,
                own_neighbors=own_neighbors,
                own_positions=own_positions,
                terraform=terraform_by_pos.get(con.position),
                productive_mass=productive_mass,
                hq_pos=hq.position,
                center=center,
                max_dist=max_dist,
                beaver_positions=beaver_positions,
                turn_no=state.turn_no,
                phase=phase,
                is_existing=True,
            )
            candidates[con.position] = score

        for pos in own_positions:
            for nb in _adjacent(pos):
                if nb in own_positions or nb in self._mountains or not (0 <= nb[0] < width and 0 <= nb[1] < height):
                    continue
                own_neighbors = sum(1 for n in _adjacent(nb) if n in own_positions)
                score = self._million_cell_score(
                    pos=nb,
                    own_neighbors=own_neighbors,
                    own_positions=own_positions,
                    terraform=terraform_by_pos.get(nb),
                    productive_mass=productive_mass,
                    hq_pos=hq.position,
                    center=center,
                    max_dist=max_dist,
                    beaver_positions=beaver_positions,
                    turn_no=state.turn_no,
                    phase=phase,
                    is_existing=False,
                )
                if nb not in candidates or score > candidates[nb]:
                    candidates[nb] = score

        for pos, cell in terraform_by_pos.items():
            if pos in own_positions or pos in self._mountains or not (0 <= pos[0] < width and 0 <= pos[1] < height):
                continue
            if not any(nb in own_positions for nb in _adjacent(pos)):
                continue
            own_neighbors = sum(1 for n in _adjacent(pos) if n in own_positions)
            score = self._million_cell_score(
                pos=pos,
                own_neighbors=own_neighbors,
                own_positions=own_positions,
                terraform=cell,
                productive_mass=productive_mass,
                hq_pos=hq.position,
                center=center,
                max_dist=max_dist,
                beaver_positions=beaver_positions,
                turn_no=state.turn_no,
                phase=phase,
                is_existing=False,
            )
            if pos not in candidates or score > candidates[pos]:
                candidates[pos] = score

        return sorted(candidates.items(), key=lambda item: -item[1])

    def _million_cell_score(
        self,
        *,
        pos: Position,
        own_neighbors: int,
        own_positions: set[Position],
        terraform: TerraformCell | None,
        productive_mass: dict[Position, float],
        hq_pos: Position,
        center: Position,
        max_dist: int,
        beaver_positions: list[Position],
        turn_no: int,
        phase: str,
        is_existing: bool,
    ) -> float:
        remaining_turns = 600 - turn_no
        score = self.reinforced_bonus if _is_reinforced(pos) else self._reinforced_ring_bonus(pos)
        score += self.center_weight * (1 - _manhattan(pos, center) / (max_dist * 2))
        if own_neighbors >= 2:
            score += self.bridge_bonus + own_neighbors * self.dense_bonus
        else:
            score -= self.branch_penalty + max(0, _manhattan(pos, hq_pos) - 4) * self.branch_distance_penalty
        if self._is_critical(pos, own_positions, hq_pos):
            score -= self.branch_penalty * 0.4

        score += productive_mass.get(pos, 0.0) * 0.1

        if terraform is not None:
            progress = terraform.terraformation_progress
            score += progress * (1.0 if phase == "expand" else 1.7 if phase == "cycle" else 2.4)
            if progress >= 100:
                if terraform.turns_until_degradation <= (5 if phase == "cycle" else 10):
                    score += 320 if _is_reinforced(pos) else 220
                    score += max(0, 10 - terraform.turns_until_degradation) * (40 if _is_reinforced(pos) else 24)
                elif phase == "harvest":
                    score -= max(0, terraform.turns_until_degradation - 10) * 3
            elif progress >= 80:
                score += 180
            elif progress >= 50:
                score += 90
        elif phase == "harvest":
            score -= max(0, _manhattan(pos, hq_pos) - 3) * 6

        if phase == "harvest":
            score += remaining_turns * 0.15
            if terraform is None:
                score -= 70
        elif phase == "cycle" and terraform is None and _manhattan(pos, hq_pos) > 8:
            score -= 45

        penalty = 0.0
        for bp in beaver_positions:
            dist = _chebyshev(pos, bp)
            if dist <= 2:
                penalty -= self.inner_beaver_penalty
                break
            if dist <= 4:
                penalty -= self.leaf_beaver_penalty if own_neighbors <= 1 else self.leaf_beaver_penalty * 0.5
        if turn_no >= self.lodge_commit_turn:
            penalty *= self.late_lodge_bias
        score += penalty

        if is_existing:
            score += 35.0
        return score

    def _builder_cap(self, pos: Position, terraform: TerraformCell | None, turn_no: int, score: float) -> int:
        if terraform is not None:
            if terraform.terraformation_progress >= 100 and terraform.turns_until_degradation <= 8:
                return 5
            if terraform.terraformation_progress >= 80:
                return 4
        if _is_reinforced(pos):
            return 4
        if score >= 350:
            return 4
        return 3

    def _compute_productive_mass(
        self,
        own_positions: set[Position],
        terraform_by_pos: dict[Position, TerraformCell],
    ) -> dict[Position, float]:
        mass: dict[Position, float] = {}
        for pos in own_positions:
            value = 120.0 if _is_reinforced(pos) else 0.0
            cell = terraform_by_pos.get(pos)
            if cell is not None:
                value += cell.terraformation_progress * 3.0
            for nb in _adjacent(pos):
                neighbor_cell = terraform_by_pos.get(nb)
                if neighbor_cell is not None:
                    value += neighbor_cell.terraformation_progress * 0.9
            mass[pos] = value
        return mass

    def _maybe_relocate_hq_million(
        self,
        state: GameState,
        cmd: Command,
        own_positions: set[Position],
        plant_by_pos: dict[Position, Plantation],
        hq: Plantation,
        productive_mass: dict[Position, float],
        terraform_by_pos: dict[Position, TerraformCell],
    ) -> None:
        if len(state.plantations) < 2:
            return
        hq_progress = terraform_by_pos.get(hq.position).terraformation_progress if hq.position in terraform_by_pos else 0
        if hq_progress < 65 and hq.hp >= 25 and self._nearest_beaver_distance(hq.position, state.beavers) > 2:
            return
        best_candidate: Position | None = None
        best_score = -1.0
        for nb_pos in _adjacent(hq.position):
            if nb_pos not in own_positions:
                continue
            nb = plant_by_pos.get(nb_pos)
            if nb is None or nb.is_isolated:
                continue
            progress = terraform_by_pos.get(nb_pos).terraformation_progress if nb_pos in terraform_by_pos else 0
            neighbor_count = sum(1 for n in _adjacent(nb_pos) if n in own_positions)
            score = productive_mass.get(nb_pos, 0.0) + nb.hp * 2 + neighbor_count * 8 - progress
            score += self._nearest_beaver_distance(nb_pos, state.beavers) * 6
            if score > best_score:
                best_score = score
                best_candidate = nb_pos
        if best_candidate is not None:
            cmd.relocate_main(hq.position, best_candidate)


class BenchmarkBlobBot(_BenchmarkBase):
    name = "bench_blob"
    upgrade_priority = [
        "settlement_limit",
        "repair_power",
        "signal_range",
        "max_hp",
        "decay_mitigation",
        "vision_range",
        "beaver_damage_mitigation",
        "earthquake_mitigation",
    ]
    repair_threshold = 0.45
    critical_repair_threshold = 0.7
    center_weight = 24.0
    reinforced_bonus = 70.0
    reinforced_ring_1_bonus = 20.0
    reinforced_ring_2_bonus = 8.0
    branch_penalty = 5.0
    branch_distance_penalty = 0.5
    leaf_beaver_penalty = 22.0
    inner_beaver_penalty = 80.0
    bridge_bonus = 28.0
    dense_bonus = 20.0
    lodge_commit_turn = 500
    late_lodge_bias = 0.3

    def _cell_shape_score(
        self,
        pos: Position,
        own_neighbors: int,
        own_positions: set[Position],
        hq_pos: Position,
        center: Position,
        max_dist: int,
    ) -> float:
        score = self.reinforced_bonus if _is_reinforced(pos) else self._reinforced_ring_bonus(pos)
        score += self.center_weight * (1 - _manhattan(pos, center) / (max_dist * 2))
        score += own_neighbors * self.dense_bonus
        if own_neighbors == 1:
            score -= self.branch_penalty
        if own_neighbors == 0:
            score -= self.branch_penalty * 4 + max(0, _manhattan(pos, hq_pos) - 3) * self.branch_distance_penalty
        if self._is_critical(pos, own_positions, hq_pos):
            score -= 4
        return score


class BenchmarkFactoryBot(_BenchmarkBase):
    name = "bench_factory"
    upgrade_priority = [
        "repair_power",
        "settlement_limit",
        "signal_range",
        "max_hp",
        "decay_mitigation",
        "vision_range",
        "beaver_damage_mitigation",
        "earthquake_mitigation",
    ]
    repair_threshold = 0.38
    critical_repair_threshold = 0.62
    center_weight = 20.0
    reinforced_bonus = 45.0
    reinforced_ring_1_bonus = 16.0
    reinforced_ring_2_bonus = 6.0
    branch_penalty = 2.0
    branch_distance_penalty = 0.2
    leaf_beaver_penalty = 18.0
    inner_beaver_penalty = 70.0
    bridge_bonus = 24.0
    dense_bonus = 26.0
    lodge_commit_turn = 999
    late_lodge_bias = 0.0

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

        assigned: set[Position] = set()
        self._assign_repairs_factory(state, cmd, assigned, own_positions, hq, terraform_by_pos)
        self._assign_builds_factory(state, cmd, assigned, own_positions, hq, terraform_by_pos)
        self._maybe_relocate_hq_factory(state, cmd, own_positions, plant_by_pos, hq, terraform_by_pos)
        return cmd

    def _assign_repairs_factory(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
        hq: Plantation,
        terraform_by_pos: dict[Position, TerraformCell],
    ) -> None:
        max_hp = 50
        if state.plantation_upgrades:
            for tier in state.plantation_upgrades.tiers:
                if tier.name == "max_hp":
                    max_hp = 50 + tier.current * 10
                    break

        damaged = sorted(
            [
                p for p in state.plantations
                if not p.is_isolated and (
                    p.hp < max_hp * self.repair_threshold
                    or (
                        (
                            p.is_main
                            or self._factory_mass(p.position, own_positions, terraform_by_pos) >= 260
                            or self._is_critical(p.position, own_positions, hq.position)
                        )
                        and p.hp < max_hp * self.critical_repair_threshold
                    )
                )
            ],
            key=lambda p: (
                0 if p.is_main else 1,
                -self._factory_mass(p.position, own_positions, terraform_by_pos),
                p.hp,
            ),
        )

        for target in damaged:
            best_author: Position | None = None
            best_exit: Position | None = None
            best_quality = -1.0
            for plant in state.plantations:
                if plant.position in assigned or plant.is_isolated or plant.position == target.position:
                    continue
                exit_point = self._best_factory_exit(plant.position, target.position, own_positions, state)
                if exit_point is None:
                    continue
                quality = 100 - _chebyshev(exit_point, target.position)
                quality += self._factory_mass(plant.position, own_positions, terraform_by_pos) * 0.02
                if quality > best_quality:
                    best_author = plant.position
                    best_exit = exit_point
                    best_quality = quality
            if best_author is None or best_exit is None:
                continue
            assigned.add(best_author)
            if best_exit == best_author:
                cmd.repair(best_author, target.position)
            else:
                cmd.repair_via(best_author, best_exit, target.position)

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
        used_exits: dict[Position, int] = defaultdict(int)
        limit = self._plantation_limit(state)
        current_count = len(state.plantations)
        overbuild_allowance = 10 if state.turn_no < 120 else 6 if state.turn_no < 280 else 3
        immediate_budget = max(1, limit + overbuild_allowance - current_count)
        pipeline_cap = max(8, current_count // 2 + 8)
        staged_count = len(state.constructions)
        births_committed = 0

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

    def _factory_targets(
        self,
        state: GameState,
        own_positions: set[Position],
        hq: Plantation,
        terraform_by_pos: dict[Position, TerraformCell],
    ) -> list[tuple[Position, float]]:
        width, height = self._map_size
        center = (width // 2, height // 2)
        construction_by_pos = {c.position: c for c in state.constructions}
        beaver_positions = [b.position for b in state.beavers]
        candidates: dict[Position, float] = {}
        hq_escape_neighbors = 0
        for nb in _adjacent(hq.position):
            if nb in own_positions:
                cell = terraform_by_pos.get(nb)
                progress = cell.terraformation_progress if cell is not None else 0
                if progress <= 45:
                    hq_escape_neighbors += 1

        for con in state.constructions:
            own_neighbors = sum(1 for n in _adjacent(con.position) if n in own_positions)
            reach = self._reachable_authors_count(state, own_positions, con.position)
            score = 300 + con.progress * 12 + reach * 18 + own_neighbors * self.dense_bonus
            score += self._factory_target_shape(con.position, own_neighbors, own_positions, hq.position, center, beaver_positions)
            if _manhattan(con.position, hq.position) == 1 and hq_escape_neighbors < 2:
                score += 220
            cell = terraform_by_pos.get(con.position)
            if cell is not None and cell.terraformation_progress >= 100:
                score += 120 - cell.turns_until_degradation * 4
            candidates[con.position] = score

        for pos in own_positions:
            for nb in _adjacent(pos):
                if nb in own_positions or nb in self._mountains:
                    continue
                if not (0 <= nb[0] < width and 0 <= nb[1] < height):
                    continue
                own_neighbors = sum(1 for n in _adjacent(nb) if n in own_positions)
                if own_neighbors == 0:
                    continue
                reach = self._reachable_authors_count(state, own_positions, nb)
                if reach == 0:
                    continue
                score = reach * 14 + own_neighbors * self.dense_bonus
                score += self._factory_target_shape(nb, own_neighbors, own_positions, hq.position, center, beaver_positions)
                if _manhattan(nb, hq.position) == 1 and hq_escape_neighbors < 2:
                    score += 260
                cell = terraform_by_pos.get(nb)
                if cell is not None and cell.terraformation_progress >= 100:
                    score += 160 - cell.turns_until_degradation * 6
                elif cell is not None:
                    score += cell.terraformation_progress * 2.5
                candidates[nb] = max(candidates.get(nb, -10_000.0), score)

        return sorted(candidates.items(), key=lambda item: -item[1])

    def _factory_target_shape(
        self,
        pos: Position,
        own_neighbors: int,
        own_positions: set[Position],
        hq_pos: Position,
        center: Position,
        beaver_positions: list[Position],
    ) -> float:
        score = own_neighbors * self.dense_bonus
        score += self.center_weight * (1 - _manhattan(pos, center) / max(1, self._map_size[0] + self._map_size[1]))
        score += self.reinforced_bonus if _is_reinforced(pos) else self._reinforced_ring_bonus(pos)
        score -= max(0, _manhattan(pos, hq_pos) - 8) * self.branch_distance_penalty
        if own_neighbors <= 1:
            score -= self.branch_penalty
        for bp in beaver_positions:
            dist = _chebyshev(pos, bp)
            if dist <= 2:
                score -= 120
                break
            if dist <= 4:
                score -= 28
        if self._is_critical(pos, own_positions, hq_pos):
            score -= 2
        return score

    def _best_factory_exit(
        self,
        author: Position,
        target: Position,
        own_positions: set[Position],
        state: GameState,
    ) -> Position | None:
        if _chebyshev(author, target) <= state.action_range:
            return author

        signal_range = 3
        if state.plantation_upgrades:
            for tier in state.plantation_upgrades.tiers:
                if tier.name == "signal_range":
                    signal_range = 3 + tier.current
                    break

        best: Position | None = None
        best_score = -1_000.0
        for pos in own_positions:
            if _chebyshev(author, pos) > signal_range or _chebyshev(pos, target) > state.action_range:
                continue
            score = 20 - _chebyshev(pos, target) * 5 - _chebyshev(author, pos)
            if pos == author:
                score += 8
            if score > best_score:
                best = pos
                best_score = score
        return best

    def _reachable_authors_count(
        self,
        state: GameState,
        own_positions: set[Position],
        target: Position,
    ) -> int:
        count = 0
        for plant in state.plantations:
            if plant.is_isolated:
                continue
            if self._best_factory_exit(plant.position, target, own_positions, state) is not None:
                count += 1
        return count

    def _factory_mass(
        self,
        pos: Position,
        own_positions: set[Position],
        terraform_by_pos: dict[Position, TerraformCell],
    ) -> float:
        value = 0.0
        if _is_reinforced(pos):
            value += 80
        cell = terraform_by_pos.get(pos)
        if cell is not None:
            value += cell.terraformation_progress * 2.2
        for nb in _adjacent(pos):
            if nb in own_positions:
                value += 20
            neighbor_cell = terraform_by_pos.get(nb)
            if neighbor_cell is not None:
                value += neighbor_cell.terraformation_progress * 0.6
        return value

    def _construction_speed(self, state: GameState) -> int:
        speed = 5
        if state.plantation_upgrades:
            for tier in state.plantation_upgrades.tiers:
                if tier.name == "repair_power":
                    speed = 5 + tier.current
                    break
        return speed

    def _plantation_limit(self, state: GameState) -> int:
        limit = 30
        if state.plantation_upgrades:
            for tier in state.plantation_upgrades.tiers:
                if tier.name == "settlement_limit":
                    limit = 30 + tier.current
                    break
        return limit

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
        hq_progress = terraform_by_pos.get(hq.position).terraformation_progress if hq.position in terraform_by_pos else 0
        hq_beaver_dist = self._nearest_beaver_distance(hq.position, state.beavers)
        if hq_progress < 40 and hq.hp >= 18 and hq_beaver_dist > 2:
            return

        best_candidate: Position | None = None
        best_score = -1_000.0
        for nb_pos in _adjacent(hq.position):
            nb = plant_by_pos.get(nb_pos)
            if nb is None or nb.is_isolated:
                continue
            progress = terraform_by_pos.get(nb_pos).terraformation_progress if nb_pos in terraform_by_pos else 0
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
