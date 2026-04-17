from __future__ import annotations

from collections import defaultdict

from api.models import Command, PlantationAction, Position
from .state import (
    WorldState,
    SimPlantation,
    SimConstruction,
    SimTerraformCell,
    PlayerState,
    UPGRADE_TIERS,
    DEFAULT_UPGRADE_INTERVAL,
    DEFAULT_MAX_UPGRADE_POINTS,
    DEFAULT_MHP,
    DEFAULT_AR,
)

TS = 5
CS = 5
RS = 5
DS = 10
BUILD_THRESHOLD = 50
IMMUNITY_DURATION = 3
CELL_DEGRADE_DELAY = 80
CELL_DEGRADE_SPEED = 10
POINTS_PER_PERCENT = 10
REINFORCED_MULTIPLIER = 1.5
HQ_LOSS_PENALTY = 0.05
MAX_TURNS = 600


def is_reinforced(pos: Position) -> bool:
    return pos[0] % 7 == 0 and pos[1] % 7 == 0


def simulate_turn(world: WorldState, commands: dict[str, Command]) -> WorldState:
    _phase_upgrades(world, commands)
    _phase_build_repair(world, commands)
    _phase_degradation_isolated(world)
    _phase_degradation_constructions(world)
    _phase_terraformation(world)
    _phase_remove_completed(world)
    _phase_connectivity(world)
    _phase_enforce_limits(world)
    world.turn_no += 1
    return world


def _phase_upgrades(world: WorldState, commands: dict[str, Command]) -> None:
    for pid, ps in world.players.items():
        if ps.hq_lost_turn >= 0:
            continue

        ps.turns_until_points -= 1
        if ps.turns_until_points <= 0:
            ps.upgrade_points = min(ps.upgrade_points + 1, DEFAULT_MAX_UPGRADE_POINTS)
            ps.turns_until_points = DEFAULT_UPGRADE_INTERVAL

        cmd = commands.get(pid)
        if cmd is None:
            continue
        upgrade_name = cmd._upgrade
        if upgrade_name is None or ps.upgrade_points <= 0:
            continue

        tier_max = dict(UPGRADE_TIERS)
        if upgrade_name not in tier_max:
            continue
        if ps.upgrade_levels.get(upgrade_name, 0) >= tier_max[upgrade_name]:
            continue

        ps.upgrade_levels[upgrade_name] = ps.upgrade_levels.get(upgrade_name, 0) + 1
        ps.upgrade_points -= 1


def _phase_build_repair(world: WorldState, commands: dict[str, Command]) -> None:
    exit_point_usage: dict[Position, int] = defaultdict(int)
    used_authors: set[str] = set()

    all_actions: list[tuple[str, PlantationAction]] = []
    for pid, cmd in commands.items():
        for action in cmd._actions:
            all_actions.append((pid, action))

    plant_by_pos: dict[Position, SimPlantation] = {}
    for p in world.plantations.values():
        plant_by_pos[p.position] = p

    for pid, action in all_actions:
        if len(action.path) != 3:
            continue

        author_pos, exit_pos, target_pos = action.path
        author = plant_by_pos.get(author_pos)
        if author is None or author.owner != pid or author.is_isolated:
            continue
        if author.id in used_authors:
            continue

        ps = world.players[pid]
        own_positions = world.get_player_plantation_positions(pid)

        if exit_pos != author_pos:
            if exit_pos not in own_positions:
                continue
            dx = abs(author_pos[0] - exit_pos[0])
            dy = abs(author_pos[1] - exit_pos[1])
            if max(dx, dy) > ps.signal_range:
                continue

        exit_plant = plant_by_pos.get(exit_pos)
        if exit_plant is None or exit_plant.owner != pid:
            continue

        dx = abs(exit_pos[0] - target_pos[0])
        dy = abs(exit_pos[1] - target_pos[1])
        if max(dx, dy) > DEFAULT_AR:
            continue

        tx, ty = target_pos
        if not (0 <= tx < world.map_size[0] and 0 <= ty < world.map_size[1]):
            continue
        if target_pos in world.mountains:
            continue

        used_authors.add(author.id)

        congestion = exit_point_usage[exit_pos]
        exit_point_usage[exit_pos] += 1

        target_plant = plant_by_pos.get(target_pos)

        if target_plant is not None and target_plant.owner == pid:
            effective_rs = max(0, ps.repair_speed - congestion)
            if effective_rs > 0:
                target_plant.hp = min(target_plant.hp + effective_rs, ps.max_hp)
        elif target_plant is not None and target_plant.owner != pid:
            if target_plant.immunity_until_turn > world.turn_no:
                continue
            effective_se = max(0, 5 - congestion)
            target_plant.hp -= effective_se
        else:
            effective_cs = max(0, CS - congestion)
            if effective_cs <= 0:
                continue
            con = world.constructions.get(target_pos)
            if con is None:
                con = SimConstruction(position=target_pos, progress=0, owner=pid)
                world.constructions[target_pos] = con
            elif con.owner != pid:
                continue
            con.progress += effective_cs
            con.had_progress_this_turn = True

            if con.progress >= BUILD_THRESHOLD:
                if len([p for p in world.plantations.values() if p.owner == pid and not p.is_isolated]) >= ps.plantation_limit:
                    continue
                plant_id = world.next_id()
                new_plant = SimPlantation(
                    id=plant_id,
                    position=target_pos,
                    hp=ps.max_hp,
                    is_main=False,
                    is_isolated=False,
                    owner=pid,
                    immunity_until_turn=world.turn_no + IMMUNITY_DURATION,
                    created_turn=world.turn_no,
                )
                world.plantations[plant_id] = new_plant
                plant_by_pos[target_pos] = new_plant
                del world.constructions[target_pos]

    dead_plants = [pid for pid, p in world.plantations.items() if p.hp <= 0]
    for pid in dead_plants:
        plant = world.plantations[pid]
        if plant.is_main:
            _destroy_player(world, plant.owner)
        else:
            del world.plantations[pid]


