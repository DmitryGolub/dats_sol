from __future__ import annotations

import random as _random
from dataclasses import dataclass, field
from collections import deque

from api.models import (
    Beaver,
    Construction,
    EnemyPlantation,
    GameState,
    MeteoEvent,
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
DEFAULT_TS = 5
DEFAULT_CS = 5
DEFAULT_RS = 5
DEFAULT_SE = 5
DEFAULT_BE = 5
DEFAULT_DS = 10
DEFAULT_AR = 2
DEFAULT_SR = 3
DEFAULT_VR = 3
CELL_DEGRADE_DELAY = 30
CELL_DEGRADE_SPEED = 10
POINTS_PER_PERCENT = 10
REINFORCED_MULTIPLIER = 1.5
BEAVER_LODGE_REWARD_MULTIPLIER = 20
BUILD_THRESHOLD = 50
IMMUNITY_DURATION = 3
LODGE_HP = 100
LODGE_REGEN = 5
LODGE_RADIUS = 2
LODGE_DAMAGE = 15
EARTHQUAKE_DAMAGE = 10
EARTHQUAKE_SPAWN_PROB = 0.05
SANDSTORM_SPAWN_PROB = 0.10
SANDSTORM_FORMING_TURNS = 5
SANDSTORM_DAMAGE = 2
SANDSTORM_SPEED_MIN = 5
SANDSTORM_SPEED_MAX = 15
SANDSTORM_RADIUS = 3
HQ_LOSS_PENALTY = 0.05


def is_reinforced(pos: Position) -> bool:
    return pos[0] % 7 == 0 and pos[1] % 7 == 0


def cell_max_points(pos: Position) -> int:
    return int(100 * POINTS_PER_PERCENT * (REINFORCED_MULTIPLIER if is_reinforced(pos) else 1.0))


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
    damage_by: dict[str, int] = field(default_factory=dict)


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
class SimBeaverLodge:
    id: str
    position: Position
    hp: int
    damage_by: dict[str, int] = field(default_factory=dict)


@dataclass
class SimMeteoEvent:
    id: str
    kind: str  # "earthquake" | "sandstorm"
    position: Position | None = None
    turns_until: int = 0           # earthquake: turns until fires; sandstorm: turns until starts moving
    is_forming: bool = False       # sandstorm only
    velocity: tuple[int, int] | None = None  # sandstorm only, e.g. (+1,+1) direction per turn
    speed: int = 0                  # sandstorm only, cells per turn (5..15)


@dataclass
class PlayerState:
    player_id: str
    score: float = 0.0
    upgrade_points: int = 0
    upgrade_levels: dict[str, int] = field(default_factory=dict)
    turns_until_points: int = DEFAULT_UPGRADE_INTERVAL
    hq_lost_turn: int = -1
    lost_plantations: int = 0
    beaver_kills: int = 0
    sabotage_kills: int = 0
    respawns: int = 0
    terraform_score: float = 0.0
    kill_score: float = 0.0
    built_plantations: int = 0
    upgrades_purchased: int = 0
    sabotage_damage_dealt: int = 0
    sabotage_damage_taken: int = 0
    storm_damage_taken: int = 0
    earthquake_damage_taken: int = 0
    lodge_damage_taken_hp: int = 0
    sabotage_lost_plantations: int = 0
    cataclysm_lost_plantations: int = 0
    lodge_lost_plantations: int = 0
    decay_lost_plantations: int = 0
    limit_lost_plantations: int = 0

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
        return DEFAULT_RS + self.upgrade_levels.get("repair_power", 0)

    @property
    def construction_speed(self) -> int:
        return DEFAULT_CS + self.upgrade_levels.get("repair_power", 0)

    @property
    def sabotage_efficiency(self) -> int:
        return DEFAULT_SE

    @property
    def beaver_efficiency(self) -> int:
        return DEFAULT_BE

    @property
    def degradation_speed(self) -> int:
        return max(0, DEFAULT_DS - self.upgrade_levels.get("decay_mitigation", 0) * 2)

    @property
    def earthquake_damage(self) -> int:
        return max(0, EARTHQUAKE_DAMAGE - self.upgrade_levels.get("earthquake_mitigation", 0) * 2)

    @property
    def lodge_damage_taken(self) -> int:
        return max(0, LODGE_DAMAGE - self.upgrade_levels.get("beaver_damage_mitigation", 0) * 2)

    @property
    def signal_range(self) -> int:
        return DEFAULT_SR + self.upgrade_levels.get("signal_range", 0)

    @property
    def vision_range(self) -> int:
        return DEFAULT_VR + self.upgrade_levels.get("vision_range", 0) * 2


@dataclass
class WorldState:
    turn_no: int
    map_size: tuple[int, int]
    mountains: set[Position]
    plantations: dict[str, SimPlantation] = field(default_factory=dict)
    constructions: dict[tuple[Position, str], SimConstruction] = field(default_factory=dict)
    terraformed: dict[Position, SimTerraformCell] = field(default_factory=dict)
    players: dict[str, PlayerState] = field(default_factory=dict)
    beaver_lodges: dict[str, SimBeaverLodge] = field(default_factory=dict)
    meteo_events: list[SimMeteoEvent] = field(default_factory=list)
    rng: _random.Random = field(default_factory=_random.Random)
    _id_counter: int = 0
    _lodge_counter: int = 0
    _meteo_counter: int = 0

    def next_id(self) -> str:
        self._id_counter += 1
        return f"p-{self._id_counter}"

    def next_lodge_id(self) -> str:
        self._lodge_counter += 1
        return f"lodge-{self._lodge_counter}"

    def next_meteo_id(self) -> str:
        self._meteo_counter += 1
        return f"meteo-{self._meteo_counter}"

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

    def visible_cells(self, player_id: str) -> set[Position]:
        ps = self.players.get(player_id)
        if ps is None:
            return set()
        vr = ps.vision_range
        visible: set[Position] = set()
        for p in self.plantations.values():
            if p.owner != player_id:
                continue
            x, y = p.position
            for dx in range(-vr, vr + 1):
                for dy in range(-vr, vr + 1):
                    visible.add((x + dx, y + dy))
        return visible

    def to_game_state(self, player_id: str) -> GameState:
        ps = self.players[player_id]
        visible = self.visible_cells(player_id)

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
            elif p.position in visible:
                enemy_plants.append(EnemyPlantation(id=p.id, position=p.position, hp=p.hp))

        own_constructions = [
            Construction(position=c.position, progress=c.progress)
            for (pos, owner), c in self.constructions.items()
            if owner == player_id
        ]

        cells = [
            TerraformCell(
                position=tc.position,
                terraformation_progress=tc.progress,
                turns_until_degradation=max(0, CELL_DEGRADE_DELAY - tc.turns_since_complete) if tc.turns_since_complete >= 0 else 0,
            )
            for tc in self.terraformed.values()
            if tc.progress > 0 and tc.position in visible
        ]

        beavers_visible = [
            Beaver(id=lodge.id, position=lodge.position, hp=lodge.hp)
            for lodge in self.beaver_lodges.values()
            if lodge.position in visible
        ]

        meteo_dtos = []
        for ev in self.meteo_events:
            if ev.kind == "sandstorm":
                nxt = None
                if not ev.is_forming and ev.position is not None and ev.velocity is not None:
                    nxt = (
                        ev.position[0] + ev.velocity[0] * ev.speed,
                        ev.position[1] + ev.velocity[1] * ev.speed,
                    )
                meteo_dtos.append(MeteoEvent(
                    id=ev.id,
                    kind="sandstorm",
                    position=ev.position,
                    radius=SANDSTORM_RADIUS,
                    turns_until=ev.turns_until if ev.is_forming else None,
                    is_forming=ev.is_forming,
                    next_position=nxt,
                ))
            elif ev.kind == "earthquake":
                meteo_dtos.append(MeteoEvent(
                    id=ev.id,
                    kind="earthquake",
                    position=None,
                    radius=None,
                    turns_until=ev.turns_until,
                    is_forming=None,
                    next_position=None,
                ))

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
            beavers=beavers_visible,
            plantations=own_plants,
            enemy_plantations=enemy_plants,
            meteo_forecasts=meteo_dtos,
            constructions=own_constructions,
            mountains=frozenset(self.mountains),
            terraformed_cells=cells,
            plantation_upgrades=upgrades,
        )
