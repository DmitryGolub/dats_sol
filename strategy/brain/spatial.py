"""Spatial awareness: influence maps, zones, heatmaps."""
from __future__ import annotations

from api.models import GameState, Position
from strategy.brain.utils import adjacent, chebyshev, is_reinforced


class InfluenceMap:
    """Grid analysis + influence maps for the current turn."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.value_heatmap: dict[Position, float] = {}
        self.threat_heatmap: dict[Position, float] = {}
        self.zone_map: dict[Position, str] = {}
        self.center: Position = (width // 2, height // 2)
        self.max_dist = max(width, height)

    def update(self, state: GameState) -> None:
        self.value_heatmap.clear()
        self.threat_heatmap.clear()
        self.zone_map.clear()

        own_positions = {p.position for p in state.plantations}
        beaver_positions = [b.position for b in state.beavers]

        # 1. Value heatmap & zones
        for x in range(self.width):
            for y in range(self.height):
                pos = (x, y)
                if pos in state.mountains:
                    continue
                val = self._cell_value(pos, own_positions, state)
                self.value_heatmap[pos] = val
                self.zone_map[pos] = self._classify_zone(pos, own_positions, beaver_positions, state)

        # 2. Threat heatmap
        for lodge_pos in beaver_positions:
            for x in range(self.width):
                for y in range(self.height):
                    pos = (x, y)
                    dist = chebyshev(pos, lodge_pos)
                    if dist <= 5:
                        threat = max(0, 5 - dist) * 15.0
                        self.threat_heatmap[pos] = self.threat_heatmap.get(pos, 0.0) + threat

        # 3. Sandstorm threat (minimal — if storm is visible and formed)
        for evt in state.meteo_forecasts:
            if evt.kind == "sandstorm" and not evt.is_forming and evt.next_position:
                # deterministic path
                spos = evt.next_position
                for dx in range(-2, 3):
                    for dy in range(-2, 3):
                        pos = (spos[0] + dx, spos[1] + dy)
                        if 0 <= pos[0] < self.width and 0 <= pos[1] < self.height:
                            self.threat_heatmap[pos] = self.threat_heatmap.get(pos, 0.0) + 25.0

    def _cell_value(self, pos: Position, own_positions: set[Position], state: GameState) -> float:
        val = 0.0
        # Center gradient
        dist_to_center = manhattan(pos, self.center)
        val += 18.0 * (1 - dist_to_center / (self.max_dist * 2))
        # Reinforced
        if is_reinforced(pos):
            val += 150.0
        elif chebyshev(pos, (pos[0] // 7 * 7, pos[1] // 7 * 7)) <= 1:
            val += 54.0
        elif chebyshev(pos, (pos[0] // 7 * 7, pos[1] // 7 * 7)) <= 2:
            val += 22.0
        # Own cluster bonus
        own_neighbors = sum(1 for n in adjacent(pos) if n in own_positions)
        val += own_neighbors * 12.0
        # Frontier bonus (adjacent to own but not own)
        if own_neighbors > 0 and pos not in own_positions:
            val += 8.0
        return val

    def _classify_zone(
        self,
        pos: Position,
        own_positions: set[Position],
        beaver_positions: list[Position],
        state: GameState,
    ) -> str:
        # Hazard: close to beaver or high threat
        threat = self.threat_heatmap.get(pos, 0.0)
        if threat >= 40:
            return "hazard"
        # Safe: own cell with healthy neighbors
        if pos in own_positions:
            safe_neighbors = sum(1 for n in adjacent(pos) if n in own_positions)
            if safe_neighbors >= 2:
                return "safe"
            return "contested"
        # Frontier: adjacent to own
        if any(n in own_positions for n in adjacent(pos)):
            return "frontier"
        return "neutral"


def manhattan(a: Position, b: Position) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])
