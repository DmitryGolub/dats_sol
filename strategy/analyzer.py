from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

log = logging.getLogger("analyzer")

RUNS_CSV = Path(__file__).parent / "experiments" / "runs.csv"


def load_results(path: Path = RUNS_CSV) -> list[dict]:
    if not path.exists():
        log.error("Файл %s не найден. Сначала запустите турнир.", path)
        sys.exit(1)

    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            row["score"] = float(row["score"])
            row["max_plantations"] = int(row["max_plantations"])
            row["cells_terraformed"] = int(row["cells_terraformed"])
            row["seed"] = int(row["seed"])
            rows.append(row)
    return rows


def analyze_all(results: list[dict]) -> None:
    bots: dict[str, list[dict]] = {}
    for r in results:
        bots.setdefault(r["bot"], []).append(r)

    print("\n" + "=" * 70)
    print(f"{'Бот':<20} {'Партий':>7} {'Ср.очки':>10} {'Медиана':>10} {'Ст.откл':>10} {'Win%':>7}")
    print("-" * 70)

    bot_means: dict[str, float] = {}
    for name, rows in sorted(bots.items()):
        scores = sorted([r["score"] for r in rows])
        n = len(scores)
        mean = sum(scores) / n
        median = scores[n // 2] if n % 2 else (scores[n // 2 - 1] + scores[n // 2]) / 2
        variance = sum((s - mean) ** 2 for s in scores) / n
        std = variance ** 0.5
        bot_means[name] = mean

        wins = 0
        seeds_seen: dict[int, float] = {}
        for r in results:
            if r["seed"] not in seeds_seen or r["score"] > seeds_seen[r["seed"]]:
                seeds_seen[r["seed"]] = r["score"]
        for r in rows:
            if r["score"] >= seeds_seen.get(r["seed"], 0):
                wins += 1
        win_pct = wins / n * 100 if n else 0

        print(f"{name:<20} {n:>7} {mean:>10.0f} {median:>10.0f} {std:>10.0f} {win_pct:>6.1f}%")

    print("=" * 70)

    if bot_means:
        best = max(bot_means, key=lambda k: bot_means[k])
        print(f"\nЛучший бот: {best} (ср. очки: {bot_means[best]:.0f})")


def compare_bots(results: list[dict], bot_a: str, bot_b: str) -> None:
    a_by_seed: dict[int, float] = {}
    b_by_seed: dict[int, float] = {}

    for r in results:
        if r["bot"] == bot_a:
            a_by_seed[r["seed"]] = r["score"]
        elif r["bot"] == bot_b:
            b_by_seed[r["seed"]] = r["score"]

    common_seeds = sorted(set(a_by_seed) & set(b_by_seed))
    if not common_seeds:
        print(f"Нет общих сидов для {bot_a} и {bot_b}")
        return

    print(f"\n{'Сид':>6} {bot_a:>15} {bot_b:>15} {'Дельта':>10} {'Победитель':>12}")
    print("-" * 62)

    a_wins = 0
    b_wins = 0
    deltas = []

    for seed in common_seeds:
        sa = a_by_seed[seed]
        sb = b_by_seed[seed]
        delta = sa - sb
        deltas.append(delta)
        winner = bot_a if delta > 0 else (bot_b if delta < 0 else "ничья")
        if delta > 0:
            a_wins += 1
        elif delta < 0:
            b_wins += 1
        print(f"{seed:>6} {sa:>15.0f} {sb:>15.0f} {delta:>+10.0f} {winner:>12}")

    n = len(common_seeds)
    mean_delta = sum(deltas) / n
    print("-" * 62)
    print(f"{'Итого':<6} {'':<15} {'':<15} {mean_delta:>+10.0f}")
    print(f"\n{bot_a}: {a_wins} побед | {bot_b}: {b_wins} побед | Ничьих: {n - a_wins - b_wins}")
    print(f"Win-rate {bot_a}: {a_wins / n * 100:.1f}%")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    parser = argparse.ArgumentParser(description="Анализ результатов турнира DatsSol")
    parser.add_argument("--input", type=str, default=str(RUNS_CSV))
    parser.add_argument("--compare", type=str, default="", help="Два бота через запятую для сравнения")
    args = parser.parse_args()

    results = load_results(Path(args.input))

    if args.compare:
        parts = [n.strip() for n in args.compare.split(",")]
        if len(parts) != 2:
            log.error("--compare принимает ровно два имени через запятую")
            sys.exit(1)
        compare_bots(results, parts[0], parts[1])
    else:
        analyze_all(results)


if __name__ == "__main__":
    main()
