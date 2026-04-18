"""Основная стратегия: композиция этапов (см. docs/strategy.md §5, §13).

Каждый ход:
    1. Обновить BotMemory (и поймать начало нового раунда).
    2. Диагностировать угрозу ЦУ → UrgencyLevel.
    3. Спланировать защиту (ремонт соседей, перенос ЦУ).
    4. Выбрать апгрейд (max_hp приоритет).
    5. Проверить safe-to-build (чтобы не снести ЦУ/щит).
    6. Сгенерировать цели стройки (×7 → мост → fallback).
    7. Назначить команды свободным плантациям.
    8. Сохранить память.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from api import Command, GameState, Plantation

from . import config as cfg
from .assign import AssignTarget, assign_commands
from .build_guard import can_build_safely
from .defense import DefensePlan, missing_shield_positions, plan_defense
from .diagnose import Diagnosis, UrgencyLevel, diagnose
from .memory import BotMemory, load_memory, save_memory
from .targets import BuildTarget, generate_build_targets
from .upgrade import choose_upgrade

log = logging.getLogger("strategy")


class Strategy:
    """Интерфейс `decide(state) -> Command`, ожидаемый `main.py`."""

    def __init__(self, memory_path: str | Path = "data/memory.json") -> None:
        self.memory_path = Path(memory_path)
        self.memory: BotMemory = load_memory(self.memory_path)

    # ------------------------------------------------------------------ decide
    def decide(self, state: GameState) -> Command:
        # 1. Память
        if self.memory.detect_round_reset(state):
            log.info("Новый раунд: сбрасываем память бота")
            self.memory.reset_for_new_round()
        self.memory.update_with(state)

        # 2. Диагностика + защита
        diag = diagnose(state)
        defense = plan_defense(state, diag)

        cmd = Command()

        # 3. Апгрейд
        upgrade = choose_upgrade(state)
        if upgrade is not None:
            cmd.upgrade_plantation(upgrade)

        # 4. Перенос ЦУ (экстренный сценарий)
        if diag.cu is not None and defense.relocate_cu_to is not None:
            cmd.relocate_main(diag.cu.position, defense.relocate_cu_to)

        # 5. Команды защиты (ремонт ЦУ и старого места после переноса)
        self._add_defense_commands(cmd, diag, defense)

        # 6. Стройка и ремонт буферных плантаций
        self._add_action_commands(cmd, state, diag, defense)

        # 7. Сохранение памяти
        try:
            save_memory(self.memory_path, self.memory)
        except Exception as exc:
            log.warning("Не удалось сохранить память: %s", exc)

        log.info(
            "t=%d urg=%s hp=%.0f/%d shields=%d cmds=%d upg=%s reloc=%s",
            state.turn_no,
            diag.urgency.name,
            diag.cu.hp if diag.cu else 0,
            diag.cu_mhp,
            len(diag.shields),
            len(cmd._actions),
            upgrade or "-",
            defense.relocate_cu_to or "-",
        )
        return cmd

    # --------------------------------------------------------------- helpers

    def _add_defense_commands(
        self,
        cmd: Command,
        diag: Diagnosis,
        defense: DefensePlan,
    ) -> None:
        if diag.cu is None:
            return

        for repairer in defense.cu_repairers:
            # Самый простой путь — автор == exit-point; штрафа нет.
            cmd.repair(repairer.position, diag.cu.position)

        # После переноса старое место ЦУ станет обычной плантацией с её HP.
        # Её координата совпадает со старой cu.position, поэтому чиним туда.
        for repairer in defense.old_cu_repairers:
            cmd.repair(repairer.position, diag.cu.position)

    def _add_action_commands(
        self,
        cmd: Command,
        state: GameState,
        diag: Diagnosis,
        defense: DefensePlan,
    ) -> None:
        if diag.cu is None:
            return

        # Свободные — все не-изолированные минус зарезервированные под защиту.
        free: list[Plantation] = [
            p for p in state.plantations
            if not p.is_isolated and p.id not in defense.reserved and not p.is_main
        ]
        # ЦУ тоже может участвовать в действиях (строить соседей).
        if diag.cu is not None and diag.cu.id not in defense.reserved:
            free.append(diag.cu)

        shield_ids = {s.id for s in diag.shields}
        allow_build, reason = can_build_safely(state, self.memory, shield_ids)

        assign_targets: list[AssignTarget] = []

        # Щиты строим даже у лимита, но только если это не снесёт ЦУ/щит.
        if allow_build or reason == "ok_below_limit":
            for pos in missing_shield_positions(state, diag.cu):
                assign_targets.append(AssignTarget(pos, "build", priority=0))

        # Общая стройка: только если safe-to-build прошёл.
        if allow_build:
            for t in generate_build_targets(state, diag.cu):
                assign_targets.append(
                    AssignTarget(t.position, "build", priority=1 + t.priority)
                )
        else:
            log.debug("Стройка заблокирована: %s", reason)

        # Ремонт не-ЦУ плантаций с HP < NON_CU_REPAIR_HP_RATIO * mhp.
        repair_threshold = diag.cu_mhp * cfg.NON_CU_REPAIR_HP_RATIO
        for p in state.plantations:
            if p.is_main or p.is_isolated:
                continue
            if p.id in defense.reserved:
                continue
            if p.hp < repair_threshold:
                assign_targets.append(AssignTarget(p.position, "repair", priority=4))

        assign_commands(state, free, assign_targets, cmd)
