from __future__ import annotations

from collections import defaultdict

from api.models import Command, PlantationAction, Position
from .state import (
    BUILD_THRESHOLD,
    CELL_DEGRADE_DELAY,
    CELL_DEGRADE_SPEED,
    DEFAULT_AR,
    DEFAULT_MAX_UPGRADE_POINTS,
    DEFAULT_TS,
    DEFAULT_UPGRADE_INTERVAL,
    EARTHQUAKE_SPAWN_PROB,
    HQ_LOSS_PENALTY,
    IMMUNITY_DURATION,
    LODGE_RADIUS,
    POINTS_PER_PERCENT,
    REINFORCED_MULTIPLIER,
    SANDSTORM_DAMAGE,
    SANDSTORM_FORMING_TURNS,
    SANDSTORM_RADIUS,
    SANDSTORM_SPAWN_PROB,
    SANDSTORM_SPEED_MAX,
    SANDSTORM_SPEED_MIN,
    SimBeaverLodge,
    SimConstruction,
    SimMeteoEvent,
    SimPlantation,
    SimTerraformCell,
    UPGRADE_TIERS,
    WorldState,
    cell_max_points,
    is_reinforced,
)

MAX_TURNS = 600


def simulate_turn(world: WorldState, commands: dict[str, Command]) -> WorldState:
    classified = _classify_actions(world, commands)

    _phase_upgrades(world, commands)
    _phase_repair_build(world, classified)
    _recompute_connectivity(world)
    _phase_sabotage(world, classified)
    _recompute_connectivity(world)
    _phase_player_attack_lodges(world, classified)
    _phase_relocate_hq(world, commands)
    _recompute_connectivity(world)
    _phase_lodge_damage(world)
    _recompute_connectivity(world)
    _phase_degradation_isolated(world)
    _phase_degradation_constructions(world)
    _phase_terraformation(world)
    _recompute_connectivity(world)
    _phase_respawn(world)
    _phase_cataclysms(world)
    _recompute_connectivity(world)
    _phase_enforce_limits(world)
    _phase_lodge_regen(world)

    world.turn_no += 1
    return world


# ---------- classification ----------


def _classify_actions(world: WorldState, commands: dict[str, Command]) -> dict[str, dict[str, list]]:
    """Проверить и классифицировать действия игроков.

    Возвращает: {player_id: {'repair': [...], 'build': [...], 'sabotage': [...], 'lodge': [...]}}
    Каждый элемент — (author_plant, exit_pos, target_pos).
    """
    plant_by_pos: dict[Position, SimPlantation] = {p.position: p for p in world.plantations.values()}
    lodge_by_pos: dict[Position, SimBeaverLodge] = {l.position: l for l in world.beaver_lodges.values()}

    result: dict[str, dict[str, list]] = {pid: {"repair": [], "build": [], "sabotage": [], "lodge": []} for pid in world.players}
    used_authors: dict[str, set[str]] = defaultdict(set)

    for pid, cmd in commands.items():
        ps = world.players.get(pid)
        if ps is None:
            continue
        own_positions = world.get_player_plantation_positions(pid)

        for action in cmd._actions:
            if len(action.path) != 3:
                continue
            author_pos, exit_pos, target_pos = action.path

            author = plant_by_pos.get(author_pos)
            if author is None or author.owner != pid or author.is_isolated:
                continue
            if author.id in used_authors[pid]:
                continue

            # No self-heal
            if target_pos == author_pos:
                continue

            # Signal-range check (if exit != author)
            if exit_pos != author_pos:
                if exit_pos not in own_positions:
                    continue
                if _cheb(author_pos, exit_pos) > ps.signal_range:
                    continue

            exit_plant = plant_by_pos.get(exit_pos)
            if exit_plant is None or exit_plant.owner != pid or exit_plant.is_isolated:
                continue

            # AR check from exit
            if _cheb(exit_pos, target_pos) > DEFAULT_AR:
                continue

            # Bounds + mountain
            tx, ty = target_pos
            w, h = world.map_size
            if not (0 <= tx < w and 0 <= ty < h):
                continue
            if target_pos in world.mountains:
                continue

            used_authors[pid].add(author.id)

            target_plant = plant_by_pos.get(target_pos)
            lodge = lodge_by_pos.get(target_pos)

            if target_plant is not None:
                if target_plant.owner == pid:
                    result[pid]["repair"].append((author, exit_pos, target_pos))
                else:
                    if target_plant.immunity_until_turn > world.turn_no:
                        continue
                    result[pid]["sabotage"].append((author, exit_pos, target_pos))
            elif lodge is not None:
                result[pid]["lodge"].append((author, exit_pos, target_pos))
            else:
                # Empty cell (or own/enemy construction) → build by collision
                result[pid]["build"].append((author, exit_pos, target_pos))

    return result


