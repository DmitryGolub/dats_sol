"""Базовая стратегия — заглушка.

Возвращает пустую команду (без действий), чтобы игровой цикл
мог работать до реализации полноценной стратегии.
"""

from __future__ import annotations

import logging

from api.models import Command, GameState

log = logging.getLogger("strategy")


class Strategy:
    """Заглушка стратегии. Переопредели decide() для реальной логики."""

    def __init__(self) -> None:
        self._turn_count = 0

    def decide(self, state: GameState) -> Command:
        """Принять решение на основе текущего состояния игры.

        Возвращает Command с действиями плантаций, апгрейдами и/или переносом ЦУ.
        """
        self._turn_count += 1
        cmd = Command()

        hq = next((p for p in state.plantations if p.is_main), None)
        if hq:
            log.info(
                "Ход %d | Плантаций: %d | HP ЦУ: %d | Врагов: %d | Бобров: %d",
                state.turn_no,
                len(state.plantations),
                hq.hp,
                len(state.enemy_plantations),
                len(state.beavers),
            )

        # --- Апгрейд: если есть очки, тратим на лимит плантаций ---
        if state.plantation_upgrades and state.plantation_upgrades.points > 0:
            for tier in state.plantation_upgrades.tiers:
                if tier.name == "settlement_limit" and tier.current < tier.max:
                    cmd.upgrade_plantation("settlement_limit")
                    log.info("Апгрейд: settlement_limit (%d → %d)", tier.current, tier.current + 1)
                    break

        # TODO: реализовать стратегию строительства, ремонта, диверсий, атаки бобров

        return cmd
