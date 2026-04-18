"""Operational layer: logistics, routing, conflict resolution."""
from __future__ import annotations

from collections import defaultdict

from api.models import Command, GameState, Position
from strategy.brain.utils import chebyshev
from strategy.brain.value import ActionCandidate


class LogisticsSolver:
    """Finds optimal author and exit point for actions, resolves conflicts."""

    def resolve_actions(
        self,
        state: GameState,
        candidates: list[ActionCandidate],
    ) -> list[ActionCandidate]:
        """Resolve conflicts and return the final set of executable actions.
        
        Greedy auction by utility (priority first), respects per-target caps
        and exit congestion.
        """
        assigned_authors: set[Position] = set()
        assigned_exits: dict[Position, int] = {}
        per_target: dict[Position, int] = defaultdict(int)
        own_positions = {p.position for p in state.plantations}

        # Sort: priority actions first, then by descending utility
        sorted_candidates = sorted(
            candidates,
            key=lambda a: (0 if a.priority else 1, -a.utility),
        )

        final: list[ActionCandidate] = []

        for cand in sorted_candidates:
            if cand.action_type == "idle":
                continue

            # Check per-target cap
            cap = getattr(cand, "target_cap", 1)
            if per_target[cand.target] >= cap:
                continue

            # Find best author for this target
            author, exit_point = self._find_best_routing(
                cand.target,
                state,
                own_positions,
                assigned_authors,
                assigned_exits,
            )

            if author is None:
                continue

            cand.author = author
            cand.exit_point = exit_point
            assigned_authors.add(author)
            assigned_exits[exit_point] = assigned_exits.get(exit_point, 0) + 1
            per_target[cand.target] += 1
            final.append(cand)

        return final

    def _find_best_routing(
        self,
        target: Position,
        state: GameState,
        own_positions: set[Position],
        assigned_authors: set[Position],
        assigned_exits: dict[Position, int],
    ) -> tuple[Position | None, Position | None]:
        """Find the best unassigned author and exit for a target."""
        best_author: Position | None = None
        best_exit: Position | None = None
        best_eff = -1.0

        for plant in state.plantations:
            author = plant.position
            if author in assigned_authors or plant.is_isolated or author == target:
                continue

            # Direct action range
            if chebyshev(author, target) <= state.action_range:
                exit_point = author
                congestion = assigned_exits.get(exit_point, 0)
                eff = 10 - congestion
                if eff > best_eff:
                    best_author = author
                    best_exit = exit_point
                    best_eff = eff
                continue

            # Relay via exit point
            exit_point = self._find_exit_point(author, target, own_positions, state)
            if exit_point is not None:
                congestion = assigned_exits.get(exit_point, 0)
                eff = 8 - congestion
                if eff > best_eff:
                    best_author = author
                    best_exit = exit_point
                    best_eff = eff

        return best_author, best_exit

    def _find_exit_point(
        self,
        author: Position,
        target: Position,
        own_positions: set[Position],
        state: GameState,
    ) -> Position | None:
        """Find the best own-position relay between author and target."""
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

    def build_command(self, actions: list[ActionCandidate]) -> Command:
        """Convert resolved actions into a Command."""
        cmd = Command()
        for a in actions:
            if a.action_type == "build":
                if a.exit_point == a.author or a.exit_point is None:
                    cmd.build(a.author, a.target)
                else:
                    cmd.build_via(a.author, a.exit_point, a.target)
            elif a.action_type == "repair":
                if a.exit_point == a.author or a.exit_point is None:
                    cmd.repair(a.author, a.target)
                else:
                    cmd.repair_via(a.author, a.exit_point, a.target)
            elif a.action_type == "sabotage":
                if a.exit_point == a.author or a.exit_point is None:
                    cmd.sabotage(a.author, a.target)
                else:
                    cmd.sabotage_via(a.author, a.exit_point, a.target)
            elif a.action_type == "attack_beaver":
                if a.exit_point == a.author or a.exit_point is None:
                    cmd.attack_beaver(a.author, a.target)
                else:
                    cmd.attack_beaver_via(a.author, a.exit_point, a.target)
        return cmd