# ---------- phase 1: upgrades ----------


def _phase_upgrades(world: WorldState, commands: dict[str, Command]) -> None:
    tier_max = dict(UPGRADE_TIERS)
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
        name = cmd._upgrade
        if name is None or ps.upgrade_points <= 0:
            continue
        if name not in tier_max:
            continue
        if ps.upgrade_levels.get(name, 0) >= tier_max[name]:
            continue
        ps.upgrade_levels[name] = ps.upgrade_levels.get(name, 0) + 1
        ps.upgrade_points -= 1
        ps.upgrades_purchased += 1


# ---------- phase 2: repair + build ----------


def _phase_repair_build(world: WorldState, classified: dict[str, dict[str, list]]) -> None:
    plant_by_pos: dict[Position, SimPlantation] = {p.position: p for p in world.plantations.values()}
    exit_usage: dict[tuple[str, Position], int] = defaultdict(int)

    # --- Repairs ---
    for pid, groups in classified.items():
        ps = world.players[pid]
        for author, exit_pos, target_pos in groups["repair"]:
            congestion = exit_usage[(pid, exit_pos)]
            exit_usage[(pid, exit_pos)] += 1
            effective = max(0, ps.repair_speed - congestion)
            if effective <= 0:
                continue
            target = plant_by_pos.get(target_pos)
            if target is None or target.owner != pid:
                continue
            target.hp = min(target.hp + effective, ps.max_hp)

    # --- Builds: accumulate progress per (position, owner) ---
    contributions: dict[tuple[Position, str], int] = defaultdict(int)
    for pid, groups in classified.items():
        ps = world.players[pid]
        for author, exit_pos, target_pos in groups["build"]:
            congestion = exit_usage[(pid, exit_pos)]
            exit_usage[(pid, exit_pos)] += 1
            effective = max(0, ps.construction_speed - congestion)
            if effective <= 0:
                continue
            contributions[(target_pos, pid)] += effective

    for (pos, owner), gained in contributions.items():
        con = world.constructions.get((pos, owner))
        if con is None:
            con = SimConstruction(position=pos, progress=0, owner=owner)
            world.constructions[(pos, owner)] = con
        con.progress += gained
        con.had_progress_this_turn = True

    # --- Completions (handle collisions) ---
    completed_by_cell: dict[Position, list[str]] = defaultdict(list)
    for (pos, owner), con in list(world.constructions.items()):
        if con.progress >= BUILD_THRESHOLD:
            completed_by_cell[pos].append(owner)

    for cell, owners in completed_by_cell.items():
        if len(owners) > 1:
            # Все прогрессы обнуляются
            for owner in owners:
                del world.constructions[(cell, owner)]
            continue
        owner = owners[0]
        ps = world.players[owner]
        # Проверка лимита (включая изолированные)
        own_count = sum(1 for p in world.plantations.values() if p.owner == owner)
        if own_count >= ps.plantation_limit:
            # лимит превышен — оставляем постройку на 50 HP, но плантация создаётся и
            # древнейшая удаляется (см. _phase_enforce_limits)
            pass
        plant_id = world.next_id()
        new_plant = SimPlantation(
            id=plant_id,
            position=cell,
            hp=ps.max_hp,
            is_main=False,
            is_isolated=False,
            owner=owner,
            immunity_until_turn=world.turn_no + IMMUNITY_DURATION,
            created_turn=world.turn_no,
        )
        world.plantations[plant_id] = new_plant
        ps.built_plantations += 1
        # очистить ВСЕ постройки на этой клетке (включая собственную)
        to_del = [key for key in world.constructions.keys() if key[0] == cell]
        for key in to_del:
            del world.constructions[key]


