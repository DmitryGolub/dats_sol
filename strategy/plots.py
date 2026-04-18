"""Анализ metrics.jsonl и генерация графиков/сводки по раунду.

Запуск:
    uv run python -m strategy.plots
    uv run python -m strategy.plots --input logs/metrics.jsonl --out logs/plots
    uv run python -m strategy.plots --show
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Series:
    turns: list[int]
    plantations: list[int]
    main: list[int]
    isolated: list[int]
    plantations_hp: list[int]
    beavers: list[int]
    beavers_hp: list[int]
    enemies: list[int]
    enemy_hp: list[int]
    constructions: list[int]
    terraformed: list[int]
    meteo: list[int]
    upgrade_points: list[float]
    actions: list[int]
    builds: list[int]
    repairs: list[int]
    sabotages: list[int]
    attacks: list[int]
    decide_ms: list[float]
    send_ms: list[float]
    success: list[bool]


def load(path: Path) -> Series:
    turns: dict[int, dict] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = row.get("turn_no")
            if t is None:
                continue
            # если ход встречается несколько раз (рестарт) — берём последний
            turns[int(t)] = row

    ordered = [turns[t] for t in sorted(turns)]

    def col(key: str, default=0):
        return [r.get(key, default) if r.get(key) is not None else default for r in ordered]

    return Series(
        turns=[int(r["turn_no"]) for r in ordered],
        plantations=col("plantations"),
        main=col("main_plantations"),
        isolated=col("isolated_plantations"),
        plantations_hp=col("plantations_hp_sum"),
        beavers=col("beavers"),
        beavers_hp=col("beavers_hp_sum"),
        enemies=col("enemy_plantations"),
        enemy_hp=col("enemy_hp_sum"),
        constructions=col("constructions"),
        terraformed=col("terraformed_cells"),
        meteo=col("meteo_forecasts"),
        upgrade_points=col("upgrade_points", 0),
        actions=col("actions"),
        builds=col("builds"),
        repairs=col("repairs"),
        sabotages=col("sabotages"),
        attacks=col("attacks"),
        decide_ms=col("decide_ms", 0.0),
        send_ms=col("send_ms", 0.0),
        success=[bool(r.get("success", True)) for r in ordered],
    )


@dataclass
class Summary:
    turns_seen: int
    first_turn: int
    last_turn: int
    max_plantations: int
    max_plantations_at: int
    total_built: int
    total_repairs: int
    total_sabotages: int
    total_attacks: int
    hq_losses: int
    hq_loss_turns: list[int]
    scoring_start_turn: int | None
    tempo_loss_turn: int | None
    tempo_loss_reason: str | None
    fail_turns: int


def _find_tempo_loss(s: Series) -> tuple[int | None, str | None]:
    """Где темп упал: сначала ищем крупнейшее падение числа плантаций,
    потом — окно минимальной агрегированной активности после пика."""
    if len(s.turns) < 5:
        return None, None

    # 1) резкое падение кол-ва плантаций
    peak = max(s.plantations) if s.plantations else 0
    if peak > 0:
        peak_idx = s.plantations.index(peak)
        max_drop = 0
        drop_idx = None
        for i in range(peak_idx + 1, len(s.plantations)):
            drop = peak - s.plantations[i]
            if drop > max_drop:
                max_drop = drop
                drop_idx = i
        if drop_idx is not None and max_drop >= max(2, peak // 4):
            return s.turns[drop_idx], f"plantations {peak}→{s.plantations[drop_idx]}"

    # 2) минимум активности в скользящем окне после пика плантаций
    window = 10
    activity = [b + r + sab + a for b, r, sab, a in zip(s.builds, s.repairs, s.sabotages, s.attacks)]
    if len(activity) < window:
        return None, None
    peak_idx = s.plantations.index(max(s.plantations)) if s.plantations else 0
    best_i = None
    best_sum = None
    for i in range(peak_idx, len(activity) - window):
        ws = sum(activity[i:i + window])
        if best_sum is None or ws < best_sum:
            best_sum = ws
            best_i = i
    if best_i is not None and best_sum is not None:
        return s.turns[best_i], f"activity window sum={best_sum} over {window} turns"
    return None, None


def summarize(s: Series) -> Summary:
    max_p = max(s.plantations) if s.plantations else 0
    max_p_idx = s.plantations.index(max_p) if s.plantations else 0

    # HQ loss = переход main_plantations с >=1 на 0
    hq_losses = 0
    hq_turns: list[int] = []
    prev_main = 0
    for turn, m in zip(s.turns, s.main):
        if prev_main >= 1 and m == 0:
            hq_losses += 1
            hq_turns.append(turn)
        prev_main = m

    # Scoring start — первый ход, когда плантаций >= 3 и продержалось 3 хода подряд
    scoring_start = None
    target = max(3, max_p // 3)
    streak = 0
    for turn, p in zip(s.turns, s.plantations):
        if p >= target:
            streak += 1
            if streak >= 3:
                scoring_start = turn - 2
                break
        else:
            streak = 0

    tempo_turn, tempo_reason = _find_tempo_loss(s)

    return Summary(
        turns_seen=len(s.turns),
        first_turn=s.turns[0] if s.turns else 0,
        last_turn=s.turns[-1] if s.turns else 0,
        max_plantations=max_p,
        max_plantations_at=s.turns[max_p_idx] if s.turns else 0,
        total_built=sum(s.builds),
        total_repairs=sum(s.repairs),
        total_sabotages=sum(s.sabotages),
        total_attacks=sum(s.attacks),
        hq_losses=hq_losses,
        hq_loss_turns=hq_turns,
        scoring_start_turn=scoring_start,
        tempo_loss_turn=tempo_turn,
        tempo_loss_reason=tempo_reason,
        fail_turns=sum(1 for ok in s.success if not ok),
    )


def render_plots(s: Series, summary: Summary, out_dir: Path, show: bool = False) -> list[Path]:
    import matplotlib
    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    def _mark(ax) -> None:
        if summary.scoring_start_turn is not None:
            ax.axvline(summary.scoring_start_turn, color="green", linestyle="--", alpha=0.6, label="scoring start")
        if summary.tempo_loss_turn is not None:
            ax.axvline(summary.tempo_loss_turn, color="red", linestyle="--", alpha=0.6, label="tempo loss")
        for t in summary.hq_loss_turns:
            ax.axvline(t, color="black", linestyle=":", alpha=0.5)

    # Dashboard 2x2
    fig, axes = plt.subplots(2, 2, figsize=(14, 9))

    ax = axes[0][0]
    ax.plot(s.turns, s.plantations, label="plantations", color="#2b8")
    ax.plot(s.turns, s.main, label="main (HQ)", color="#c33")
    ax.plot(s.turns, s.isolated, label="isolated", color="#888")
    ax.plot(s.turns, s.constructions, label="constructions", color="#38c", alpha=0.7)
    ax.set_title(f"Plantations (peak={summary.max_plantations} @ t={summary.max_plantations_at})")
    ax.set_xlabel("turn")
    ax.legend(loc="best", fontsize=8)
    ax.grid(True, alpha=0.3)
    _mark(ax)

    ax = axes[0][1]
    ax.plot(s.turns, s.plantations_hp, label="own HP", color="#2b8")
    ax.plot(s.turns, s.enemy_hp, label="enemy HP", color="#c33")
    ax.plot(s.turns, s.beavers_hp, label="beavers HP", color="#a50")
    ax.set_title("HP over time")
    ax.set_xlabel("turn")
    ax.legend(loc="best", fontsize=8)
    ax.grid(True, alpha=0.3)
    _mark(ax)

    ax = axes[1][0]
    width = 0.9
    ax.bar(s.turns, s.builds, label="build", color="#38c", width=width)
    bottoms = list(s.builds)
    ax.bar(s.turns, s.repairs, bottom=bottoms, label="repair", color="#2b8", width=width)
    bottoms = [a + b for a, b in zip(bottoms, s.repairs)]
    ax.bar(s.turns, s.sabotages, bottom=bottoms, label="sabotage", color="#c33", width=width)
    bottoms = [a + b for a, b in zip(bottoms, s.sabotages)]
    ax.bar(s.turns, s.attacks, bottom=bottoms, label="attack", color="#a50", width=width)
    ax.set_title(f"Actions per turn (built={summary.total_built}, rep={summary.total_repairs}, sab={summary.total_sabotages}, atk={summary.total_attacks})")
    ax.set_xlabel("turn")
    ax.legend(loc="best", fontsize=8)
    ax.grid(True, alpha=0.3)
    _mark(ax)

    ax = axes[1][1]
    ax.plot(s.turns, s.decide_ms, label="decide ms", color="#38c")
    ax.plot(s.turns, s.send_ms, label="send ms", color="#c33")
    ax.set_title("Timings (ms)")
    ax.set_xlabel("turn")
    ax.legend(loc="best", fontsize=8)
    ax.grid(True, alpha=0.3)
    _mark(ax)

    fig.tight_layout()
    dash_path = out_dir / "dashboard.png"
    fig.savefig(dash_path, dpi=110)
    paths.append(dash_path)

    # Отдельный график по упрейдам / метео / террафрму
    fig2, ax2 = plt.subplots(figsize=(12, 4))
    ax2.plot(s.turns, s.upgrade_points, label="upgrade points", color="#a50")
    ax2.plot(s.turns, s.terraformed, label="terraformed cells", color="#2b8")
    ax2.plot(s.turns, s.meteo, label="meteo forecasts", color="#c33")
    ax2.set_xlabel("turn")
    ax2.set_title("Upgrades / terraform / meteo")
    ax2.legend(loc="best", fontsize=8)
    ax2.grid(True, alpha=0.3)
    _mark(ax2)
    fig2.tight_layout()
    aux_path = out_dir / "aux.png"
    fig2.savefig(aux_path, dpi=110)
    paths.append(aux_path)

    if show:
        plt.show()
    plt.close("all")
    return paths


def format_summary(summary: Summary) -> str:
    lines = [
        f"Turns seen: {summary.turns_seen} (t={summary.first_turn}..{summary.last_turn})",
        f"Max plantations: {summary.max_plantations} @ turn {summary.max_plantations_at}",
        f"Totals — built: {summary.total_built}, repairs: {summary.total_repairs}, sabotages: {summary.total_sabotages}, attacks: {summary.total_attacks}",
        f"HQ losses: {summary.hq_losses}" + (f" @ turns {summary.hq_loss_turns}" if summary.hq_loss_turns else ""),
        f"Scoring start turn: {summary.scoring_start_turn if summary.scoring_start_turn is not None else 'n/a'}",
        f"Tempo loss: {summary.tempo_loss_turn if summary.tempo_loss_turn is not None else 'n/a'}"
        + (f" ({summary.tempo_loss_reason})" if summary.tempo_loss_reason else ""),
        f"Failed turns (command errors): {summary.fail_turns}",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Graph & summary DatsSol metrics")
    parser.add_argument("--input", type=str, default="logs/metrics.jsonl", help="metrics.jsonl path")
    parser.add_argument("--out", type=str, default="logs/plots", help="output dir for PNGs")
    parser.add_argument("--show", action="store_true", help="show windows instead of saving only")
    args = parser.parse_args()

    src = Path(args.input)
    if not src.exists():
        print(f"metrics file not found: {src}", file=sys.stderr)
        sys.exit(1)

    series = load(src)
    if not series.turns:
        print("no turns in metrics file", file=sys.stderr)
        sys.exit(1)

    summary = summarize(series)
    out_dir = Path(args.out)
    paths = render_plots(series, summary, out_dir, show=args.show)

    print(format_summary(summary))
    print("Plots:")
    for p in paths:
        print(f"  {p}")


if __name__ == "__main__":
    main()
