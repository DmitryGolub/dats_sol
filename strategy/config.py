"""Игровые константы и стратегические параметры (см. docs/strategy.md §1-2)."""

from __future__ import annotations

# --- Базовые характеристики плантации ---
DEFAULT_MHP = 50
DEFAULT_TS = 5
DEFAULT_CS = 5
DEFAULT_RS = 5
DEFAULT_SE = 5
DEFAULT_BE = 5
DEFAULT_DS = 10
DEFAULT_AR = 2
DEFAULT_SR = 3
DEFAULT_VR = 3

# --- Лимиты ---
DEFAULT_SETTLEMENT_LIMIT = 30
MAX_UPGRADE_POINTS = 15
UPGRADE_INTERVAL = 30

# --- Иммунитет ---
BUILD_IMMUNITY_TURNS = 3

# --- Раунд ---
TURNS_PER_ROUND = 600

# --- Клетки и очки ---
NORMAL_CELL_MAX_POINTS = 1000
BOOSTED_CELL_MAX_POINTS = 1500
BOOSTED_CELL_MODULO = 7
CELL_FULL_TERRAFORMATION = 100
CELL_DEGRADATION_AFTER = 80
CELL_DEGRADATION_SPEED = 10

# --- Катаклизмы ---
SANDSTORM_DAMAGE = 2
EARTHQUAKE_DAMAGE = 10

# --- Штраф за потерю ЦУ ---
CU_LOSS_PENALTY_PERCENT = 5

# --- Апгрейды: cap по правилам ---
UPGRADE_CAPS: dict[str, int] = {
    "repair_power": 3,
    "max_hp": 5,
    "settlement_limit": 10,
    "signal_range": 10,
    "decay_mitigation": 3,
    "earthquake_mitigation": 3,
    "beaver_damage_mitigation": 5,
    "vision_range": 5,
}

# ===== Стратегические параметры (тюнинг) =====

# Пороги HP ЦУ
CU_HP_THRESHOLD_EMERGENCY = 0.20
CU_HP_THRESHOLD_ALL_REPAIR = 0.50
CU_HP_THRESHOLD_SOME_REPAIR = 0.80

# Защита
SHIELDS_REQUIRED = 2
KEEP_SLOTS_FREE = 1

# Экспансия
X7_SEARCH_RADIUS_MIN = 5
X7_SEARCH_RADIUS_MAX = 7
FALLBACK_ANY_CELL = True

# Порог ремонта не-ЦУ плантаций
NON_CU_REPAIR_HP_RATIO = 0.70

# Приоритет апгрейдов: (имя, до какого уровня качаем)
UPGRADE_PRIORITY: list[tuple[str, int]] = [
    ("max_hp", 5),
    ("repair_power", 3),
    ("settlement_limit", 10),
    ("decay_mitigation", 3),
]

# Флаги стратегии v1
DO_SABOTAGE = False
DO_BEAVER_HUNT = False
DO_WEATHER_PREDICT = False
