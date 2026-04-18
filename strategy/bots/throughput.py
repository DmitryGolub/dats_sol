"""Бот-подкласс BenchmarkFactoryBot с надстройками под реальный throughput.

Результат анализа prod-раунда (map_size=318×318, 600 ходов, 62 388 очков):

1. Упрейды: бот взял RP×3 → SettlementLimit×11 → SR×2 = 16 покупок, ни одного
   DM/MH/VR. При этом за раунд не поднялся выше 11 плантаций — 11 settlement_limit
   апов были потрачены впустую. Новый priority: **SR, RP, DM** сначала, а
   `settlement_limit` — ТОЛЬКО когда `current_count + 3 >= limit`.

2. Pipeline: базовые `committed >= 3` / `committed >= 2 & current_count >= 12`
   резали параллелизм. В emergency/recovery снимаем эти ограничения —
   если есть авторы, коммитим их все. 48% ходов в prod бот имел 1 плантацию,
   то есть 1 автора — рост был физически невозможен.

3. HQ relocate: новые правила — перенос, когда progress >= 55 (раньше 40),
   hp <= 26 (раньше 18) или беавер в chebyshev<=2. Целевая клетка должна быть
   reinforced ИЛИ иметь высокий factory_mass И не быть «вот-вот в 100% + decay».

4. Emergency (current_count<=2, t>=30): бустим цели рядом с HQ, выключаем
   sabotage, расширяем overbuild до limit - current_count.

5. `_assign_lodge_finishes` не вызывается: beaver_kills=0 в прогонах, а его
   hq_threat-ветвь соскребала авторов без пользы.
"""

from __future__ import annotations

from collections import defaultdict

from api.models import Command, GameState, Plantation, Position, TerraformCell
from strategy.bots.benchmarks import (
    BenchmarkFactoryBot,
    _adjacent,
    _chebyshev,
    _is_reinforced,
)


