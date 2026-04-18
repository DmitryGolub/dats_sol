"""Бот-подкласс BenchmarkFactoryBot с надстройками под реальный throughput.

Главное отличие: вместо жёстких границ по turn_no мы смотрим на ОТНОШЕНИЕ
current_count / plantation_limit и наращиваем агрессивность, когда база далеко
от предела (метод `_budget_for_state`). В базе bench_factory allowance привязан
к ходам (≤120/280/600), из-за чего при просадке сети на позднем ходу бот
застревает в 3-х плантациях на 300 ходов, а при переполнении — лупит в
limit_lost. Фикс за счёт базового состояния даёт +4-8% в соло и +39% в версусе
(8 сидов, 60×60).

Дополнительно:
- фаза sabotage по низко-HP вражеским плантациям (до sabotage_max_authors),
- короткий recovery-бонус к overbuild_allowance при резкой потере плантаций.

Базовая ветка `_assign_lodge_finishes` намеренно НЕ подключена: её hq_threat-
ветвь соскребает авторов на любых близких бобров без реального killа, а
beaver_kills в прогонах стабильно 0. Если будет пониматься «атака логов» как
отдельная фаза — делать её с жёстким `total_damage >= beaver.hp` и без
hq_threat-бесплатника.
"""

from __future__ import annotations

from collections import defaultdict

from api.models import Command, GameState, Plantation, Position, TerraformCell
from strategy.bots.benchmarks import (
    BenchmarkFactoryBot,
    _chebyshev,
)