def _phase_degradation_isolated(world: WorldState) -> None:
    for p in list(world.plantations.values()):
        if not p.is_isolated:
            continue
        ps = world.players.get(p.owner)
        ds = ps.degradation_speed if ps else DS
        p.hp -= ds
        if p.hp <= 0:
            if p.is_main:
                _destroy_player(world, p.owner)
            else:
                del world.plantations[p.id]


def _phase_degradation_constructions(world: WorldState) -> None:
    to_remove = []
    for pos, con in world.constructions.items():
        if con.had_progress_this_turn:
            con.had_progress_this_turn = False
            continue
        ps = world.players.get(con.owner)
        ds = ps.degradation_speed if ps else DS
        con.progress -= ds
        if con.progress <= 0:
            to_remove.append(pos)
    for pos in to_remove:
        del world.constructions[pos]


def _phase_terraformation(world: WorldState) -> None:
    plants_to_remove = []

    for p in list(world.plantations.values()):
        if p.is_isolated:
            continue
        ps = world.players.get(p.owner)
        if ps is None or ps.hq_lost_turn >= 0:
            continue

        cell = world.terraformed.get(p.position)
        if cell is None:
            cell = SimTerraformCell(position=p.position, progress=0)
            world.terraformed[p.position] = cell

        if cell.progress >= 100:
            continue

        old_progress = cell.progress
        cell.progress = min(100, cell.progress + TS)
        gained = cell.progress - old_progress

        multiplier = REINFORCED_MULTIPLIER if is_reinforced(p.position) else 1.0
        ps.score += gained * POINTS_PER_PERCENT * multiplier

        if cell.progress >= 100:
            cell.turns_since_complete = 0
            plants_to_remove.append(p.id)

    for plant_id in plants_to_remove:
        if plant_id in world.plantations:
            plant = world.plantations[plant_id]
            if plant.is_main:
                _destroy_player(world, plant.owner)
            else:
                del world.plantations[plant_id]

    for cell in world.terraformed.values():
        if cell.turns_since_complete >= 0:
            cell.turns_since_complete += 1
            if cell.turns_since_complete > CELL_DEGRADE_DELAY:
                cell.progress = max(0, cell.progress - CELL_DEGRADE_SPEED)
                if cell.progress <= 0:
                    cell.turns_since_complete = -1


def _phase_remove_completed(world: WorldState) -> None:
    pass


def _phase_connectivity(world: WorldState) -> None:
    for pid in world.players:
        connected = world.compute_connectivity(pid)
        for p in world.plantations.values():
            if p.owner == pid:
                p.is_isolated = p.position not in connected


def _phase_enforce_limits(world: WorldState) -> None:
    for pid, ps in world.players.items():
        own = [p for p in world.plantations.values() if p.owner == pid]
        if len(own) <= ps.plantation_limit:
            continue
        own.sort(key=lambda p: p.created_turn)
        while len(own) > ps.plantation_limit:
            oldest = own.pop(0)
            if oldest.is_main:
                _destroy_player(world, pid)
                break
            del world.plantations[oldest.id]


def _destroy_player(world: WorldState, player_id: str) -> None:
    ps = world.players.get(player_id)
    if ps is None:
        return
    penalty = max(1, int(ps.score * HQ_LOSS_PENALTY))
    ps.score = max(0, ps.score - penalty)
    ps.hq_lost_turn = world.turn_no

    to_del = [pid for pid, p in world.plantations.items() if p.owner == player_id]
    for pid in to_del:
        del world.plantations[pid]

    cons_del = [pos for pos, c in world.constructions.items() if c.owner == player_id]
    for pos in cons_del:
        del world.constructions[pos]

    _respawn_player(world, player_id)


def _respawn_player(world: WorldState, player_id: str) -> None:
    ps = world.players[player_id]
    ps.hq_lost_turn = -1

    w, h = world.map_size
    margin = 5
    candidates = [
        (margin, margin),
        (w - 1 - margin, margin),
        (margin, h - 1 - margin),
        (w - 1 - margin, h - 1 - margin),
    ]

    occupied = {p.position for p in world.plantations.values()}
    for pos in candidates:
        if pos not in occupied and pos not in world.mountains:
            plant_id = world.next_id()
            world.plantations[plant_id] = SimPlantation(
                id=plant_id,
                position=pos,
                hp=ps.max_hp,
                is_main=True,
                is_isolated=False,
                owner=player_id,
                immunity_until_turn=world.turn_no + IMMUNITY_DURATION,
                created_turn=world.turn_no,
            )
            return