# ---------- phase 3: sabotage ----------


def _phase_sabotage(world: WorldState, classified: dict[str, dict[str, list]]) -> None:
    plant_by_pos: dict[Position, SimPlantation] = {p.position: p for p in world.plantations.values()}
    exit_usage: dict[tuple[str, Position], int] = defaultdict(int)

    # Очистим журнал урона этого хода
    for p in world.plantations.values():
        p.damage_by.clear()

    for pid, groups in classified.items():
        ps = world.players[pid]
        for author, exit_pos, target_pos in groups["sabotage"]:
            congestion = exit_usage[(pid, exit_pos)]
            exit_usage[(pid, exit_pos)] += 1
            effective = max(0, ps.sabotage_efficiency - congestion)
            if effective <= 0:
                continue
            target = plant_by_pos.get(target_pos)
            if target is None or target.owner == pid:
                continue
            target.hp -= effective
            target.damage_by[pid] = target.damage_by.get(pid, 0) + effective
            ps.sabotage_damage_dealt += effective
            victim_ps = world.players.get(target.owner)
            if victim_ps is not None:
                victim_ps.sabotage_damage_taken += effective

    # Проверяем убийства и начисляем очки по last-hit
    dead: list[SimPlantation] = []
    for p in list(world.plantations.values()):
        if p.hp <= 0 and p.damage_by:
            dead.append(p)

    for p in dead:
        reward = cell_max_points(p.position)
        max_dmg = max(p.damage_by.values())
        winners = [pid for pid, dmg in p.damage_by.items() if dmg == max_dmg]
        share = reward / len(winners)
        for winner in winners:
            ws = world.players.get(winner)
            if ws is not None:
                ws.score += share
                ws.kill_score += share
                ws.sabotage_kills += 1
        # Учитываем потерю
        victim_ps = world.players.get(p.owner)
        if victim_ps is not None:
            victim_ps.lost_plantations += 1
            victim_ps.sabotage_lost_plantations += 1
        if p.is_main:
            _destroy_player(world, p.owner, skip_respawn=True)
        else:
            if p.id in world.plantations:
                del world.plantations[p.id]


# ---------- phase 4: player attacks on lodges ----------


def _phase_player_attack_lodges(world: WorldState, classified: dict[str, dict[str, list]]) -> None:
    exit_usage: dict[tuple[str, Position], int] = defaultdict(int)

    for lodge in world.beaver_lodges.values():
        lodge.damage_by.clear()

    lodge_by_pos: dict[Position, SimBeaverLodge] = {l.position: l for l in world.beaver_lodges.values()}

    for pid, groups in classified.items():
        ps = world.players[pid]
        for author, exit_pos, target_pos in groups["lodge"]:
            congestion = exit_usage[(pid, exit_pos)]
            exit_usage[(pid, exit_pos)] += 1
            effective = max(0, ps.beaver_efficiency - congestion)
            if effective <= 0:
                continue
            lodge = lodge_by_pos.get(target_pos)
            if lodge is None:
                continue
            lodge.hp -= effective
            lodge.damage_by[pid] = lodge.damage_by.get(pid, 0) + effective

    dead_lodges = [lid for lid, l in world.beaver_lodges.items() if l.hp <= 0]
    for lid in dead_lodges:
        lodge = world.beaver_lodges[lid]
        if lodge.damage_by:
            reward = 10 * cell_max_points(lodge.position)
            max_dmg = max(lodge.damage_by.values())
            winners = [pid for pid, dmg in lodge.damage_by.items() if dmg == max_dmg]
            share = reward / len(winners)
            for winner in winners:
                ws = world.players.get(winner)
                if ws is not None:
                    ws.score += share
                    ws.kill_score += share
                    ws.beaver_kills += 1
        del world.beaver_lodges[lid]


