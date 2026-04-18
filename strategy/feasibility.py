from __future__ import annotations

import argparse
import math
from statistics import mean

from strategy.core.mapgen import generate_map
from strategy.core.rules import MAX_TURNS
from strategy.core.state import (
    BUILD_THRESHOLD,
    CELL_DEGRADE_DELAY,
    CELL_DEGRADE_SPEED,
    DEFAULT_CS,
    DEFAULT_MAX_UPGRADE_POINTS,
    DEFAULT_PLANTATION_LIMIT,
    DEFAULT_TS,
    DEFAULT_UPGRADE_INTERVAL,
    POINTS_PER_PERCENT,
    REINFORCED_MULTIPLIER,
    cell_max_points,
)
from strategy.runner import (
    DEFAULT_TEST_MAP_HEIGHT,
    DEFAULT_TEST_MAP_WIDTH,
    run_simulation,
)

BENCHMARK_BOTS = [
    "current",
    "v003",
    "bench_reinf",
    "bench_stable",
    "bench_overdrive",
    "bench_million",
    "bench_blob",
    "bench_factory",
    "bench_peak",
]


def _theoretical_limit_area(turns: int) -> int:
    total = 0
    spent = 0
    next_upgrade_turn = DEFAULT_UPGRADE_INTERVAL
    for turn in range(turns):
        if turn >= next_upgrade_turn and spent < min(DEFAULT_MAX_UPGRADE_POINTS, 10):
            spent += 1
            next_upgrade_turn += DEFAULT_UPGRADE_INTERVAL
        total += DEFAULT_PLANTATION_LIMIT + spent
    return total


def _tile_cycle_turns() -> int:
    terraform_turns = math.ceil(100 / DEFAULT_TS)
    build_turns = math.ceil(BUILD_THRESHOLD / DEFAULT_CS)
    degrade_turns = math.ceil(100 / CELL_DEGRADE_SPEED)
    return terraform_turns + CELL_DEGRADE_DELAY + degrade_turns + build_turns


def compute_seed_bounds(seed: int, width: int, height: int, density: float, turns: int) -> dict:
    world = generate_map(seed, width, height, density, num_players=1)
    reinforced_positions = [
        pos for pos in ((x, y) for x in range(width) for y in range(height))
        if pos not in world.mountains and pos[0] % 7 == 0 and pos[1] % 7 == 0
    ]
    normal_positions = [
        pos for pos in ((x, y) for x in range(width) for y in range(height))
        if pos not in world.mountains and pos not in reinforced_positions
    ]

    slot_turn_capacity = _theoretical_limit_area(turns) * DEFAULT_TS * POINTS_PER_PERCENT * REINFORCED_MULTIPLIER
    cycle_turns = _tile_cycle_turns()
    reinforced_tile_capacity = len(reinforced_positions) * cell_max_points((0, 0)) * (turns / cycle_turns)
    normal_tile_capacity = len(normal_positions) * int(100 * POINTS_PER_PERCENT) * (turns / cycle_turns)
    optimistic_mix_capacity = reinforced_tile_capacity + normal_tile_capacity * 0.15

    return {
        "seed": seed,
        "mountains": len(world.mountains),
        "reinforced_tiles": len(reinforced_positions),
        "normal_tiles": len(normal_positions),
        "slot_turn_hard_cap": round(slot_turn_capacity, 0),
        "reinforced_cycle_cap": round(reinforced_tile_capacity, 0),
        "optimistic_mix_cap": round(min(slot_turn_capacity, optimistic_mix_capacity), 0),
        "cycle_turns": cycle_turns,
    }


def run_empirical_benchmarks(
    bots: list[str],
    seeds: list[int],
    width: int,
    height: int,
    density: float,
    turns: int,
) -> list[dict]:
    rows: list[dict] = []
    for bot in bots:
        for seed in seeds:
            result = run_simulation(
                bot_name=bot,
                seed=seed,
                turns=turns,
                width=width,
                height=height,
                mountain_density=density,
            )
            result["score_per_built"] = round(result["score"] / max(1, result["built_plantations"]), 2)
            result["score_per_visible_cell"] = round(result["score"] / max(1, result["cells_terraformed"]), 2)
            rows.append(result)
    return rows


def _print_bounds(bounds: list[dict]) -> None:
    print("\n=== Analytical Bounds ===")
    print(f"{'seed':>4} {'reinf':>6} {'cycle':>6} {'slot_cap':>12} {'reinf_cap':>12} {'mix_cap':>12}")
    for row in bounds:
        print(
            f"{row['seed']:>4} {row['reinforced_tiles']:>6} {row['cycle_turns']:>6} "
            f"{row['slot_turn_hard_cap']:>12.0f} {row['reinforced_cycle_cap']:>12.0f} {row['optimistic_mix_cap']:>12.0f}"
        )
    print(
        f"mean optimistic_mix_cap={mean(row['optimistic_mix_cap'] for row in bounds):.0f} | "
        f"best optimistic_mix_cap={max(row['optimistic_mix_cap'] for row in bounds):.0f}"
    )


