"""Tactical layer: utility-based action generation and scoring."""
from __future__ import annotations

from api.models import GameState, Position, Plantation
from strategy.brain.utils import adjacent, chebyshev, manhattan
from strategy.brain.spatial import InfluenceMap
from strategy.brain.temporal import Predictions
from strategy.brain.strategic import StateMachine, PhaseWeights
from strategy.brain.value import ValueFunction, ActionCandidate


class UtilityEngine:
    """Generates and scores all possible actions for the current turn."""

    def __init__(self) -> None:
        self.value_function = ValueFunction()

    def generate_and_score(
        self,
        state: GameState,
        spatial: InfluenceMap,
        phase: str,
        weights: PhaseWeights,
        predictions: Predictions,
        head_plan: list[Position],
    ) -> list[ActionCandidate]:
        """Generate ActionCandidates for all plantations and score them."""
        own_positions = {p.position for p in state.plantations}
        hq = next((p for p in state.plantations if p.is_main), None)
        terraform_by_pos = {c.position: c for c in state.terraformed_cells}

        candidates: list[ActionCandidate] = []

        # --- BUILD candidates ---
        candidates.extend(self._generate_builds(state, own_positions, hq, spatial, terraform_by_pos))

        # --- REPAIR candidates ---
        candidates.extend(self._generate_repairs(state, own_positions, hq, terraform_by_pos))

        # --- ATTACK BEAVER candidates ---
        candidates.extend(self._generate_beaver_attacks(state, own_positions))

        # --- SABOTAGE candidates ---
        candidates.extend(self._generate_sabotage(state, own_positions))

        # Score all candidates
        for cand in candidates:
            base = self.value_function.evaluate(
                cand, state, spatial, own_positions, hq, terraform_by_pos
            )
            # Head grooming bonus
            base += self.value_function.head_grooming_bonus(cand.target, head_plan, cand.action_type)

            # Strategic phase multiplier
            mult = StateMachine().phase_multiplier(cand.action_type, weights)
            base *= max(0.3, mult)

            # Temporal modifier: if HQ dies soon, boost repair/build on escape cells
            if predictions.hq_death_turn is not None and predictions.hq_death_turn < 20:
                if hq is not None and cand.target in adjacent(hq.position):
                    base *= 2.5

            cand.utility = base

        return candidates

    def _generate_builds(
        self,
        state: GameState,
        own_positions: set[Position],
        hq: Plantation | None,
        spatial: InfluenceMap,
        terraform_by_pos: dict[Position, object],
    ) -> list[ActionCandidate]:
        """Generate BUILD candidates for frontier and existing constructions."""
        candidates: list[ActionCandidate] = []
        frontier: set[Position] = set()

        # Existing constructions — high cap to finish quickly
        for con in state.constructions:
            remaining = 50 - con.progress
            cap = 3 if remaining > 20 else 2
            candidates.append(ActionCandidate(
                action_type="build",
                author=(0, 0),  # placeholder, resolved by operational
                target=con.position,
                base_score=120.0 + con.progress * 3.5,
                target_cap=cap,
            ))

        # Frontier cells (Chebyshev <= 2 from own, not own, not mountain)
        for pos in own_positions:
            for dx in range(-2, 3):
                for dy in range(-2, 3):
                    nb = (pos[0] + dx, pos[1] + dy)
                    if chebyshev(pos, nb) > 2:
                        continue
                    if nb in own_positions or nb in state.mountains:
                        continue
                    if not (0 <= nb[0] < spatial.width and 0 <= nb[1] < spatial.height):
                        continue
                    frontier.add(nb)

        for pos in frontier:
            # Skip if already a construction
            if any(c.position == pos for c in state.constructions):
                continue
            # Skip cells that would cut HQ escape
            if hq is not None and self._is_hq_escape_cut(pos, hq.position, own_positions):
                continue

            cell = terraform_by_pos.get(pos)
            progress = cell.terraformation_progress if cell else 0
            # Cap depends on importance
            if hq is not None and pos in adjacent(hq.position):
                cap = 3  # HQ escape routes
            elif progress >= 80:
                cap = 3
            else:
                cap = 2

            # Generate multiple candidate slots for this target
            for _ in range(cap):
                candidates.append(ActionCandidate(
                    action_type="build",
                    author=(0, 0),
                    target=pos,
                    base_score=spatial.value_heatmap.get(pos, 0.0),
                    target_cap=1,
                ))

        return candidates

    def _generate_repairs(
        self,
        state: GameState,
        own_positions: set[Position],
        hq: Plantation | None,
        terraform_by_pos: dict[Position, object],
    ) -> list[ActionCandidate]:
        """Generate REPAIR candidates for damaged plants."""
        candidates: list[ActionCandidate] = []
        max_hp = 50
        if state.plantation_upgrades:
            for tier in state.plantation_upgrades.tiers:
                if tier.name == "max_hp":
                    max_hp = 50 + tier.current * 10
                    break

        threshold = 0.45
        critical_threshold = 0.75

        for plant in state.plantations:
            if plant.is_isolated:
                continue
            hp_ratio = plant.hp / max_hp
            needs_repair = hp_ratio < threshold
            is_critical = (
                (plant.is_main or self._is_critical(plant.position, own_positions, hq.position if hq else None))
                and hp_ratio < critical_threshold
            )
            if needs_repair or is_critical:
                priority = plant.is_main or is_critical
                candidates.append(ActionCandidate(
                    action_type="repair",
                    author=(0, 0),
                    target=plant.position,
                    base_score=100.0 if priority else 50.0,
                    priority=priority,
                ))
        return candidates

    def _generate_beaver_attacks(
        self,
        state: GameState,
        own_positions: set[Position],
    ) -> list[ActionCandidate]:
        """Generate ATTACK_BEAVER candidates for visible lodges."""
        candidates: list[ActionCandidate] = []
        for beaver in state.beavers:
            # Only attack if lodge is reasonably close to our network
            min_dist = min(
                (manhattan(beaver.position, op) for op in own_positions),
                default=999,
            )
            if min_dist <= 8:
                candidates.append(ActionCandidate(
                    action_type="attack_beaver",
                    author=(0, 0),
                    target=beaver.position,
                    base_score=40.0,
                ))
        return candidates

    def _generate_sabotage(
        self,
        state: GameState,
        own_positions: set[Position],
    ) -> list[ActionCandidate]:
        """Generate SABOTAGE candidates for visible enemy plantations."""
        candidates: list[ActionCandidate] = []
        for ep in state.enemy_plantations:
            min_dist = min(
                (manhattan(ep.position, op) for op in own_positions),
                default=999,
            )
            if min_dist <= 6:
                candidates.append(ActionCandidate(
                    action_type="sabotage",
                    author=(0, 0),
                    target=ep.position,
                    base_score=30.0,
                ))
        return candidates

    def _is_critical(
        self,
        pos: Position,
        own_positions: set[Position],
        hq_pos: Position | None,
    ) -> bool:
        """Check if a cell is critical for HQ connectivity."""
        if hq_pos is None:
            return False
        if pos == hq_pos:
            return True
        # Simple: cells adjacent to HQ are critical
        if pos in adjacent(hq_pos):
            return True
        return False

    def _is_hq_escape_cut(
        self,
        pos: Position,
        hq_pos: Position,
        own_positions: set[Position],
    ) -> bool:
        """Check if building on pos would reduce HQ escape options."""
        # Building ON the HQ or its immediate neighbors (except to add escape) is bad
        if pos == hq_pos:
            return True
        # Actually building adjacent to HQ is generally good (adds escape)
        # We block building ON HQ or within Chebyshev 1 if it would replace a plant
        return False
