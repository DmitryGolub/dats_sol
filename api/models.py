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


class Command:
    """Builder для конструирования команды перед отправкой."""

    def __init__(self) -> None:
        self._moves: dict[str, Path] = {}
        self._upgrade: str | None = None
        self._relocate_main: Position | None = None

    # --- builder methods ---

    def move_beaver(self, beaver_id: str, path: Path) -> Command:
        """Задать путь для бобра."""
        self._moves[beaver_id] = list(path)
        return self

    def upgrade_plantation(self, upgrade_type: str) -> Command:
        """Выбрать улучшение плантации."""
        self._upgrade = upgrade_type
        return self

    def relocate_main_base(self, to: Position) -> Command:
        """Переместить главную базу."""
        self._relocate_main = tuple(to)  # type: ignore[assignment]
        return self

    # --- helpers ---

    def has_actions(self) -> bool:
        """Есть ли какие-либо действия для отправки."""
        return bool(self._moves or self._upgrade is not None or self._relocate_main is not None)

    def to_dict(self) -> dict:
        """Сериализовать в тело запроса для /api/command."""
        payload: dict = {"command": []}
        for beaver_id, path in self._moves.items():
            payload["command"].append({
                "beaverId": beaver_id,
                "path": [list(p) for p in path],
            })
        if self._upgrade is not None:
            payload["plantationUpgrade"] = self._upgrade
        if self._relocate_main is not None:
            payload["relocateMain"] = list(self._relocate_main)
        return payload

    # --- validation ---

    def validate(self, state: GameState) -> list[str]:
        """Синхронная валидация команды относительно состояния игры."""
        errors: list[str] = []
        beaver_map = {b.id: b for b in state.beavers}
        max_x, max_y = state.map_size

        for beaver_id, path in self._moves.items():
            if beaver_id not in beaver_map:
                errors.append(f"Бобёр '{beaver_id}' не найден в состоянии игры.")
                continue

            if len(path) > state.action_range:
                errors.append(
                    f"Путь бобра '{beaver_id}' ({len(path)} клеток) превышает action_range={state.action_range}."
                )

            for idx, (x, y) in enumerate(path):
                if not (0 <= x < max_x and 0 <= y < max_y):
                    errors.append(
                        f"Точка {idx} пути бобра '{beaver_id}' ({x},{y}) выходит за границы карты {max_x}x{max_y}."
                    )
                if (x, y) in state.mountains:
                    errors.append(
                        f"Точка {idx} пути бобра '{beaver_id}' ({x},{y}) проходит через гору."
                    )

        if self._relocate_main is not None:
            rx, ry = self._relocate_main
            if not (0 <= rx < max_x and 0 <= ry < max_y):
                errors.append(
                    f"Цель relocateMain ({rx},{ry}) выходит за границы карты {max_x}x{max_y}."
                )
            if (rx, ry) in state.mountains:
                errors.append(
                    f"Цель relocateMain ({rx},{ry}) находится на горе."
                )

        return errors
