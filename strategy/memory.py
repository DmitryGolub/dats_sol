"""Персистентное состояние бота между ходами (см. docs/strategy.md §4.2)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from api import GameState, Plantation

# Условный "очень старый" birth_turn для ЦУ, когда её истинный возраст неизвестен.
UNKNOWN_CU_BIRTH = -10_000


@dataclass
class BotMemory:
    """Что бот запоминает между ходами."""

    plantation_birth_turn: dict[str, int] = field(default_factory=dict)
    known_plantation_ids: set[str] = field(default_factory=set)
    round_no: int = 0
    last_turn_no: int = -1

    # -------- жизненный цикл --------

    def detect_round_reset(self, state: GameState) -> bool:
        """Новый раунд, если turn_no резко меньше предыдущего."""
        return 0 <= state.turn_no < self.last_turn_no - 5

    def reset_for_new_round(self) -> None:
        self.plantation_birth_turn.clear()
        self.known_plantation_ids.clear()
        self.round_no += 1

    def update_with(self, state: GameState) -> None:
        """Обновить память по текущему состоянию."""
        current_ids = {p.id for p in state.plantations}

        for p in state.plantations:
            if p.id in self.plantation_birth_turn:
                continue
            if p.is_main and state.turn_no <= 1:
                # ЦУ существует с самого начала — считаем её самой старой.
                self.plantation_birth_turn[p.id] = UNKNOWN_CU_BIRTH
            elif p.id not in self.known_plantation_ids:
                # Новая плантация — запомнить ход её появления.
                # Для ЦУ, появившейся после старта бота, тоже работает корректно.
                if p.is_main and not self.plantation_birth_turn:
                    self.plantation_birth_turn[p.id] = UNKNOWN_CU_BIRTH
                else:
                    self.plantation_birth_turn[p.id] = state.turn_no

        # Подчистить плантации, которых больше нет.
        for pid in list(self.plantation_birth_turn.keys()):
            if pid not in current_ids:
                del self.plantation_birth_turn[pid]

        self.known_plantation_ids = current_ids
        self.last_turn_no = state.turn_no

    # -------- запросы --------

    def get_oldest_plantation(self, state: GameState) -> Optional[Plantation]:
        if not state.plantations:
            return None
        birth = self.plantation_birth_turn
        return min(
            state.plantations,
            key=lambda p: (birth.get(p.id, state.turn_no), p.id),
        )

    # -------- сериализация --------

    def to_dict(self) -> dict:
        return {
            "plantation_birth_turn": self.plantation_birth_turn,
            "known_plantation_ids": sorted(self.known_plantation_ids),
            "round_no": self.round_no,
            "last_turn_no": self.last_turn_no,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BotMemory":
        return cls(
            plantation_birth_turn=dict(data.get("plantation_birth_turn", {})),
            known_plantation_ids=set(data.get("known_plantation_ids", [])),
            round_no=int(data.get("round_no", 0)),
            last_turn_no=int(data.get("last_turn_no", -1)),
        )


def load_memory(path: Path) -> BotMemory:
    if not path.exists():
        return BotMemory()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return BotMemory.from_dict(data)
    except (OSError, json.JSONDecodeError, ValueError):
        return BotMemory()


def save_memory(path: Path, memory: BotMemory) -> None:
    """Атомарная запись в JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(memory.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        pass
