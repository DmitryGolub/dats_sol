from __future__ import annotations

import logging
from collections import defaultdict

from api.models import Command, GameState, Plantation, Position
from api.helpers import Pathfinder
from strategy.base import BaseStrategy

log = logging.getLogger("bot.current")

UPGRADE_PRIORITY = [
    "repair_power",
    "signal_range",
    "max_hp",
    "vision_range",
    "beaver_damage_mitigation",
    "decay_mitigation",
    "settlement_limit",
    "earthquake_mitigation",
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
        self._known_lodges: set[Position] = set()
        self._connected: set[Position] = set()
        self._initialized = False

    def reset(self) -> None:
        self._initialized = False
        self._known_lodges = set()

    def decide(self, state: GameState) -> Command:
        cmd = Command()

        if not state.plantations:
            return cmd

        if not self._initialized:
            self._init(state)

        self._apply_upgrade(state, cmd)

        own_positions = {p.position for p in state.plantations}
        self._connected = {p.position for p in state.plantations if not p.is_isolated}
        plant_by_pos = {p.position: p for p in state.plantations}
        construction_positions = {c.position for c in state.constructions}

        hq = next((p for p in state.plantations if p.is_main), None)
        if hq is None:
            return cmd

        max_hp = self._get_max_hp(state)
        assigned: set[Position] = set()
        exit_usage: dict[Position, int] = defaultdict(int)

        danger_zone = self._lodge_danger_zone(state)

        self._assign_repairs(state, cmd, assigned, exit_usage, plant_by_pos, own_positions, max_hp, hq)
        self._assign_lodge_attacks(state, cmd, assigned, exit_usage, own_positions, danger_zone)
        self._assign_builds(state, cmd, assigned, exit_usage, plant_by_pos, own_positions, construction_positions, hq, danger_zone)
        self._assign_sabotage(state, cmd, assigned, exit_usage, own_positions)
        self._maybe_relocate_hq(state, cmd, hq, own_positions)

        return cmd

    def _lodge_danger_zone(self, state: GameState) -> set[Position]:
        """Cells within lodge AoE radius (2) — avoid building here.

        Uses persistent memory: lodges remain "known" after first sighting
        until we see them missing from a visible position.
        """
        visible = self._visible_cells(state)
        visible_lodge_positions = {b.position for b in state.beavers}
        # Drop known lodges that are now visible-empty (killed or wrong memory)
        self._known_lodges = {
            pos for pos in self._known_lodges
            if pos not in visible or pos in visible_lodge_positions
        }
        self._known_lodges |= visible_lodge_positions

        zone: set[Position] = set()
        for lx, ly in self._known_lodges:
            for dx in range(-2, 3):
                for dy in range(-2, 3):
                    zone.add((lx + dx, ly + dy))
        return zone

    def _visible_cells(self, state: GameState) -> set[Position]:
        vr = 3
        if state.plantation_upgrades:
            for t in state.plantation_upgrades.tiers:
                if t.name == "vision_range":
                    vr = 3 + t.current * 2
                    break
        visible: set[Position] = set()
        for p in state.plantations:
            px, py = p.position
            for dx in range(-vr, vr + 1):
                for dy in range(-vr, vr + 1):
                    visible.add((px + dx, py + dy))
        return visible

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

    def _repair_power_bonus(self, state: GameState) -> int:
        if state.plantation_upgrades:
            for t in state.plantation_upgrades.tiers:
                if t.name == "repair_power":
                    return t.current
        return 0

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
        # Early-game: every non-HQ repair costs a builder turn. Only step in
        # when the plantation is about to die; otherwise let it take hits and
        # keep growing the network.
        early_game = len(state.plantations) <= 5
        non_hq_threshold = 0.3 if early_game else 0.7
        damaged = sorted(
            [
                p for p in state.plantations
                if not p.is_isolated
                and ((p.is_main and p.hp < max_hp) or (not p.is_main and p.hp < max_hp * non_hq_threshold))
            ],
            key=lambda p: (0 if p.is_main else 1, p.hp),
        )

        rs_cap = 5 + self._repair_power_bonus(state)

        for target in damaged:
            best_repairer: Position | None = None
            best_exit: Position | None = None
            best_key = (999, 999)

            for p in state.plantations:
                if p.position in assigned or p.is_isolated or p.position == target.position:
                    continue
                exit_point = self._find_exit_point(p.position, target.position, own_positions, state, exit_usage, max_usage=rs_cap)
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

    def _assign_lodge_attacks(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        exit_usage: dict[Position, int],
        own_positions: set[Position],
        danger_zone: set[Position],
    ) -> None:
        if not state.beavers:
            return
        # Only attack lodges that endanger at least one of our plantations.
        threatening = [b for b in state.beavers if b.position in danger_zone and any(p in danger_zone for p in own_positions)]
        if not threatening:
            return
        # Target the lowest-HP threatening lodge.
        threatening.sort(key=lambda b: b.hp)
        # Never commit more than half our network to a lodge — keep builders alive.
        plantation_count = len(state.plantations)
        attacker_budget = max(1, plantation_count // 2)
        for lodge in threatening:
            # Cap attackers: BE=5 base, lodge regens 5/turn. Need enough to out-damage
            # regen plus clear remaining HP in reasonable turns.
            # ceil(hp / BE) attackers finishes in 1 turn; we allow up to that many.
            be = 5  # base, ignoring per-exit-point decrement
            max_attackers = min(attacker_budget, max(2, min(6, (lodge.hp + be - 1) // be)))
            attackers_used = 0
            for p in state.plantations:
                if attackers_used >= max_attackers:
                    break
                if p.position in assigned or p.is_isolated:
                    continue
                exit_point = self._find_exit_point(p.position, lodge.position, own_positions, state, exit_usage, max_usage=be)
                if exit_point is None:
                    continue
                assigned.add(p.position)
                exit_usage[exit_point] += 1
                attackers_used += 1
                if exit_point == p.position:
                    cmd.attack_beaver(p.position, lodge.position)
                else:
                    cmd.attack_beaver_via(p.position, exit_point, lodge.position)

    def _assign_sabotage(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        exit_usage: dict[Position, int],
        own_positions: set[Position],
    ) -> None:
        """Attack visible enemy plantations with any remaining idle plantations.

        Runs after repairs/builds/lodge-attacks so we only use truly idle units.
        Target lowest-HP first (cheapest kill → score + denial).
        """
        if not state.enemy_plantations:
            return

        # Early game: every idle plantation should be building, not harassing.
        # Sabotage only pays off once our network is established.
        if len(state.plantations) < 6:
            return

        enemies = sorted(state.enemy_plantations, key=lambda e: e.hp)
        # Cap attackers per enemy: SE=5 base. 2-3 is enough to out-damage self-repair.
        max_per_enemy = 3
        se_cap = 5

        for enemy in enemies:
            attackers = 0
            for p in state.plantations:
                if attackers >= max_per_enemy:
                    break
                if p.position in assigned or p.is_isolated:
                    continue
                exit_point = self._find_exit_point(p.position, enemy.position, own_positions, state, exit_usage, max_usage=se_cap)
                if exit_point is None:
                    continue
                assigned.add(p.position)
                exit_usage[exit_point] += 1
                attackers += 1
                if exit_point == p.position:
                    cmd.sabotage(p.position, enemy.position)
                else:
                    cmd.sabotage_via(p.position, exit_point, enemy.position)

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
        danger_zone: set[Position],
    ) -> None:
        frontier = self._score_frontier(state, own_positions, construction_positions, hq, danger_zone)

        target_assignments: dict[Position, int] = defaultdict(int)
        cs_cap = 5 + self._repair_power_bonus(state)

        for target_pos, score in frontier:
            if target_assignments[target_pos] >= 3:
                continue

            best_builder: Position | None = None
            best_exit: Position | None = None
            best_key = (999, 999)

            for p in state.plantations:
                if p.position in assigned or p.is_isolated:
                    continue
                exit_point = self._find_exit_point(p.position, target_pos, own_positions, state, exit_usage, max_usage=cs_cap)
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
                exit_point = self._find_exit_point(p.position, target_pos, own_positions, state, exit_usage, max_usage=cs_cap)
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
        danger_zone: set[Position],
    ) -> list[tuple[Position, float]]:
        w, h = self._map_size
        center = (w // 2, h // 2)
        max_dist = max(w, h)

        candidates: dict[Position, float] = {}

        # Progress map for O(1) urgency lookups: a plantation on a cell with
        # high terraform progress will self-destruct soon; building a neighbor
        # before then keeps the chain from snapping.
        progress_by_pos = {c.position: c.terraformation_progress for c in state.terraformed_cells}

        # Don't let the bot reinforce a construction sitting on (or under) an HQ cell
        # that is close to completing terraformation — HQ dies if the cell hits 100%.
        hq_progress = progress_by_pos.get(hq.position, 0)
        hq_neighbors_count = sum(1 for nb in _adjacent(hq.position) if nb in own_positions)
        hq_adjacent_cells = set(_adjacent(hq.position))
        hq_needs_escape = hq_neighbors_count < 2

        for con in state.constructions:
            if con.position == hq.position and hq_progress > 80:
                continue
            if con.position in danger_zone:
                continue
            score = 100.0 + con.progress * 2
            if hq_needs_escape and con.position in hq_adjacent_cells:
                score += 200
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

        # Only connected cells extend the network — isolated plantations can't
        # route a build command, so their neighbors would also be isolated.
        for pos in self._connected:
            for nb in _adjacent(pos):
                if nb in own_positions or nb in self._mountains or nb in candidates:
                    continue
                if not (0 <= nb[0] < w and 0 <= nb[1] < h):
                    continue
                if nb in danger_zone:
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

                own_neighbor_positions = [n2 for n2 in _adjacent(nb) if n2 in self._connected]
                own_neighbors = len(own_neighbor_positions)
                # Reward redundant connectivity — clusters survive mid-chain deaths.
                if own_neighbors >= 2:
                    score += 25 * (own_neighbors - 1)

                # Urgency: if a neighbor plantation is close to self-terraforming,
                # its chain will break. Replace it before it does.
                max_nb_progress = max(
                    (progress_by_pos.get(p, 0) for p in own_neighbor_positions),
                    default=0,
                )
                if max_nb_progress >= 60:
                    score += 60
                elif max_nb_progress >= 40:
                    score += 25

                # Survival priority: HQ must have ≥2 own neighbors to enable future
                # relocation before its cell hits 100%. Boost direct HQ neighbors
                # until that condition holds.
                if hq_needs_escape and nb in hq_adjacent_cells:
                    score += 200

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
        max_usage: int | None = None,
    ) -> Position | None:
        # Isolated plantations "не ведают повелений" — a command routed through
        # them is dead on arrival. Only connected cells are valid exit points.
        if author not in self._connected:
            return None
        author_usage = exit_usage[author] if exit_usage is not None else 0
        if max_usage is not None and author_usage >= max_usage:
            return None
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

        for pos in self._connected:
            if pos == author:
                continue
            if _chebyshev(author, pos) > sr:
                continue
            if _chebyshev(pos, target) > state.action_range:
                continue
            usage = exit_usage[pos] if exit_usage is not None else 0
            # Once usage hits the stat cap, the next command contributes 0.
            if max_usage is not None and usage >= max_usage:
                continue
            dist = _chebyshev(pos, target)
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
        if hq_progress < 70 and hq_neighbors >= 2:
            return

        best_candidate: Position | None = None
        best_score = -1.0
        best_neighbors = 0
        best_reachable = 0

        for nb_pos in _adjacent(hq.position):
            if nb_pos not in own_positions:
                continue
            nb_progress = self._terraform_progress(state, nb_pos)
            if nb_progress >= 95:
                continue
            neighbor_count = sum(1 for n2 in _adjacent(nb_pos) if n2 in own_positions)
            # Simulate old HQ cell dying (it's near 100% anyway): count how many
            # of our plantations remain connected via this candidate.
            reachable = self._reachable_from(nb_pos, own_positions, exclude=hq.position)
            # Reachability dominates — orphaned branches die to DS=10/turn.
            score = reachable * 100 + (100 - nb_progress) + neighbor_count * 15
            if score > best_score:
                best_score = score
                best_candidate = nb_pos
                best_neighbors = neighbor_count
                best_reachable = reachable

        if best_candidate is None:
            return

        # Below 85%: require both ≥2 local neighbors AND that the move preserves
        # most of the network (≥75% reachability), otherwise stand pat.
        if hq_progress < 85:
            total_own = len(own_positions)
            if best_neighbors < 2:
                return
            if total_own > 2 and best_reachable < max(2, (total_own * 3) // 4):
                return

        cmd.relocate_main(hq.position, best_candidate)

    def _reachable_from(
        self,
        start: Position,
        own_positions: set[Position],
        exclude: Position | None = None,
    ) -> int:
        if start not in own_positions or start == exclude:
            return 0
        visited = {start}
        stack = [start]
        while stack:
            cur = stack.pop()
            for nb in _adjacent(cur):
                if nb == exclude or nb in visited or nb not in own_positions:
                    continue
                visited.add(nb)
                stack.append(nb)
        return len(visited)