class ThroughputBot(BenchmarkFactoryBot):
    name = "throughput"

    # --- upgrade priority ---
    # Главное изменение: settlement_limit уходит в конец и покупается только
    # когда бот реально близко к лимиту. Первым — signal_range: при action_range=2
    # и signal_range=3 радиус строя всего 5 клеток, что на 318×318 никак.
    upgrade_priority = [
        "signal_range",
        "repair_power",
        "decay_mitigation",
        "max_hp",
        "vision_range",
        "earthquake_mitigation",
        "beaver_damage_mitigation",
        "settlement_limit",
    ]
    # Не покупаем settlement_limit, пока current_count не подошёл к лимиту.
    settlement_limit_cushion = 6

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

    # --- emergency: база схлопнулась до <= emergency_count_threshold ---
    emergency_count_threshold = 2
    emergency_min_turn = 30
    emergency_adjacent_bonus = 120.0
    emergency_reinforced_bonus = 60.0

    # --- HQ relocation thresholds ---
    # Переносим раньше, чем база (progress<40, hp>=18). Причина: после 50%
    # клетка быстро добирает до 100% + decay, и HQ окажется на деградирующей
    # клетке. Переносим, пока есть куда.
    hq_relocate_progress_gate = 55
    hq_relocate_hp_gate = 26
    hq_relocate_beaver_dist = 2
    # Защита: не переносим, если кандидат-сосед «доживает» до 100% в ближайшие
    # turns — бессмысленно перезжать на клетку, которая скоро сдуется.
    hq_candidate_max_progress = 85

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

    # ---------------------------------------------------------------- upgrade

    def _apply_upgrade(self, state: GameState, cmd: Command) -> None:
        """Как в базе, но settlement_limit пропускаем, пока есть запас."""
        upgrades = state.plantation_upgrades
        if upgrades is None or upgrades.points <= 0:
            return
        tier_map = {tier.name: tier for tier in upgrades.tiers}
        current_count = len(state.plantations)
        limit = self._plantation_limit(state)
        can_buy_limit = current_count + self.settlement_limit_cushion >= limit

        for name in self.upgrade_priority:
            tier = tier_map.get(name)
            if tier is None or tier.current >= tier.max:
                continue
            if name == "settlement_limit" and not can_buy_limit:
                continue
            cmd.upgrade_plantation(name)
            return

    # --------------------------------------------------------------- recovery

    def _update_recovery_state(self, state: GameState) -> None:
        count = len(state.plantations)
        if count + self.recovery_drop_threshold <= self._prev_plant_count:
            self._recovery_until_turn = state.turn_no + self.recovery_hold_turns
        self._prev_plant_count = count

    def _in_recovery(self, turn: int) -> bool:
        return turn <= self._recovery_until_turn

    def _in_emergency(self, state: GameState) -> bool:
        """Сеть критически мала (после стартового окна). Отключает sabotage,
        расширяет overbuild, снимает ограничения на параллельные стройки."""
        if state.turn_no < self.emergency_min_turn:
            return False
        return len(state.plantations) <= self.emergency_count_threshold

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
        if self._in_recovery(state.turn_no) or self._in_emergency(state):
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

    # ---------------------------------------------------- target post-processing

    def _factory_targets(
        self,
        state: GameState,
        own_positions: set[Position],
        hq: Plantation,
        terraform_by_pos: dict[Position, TerraformCell],
    ) -> list[tuple[Position, float]]:
        base = super()._factory_targets(state, own_positions, hq, terraform_by_pos)
        if not base or not self._in_emergency(state):
            return base

        hq_pos = hq.position
        adjusted: list[tuple[Position, float]] = []
        for pos, score in base:
            cheb = _chebyshev(pos, hq_pos)
            bonus = 0.0
            if cheb <= 1:
                bonus += self.emergency_adjacent_bonus
            elif cheb == 2:
                bonus += self.emergency_adjacent_bonus * 0.5
            if _is_reinforced(pos) and cheb <= 3:
                bonus += self.emergency_reinforced_bonus
            adjusted.append((pos, score + bonus))
        adjusted.sort(key=lambda item: -item[1])
        return adjusted

    # ---------------------------------------------------- base-state scaling

    def _budget_for_state(
        self,
        current_count: int,
        limit: int,
        *,
        emergency: bool = False,
    ) -> tuple[int, int]:
        if emergency:
            return 16, max(12, limit - current_count)
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
        """Копия _assign_builds_factory из базы с тремя изменениями:

        1. overbuild_allowance + pipeline_cap — из `_budget_for_state`, а не из
           turn_no (см. базу benchmarks.py:1059-1061).
        2. В emergency/recovery: ослабляем «потолок committed» (base: 3 и 2 при
           current_count>=12). Без этого даже при 2 плантациях бот режет сам
           себя, не давая второй стройке разогнаться.
        3. В emergency: снимаем `staged_count >= pipeline_cap` отказ — пока
           ratio ≈ 0 и авторов мало, лучше стажировать всё что можно.
        """
        available = [p for p in state.plantations if p.position not in assigned and not p.is_isolated]
        if not available:
            return

        construction_by_pos = {c.position: c for c in state.constructions}
        targets = self._factory_targets(state, own_positions, hq, terraform_by_pos)
        used_exits: dict[Position, int] = defaultdict(int)
        limit = self._plantation_limit(state)
        current_count = len(state.plantations)

        emergency = self._in_emergency(state)
        recovery = self._in_recovery(state.turn_no)

        overbuild_allowance, pipeline_cap = self._budget_for_state(
            current_count, limit, emergency=emergency
        )
        if recovery:
            overbuild_allowance += self.recovery_overbuild_bonus
        immediate_budget = max(1, limit + overbuild_allowance - current_count)
        staged_count = len(state.constructions)
        births_committed = 0

        # В emergency/recovery ослабляем «потолок committed», чтобы не резать
        # параллелизм там, где его и так физически мало.
        cap_active_commit = 5 if (emergency or recovery) else 3
        cap_fresh_commit = 5 if (emergency or recovery) else 2
        # Минимальное current_count, после которого включается fresh-cap.
        cap_fresh_min = 8 if (emergency or recovery) else 12

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
                # В emergency pipeline_cap не блокирует ни fresh, ни continue:
                # нам важнее разогнать любую стройку, чем экономить стражу.
                allow_fresh_stage = progress <= 0 and (
                    emergency
                    or current_count < 12
                    or staged_count < max(4, pipeline_cap // 2)
                )
                pipeline_blocked = staged_count >= pipeline_cap and not emergency
                if (progress <= 0 and not allow_fresh_stage) or pipeline_blocked:
                    for _, exit_point, _ in committed:
                        used_exits[exit_point] -= 1
                    continue
                for _, exit_point, _ in committed:
                    used_exits[exit_point] -= 1
                committed = []
                total = 0
                for _, author, exit_point, _ in candidates:
                    if progress > 0 and len(committed) >= cap_active_commit:
                        break
                    if progress <= 0 and len(committed) >= cap_fresh_commit and current_count >= cap_fresh_min:
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

    # ------------------------------------------------------- HQ relocation

    def _maybe_relocate_hq_factory(
        self,
        state: GameState,
        cmd: Command,
        own_positions: set[Position],
        plant_by_pos: dict[Position, Plantation],
        hq: Plantation,
        terraform_by_pos: dict[Position, TerraformCell],
    ) -> None:
        """Переносим HQ:
        - hq_progress >= hq_relocate_progress_gate (55) — клетка добирает до
          100% и скоро начнёт деградировать, HQ потеряет HP;
        - hq.hp <= hq_relocate_hp_gate (26) — HQ под угрозой, надо уходить;
        - beaver в chebyshev <= hq_relocate_beaver_dist (2).

        Кандидат — один из соседей, который:
        - reinforced ИЛИ имеет высокий factory_mass (высокая плотность);
        - progress <= hq_candidate_max_progress (иначе переезжаем на клетку,
          которая сама вот-вот сдуется).
        """
        if len(state.plantations) < 2:
            return

        hq_cell = terraform_by_pos.get(hq.position)
        hq_progress = hq_cell.terraformation_progress if hq_cell else 0
        hq_beaver_dist = self._nearest_beaver_distance(hq.position, state.beavers)

        needs_move = (
            hq_progress >= self.hq_relocate_progress_gate
            or hq.hp <= self.hq_relocate_hp_gate
            or hq_beaver_dist <= self.hq_relocate_beaver_dist
        )
        if not needs_move:
            return

        best_candidate: Position | None = None
        best_score = -1e9
        for nb_pos in _adjacent(hq.position):
            nb = plant_by_pos.get(nb_pos)
            if nb is None or nb.is_isolated:
                continue
            cell = terraform_by_pos.get(nb_pos)
            nb_progress = cell.terraformation_progress if cell else 0
            # Не переезжаем на «предсмертную» клетку
            if nb_progress > self.hq_candidate_max_progress:
                continue
            neighbor_count = sum(1 for n in _adjacent(nb_pos) if n in own_positions)
            mass = self._factory_mass(nb_pos, own_positions, terraform_by_pos)
            beaver_dist = self._nearest_beaver_distance(nb_pos, state.beavers)

            score = 0.0
            # Приоритет: клетки с максимальным запасом по развитию
            score += max(0, 70 - nb_progress) * 5.0
            score += neighbor_count * 22
            score += nb.hp * 1.8
            score += mass * 0.08
            score += beaver_dist * 10
            # Reinforced — штраф, потому что такие клетки лучше использовать как
            # обычные плантации (они дают max_points 1.5×, а HQ ничего не даёт).
            if _is_reinforced(nb_pos):
                score -= 60
            if score > best_score:
                best_score = score
                best_candidate = nb_pos

        if best_candidate is not None:
            cmd.relocate_main(hq.position, best_candidate)
