from __future__ import annotations

import argparse
import logging
import sys
import time

from api.models import Command
from strategy.bots import get_all_bots
from strategy.core.mapgen import generate_map
from strategy.core.rules import simulate_turn, MAX_TURNS

log = logging.getLogger("runner")

DEFAULT_TEST_MAP_WIDTH = 60
DEFAULT_TEST_MAP_HEIGHT = 60


def run_simulation(
    bot_name: str = "current",
    seed: int = 1,
    turns: int = MAX_TURNS,
    width: int = DEFAULT_TEST_MAP_WIDTH,
    height: int = DEFAULT_TEST_MAP_HEIGHT,
    mountain_density: float = 0.08,
    verbose: bool = False,
) -> dict:
    return run_match(
        bot_names=[bot_name],
        seed=seed,
        turns=turns,
        width=width,
        height=height,
        mountain_density=mountain_density,
        verbose=verbose,
    )[bot_name]


def run_match(
    bot_names: list[str],
    seed: int = 1,
    turns: int = MAX_TURNS,
    width: int = DEFAULT_TEST_MAP_WIDTH,
    height: int = DEFAULT_TEST_MAP_HEIGHT,
    mountain_density: float = 0.08,
    verbose: bool = False,
) -> dict[str, dict]:
    all_bots = get_all_bots()
    for name in bot_names:
        if name not in all_bots:
            log.error("Бот '%s' не найден. Доступные: %s", name, list(all_bots.keys()))
            sys.exit(1)

    num_players = len(bot_names)
    world = generate_map(seed, width, height, mountain_density, num_players=num_players)

    player_ids = [f"p{i}" for i in range(num_players)]
    bots = {}
    for i, name in enumerate(bot_names):
        bot = all_bots[name]()
        bot.reset()
        bots[player_ids[i]] = (name, bot)

    max_plants: dict[str, int] = {pid: 0 for pid in player_ids}
    start = time.monotonic()

    for turn in range(turns):
        commands: dict[str, Command] = {}
        for pid, (name, bot) in bots.items():
            perception = world.to_game_state(pid)
            try:
                cmd = bot.decide(perception)
            except Exception as exc:
                if verbose:
                    log.warning("Ход %d, %s: ошибка: %s", turn, name, exc)
                cmd = Command()
            commands[pid] = cmd

        simulate_turn(world, commands)

        for pid in player_ids:
            count = len(world.get_player_plantations(pid))
            if count > max_plants[pid]:
                max_plants[pid] = count

        if verbose and turn % 50 == 0:
            parts = []
            for pid, (name, _) in bots.items():
                ps = world.players[pid]
                pc = len(world.get_player_plantations(pid))
                parts.append(f"{name}={ps.score:.0f}({pc}p)")
            log.info("Ход %3d | %s", turn, " | ".join(parts))

    elapsed = time.monotonic() - start

    results: dict[str, dict] = {}
    name_counts: dict[str, int] = {}
    for pid, (name, _) in bots.items():
        ps = world.players[pid]
        own_cells = sum(
            1 for c in world.terraformed.values()
            if c.progress > 0 and any(
                p.owner == pid and p.position == c.position
                for p in world.plantations.values()
            )
        )
        cells = sum(1 for c in world.terraformed.values() if c.progress > 0)
        # Уникализируем ключ для случая одинаковых имён ботов в матче
        name_counts[name] = name_counts.get(name, 0) + 1
        key = name if name_counts[name] == 1 else f"{name}#{name_counts[name]}"
        final_plantations = len(world.get_player_plantations(pid))
        results[key] = {
            "seed": seed,
            "bot": name,
            "player_id": pid,
            "score": ps.score,
            "hq_lost_turn": ps.hq_lost_turn if ps.hq_lost_turn >= 0 else None,
            "max_plantations": max_plants[pid],
            "final_plantations": final_plantations,
            "own_cells": own_cells,
            "cells_terraformed": cells,
            "lost_plantations": ps.lost_plantations,
            "beaver_kills": ps.beaver_kills,
            "sabotage_kills": ps.sabotage_kills,
            "respawns": ps.respawns,
            "terraform_score": round(ps.terraform_score, 2),
            "kill_score": round(ps.kill_score, 2),
            "built_plantations": ps.built_plantations,
            "upgrades_purchased": ps.upgrades_purchased,
            "sabotage_damage_dealt": ps.sabotage_damage_dealt,
            "sabotage_damage_taken": ps.sabotage_damage_taken,
            "storm_damage_taken": ps.storm_damage_taken,
            "earthquake_damage_taken": ps.earthquake_damage_taken,
            "lodge_damage_taken_hp": ps.lodge_damage_taken_hp,
            "sabotage_lost_plantations": ps.sabotage_lost_plantations,
            "cataclysm_lost_plantations": ps.cataclysm_lost_plantations,
            "lodge_lost_plantations": ps.lodge_lost_plantations,
            "decay_lost_plantations": ps.decay_lost_plantations,
            "limit_lost_plantations": ps.limit_lost_plantations,
            "turns": turns,
            "elapsed": round(elapsed, 2),
        }

    if verbose:
        log.info("--- Итог (%.2fs) ---", elapsed)
        ranking = sorted(results.values(), key=lambda r: -r["score"])
        for i, r in enumerate(ranking):
            log.info("#%d %s: %.0f очков, макс %d плантаций", i + 1, r["bot"], r["score"], r["max_plantations"])

    return results


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    parser = argparse.ArgumentParser(description="Симуляция DatsSol (соло или мультиплеер)")
    parser.add_argument("--bots", default="current", help="Имена ботов через запятую (напр. current,v001)")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--turns", type=int, default=MAX_TURNS)
    parser.add_argument("--width", type=int, default=DEFAULT_TEST_MAP_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_TEST_MAP_HEIGHT)
    parser.add_argument("--density", type=float, default=0.08)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    bot_names = [n.strip() for n in args.bots.split(",") if n.strip()]

    results = run_match(
        bot_names=bot_names,
        seed=args.seed,
        turns=args.turns,
        width=args.width,
        height=args.height,
        mountain_density=args.density,
        verbose=args.verbose,
    )

    for name, r in results.items():
        hq = r["hq_lost_turn"] if r["hq_lost_turn"] is not None else "none"
        print(f"bot={name} seed={r['seed']} score={r['score']:.0f} "
              f"tf={r.get('terraform_score', 0):.0f} kill={r.get('kill_score', 0):.0f} "
              f"max_plant={r['max_plantations']} fin_plant={r.get('final_plantations', 0)} "
              f"built={r.get('built_plantations', 0)} cells={r['cells_terraformed']} "
              f"hq_lost={hq} respawns={r.get('respawns', 0)} "
              f"sabo_k={r.get('sabotage_kills', 0)} beav_k={r.get('beaver_kills', 0)} "
              f"lost={r.get('lost_plantations', 0)}"
              f" (sabo={r.get('sabotage_lost_plantations', 0)},"
              f"cata={r.get('cataclysm_lost_plantations', 0)},"
              f"lodge={r.get('lodge_lost_plantations', 0)},"
              f"decay={r.get('decay_lost_plantations', 0)},"
              f"limit={r.get('limit_lost_plantations', 0)}) "
              f"dmg_dealt={r.get('sabotage_damage_dealt', 0)} "
              f"dmg_taken=sabo:{r.get('sabotage_damage_taken', 0)},"
              f"storm:{r.get('storm_damage_taken', 0)},"
              f"quake:{r.get('earthquake_damage_taken', 0)},"
              f"lodge:{r.get('lodge_damage_taken_hp', 0)} "
              f"upgrades={r.get('upgrades_purchased', 0)}")


if __name__ == "__main__":
    main()
