"""Геометрические утилиты (см. docs/strategy.md Приложение A)."""

from __future__ import annotations

from typing import Iterator

from .config import BOOSTED_CELL_MODULO

Coord = tuple[int, int]


def chebyshev(a: Coord, b: Coord) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def manhattan(a: Coord, b: Coord) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def ortho_neighbors(pos: Coord) -> list[Coord]:
    x, y = pos
    return [(x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)]


def all_neighbors(pos: Coord) -> list[Coord]:
    x, y = pos
    return [
        (x + dx, y + dy)
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
        if (dx, dy) != (0, 0)
    ]


def in_bounds(pos: Coord, size: tuple[int, int]) -> bool:
    return 0 <= pos[0] < size[0] and 0 <= pos[1] < size[1]


def is_boosted(pos: Coord) -> bool:
    return pos[0] % BOOSTED_CELL_MODULO == 0 and pos[1] % BOOSTED_CELL_MODULO == 0


def cells_in_radius(center: Coord, radius: int) -> Iterator[Coord]:
    cx, cy = center
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            yield (cx + dx, cy + dy)
