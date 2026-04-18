"""Strategic layer: state machine with weighted priorities."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from api.models import GameState, Plantation, Position
from strategy.brain.utils import adjacent


Phase = Literal["expand", "stabilize", "harvest", "aggress"]


@dataclass
class PhaseWeights:
    expand_weight: float = 0.5
    stabilize_weight: float = 0.5
    harvest_weight: float = 0.0
    aggress_weight: float = 0.0


class StateMachine:
    """Strategic policy engine. Determines phase weights each turn."""

    def evaluate(
        self,
        state: GameState,
        predictions,
        own_positions: set[Position],
        hq: Plantation | None,
    ) -> tuple[Phase, PhaseWeights]:
        """Return active phase and all phase weights."""
        plant_count = len(state.plantations)
        limit = self._plantation_limit(state)

        # --- Base weights ---
        expand = 0.4
        stabilize = 0.3
        harvest = 0.1
        aggress = 0.0

        # --- HQ danger dramatically increases stabilize ---
        if hq is not None:
            safe_neighbors = self._count_safe_hq_neighbors(hq, own_positions, state)
            terraform_progress = self._hq_terraform_progress(hq, state)

            if safe_neighbors < 2:
                stabilize += 0.5
                expand -= 0.3
            if terraform_progress > 25:
                stabilize += 0.3
                expand -= 0.2
            if predictions and predictions.hq_death_turn is not None and predictions.hq_death_turn < 50:
                stabilize += 0.6
                expand -= 0.4
            if safe_neighbors == 0:
                stabilize += 0.4
                expand -= 0.3

        # --- Plant count pushes expand or harvest ---
        if plant_count < 8:
            expand += 0.3
            stabilize -= 0.1
        elif plant_count < limit - 5:
            expand += 0.2
        elif plant_count >= limit - 2:
            expand -= 0.2
            harvest += 0.2

        # --- Late game harvest ---
        if state.turn_no > 400:
            harvest += 0.2
            expand -= 0.1
        if state.turn_no > 500:
            harvest += 0.3
            expand -= 0.2

        # --- Damage / pressure -> stabilize ---
        damaged_ratio = self._damaged_ratio(state)
        if damaged_ratio > 0.3:
            stabilize += 0.2
            expand -= 0.1

        # Normalize
        total = expand + stabilize + harvest + aggress
        if total > 0:
            expand /= total
            stabilize /= total
            harvest /= total
            aggress /= total

        weights = PhaseWeights(
            expand_weight=expand,
            stabilize_weight=stabilize,
            harvest_weight=harvest,
            aggress_weight=aggress,
        )

        # Determine active phase
        active = max(
            [("expand", expand), ("stabilize", stabilize), ("harvest", harvest), ("aggress", aggress)],
            key=lambda x: x[1],
        )[0]

        return active, weights

    def phase_multiplier(self, action_type: str, weights: PhaseWeights) -> float:
        """Return utility multiplier for an action type given phase weights."""
        multipliers = {
            "build": weights.expand_weight * 1.0 + weights.harvest_weight * 0.5,
            "repair": weights.stabilize_weight * 1.0 + weights.expand_weight * 0.3,
            "sabotage": weights.aggress_weight * 1.0,
            "attack_beaver": weights.aggress_weight * 1.0,
            "idle": 0.0,
        }
        return multipliers.get(action_type, 0.5)

    def _count_safe_hq_neighbors(self, hq: Plantation, own_positions: set[Position], state: GameState) -> int:
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
                beaver_dist = min(beaver_dist, max(abs(nb[0] - b.position[0]), abs(nb[1] - b.position[1])))
            if beaver_dist <= 2:
                continue
            safe += 1
        return safe

    def _hq_terraform_progress(self, hq: Plantation, state: GameState) -> int:
        cell = next((c for c in state.terraformed_cells if c.position == hq.position), None)
        return cell.terraformation_progress if cell else 0

    def _plantation_limit(self, state: GameState) -> int:
        limit = 30
        if state.plantation_upgrades:
            for tier in state.plantation_upgrades.tiers:
                if tier.name == "settlement_limit":
                    limit = 30 + tier.current
                    break
        return limit

    def _damaged_ratio(self, state: GameState) -> float:
        if not state.plantations:
            return 0.0
        max_hp = 50
        if state.plantation_upgrades:
            for tier in state.plantation_upgrades.tiers:
                if tier.name == "max_hp":
                    max_hp = 50 + tier.current * 10
                    break
        damaged = sum(1 for p in state.plantations if p.hp < max_hp * 0.5 and not p.is_isolated)
        return damaged / len(state.plantations)
