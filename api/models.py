"""Модели данных API — immutable dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Position = tuple[int, int]
Path = list[Position]


@dataclass(frozen=True, slots=True)
class Beaver:
    id: str
    position: Position
    hp: int


@dataclass(frozen=True, slots=True)
class Plantation:
    id: str
    position: Position
    hp: int
    is_main: bool
    is_isolated: bool
    immunity_until_turn: int | None


@dataclass(frozen=True, slots=True)
class EnemyPlantation:
    id: str
    position: Position
    hp: int


@dataclass(frozen=True, slots=True)
class MeteoEvent:
    id: str | None
    kind: Literal["earthquake", "sandstorm"]
    position: Position | None
    radius: int | None
    turns_until: int | None
    is_forming: bool | None
    next_position: Position | None


@dataclass(frozen=True, slots=True)
class Construction:
    position: Position
    progress: int


@dataclass(frozen=True, slots=True)
class TerraformCell:
    position: Position
    terraformation_progress: int
    turns_until_degradation: int


@dataclass(frozen=True, slots=True)
class PlantationUpgradeTier:
    name: str
    current: int
    max: int


@dataclass(frozen=True, slots=True)
class PlantationUpgradesState:
    points: int
    max_points: int
    interval_turns: int
    turns_until_points: int
    tiers: list[PlantationUpgradeTier]


@dataclass(frozen=True, slots=True)
class GameState:
    turn_no: int
    next_turn_in: float
    map_size: tuple[int, int]
    action_range: int
    beavers: list[Beaver]
    plantations: list[Plantation]
    enemy_plantations: list[EnemyPlantation]
    meteo_forecasts: list[MeteoEvent]
    constructions: list[Construction]
    mountains: set[Position]
    terraformed_cells: list[TerraformCell]
    plantation_upgrades: PlantationUpgradesState | None


@dataclass(frozen=True, slots=True)
class Log:
    message: str
    time: str


@dataclass
class CommandResult:
    success: bool
    errors: list[str]
    raw_response: dict


@dataclass(frozen=True, slots=True)
class PlantationAction:
    """Действие плантации: path = [автор, выходная_точка, цель]."""
    path: list[Position]


class Command:
    """Builder для конструирования команды перед отправкой.

    Формат API:
    - command[]: массив {path: [[x1,y1], [x2,y2], [x3,y3]]}
      где: [0]=автор, [1]=выходная точка, [2]=цель
      Тип действия определяется целью:
        - своя плантация → ремонт
        - чужая плантация → диверсия
        - логово бобров → атака
        - пустая клетка → строительство
    - plantationUpgrade: str — название апгрейда
    - relocateMain: [[fromX,fromY], [toX,toY]] — перенос ЦУ
    """

    def __init__(self) -> None:
        self._actions: list[PlantationAction] = []
        self._upgrade: str | None = None
        self._relocate_main: list[Position] | None = None

    # --- builder methods ---

    def add_action(self, author: Position, exit_point: Position, target: Position) -> Command:
        """Добавить действие плантации."""
        self._actions.append(PlantationAction(path=[author, exit_point, target]))
        return self

    def build(self, author: Position, target: Position) -> Command:
        """Строительство напрямую (автор = выходная точка)."""
        return self.add_action(author, author, target)

    def build_via(self, author: Position, exit_point: Position, target: Position) -> Command:
        """Строительство через выходную точку."""
        return self.add_action(author, exit_point, target)

    def repair(self, author: Position, target: Position) -> Command:
        """Ремонт напрямую (автор = выходная точка)."""
        return self.add_action(author, author, target)

    def repair_via(self, author: Position, exit_point: Position, target: Position) -> Command:
        """Ремонт через выходную точку."""
        return self.add_action(author, exit_point, target)

    def sabotage(self, author: Position, target: Position) -> Command:
        """Диверсия напрямую."""
        return self.add_action(author, author, target)

    def sabotage_via(self, author: Position, exit_point: Position, target: Position) -> Command:
        """Диверсия через выходную точку."""
        return self.add_action(author, exit_point, target)

    def attack_beaver(self, author: Position, target: Position) -> Command:
        """Атака логова бобров напрямую."""
        return self.add_action(author, author, target)

    def attack_beaver_via(self, author: Position, exit_point: Position, target: Position) -> Command:
        """Атака логова бобров через выходную точку."""
        return self.add_action(author, exit_point, target)

    def upgrade_plantation(self, upgrade_type: str) -> Command:
        """Выбрать улучшение плантации."""
        self._upgrade = upgrade_type
        return self

    def relocate_main(self, from_pos: Position, to_pos: Position) -> Command:
        """Переместить ЦУ на соседнюю плантацию."""
        self._relocate_main = [from_pos, to_pos]
        return self

    # --- helpers ---

    def has_actions(self) -> bool:
        return bool(self._actions or self._upgrade is not None or self._relocate_main is not None)

    def to_dict(self) -> dict:
        """Сериализовать в тело запроса для POST /api/command."""
        payload: dict = {}

        if self._actions:
            payload["command"] = [
                {"path": [list(pos) for pos in action.path]}
                for action in self._actions
            ]

        if self._upgrade is not None:
            payload["plantationUpgrade"] = self._upgrade

        if self._relocate_main is not None:
            payload["relocateMain"] = [list(pos) for pos in self._relocate_main]

        return payload