# ---------- phase 5: HQ relocate ----------


def _phase_relocate_hq(world: WorldState, commands: dict[str, Command]) -> None:
    for pid, cmd in commands.items():
        if cmd._relocate_main is None:
            continue
        from_pos, to_pos = cmd._relocate_main
        ps = world.players.get(pid)
        if ps is None or ps.hq_lost_turn >= 0:
            continue
        hq = world.find_hq(pid)
        if hq is None or hq.position != tuple(from_pos):
            continue
        if _manhattan(tuple(from_pos), tuple(to_pos)) != 1:
            continue
        target = None
        for p in world.plantations.values():
            if p.owner == pid and p.position == tuple(to_pos):
                target = p
                break
        if target is None or target.is_isolated:
            continue
        hq.is_main = False
        target.is_main = True


# ---------- phase 6: beaver AoE damage ----------


def _phase_lodge_damage(world: WorldState) -> None:
    if not world.beaver_lodges:
        return
    dead: list[SimPlantation] = []
    for lodge in world.beaver_lodges.values():
        lx, ly = lodge.position
        for p in world.plantations.values():
            if p.immunity_until_turn > world.turn_no:
                continue
            px, py = p.position
            if abs(px - lx) <= LODGE_RADIUS and abs(py - ly) <= LODGE_RADIUS:
                ps = world.players.get(p.owner)
                dmg = ps.lodge_damage_taken if ps else 0
                if dmg > 0 and ps is not None:
                    ps.lodge_damage_taken_hp += dmg
                p.hp -= dmg
                if p.hp <= 0:
                    dead.append(p)
    # Также — по недостроенным в радиусе
    for (pos, owner), con in list(world.constructions.items()):
        for lodge in world.beaver_lodges.values():
            lx, ly = lodge.position
            if abs(pos[0] - lx) <= LODGE_RADIUS and abs(pos[1] - ly) <= LODGE_RADIUS:
                ps = world.players.get(owner)
                dmg = ps.lodge_damage_taken if ps else 0
                con.progress -= dmg
        if con.progress <= 0 and (pos, owner) in world.constructions:
            del world.constructions[(pos, owner)]

    for p in dead:
        if p.id not in world.plantations:
            continue
        victim_ps = world.players.get(p.owner)
        if victim_ps is not None:
            victim_ps.lost_plantations += 1
            victim_ps.lodge_lost_plantations += 1
        if p.is_main:
            _destroy_player(world, p.owner, skip_respawn=True)
        else:
            del world.plantations[p.id]


# ---------- phase 7: isolated decay ----------


def _phase_degradation_isolated(world: WorldState) -> None:
    dead: list[SimPlantation] = []
    for p in list(world.plantations.values()):
        if not p.is_isolated:
            continue
        ps = world.players.get(p.owner)
        ds = ps.degradation_speed if ps else 10
        p.hp -= ds
        if p.hp <= 0:
            dead.append(p)
    for p in dead:
        victim_ps = world.players.get(p.owner)
        if victim_ps is not None:
            victim_ps.lost_plantations += 1
            victim_ps.decay_lost_plantations += 1
        if p.is_main:
            _destroy_player(world, p.owner, skip_respawn=True)
        elif p.id in world.plantations:
            del world.plantations[p.id]


# ---------- phase 8: construction decay ----------


def _phase_degradation_constructions(world: WorldState) -> None:
    to_remove = []
    for key, con in world.constructions.items():
        if con.had_progress_this_turn:
            con.had_progress_this_turn = False
            continue
        ps = world.players.get(con.owner)
        ds = ps.degradation_speed if ps else 10
        con.progress -= ds
        if con.progress <= 0:
            to_remove.append(key)
    for key in to_remove:
        del world.constructions[key]


# ---------- phase 9: terraformation ----------


