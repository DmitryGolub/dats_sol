from __future__ import annotations

import random as _random

from api.models import Position
from .state import WorldState, SimPlantation, PlayerState, DEFAULT_MHP


def is_reinforced(x: int, y: int) -> bool:
    return x % 7 == 0 and y % 7 == 0


def generate_map(
    seed: int,
    width: int = 80,
    height: int = 80,
    mountain_density: float = 0.08,
    num_players: int = 1,
) -> WorldState:
    rng = _random.Random(seed)

    spawn_points = _pick_spawns(width, height, num_players, rng)
    spawn_zones = set()
    for sx, sy in spawn_points:
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                spawn_zones.add((sx + dx, sy + dy))

    mountains: set[Position] = set()
    num_mountains = int(width * height * mountain_density)
    attempts = 0
    while len(mountains) < num_mountains and attempts < num_mountains * 10:
        x = rng.randint(0, width - 1)
        y = rng.randint(0, height - 1)
        attempts += 1
        if is_reinforced(x, y):
            continue
        if (x, y) in spawn_zones:
            continue
        mountains.add((x, y))

    world = WorldState(
        turn_no=0,
        map_size=(width, height),
        mountains=mountains,
    )

    for i, pos in enumerate(spawn_points):
        pid = f"p{i}"
        plant_id = world.next_id()
        world.plantations[plant_id] = SimPlantation(
            id=plant_id,
            position=pos,
            hp=DEFAULT_MHP,
            is_main=True,
            is_isolated=False,
            owner=pid,
            immunity_until_turn=3,
            created_turn=0,
        )
        world.players[pid] = PlayerState(player_id=pid)

    return world


def _pick_spawns(
    width: int, height: int, num_players: int, rng: _random.Random
) -> list[Position]:
    margin = 5
    corners = [
        (margin, margin),
        (width - 1 - margin, margin),
        (margin, height - 1 - margin),
        (width - 1 - margin, height - 1 - margin),
    ]
    edges = [
        (width // 2, margin),
        (width // 2, height - 1 - margin),
        (margin, height // 2),
        (width - 1 - margin, height // 2),
    ]
    candidates = corners + edges
    rng.shuffle(candidates)
    return candidates[:num_players]
