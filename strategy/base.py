from __future__ import annotations

from abc import ABC, abstractmethod

from api.models import Command, GameState


class BaseStrategy(ABC):
    name: str = "base"

    @abstractmethod
    def decide(self, state: GameState) -> Command: ...

    def reset(self) -> None:
        pass


Strategy = BaseStrategy