def _phase_terraformation(world: WorldState) -> None:
    plants_to_remove: list[str] = []
    for p in list(world.plantations.values()):
        ps = world.players.get(p.owner)
        if ps is None or ps.hq_lost_turn >= 0:
            continue

        cell = world.terraformed.get(p.position)
        if cell is None:
            cell = SimTerraformCell(position=p.position, progress=0)
            world.terraformed[p.position] = cell

        if cell.progress >= 100:
            continue

        old = cell.progress
        cell.progress = min(100, cell.progress + DEFAULT_TS)
        gained = cell.progress - old

        # Изолированные терраформируют, но без очков
        if not p.is_isolated and gained > 0:
            mult = REINFORCED_MULTIPLIER if is_reinforced(p.position) else 1.0
            earned = gained * POINTS_PER_PERCENT * mult
            ps.score += earned
            ps.terraform_score += earned

        if cell.progress >= 100:
            cell.turns_since_complete = 0
            plants_to_remove.append(p.id)

    for pid_str in plants_to_remove:
        p = world.plantations.get(pid_str)
        if p is None:
            continue
        if p.is_main:
            _destroy_player(world, p.owner, skip_respawn=True)
        else:
            del world.plantations[pid_str]

    # Износ клетки после 100%
    for cell in world.terraformed.values():
        if cell.turns_since_complete >= 0:
            cell.turns_since_complete += 1
            if cell.turns_since_complete > CELL_DEGRADE_DELAY:
                cell.progress = max(0, cell.progress - CELL_DEGRADE_SPEED)
                if cell.progress <= 0:
                    cell.turns_since_complete = -1


# ---------- phase 10: respawn ----------


def _phase_respawn(world: WorldState) -> None:
    for pid, ps in world.players.items():
        if ps.hq_lost_turn < 0:
            continue
        _respawn_player(world, pid)


# ---------- phase 11: cataclysms ----------


def _phase_cataclysms(world: WorldState) -> None:
    rng = world.rng
    w, h = world.map_size

    # Спавн нового землетрясения (если нет активного)
    if not any(e.kind == "earthquake" for e in world.meteo_events):
        if rng.random() < EARTHQUAKE_SPAWN_PROB:
            world.meteo_events.append(SimMeteoEvent(
                id=world.next_meteo_id(),
                kind="earthquake",
                turns_until=1,
            ))

    # Спавн новой бури (если нет активной)
    if not any(e.kind == "sandstorm" for e in world.meteo_events):
        if rng.random() < SANDSTORM_SPAWN_PROB:
            cx = rng.randint(5, max(6, w - 6))
            cy = rng.randint(5, max(6, h - 6))
            vx = rng.choice([-1, 1])
            vy = rng.choice([-1, 1])
            speed = rng.randint(SANDSTORM_SPEED_MIN, SANDSTORM_SPEED_MAX)
            world.meteo_events.append(SimMeteoEvent(
                id=world.next_meteo_id(),
                kind="sandstorm",
                position=(cx, cy),
                turns_until=SANDSTORM_FORMING_TURNS,
                is_forming=True,
                velocity=(vx, vy),
                speed=speed,
            ))

    remaining: list[SimMeteoEvent] = []
    dead_plants: list[SimPlantation] = []

    for ev in world.meteo_events:
        if ev.kind == "earthquake":
            ev.turns_until -= 1
            if ev.turns_until <= 0:
                for p in world.plantations.values():
                    if p.immunity_until_turn > world.turn_no:
                        continue
                    ps = world.players.get(p.owner)
                    dmg = ps.earthquake_damage if ps else 0
                    if dmg > 0 and ps is not None:
                        ps.earthquake_damage_taken += dmg
                    p.hp -= dmg
                    if p.hp <= 0:
                        dead_plants.append(p)
                for (pos, owner), con in list(world.constructions.items()):
                    ps = world.players.get(owner)
                    dmg = ps.earthquake_damage if ps else 0
                    con.progress -= dmg
                    if con.progress <= 0:
                        del world.constructions[(pos, owner)]
                # не добавляем в remaining → исчезает
            else:
                remaining.append(ev)
            continue

        if ev.kind == "sandstorm":
            if ev.is_forming:
                ev.turns_until -= 1
                if ev.turns_until <= 0:
                    ev.is_forming = False
                remaining.append(ev)
                continue
            if ev.position is None or ev.velocity is None:
                continue
            # Наносим урон в диске радиуса SANDSTORM_RADIUS (квадрат для простоты)
            cx, cy = ev.position
            for p in world.plantations.values():
                if p.immunity_until_turn > world.turn_no:
                    continue
                px, py = p.position
                if abs(px - cx) <= SANDSTORM_RADIUS and abs(py - cy) <= SANDSTORM_RADIUS:
                    new_hp = p.hp - SANDSTORM_DAMAGE
                    if new_hp < 1:
                        new_hp = 1
                    actual = p.hp - new_hp
                    if actual > 0:
                        ps = world.players.get(p.owner)
                        if ps is not None:
                            ps.storm_damage_taken += actual
                    p.hp = new_hp
            # Движение
            nx = cx + ev.velocity[0] * ev.speed
            ny = cy + ev.velocity[1] * ev.speed
            if -SANDSTORM_RADIUS <= nx < w + SANDSTORM_RADIUS and -SANDSTORM_RADIUS <= ny < h + SANDSTORM_RADIUS:
                ev.position = (nx, ny)
                remaining.append(ev)
            # иначе — ушла с карты

    world.meteo_events = remaining

    for p in dead_plants:
        if p.id not in world.plantations:
            continue
        victim_ps = world.players.get(p.owner)
        if victim_ps is not None:
            victim_ps.lost_plantations += 1
            victim_ps.cataclysm_lost_plantations += 1
        if p.is_main:
            _destroy_player(world, p.owner, skip_respawn=True)
        else:
            del world.plantations[p.id]


