"""Сбор и форматирование метрик бота по ходу игры."""

from __future__ import annotations

from dataclasses import dataclass, field

from api.models import Command, CommandResult, GameState


@dataclass
class TurnMetrics:
    turn_no: int
    next_turn_in: float
    decide_ms: float
    send_ms: float

    # карта / игрок
    map_size: tuple[int, int]
    action_range: int
    beavers: int
    beavers_hp_sum: int
    plantations: int
    plantations_hp_sum: int
    main_plantations: int
    isolated_plantations: int
    enemy_plantations: int
    enemy_hp_sum: int
    constructions: int
    terraformed_cells: int
    mountains: int
    meteo_forecasts: int

    # апгрейды
    upgrade_points: int | None
    upgrade_max_points: int | None
    upgrade_turns_until: int | None

    # команда
    actions: int
    builds: int
    repairs: int
    sabotages: int
    attacks: int
    upgrade_chosen: str | None
    relocate_main: bool

    # результат
    success: bool
    errors: list[str] = field(default_factory=list)


def _classify_action(state: GameState, target: tuple[int, int]) -> str:
    """Определить тип действия по цели."""
    for p in state.plantations:
        if p.position == target:
            return "repair"
    for e in state.enemy_plantations:
        if e.position == target:
            return "sabotage"
    for b in state.beavers:
        if b.position == target:
            return "attack"
    return "build"


def collect_metrics(
    state: GameState,
    cmd: Command,
    result: CommandResult | None,
    decide_ms: float,
    send_ms: float,
) -> TurnMetrics:
    payload = cmd.to_dict()
    actions_raw = payload.get("command", []) or []

    builds = repairs = sabotages = attacks = 0
    for action in actions_raw:
        path = action.get("path") or []
        if len(path) < 3:
            continue
        target = (int(path[2][0]), int(path[2][1]))
        kind = _classify_action(state, target)
        if kind == "build":
            builds += 1
        elif kind == "repair":
            repairs += 1
        elif kind == "sabotage":
            sabotages += 1
        elif kind == "attack":
            attacks += 1

    up = state.plantation_upgrades

    return TurnMetrics(
        turn_no=state.turn_no,
        next_turn_in=state.next_turn_in,
        decide_ms=decide_ms,
        send_ms=send_ms,
        map_size=state.map_size,
        action_range=state.action_range,
        beavers=len(state.beavers),
        beavers_hp_sum=sum(b.hp for b in state.beavers),
        plantations=len(state.plantations),
        plantations_hp_sum=sum(p.hp for p in state.plantations),
        main_plantations=sum(1 for p in state.plantations if p.is_main),
        isolated_plantations=sum(1 for p in state.plantations if p.is_isolated),
        enemy_plantations=len(state.enemy_plantations),
        enemy_hp_sum=sum(e.hp for e in state.enemy_plantations),
        constructions=len(state.constructions),
        terraformed_cells=len(state.terraformed_cells),
        mountains=len(state.mountains),
        meteo_forecasts=len(state.meteo_forecasts),
        upgrade_points=up.points if up else None,
        upgrade_max_points=up.max_points if up else None,
        upgrade_turns_until=up.turns_until_points if up else None,
        actions=len(actions_raw),
        builds=builds,
        repairs=repairs,
        sabotages=sabotages,
        attacks=attacks,
        upgrade_chosen=payload.get("plantationUpgrade"),
        relocate_main="relocateMain" in payload,
        success=result.success if result is not None else True,
        errors=list(result.errors) if result is not None else [],
    )


def format_turn_line(m: TurnMetrics) -> str:
    """Однострочная сводка хода для stdout/файла."""
    upg = ""
    if m.upgrade_points is not None:
        upg = f" upg={m.upgrade_points}/{m.upgrade_max_points}(in {m.upgrade_turns_until})"
    extra_cmd = []
    if m.upgrade_chosen:
        extra_cmd.append(f"upgrade={m.upgrade_chosen}")
    if m.relocate_main:
        extra_cmd.append("relocate_main")
    cmd_extra = (" " + " ".join(extra_cmd)) if extra_cmd else ""
    status = "OK" if m.success else f"FAIL({';'.join(m.errors)[:120]})"
    return (
        f"turn={m.turn_no} dt={m.next_turn_in:.2f}s "
        f"decide={m.decide_ms:.0f}ms send={m.send_ms:.0f}ms "
        f"| P={m.plantations}(hp={m.plantations_hp_sum},main={m.main_plantations},iso={m.isolated_plantations}) "
        f"B={m.beavers}(hp={m.beavers_hp_sum}) "
        f"E={m.enemy_plantations}(hp={m.enemy_hp_sum}) "
        f"C={m.constructions} T={m.terraformed_cells} M={m.meteo_forecasts}{upg} "
        f"| act={m.actions}(b={m.builds},r={m.repairs},s={m.sabotages},a={m.attacks}){cmd_extra} "
        f"=> {status}"
    )
