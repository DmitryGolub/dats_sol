from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque

from api.models import (
    Construction,
    EnemyPlantation,
    GameState,
    Plantation,
    PlantationUpgradesState,
    PlantationUpgradeTier,
    Position,
    TerraformCell,
)

UPGRADE_TIERS = [
    ("repair_power", 3),
    ("max_hp", 5),
    ("settlement_limit", 10),
    ("signal_range", 15),
    ("vision_range", 5),
    ("decay_mitigation", 3),
    ("earthquake_mitigation", 3),
    ("beaver_damage_mitigation", 5),
]

DEFAULT_UPGRADE_INTERVAL = 30
DEFAULT_MAX_UPGRADE_POINTS = 15
DEFAULT_PLANTATION_LIMIT = 30
DEFAULT_MHP = 50
DEFAULT_AR = 2
DEFAULT_SR = 3


@dataclass
class SimPlantation:
    id: str
    position: Position
    hp: int
    is_main: bool
    is_isolated: bool
    owner: str
    immunity_until_turn: int
    created_turn: int


@dataclass
class SimConstruction:
    position: Position
    progress: int
    owner: str
    had_progress_this_turn: bool = False


@dataclass
class SimTerraformCell:
    position: Position
    progress: int
    turns_since_complete: int = -1


@dataclass
class PlayerState:
    player_id: str
    score: float = 0.0
    upgrade_points: int = 0
    upgrade_levels: dict[str, int] = field(default_factory=dict)
    turns_until_points: int = DEFAULT_UPGRADE_INTERVAL
    hq_lost_turn: int = -1

    def __post_init__(self) -> None:
        if not self.upgrade_levels:
            self.upgrade_levels = {name: 0 for name, _ in UPGRADE_TIERS}

    @property
    def plantation_limit(self) -> int:
        return DEFAULT_PLANTATION_LIMIT + self.upgrade_levels.get("settlement_limit", 0)

    @property
    def max_hp(self) -> int:
        return DEFAULT_MHP + self.upgrade_levels.get("max_hp", 0) * 10

    @property
    def repair_speed(self) -> int:
        return 5 + self.upgrade_levels.get("repair_power", 0)

    @property
    def degradation_speed(self) -> int:
        return max(0, 10 - self.upgrade_levels.get("decay_mitigation", 0) * 2)

    @property
    def signal_range(self) -> int:
        return DEFAULT_SR + self.upgrade_levels.get("signal_range", 0)


@dataclass
class WorldState:
    turn_no: int
    map_size: tuple[int, int]
    mountains: set[Position]
    plantations: dict[str, SimPlantation] = field(default_factory=dict)
    constructions: dict[Position, SimConstruction] = field(default_factory=dict)
    terraformed: dict[Position, SimTerraformCell] = field(default_factory=dict)
    players: dict[str, PlayerState] = field(default_factory=dict)
    _id_counter: int = 0

    def next_id(self) -> str:
        self._id_counter += 1
        return f"p-{self._id_counter}"

    def get_player_plantations(self, player_id: str) -> list[SimPlantation]:
        return [p for p in self.plantations.values() if p.owner == player_id]

    def get_player_plantation_positions(self, player_id: str) -> set[Position]:
        return {p.position for p in self.plantations.values() if p.owner == player_id}

    def find_hq(self, player_id: str) -> SimPlantation | None:
        for p in self.plantations.values():
            if p.owner == player_id and p.is_main:
                return p
        return None

    def compute_connectivity(self, player_id: str) -> set[Position]:
        hq = self.find_hq(player_id)
        if hq is None:
            return set()
        own_positions = self.get_player_plantation_positions(player_id)
        visited: set[Position] = set()
        queue = deque([hq.position])
        visited.add(hq.position)
        while queue:
            x, y = queue.popleft()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (x + dx, y + dy)
                if nb in own_positions and nb not in visited:
                    visited.add(nb)
                    queue.append(nb)
        return visited

    def to_game_state(self, player_id: str) -> GameState:
        ps = self.players[player_id]

        own_plants = []
        enemy_plants = []
        for p in self.plantations.values():
            if p.owner == player_id:
                own_plants.append(Plantation(
                    id=p.id,
                    position=p.position,
                    hp=p.hp,
                    is_main=p.is_main,
                    is_isolated=p.is_isolated,
                    immunity_until_turn=p.immunity_until_turn if p.immunity_until_turn > self.turn_no else None,
                ))
            else:
                enemy_plants.append(EnemyPlantation(id=p.id, position=p.position, hp=p.hp))

        own_constructions = [
            Construction(position=c.position, progress=c.progress)
            for c in self.constructions.values()
            if c.owner == player_id
        ]

        cells = [
            TerraformCell(
                position=tc.position,
                terraformation_progress=tc.progress,
                turns_until_degradation=max(0, 80 - tc.turns_since_complete) if tc.turns_since_complete >= 0 else 0,
            )
            for tc in self.terraformed.values()
            if tc.progress > 0
        ]

        tiers = [
            PlantationUpgradeTier(name=name, current=ps.upgrade_levels.get(name, 0), max=max_lvl)
            for name, max_lvl in UPGRADE_TIERS
        ]

        upgrades = PlantationUpgradesState(
            points=ps.upgrade_points,
            max_points=DEFAULT_MAX_UPGRADE_POINTS,
            interval_turns=DEFAULT_UPGRADE_INTERVAL,
            turns_until_points=ps.turns_until_points,
            tiers=tiers,
        )

        return GameState(
            turn_no=self.turn_no,
            next_turn_in=0.0,
            map_size=self.map_size,
            action_range=DEFAULT_AR,
            beavers=[],
            plantations=own_plants,
            enemy_plantations=enemy_plants,
            meteo_forecasts=[],
            constructions=own_constructions,
            mountains=frozenset(self.mountains),
            terraformed_cells=cells,
            plantation_upgrades=upgrades,
        )
