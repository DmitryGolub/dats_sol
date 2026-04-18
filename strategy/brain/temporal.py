"""Temporal predictions: short-horizon forecasting."""
from __future__ import annotations

from api.models import GameState, Plantation, Position, Construction
from strategy.brain.utils import adjacent


class HorizonPredictor:
    """Predicts outcomes 1-3 turns ahead."""

    def __init__(self) -> None:
        self.last_turn: int = -1
        self.terraform_delta_memory: dict[Position, int] = {}

    def predict(self, state: GameState) -> Predictions:
        """Return predictions for the current state."""
        hq = next((p for p in state.plantations if p.is_main), None)
        terraform_by_pos = {c.position: c for c in state.terraformed_cells}

        hq_death_turn: int | None = None
        if hq is not None:
            hq_death_turn = self._predict_hq_death_turn(hq, terraform_by_pos)

        return Predictions(
            hq_death_turn=hq_death_turn,
            terraform_by_pos=terraform_by_pos,
        )

    def _predict_hq_death_turn(
        self,
        hq: Plantation,
        terraform_by_pos: dict[Position, object],
    ) -> int | None:
        """Predict when HQ cell reaches 100% terraform (and dies)."""
        cell = terraform_by_pos.get(hq.position)
        if cell is None:
            return None
        progress = cell.terraformation_progress
        if progress >= 100:
            return 0  # already dead/dying
        # terraform grows by +5 per turn per own non-isolated plant
        # Conservative: assume at least 1 plant contributes
        turns_to_100 = (100 - progress + 4) // 5
        return max(1, turns_to_100)

    def predict_isolation(
        self,
        plant: Plantation,
        own_positions: set[Position],
    ) -> bool:
        """Predict if plant will become isolated if all non-critical neighbors die."""
        # Simple: if plant has <= 1 own neighbor, it's at risk
        own_neighbors = sum(1 for n in adjacent(plant.position) if n in own_positions)
        return own_neighbors <= 1

    def predict_build_completion(
        self,
        construction: Construction,
        assigned_builders: int,
    ) -> int:
        """Estimate turns to finish a construction.
        
        Base repair speed ~5-15 per turn per builder, modified by exit congestion.
        Conservative estimate: 5 per builder per turn.
        """
        if assigned_builders <= 0:
            return 999
        remaining = 50 - construction.progress
        speed = assigned_builders * 5
        return (remaining + speed - 1) // speed


class Predictions:
    """Container for temporal predictions of the current turn."""

    def __init__(
        self,
        hq_death_turn: int | None,
        terraform_by_pos: dict[Position, object],
    ) -> None:
        self.hq_death_turn = hq_death_turn
        self.terraform_by_pos = terraform_by_pos