def _print_empirical(rows: list[dict]) -> None:
    print("\n=== Empirical Benchmarks ===")
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["bot"], []).append(row)

    print(
        f"{'bot':<16} {'mean':>10} {'best':>10} {'min':>10} "
        f"{'max_p':>8} {'built':>8} {'cells':>8} {'t30':>6} {'lowHQ':>6} {'kill':>10}"
    )
    best_row: dict | None = None
    for bot, bot_rows in sorted(grouped.items()):
        scores = [r["score"] for r in bot_rows]
        max_plants = mean(r["max_plantations"] for r in bot_rows)
        built = mean(r["built_plantations"] for r in bot_rows)
        cells = mean(r["cells_terraformed"] for r in bot_rows)
        turns_30 = mean(r.get("turns_ge_30", 0) for r in bot_rows)
        low_hq = mean(r.get("turns_low_hq_escape", 0) for r in bot_rows)
        kills = mean(r["kill_score"] for r in bot_rows)
        print(
            f"{bot:<16} {mean(scores):>10.0f} {max(scores):>10.0f} {min(scores):>10.0f} "
            f"{max_plants:>8.1f} {built:>8.1f} {cells:>8.1f} {turns_30:>6.1f} {low_hq:>6.1f} {kills:>10.0f}"
        )
        bot_best = max(bot_rows, key=lambda row: row["score"])
        if best_row is None or bot_best["score"] > best_row["score"]:
            best_row = bot_best

    assert best_row is not None
    print("\n=== Best Run Diagnostics ===")
    print(
        f"bot={best_row['bot']} seed={best_row['seed']} score={best_row['score']:.0f} "
        f"terraform={best_row['terraform_score']:.0f} kill={best_row['kill_score']:.0f} "
        f"max_plant={best_row['max_plantations']} built={best_row['built_plantations']} "
        f"cells={best_row['cells_terraformed']} t20={best_row.get('turns_ge_20', 0)} "
        f"t30={best_row.get('turns_ge_30', 0)} t35={best_row.get('turns_ge_35', 0)} "
        f"reloc={best_row.get('hq_relocations', 0)} low_hq={best_row.get('turns_low_hq_escape', 0)} "
        f"score_per_built={best_row['score_per_built']:.1f} "
        f"score_per_visible_cell={best_row['score_per_visible_cell']:.1f} "
        f"losses=sabo:{best_row['sabotage_lost_plantations']},cata:{best_row['cataclysm_lost_plantations']},"
        f"lodge:{best_row['lodge_lost_plantations']},decay:{best_row['decay_lost_plantations']},"
        f"limit:{best_row['limit_lost_plantations']}"
    )

    worst_row = min(rows, key=lambda row: row["score"])
    print("\n=== Worst Run Diagnostics ===")
    print(
        f"bot={worst_row['bot']} seed={worst_row['seed']} score={worst_row['score']:.0f} "
        f"terraform={worst_row['terraform_score']:.0f} max_plant={worst_row['max_plantations']} "
        f"built={worst_row['built_plantations']} cells={worst_row['cells_terraformed']} "
        f"respawns={worst_row.get('respawns', 0)} reloc={worst_row.get('hq_relocations', 0)} "
        f"low_hq={worst_row.get('turns_low_hq_escape', 0)} "
        f"losses=lodge:{worst_row['lodge_lost_plantations']},decay:{worst_row['decay_lost_plantations']},"
        f"limit:{worst_row['limit_lost_plantations']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Feasibility analysis for 1,000,000 local score")
    parser.add_argument("--seeds", type=int, default=8)
    parser.add_argument("--seed-start", type=int, default=1)
    parser.add_argument("--turns", type=int, default=MAX_TURNS)
    parser.add_argument("--width", type=int, default=DEFAULT_TEST_MAP_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_TEST_MAP_HEIGHT)
    parser.add_argument("--density", type=float, default=0.08)
    parser.add_argument("--bots", type=str, default=",".join(BENCHMARK_BOTS))
    args = parser.parse_args()

    seeds = list(range(args.seed_start, args.seed_start + args.seeds))
    bots = [bot.strip() for bot in args.bots.split(",") if bot.strip()]

    bounds = [compute_seed_bounds(seed, args.width, args.height, args.density, args.turns) for seed in seeds]
    rows = run_empirical_benchmarks(bots, seeds, args.width, args.height, args.density, args.turns)

    _print_bounds(bounds)
    _print_empirical(rows)

    best_score = max(row["score"] for row in rows)
    best_mix_cap = max(row["optimistic_mix_cap"] for row in bounds)
    print("\n=== Verdict ===")
    print(
        f"target=1000000 | best_empirical={best_score:.0f} | best_optimistic_mix_cap={best_mix_cap:.0f} | "
        f"gap_to_target={1000000 - best_score:.0f}"
    )
    if best_mix_cap < 1000000:
        print("Local simulator looks materially misaligned with the tournament target: optimistic cap is below 1,000,000.")
    elif best_score < 250000:
        print("The target is not disproven analytically, but current empirical strategies are far below the required throughput.")
    else:
        print("The target looks plausible enough to justify a dedicated bot rewrite.")


if __name__ == "__main__":
    main()
