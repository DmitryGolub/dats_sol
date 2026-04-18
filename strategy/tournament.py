from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime
from pathlib import Path

from strategy.bots import get_all_bots
from strategy.runner import (
    DEFAULT_TEST_MAP_HEIGHT,
    DEFAULT_TEST_MAP_WIDTH,
    run_match,
    run_simulation,
)
from strategy.core.rules import MAX_TURNS

log = logging.getLogger("tournament")

EXPERIMENTS_DIR = Path(__file__).parent / "experiments"
RUNS_CSV = EXPERIMENTS_DIR / "runs.csv"
MATRIX_CSV = EXPERIMENTS_DIR / "tournament_matrix.csv"

CSV_FIELDS = [
    "timestamp", "seed", "bot", "mode", "opponents", "turns",
    "score", "terraform_score", "kill_score",
    "hq_lost_turn", "respawns",
    "max_plantations", "final_plantations", "built_plantations",
    "cells_terraformed", "own_cells",
    "lost_plantations",
    "sabotage_lost_plantations", "cataclysm_lost_plantations",
    "lodge_lost_plantations", "decay_lost_plantations", "limit_lost_plantations",
    "sabotage_kills", "beaver_kills",
    "sabotage_damage_dealt", "sabotage_damage_taken",
    "storm_damage_taken", "earthquake_damage_taken", "lodge_damage_taken_hp",
    "upgrades_purchased",
]


DEFAULT_TOURNAMENT_SEEDS = 4


def run_tournament(
    bot_names: list[str] | None = None,
    num_seeds: int = DEFAULT_TOURNAMENT_SEEDS,
    turns: int = MAX_TURNS,
    width: int = DEFAULT_TEST_MAP_WIDTH,
    height: int = DEFAULT_TEST_MAP_HEIGHT,
    versus: bool = False,
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

    if versus:
        return _run_versus(names, seeds, turns, width, height, all_bots)
    else:
        return _run_solo(names, seeds, turns, width, height)


def _run_solo(
    names: list[str], seeds: list[int], turns: int, width: int, height: int,
) -> list[dict]:
    total = len(names) * len(seeds)
    results: list[dict] = []

    log.info("Соло-турнир: %d ботов × %d сидов = %d партий", len(names), len(seeds), total)

    for i, bot_name in enumerate(names):
        for j, seed in enumerate(seeds):
            idx = i * len(seeds) + j + 1
            log.info("[%d/%d] %s seed=%d", idx, total, bot_name, seed)
            result = run_simulation(bot_name=bot_name, seed=seed, turns=turns, width=width, height=height)
            result["timestamp"] = datetime.now().isoformat(timespec="seconds")
            result["mode"] = "solo"
            result["opponents"] = ""
            results.append(result)

    _save_results(results)
    _generate_matrix(results, names, seeds)
    _print_summary(results, names)
    return results


def _run_versus(
    names: list[str], seeds: list[int], turns: int, width: int, height: int, all_bots: dict,
) -> list[dict]:
    if len(names) < 2:
        log.error("Для versus-режима нужно минимум 2 бота. Доступные: %s", list(all_bots.keys()))
        sys.exit(1)

    total = len(seeds)
    results: list[dict] = []

    log.info("Versus-турнир: %d ботов × %d сидов = %d матчей", len(names), len(seeds), total)

    for j, seed in enumerate(seeds):
        log.info("[%d/%d] seed=%d: %s", j + 1, total, seed, " vs ".join(names))
        match_results = run_match(bot_names=names, seed=seed, turns=turns, width=width, height=height)
        for name, r in match_results.items():
            r["timestamp"] = datetime.now().isoformat(timespec="seconds")
            r["mode"] = "versus"
            r["opponents"] = ",".join(n for n in names if n != name)
            results.append(r)

    _save_results(results)
    _generate_matrix(results, names, seeds)
    _print_summary(results, names)
    return results


def _save_results(results: list[dict]) -> None:
    EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    _migrate_runs_header_if_needed()

    file_exists = RUNS_CSV.exists() and RUNS_CSV.stat().st_size > 0
    with open(RUNS_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerows(results)

    log.info("Результаты сохранены в %s", RUNS_CSV)


def _migrate_runs_header_if_needed() -> None:
    """Если у существующего runs.csv заголовок не совпадает с CSV_FIELDS,
    переписываем файл с новым заголовком, заполняя отсутствующие колонки пустыми значениями."""
    if not RUNS_CSV.exists() or RUNS_CSV.stat().st_size == 0:
        return
    with open(RUNS_CSV, newline="") as f:
        reader = csv.reader(f)
        try:
            existing_header = next(reader)
        except StopIteration:
            return
        if existing_header == CSV_FIELDS:
            return
        rows = [dict(zip(existing_header, row)) for row in reader]

    with open(RUNS_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})
    log.info("runs.csv: миграция заголовка (%d → %d колонок)", len(existing_header), len(CSV_FIELDS))


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
    mode = results[0].get("mode", "solo") if results else "solo"
    print(f"\n{'=' * 70}")
    print(f"{'Бот':<20} {'Ср. очки':>10} {'Мин':>8} {'Макс':>8} {'Пл.макс':>8} {'Win%':>7}")
    print("-" * 70)

    all_seeds = sorted({r["seed"] for r in results})
    seed_winners: dict[int, str] = {}
    for seed in all_seeds:
        seed_results = [(r["bot"], r["score"]) for r in results if r["seed"] == seed]
        if seed_results:
            seed_winners[seed] = max(seed_results, key=lambda x: x[1])[0]

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
        wins = sum(1 for s in all_seeds if seed_winners.get(s) == name)
        win_pct = wins / len(all_seeds) * 100 if all_seeds else 0
        print(f"{name:<20} {mean_s:>10.0f} {min_s:>8.0f} {max_s:>8.0f} {mean_p:>8.1f} {win_pct:>6.1f}%")

    print("=" * 70)

    if mode == "versus":
        best = max(bot_names, key=lambda n: sum(r["score"] for r in results if r["bot"] == n))
        print(f"Победитель: {best}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    parser = argparse.ArgumentParser(description="Турнир ботов DatsSol")
    parser.add_argument("--bots", type=str, default="", help="Имена ботов через запятую (пусто = все)")
    parser.add_argument("--seeds", type=int, default=DEFAULT_TOURNAMENT_SEEDS)
    parser.add_argument("--turns", type=int, default=MAX_TURNS)
    parser.add_argument("--width", type=int, default=DEFAULT_TEST_MAP_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_TEST_MAP_HEIGHT)
    parser.add_argument("--versus", action="store_true", help="Мультиплеер: все боты на одной карте")
    args = parser.parse_args()

    bot_names = [n.strip() for n in args.bots.split(",") if n.strip()] or None

    run_tournament(
        bot_names=bot_names,
        num_seeds=args.seeds,
        turns=args.turns,
        width=args.width,
        height=args.height,
        versus=args.versus,
    )


if __name__ == "__main__":
    main()