class ThroughputBot(BenchmarkFactoryBot):
    name = "throughput"

    # late_lodge_bias=0.0 + lodge_commit_turn=999 = как в базе:
    # штраф за близость к бобрам никогда не ослабевает (turn_no никогда не
    # достигает 999, условие scale-down не срабатывает).
    lodge_commit_turn = 999
    late_lodge_bias = 0.0

    # --- sabotage ---
    sabotage_max_authors = 2
    sabotage_hp_ceiling = 22

    # --- recovery: резкая потеря → короткое окно агрессивного overbuild ---
    recovery_drop_threshold = 3
    recovery_hold_turns = 20
    recovery_overbuild_bonus = 4

    def __init__(self) -> None:
        super().__init__()
        self._prev_plant_count: int = 0
        self._recovery_until_turn: int = -1

    def reset(self) -> None:
        super().reset()
        self._prev_plant_count = 0
        self._recovery_until_turn = -1

    # ------------------------------------------------------------------ decide

    def decide(self, state: GameState) -> Command:
        cmd = Command()
        if not state.plantations:
            return cmd
        if not self._initialized:
            self._init(state)

        hq = next((p for p in state.plantations if p.is_main), None)
        if hq is None:
            return cmd

        own_positions = {p.position for p in state.plantations}
        plant_by_pos = {p.position: p for p in state.plantations}
        terraform_by_pos = {cell.position: cell for cell in state.terraformed_cells}

        self._apply_upgrade(state, cmd)
        self._update_recovery_state(state)

        assigned: set[Position] = set()
        self._assign_repairs_factory(state, cmd, assigned, own_positions, hq, terraform_by_pos)
        self._assign_sabotage(state, cmd, assigned, own_positions, hq, terraform_by_pos)
        self._assign_builds_factory(state, cmd, assigned, own_positions, hq, terraform_by_pos)
        self._maybe_relocate_hq_factory(state, cmd, own_positions, plant_by_pos, hq, terraform_by_pos)
        return cmd

    # --------------------------------------------------------------- recovery

    def _update_recovery_state(self, state: GameState) -> None:
        count = len(state.plantations)
        if count + self.recovery_drop_threshold <= self._prev_plant_count:
            self._recovery_until_turn = state.turn_no + self.recovery_hold_turns
        self._prev_plant_count = count

    def _in_recovery(self, turn: int) -> bool:
        return turn <= self._recovery_until_turn

    # --------------------------------------------------------------- sabotage

    def _assign_sabotage(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
        hq: Plantation,
        terraform_by_pos: dict[Position, TerraformCell],
    ) -> None:
        if self._in_recovery(state.turn_no):
            return
        if not state.enemy_plantations:
            return

        targets = sorted(
            [e for e in state.enemy_plantations if e.hp <= self.sabotage_hp_ceiling],
            key=lambda e: (e.hp, _chebyshev(e.position, hq.position)),
        )
        if not targets:
            return

        authors_used = 0
        for ep in targets:
            if authors_used >= self.sabotage_max_authors:
                break
            best_author: Position | None = None
            best_exit: Position | None = None
            best_score = -1e9
            for plant in state.plantations:
                if plant.position in assigned or plant.is_isolated:
                    continue
                exit_point = self._best_factory_exit(plant.position, ep.position, own_positions, state)
                if exit_point is None:
                    continue
                mass = self._factory_mass(plant.position, own_positions, terraform_by_pos)
                score = 100 - _chebyshev(exit_point, ep.position) * 4 + mass * 0.01
                if score > best_score:
                    best_author = plant.position
                    best_exit = exit_point
                    best_score = score
            if best_author is None or best_exit is None:
                continue
            assigned.add(best_author)
            authors_used += 1
            if best_exit == best_author:
                cmd.sabotage(best_author, ep.position)
            else:
                cmd.sabotage_via(best_author, best_exit, ep.position)

    # ---------------------------------------------------- base-state scaling

    def _budget_for_state(self, current_count: int, limit: int) -> tuple[int, int]:
        """Вернуть (overbuild_allowance, pipeline_cap) в зависимости от того,
        насколько заполнена база. Чем дальше current_count от limit, тем
        агрессивнее overbuild и больше параллельных строек. В полностью
        заполненном состоянии pipeline всё ещё держим широким, чтобы
        компенсировать постоянные потери по decay/limit/storm."""
        ratio = current_count / max(1, limit)
        if ratio < 0.3:
            return 12, max(12, limit - current_count)
        if ratio < 0.55:
            return 10, max(10, current_count // 2 + 10)
        if ratio < 0.8:
            return 6, max(8, current_count // 2 + 8)
        return 3, max(8, current_count // 2 + 8)

    # ----------------------------------------------------------------- builds

    def _assign_builds_factory(
        self,
        state: GameState,
        cmd: Command,
        assigned: set[Position],
        own_positions: set[Position],
        hq: Plantation,
        terraform_by_pos: dict[Position, TerraformCell],
    ) -> None:
        """Копия _assign_builds_factory из базы, но: overbuild_allowance и
        pipeline_cap рассчитываются из current_count/limit (см.
        `_budget_for_state`), а не из turn_no."""
        available = [p for p in state.plantations if p.position not in assigned and not p.is_isolated]
        if not available:
            return

        construction_by_pos = {c.position: c for c in state.constructions}
        targets = self._factory_targets(state, own_positions, hq, terraform_by_pos)
        used_exits: dict[Position, int] = defaultdict(int)
        limit = self._plantation_limit(state)
        current_count = len(state.plantations)

        overbuild_allowance, pipeline_cap = self._budget_for_state(current_count, limit)
        if self._in_recovery(state.turn_no):
            overbuild_allowance += self.recovery_overbuild_bonus
        immediate_budget = max(1, limit + overbuild_allowance - current_count)
        staged_count = len(state.constructions)
        births_committed = 0

        for target_pos, priority in targets:
            if not available:
                break
            progress = construction_by_pos.get(target_pos).progress if target_pos in construction_by_pos else 0
            required = max(0, 50 - progress)
            candidates: list[tuple[float, Position, Position, int]] = []
            for plant in available:
                exit_point = self._best_factory_exit(plant.position, target_pos, own_positions, state)
                if exit_point is None:
                    continue
                eff = max(0, self._construction_speed(state) - used_exits[exit_point])
                if eff <= 0:
                    continue
                score = priority
                if exit_point == plant.position:
                    score += 25
                score -= _chebyshev(exit_point, target_pos) * 4
                score += self._factory_mass(plant.position, own_positions, terraform_by_pos) * 0.01
                candidates.append((score, plant.position, exit_point, eff))
            if not candidates:
                continue
            candidates.sort(reverse=True)

            committed: list[tuple[Position, Position, int]] = []
            total = 0
            for _, author, exit_point, _ in candidates:
                eff = max(0, self._construction_speed(state) - used_exits[exit_point])
                if eff <= 0:
                    continue
                committed.append((author, exit_point, eff))
                used_exits[exit_point] += 1
                total += eff
                if total >= required:
                    break

            immediate = total >= required
            if immediate and births_committed >= immediate_budget:
                for _, exit_point, _ in committed:
                    used_exits[exit_point] -= 1
                committed = []
                immediate = False
            if not immediate:
                allow_fresh_stage = progress <= 0 and (
                    current_count < 12 or staged_count < max(4, pipeline_cap // 2)
                )
                if (progress <= 0 and not allow_fresh_stage) or staged_count >= pipeline_cap:
                    for _, exit_point, _ in committed:
                        used_exits[exit_point] -= 1
                    continue
                for _, exit_point, _ in committed:
                    used_exits[exit_point] -= 1
                committed = []
                total = 0
                for _, author, exit_point, _ in candidates:
                    if progress > 0 and len(committed) >= 3:
                        break
                    if progress <= 0 and len(committed) >= 2 and current_count >= 12:
                        break
                    eff = max(0, self._construction_speed(state) - used_exits[exit_point])
                    if eff <= 0:
                        continue
                    committed.append((author, exit_point, eff))
                    used_exits[exit_point] += 1
                    total += eff
                    if progress > 0 and total >= max(1, required // 2):
                        break
                    if progress <= 0 and total >= max(5, required // 3):
                        break
                if not committed:
                    continue

            committed_authors = {author for author, _, _ in committed}
            available = [p for p in available if p.position not in committed_authors]
            for author, exit_point, _ in committed:
                assigned.add(author)
                if exit_point == author:
                    cmd.build(author, target_pos)
                else:
                    cmd.build_via(author, exit_point, target_pos)
            if immediate:
                births_committed += 1
            elif progress <= 0:
                staged_count += 1
