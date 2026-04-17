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


def run_simulation(
    bot_name: str = "current",
    seed: int = 1,
    turns: int = MAX_TURNS,
    width: int = 80,
    height: int = 80,
    mountain_density: float = 0.08,
    verbose: bool = False,
) -> dict:
    bots = get_all_bots()
    if bot_name not in bots:
        log.error("Бот '%s' не найден. Доступные: %s", bot_name, list(bots.keys()))
        sys.exit(1)

    bot = bots[bot_name]()
    bot.reset()

    world = generate_map(seed, width, height, mountain_density, num_players=1)
    player_id = "p0"

    max_plantations = 0
    cells_terraformed = 0

    start = time.monotonic()

    for turn in range(turns):
        ps = world.players[player_id]
        if ps.hq_lost_turn >= 0 and not world.get_player_plantations(player_id):
            pass

        perception = world.to_game_state(player_id)

        try:
            cmd = bot.decide(perception)
        except Exception as exc:
            log.warning("Ход %d: ошибка бота: %s", turn, exc)
            cmd = Command()

        commands = {player_id: cmd}
        simulate_turn(world, commands)

        plant_count = len(world.get_player_plantations(player_id))
        max_plantations = max(max_plantations, plant_count)
        cells_terraformed = sum(
            1 for c in world.terraformed.values() if c.progress > 0
        )

        if verbose and turn % 50 == 0:
            log.info(
                "Ход %3d | Очки: %8.0f | Плантаций: %2d | Клеток: %3d",
                turn, ps.score, plant_count, cells_terraformed,
            )

    elapsed = time.monotonic() - start
    ps = world.players[player_id]

    result = {
        "seed": seed,
        "bot": bot_name,
        "score": ps.score,
        "hq_lost_turn": ps.hq_lost_turn if ps.hq_lost_turn >= 0 else None,
        "max_plantations": max_plantations,
        "cells_terraformed": cells_terraformed,
        "turns": turns,
        "elapsed": round(elapsed, 2),
    }

    if verbose:
        log.info("--- Итог ---")
        log.info("Бот: %s | Сид: %d | Очки: %.0f", bot_name, seed, ps.score)
        log.info("Макс. плантаций: %d | Клеток: %d | Время: %.2fs", max_plantations, cells_terraformed, elapsed)

    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    parser = argparse.ArgumentParser(description="Запуск одной симуляции DatsSol")
    parser.add_argument("--bot", default="current")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--turns", type=int, default=MAX_TURNS)
    parser.add_argument("--width", type=int, default=80)
    parser.add_argument("--height", type=int, default=80)
    parser.add_argument("--density", type=float, default=0.08)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    result = run_simulation(
        bot_name=args.bot,
        seed=args.seed,
        turns=args.turns,
        width=args.width,
        height=args.height,
        mountain_density=args.density,
        verbose=args.verbose,
    )

    hq = result["hq_lost_turn"] or "none"
    print(f"seed={result['seed']} bot={result['bot']} score={result['score']:.0f} "
          f"max_plantations={result['max_plantations']} cells={result['cells_terraformed']} "
          f"hq_lost={hq} time={result['elapsed']}s")


if __name__ == "__main__":
    main()
