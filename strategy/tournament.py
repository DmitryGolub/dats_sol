from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime
from pathlib import Path

from strategy.bots import get_all_bots
from strategy.runner import run_simulation
from strategy.core.rules import MAX_TURNS

log = logging.getLogger("tournament")

EXPERIMENTS_DIR = Path(__file__).parent / "experiments"
RUNS_CSV = EXPERIMENTS_DIR / "runs.csv"
MATRIX_CSV = EXPERIMENTS_DIR / "tournament_matrix.csv"

CSV_FIELDS = ["timestamp", "seed", "bot", "score", "hq_lost_turn", "max_plantations", "cells_terraformed", "turns"]


def run_tournament(
    bot_names: list[str] | None = None,
    num_seeds: int = 10,
    turns: int = MAX_TURNS,
    width: int = 80,
    height: int = 80,
) -> list[dict]:
    all_bots = get_all_bots()

    if bot_names:
        for name in bot_names:
            if name not in all_bots:
                log.error("Бот '%s' не найден. Доступные: %s", name, list(all_bots.keys()))
                sys.exit(1)
        names = bot_names
    else:
        names = list(all_bots.keys())

    seeds = list(range(1, num_seeds + 1))
    total = len(names) * len(seeds)
    results: list[dict] = []

    log.info("Турнир: %d ботов × %d сидов = %d партий", len(names), len(seeds), total)

    for i, bot_name in enumerate(names):
        for j, seed in enumerate(seeds):
            idx = i * len(seeds) + j + 1
            log.info("[%d/%d] %s seed=%d", idx, total, bot_name, seed)

            result = run_simulation(
                bot_name=bot_name,
                seed=seed,
                turns=turns,
                width=width,
                height=height,
            )
            result["timestamp"] = datetime.now().isoformat(timespec="seconds")
            results.append(result)

    _save_results(results)
    _generate_matrix(results, names, seeds)
    _print_summary(results, names)

    return results


def _save_results(results: list[dict]) -> None:
    EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    file_exists = RUNS_CSV.exists() and RUNS_CSV.stat().st_size > 0

    with open(RUNS_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerows(results)

    log.info("Результаты сохранены в %s", RUNS_CSV)


def _generate_matrix(results: list[dict], bot_names: list[str], seeds: list[int]) -> None:
    lookup: dict[tuple[str, int], float] = {}
    for r in results:
        lookup[(r["bot"], r["seed"])] = r["score"]

    with open(MATRIX_CSV, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["bot"] + [f"seed_{s}" for s in seeds] + ["mean"])
        for name in bot_names:
            scores = [lookup.get((name, s), 0) for s in seeds]
            mean = sum(scores) / len(scores) if scores else 0
            writer.writerow([name] + [f"{s:.0f}" for s in scores] + [f"{mean:.0f}"])

    log.info("Матрица сохранена в %s", MATRIX_CSV)


def _print_summary(results: list[dict], bot_names: list[str]) -> None:
    print("\n" + "=" * 60)
    print(f"{'Бот':<20} {'Ср. очки':>10} {'Мин':>8} {'Макс':>8} {'Пл.макс':>8}")
    print("-" * 60)

    for name in bot_names:
        bot_results = [r for r in results if r["bot"] == name]
        scores = [r["score"] for r in bot_results]
        plants = [r["max_plantations"] for r in bot_results]
        if not scores:
            continue
        mean_s = sum(scores) / len(scores)
        min_s = min(scores)
        max_s = max(scores)
        mean_p = sum(plants) / len(plants)
        print(f"{name:<20} {mean_s:>10.0f} {min_s:>8.0f} {max_s:>8.0f} {mean_p:>8.1f}")

    print("=" * 60)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    parser = argparse.ArgumentParser(description="Турнир ботов DatsSol")
    parser.add_argument("--bots", type=str, default="", help="Имена ботов через запятую (пусто = все)")
    parser.add_argument("--seeds", type=int, default=10)
    parser.add_argument("--turns", type=int, default=MAX_TURNS)
    parser.add_argument("--width", type=int, default=80)
    parser.add_argument("--height", type=int, default=80)
    args = parser.parse_args()

    bot_names = [n.strip() for n in args.bots.split(",") if n.strip()] or None

    run_tournament(
        bot_names=bot_names,
        num_seeds=args.seeds,
        turns=args.turns,
        width=args.width,
        height=args.height,
    )


if __name__ == "__main__":
    main()
