"""Утилиты: Position, Pathfinder и вспомогательные парсеры."""

from __future__ import annotations

import heapq
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Position, Path


def parse_position(raw: list[int] | tuple[int, ...]) -> tuple[int, int]:
    """Привести [x, y] к (x, y)."""
    return int(raw[0]), int(raw[1])


def parse_optional_position(raw: list[int] | None) -> tuple[int, int] | None:
    """Привести [x, y] или None к (x, y) | None."""
    if raw is None:
        return None
    return parse_position(raw)


class Pathfinder:
    """A* pathfinder для поиска пути на карте с препятствиями."""

    def __init__(self, width: int, height: int, mountains: set[tuple[int, int]]) -> None:
        self.width = width
        self.height = height
        self.mountains = mountains

    def neighbors(self, pos: tuple[int, int]) -> list[tuple[int, int]]:
        """Возвращает соседей по 4 направлениям (без диагоналей)."""
        x, y = pos
        candidates = [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
        result = []
        for nx, ny in candidates:
            if 0 <= nx < self.width and 0 <= ny < self.height:
                if (nx, ny) not in self.mountains:
                    result.append((nx, ny))
        return result

    def find_path(self, start: tuple[int, int], goal: tuple[int, int]) -> list[tuple[int, int]] | None:
        """A* поиск пути. Возвращает список позиций включая start и goal, или None."""
        if start == goal:
            return [start]
        if goal in self.mountains:
            return None

        # (f_score, counter, position)
        counter = 0
        open_set: list[tuple[int, int, tuple[int, int]]] = []
        heapq.heappush(open_set, (0, counter, start))
        came_from: dict[tuple[int, int], tuple[int, int]] = {}
        g_score: dict[tuple[int, int], int] = {start: 0}
        f_score: dict[tuple[int, int], int] = {start: self._heuristic(start, goal)}

        while open_set:
            _, _, current = heapq.heappop(open_set)
            if current == goal:
                return self._reconstruct_path(came_from, current)

            for neighbor in self.neighbors(current):
                tentative_g = g_score[current] + 1
                if neighbor not in g_score or tentative_g < g_score[neighbor]:
                    came_from[neighbor] = current
                    g_score[neighbor] = tentative_g
                    f = tentative_g + self._heuristic(neighbor, goal)
                    f_score[neighbor] = f
                    counter += 1
                    heapq.heappush(open_set, (f, counter, neighbor))

        return None

    @staticmethod
    def _heuristic(a: tuple[int, int], b: tuple[int, int]) -> int:
        return abs(a[0] - b[0]) + abs(a[1] - b[1])

    def _reconstruct_path(
        self,
        came_from: dict[tuple[int, int], tuple[int, int]],
        current: tuple[int, int],
    ) -> list[tuple[int, int]]:
        path = [current]
        while current in came_from:
            current = came_from[current]
            path.append(current)
        path.reverse()
        return path
