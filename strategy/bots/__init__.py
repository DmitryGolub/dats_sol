from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strategy.base import BaseStrategy


def get_all_bots() -> dict[str, type[BaseStrategy]]:
    from strategy.base import BaseStrategy

    bots: dict[str, type[BaseStrategy]] = {}

    from strategy.bots.current import CurrentBot
    from strategy.bots.benchmarks import (
        BenchmarkBlobBot,
        BenchmarkFactoryBot,
        BenchmarkMillionBot,
        BenchmarkOverdriveBot,
        BenchmarkPeakBot,
        BenchmarkReinforcedBot,
        BenchmarkStableBot,
    )
    from strategy.bots.headful import HeadfulBot
    bots[CurrentBot.name] = CurrentBot
    bots[BenchmarkReinforcedBot.name] = BenchmarkReinforcedBot
    bots[BenchmarkStableBot.name] = BenchmarkStableBot
    bots[BenchmarkOverdriveBot.name] = BenchmarkOverdriveBot
    bots[BenchmarkMillionBot.name] = BenchmarkMillionBot
    bots[BenchmarkBlobBot.name] = BenchmarkBlobBot
    bots[BenchmarkFactoryBot.name] = BenchmarkFactoryBot
    bots[BenchmarkPeakBot.name] = BenchmarkPeakBot
    bots[HeadfulBot.name] = HeadfulBot

    snapshots_dir = Path(__file__).parent / "snapshots"
    if snapshots_dir.exists():
        for info in pkgutil.iter_modules([str(snapshots_dir)]):
            mod = importlib.import_module(f"strategy.bots.snapshots.{info.name}")
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, BaseStrategy)
                    and attr is not BaseStrategy
                    and hasattr(attr, "name")
                ):
                    bots[attr.name] = attr

    return bots