# ---------- phase 12 (enforce): connectivity + limits + lodge regen ----------


def _recompute_connectivity(world: WorldState) -> None:
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
                _destroy_player(world, pid, skip_respawn=True)
                break
            if oldest.id in world.plantations:
                del world.plantations[oldest.id]
                ps.lost_plantations += 1
                ps.limit_lost_plantations += 1


def _phase_lodge_regen(world: WorldState) -> None:
    from .state import LODGE_HP, LODGE_REGEN
    for lodge in world.beaver_lodges.values():
        lodge.hp = min(LODGE_HP, lodge.hp + LODGE_REGEN)


# ---------- helpers ----------


def _cheb(a: Position, b: Position) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def _manhattan(a: Position, b: Position) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _destroy_player(world: WorldState, player_id: str, skip_respawn: bool = False) -> None:
    ps = world.players.get(player_id)
    if ps is None:
        return
    penalty = max(1, int(ps.score * HQ_LOSS_PENALTY))
    ps.score = max(0, ps.score - penalty)
    ps.hq_lost_turn = world.turn_no

    to_del = [pid for pid, p in world.plantations.items() if p.owner == player_id]
    for pid in to_del:
        del world.plantations[pid]

    cons_del = [key for key, c in world.constructions.items() if c.owner == player_id]
    for key in cons_del:
        del world.constructions[key]

    if not skip_respawn:
        _respawn_player(world, player_id)


def _respawn_player(world: WorldState, player_id: str) -> None:
    ps = world.players.get(player_id)
    if ps is None:
        return

    w, h = world.map_size
    margin = 5
    candidates = [
        (margin, margin),
        (w - 1 - margin, margin),
        (margin, h - 1 - margin),
        (w - 1 - margin, h - 1 - margin),
    ]

    occupied = {p.position for p in world.plantations.values()}
    lodge_positions = {l.position for l in world.beaver_lodges.values()}
    for pos in candidates:
        if pos in occupied or pos in world.mountains or pos in lodge_positions:
            continue
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
        ps.hq_lost_turn = -1
        ps.respawns += 1
        return
