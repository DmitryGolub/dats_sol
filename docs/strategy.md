# DatsSol Bot — Strategy Specification

> Документ для Claude Code: спецификация стратегии бота для игры DatsSol. Содержит полную логику принятия решений на каждом ходу, структуру кода и пограничные случаи. Пишется как руководство к реализации, не как описание игры (правила игры упоминаются только в той мере, в которой влияют на решения).

## 0. TL;DR для разработчика

Бот реализует стратегию **"агрессивная экспансия к ×7 клеткам под защитой 2-щитового бункера"**. Каждый ход бот:
1. Получает состояние через `GET /api/arena`
2. Оценивает угрозу ЦУ (`urgency_level` от 0 до 3)
3. Резервирует плантации под защиту
4. Покупает апгрейд `max_hp` (пока не куплено 5 уровней)
5. Проверяет, безопасно ли строить (самоснос старой плантацией)
6. Назначает команды свободным плантациям (стройка/ремонт)
7. Отправляет `POST /api/command`

Цель — **не дать ЦУ умереть ни в одном раунде**, одновременно максимизируя очки через клетки X%7==0 && Y%7==0 (1500 очков/клетка вместо 1000).

---

## 1. Игровые константы (зафиксировать в config)

Эти значения берутся из правил игры и должны быть вынесены в отдельный модуль конфигурации, чтобы их можно было править без изменения логики.

```python
# Базовые характеристики плантации (без апгрейдов)
DEFAULT_MHP = 50              # max health points
DEFAULT_TS = 5                # terraforming speed (% за ход)
DEFAULT_CS = 5                # construction speed
DEFAULT_RS = 5                # repair speed
DEFAULT_SE = 5                # sabotage efficiency
DEFAULT_BE = 5                # beaver lair elimination
DEFAULT_DS = 10               # degradation speed
DEFAULT_AR = 2                # action range
DEFAULT_SR = 3                # signal range
DEFAULT_VR = 3                # vision range

# Лимиты
DEFAULT_SETTLEMENT_LIMIT = 30
MAX_UPGRADE_POINTS = 15
UPGRADE_INTERVAL = 30         # ходов между очками апгрейда

# Иммунитет после стройки
BUILD_IMMUNITY_TURNS = 3

# Раунд
TURNS_PER_ROUND = 600

# Клетки и очки
NORMAL_CELL_MAX_POINTS = 1000
BOOSTED_CELL_MAX_POINTS = 1500
BOOSTED_CELL_MODULO = 7       # клетка усиленная, если X%7==0 && Y%7==0
CELL_FULL_TERRAFORMATION = 100  # процентов
CELL_DEGRADATION_AFTER = 80   # через сколько ходов начинается деградация готовой клетки
CELL_DEGRADATION_SPEED = 10   # % за ход

# Логово бобров
BEAVER_LAIR_HP = 100
BEAVER_LAIR_REGEN = 5         # HP/ход
BEAVER_ATTACK_DAMAGE = 15     # HP/ход по всем плантациям в AR=2
BEAVER_LAIR_AR = 2
BEAVER_KILL_MULTIPLIER = 10   # очков = 10× от очков клетки

# Катаклизмы
SANDSTORM_DAMAGE = 2          # HP/ход для плантаций на траектории
SANDSTORM_MIN_HP = 1          # буря не может добить
EARTHQUAKE_DAMAGE = 10        # мгновенно при ударе

# Штраф за потерю ЦУ
CU_LOSS_PENALTY_PERCENT = 5

# Эндпоинты
API_BASE = "https://games-test.datsteam.dev"  # уточнить для финала
ENDPOINT_ARENA = "/api/arena"
ENDPOINT_COMMAND = "/api/command"
ENDPOINT_LOGS = "/api/logs"
```

### Апгрейды — названия и максимумы

```python
UPGRADE_CAPS = {
    "repair_power": 3,              # +1 RS, макс 3 → RS=8
    "max_hp": 5,                    # +10 HP, макс 5 → MHP=100
    "settlement_limit": 10,         # +1 лимит, макс 10 → 40 плантаций
    "signal_range": None,           # нет явного cap в правилах, но ограничено общим лимитом очков
    "decay_mitigation": 3,          # -2 DS, макс 3 → DS=4
    "earthquake_mitigation": 3,     # -2 урон от землетрясения, макс 3
    "beaver_damage_mitigation": 5,  # -2 урон от бобров, макс 5 → урон 15-10=5
    "vision_range": 5,              # +2 VR, макс 5 → VR=13
}
```

---

## 2. Стратегические параметры (тюнинг)

Эти параметры НЕ игровые константы — это наши настройки стратегии, которые мы будем крутить по результатам тестовых раундов. Вынести в отдельный блок конфига с комментариями.

```python
# Пороги HP ЦУ для реакции (в процентах от MHP)
CU_HP_THRESHOLD_EMERGENCY = 0.20    # <20% → экстренный перенос
CU_HP_THRESHOLD_ALL_REPAIR = 0.50   # <50% → все соседи чинят
CU_HP_THRESHOLD_SOME_REPAIR = 0.80  # <80% → 2 соседа чинят

# Защита
SHIELDS_REQUIRED = 2                # 2 щита с противоположных сторон
KEEP_SLOTS_FREE = 1                 # держим хотя бы 1 свободный слот в лимите

# Экспансия
X7_SEARCH_RADIUS_MIN = 5            # минимальный радиус поиска ×7
X7_SEARCH_RADIUS_MAX = 7            # максимальный радиус поиска ×7
FALLBACK_ANY_CELL = True            # разрешить стройку на обычных клетках после исчерпания ×7

# Апгрейды
UPGRADE_PRIORITY = [
    ("max_hp", 5),                  # сначала все 5 уровней max_hp
    # дальше логика второго этапа — определится после первых раундов
]

# Флаги стратегии
DO_SABOTAGE = False                 # диверсии против противников — выключены в v1
DO_BEAVER_HUNT = False              # охота на бобров — выключена в v1
DO_WEATHER_PREDICT = False          # предсказание бури — выключено в v1
```

---

## 3. Структура проекта

Рекомендуемое дерево модулей:

```
bot/
├── main.py                 # точка входа, цикл ходов
├── config.py               # константы и параметры (см. разделы 1-2)
├── api/
│   ├── client.py           # HTTP-клиент (requests), авторизация, ретраи
│   ├── arena.py            # парсинг /api/arena → типизированный GameState
│   ├── command.py          # сборка и отправка /api/command
│   └── logs.py             # /api/logs
├── model/
│   ├── state.py            # dataclass GameState, Plantation, Cell, Beaver, ...
│   ├── geometry.py         # утилиты: chebyshev_distance, manhattan, neighbors_ortho, is_boosted
│   └── chain.py            # построение графа связности от ЦУ, поиск shields/buffer/lost
├── decision/
│   ├── diagnose.py         # оценка urgency_level, состояние щитов, прогнозы катаклизмов
│   ├── defense.py          # резервирование плантаций на ремонт, перенос ЦУ
│   ├── upgrade.py          # выбор апгрейда на этот ход
│   ├── build_guard.py      # проверка "safe to build" (самоснос)
│   ├── targets.py          # генерация приоритезированного списка целей
│   └── assign.py           # назначение команд плантациям с учётом штрафа эффективности
├── util/
│   ├── logger.py           # структурное логирование ходов
│   └── replay.py           # сохранение состояний для анализа постфактум
└── tests/
    ├── test_geometry.py
    ├── test_chain.py
    ├── test_diagnose.py
    └── test_assign.py
```

---

## 4. Модель данных

### 4.1 Типы для представления состояния

```python
from dataclasses import dataclass, field
from typing import Optional

Coord = tuple[int, int]  # (x, y)

@dataclass(frozen=True)
class Plantation:
    id: str
    position: Coord
    is_main: bool              # True для ЦУ
    is_isolated: bool          # потеряна связь с ЦУ
    immunity_until_turn: int   # ход, до которого действует иммунитет
    hp: int

@dataclass(frozen=True)
class EnemyPlantation:
    id: str
    position: Coord
    hp: int

@dataclass(frozen=True)
class Cell:
    position: Coord
    terraformation_progress: int   # 0..100
    turns_until_degradation: Optional[int]  # None если ещё не завершена

@dataclass(frozen=True)
class Construction:
    position: Coord
    progress: int  # 0..50 (строится до MHP=50)

@dataclass(frozen=True)
class Beaver:
    id: str
    position: Coord
    hp: int

@dataclass(frozen=True)
class UpgradeTier:
    name: str
    current: int
    max: int

@dataclass(frozen=True)
class PlantationUpgrades:
    points: int
    interval_turns: int
    turns_until_points: int
    max_points: int
    tiers: list[UpgradeTier]

@dataclass(frozen=True)
class MeteoForecast:
    kind: str  # "earthquake" | "sandstorm"
    turns_until: int
    id: Optional[str] = None
    forming: Optional[bool] = None
    position: Optional[Coord] = None
    next_position: Optional[Coord] = None
    radius: Optional[int] = None

@dataclass
class GameState:
    turn_no: int
    next_turn_in: float
    size: tuple[int, int]
    action_range: int
    plantations: list[Plantation]
    enemy: list[EnemyPlantation]
    mountains: set[Coord]              # использовать set для O(1) lookup
    cells: list[Cell]
    construction: list[Construction]
    beavers: list[Beaver]
    plantation_upgrades: PlantationUpgrades
    meteo_forecasts: list[MeteoForecast]

    # Вычисляемые поля (заполняются при инициализации)
    main_plantation: Optional[Plantation] = None
    plantations_by_pos: dict[Coord, Plantation] = field(default_factory=dict)
    plantations_by_id: dict[str, Plantation] = field(default_factory=dict)
```

### 4.2 Критично важное — ПЕРСИСТЕНТНОЕ состояние бота

Игровой API **НЕ отдаёт возраст плантаций**. Это значит, что для защиты от самосноса мы обязаны сами отслеживать, когда каждая плантация была достроена.

```python
@dataclass
class BotMemory:
    """Персистентное состояние, которое бот ведёт между ходами."""
    plantation_birth_turn: dict[str, int]  # plantation_id → turn когда завершилась стройка
    known_plantation_ids: set[str]         # чтобы определить "новую" плантацию
    last_seen_state: Optional[GameState]   # для diff'а по плантациям
    round_no: int                          # текущий раунд
    last_turn_no: int                      # для детекции начала нового раунда

    def detect_new_plantations(self, current: GameState) -> list[Plantation]:
        """Плантации, появившиеся с прошлого хода → их birth_turn = current.turn_no."""
        ...
    
    def detect_round_reset(self, current: GameState) -> bool:
        """Если turn_no резко уменьшился — начался новый раунд."""
        return current.turn_no < self.last_turn_no
    
    def get_oldest_plantation(self, current: GameState) -> Optional[Plantation]:
        """Самая старая плантация по birth_turn."""
        ...
```

**Важно:** при старте бота в первом ходу мы НЕ знаем, какой birth_turn у ЦУ. Эвристика: считаем её birth_turn = −∞ (самая старая всегда). Это безопасная дефолтная гипотеза.

Состояние бота сохраняется в JSON-файл на диске после каждого хода (atomic write), чтобы пережить рестарты.

---

## 5. Главный цикл

```python
# main.py
def main():
    memory = load_memory_or_new()
    client = ApiClient(token=ENV["TOKEN"])
    
    while True:
        try:
            raw = client.get_arena()
            state = parse_arena(raw)
            
            # Детекция нового раунда
            if memory.detect_round_reset(state):
                memory.reset_for_new_round()
            
            # Обновление памяти
            memory.update_with(state)
            
            # Принятие решений
            command = build_turn_command(state, memory)
            
            # Отправка
            response = client.post_command(command)
            log_turn(state, command, response)
            
            # Сохранение памяти
            save_memory(memory)
            
        except ApiError as e:
            logger.error("API error: %s", e)
        
        # Ждём до следующего хода (next_turn_in из API минус небольшой запас)
        sleep_until_next_turn(state.next_turn_in)
```

Внутренняя функция `build_turn_command` — это основная логика, раскрытая в следующих разделах.

---

## 6. Этап 1: Диагностика угроз

```python
# decision/diagnose.py
from enum import IntEnum

class UrgencyLevel(IntEnum):
    NORMAL = 0         # HP ЦУ > 80% MHP, щиты целы
    LIGHT_REPAIR = 1   # HP ЦУ 50-80% MHP
    HEAVY_REPAIR = 2   # HP ЦУ 20-50% MHP
    EMERGENCY = 3      # HP ЦУ < 20% MHP → перенос

def compute_cu_mhp(state: GameState) -> int:
    """Текущий MHP плантаций с учётом апгрейдов."""
    max_hp_upgrade = next(
        (t.current for t in state.plantation_upgrades.tiers if t.name == "max_hp"),
        0
    )
    return DEFAULT_MHP + max_hp_upgrade * 10

def diagnose(state: GameState, memory: BotMemory) -> "Diagnosis":
    cu = state.main_plantation
    if cu is None:
        # ЦУ мертва — бот в состоянии респавна
        return Diagnosis(urgency=UrgencyLevel.EMERGENCY, reason="no_cu", ...)
    
    mhp = compute_cu_mhp(state)
    hp_ratio = cu.hp / mhp
    
    if hp_ratio < CU_HP_THRESHOLD_EMERGENCY:
        urgency = UrgencyLevel.EMERGENCY
    elif hp_ratio < CU_HP_THRESHOLD_ALL_REPAIR:
        urgency = UrgencyLevel.HEAVY_REPAIR
    elif hp_ratio < CU_HP_THRESHOLD_SOME_REPAIR:
        urgency = UrgencyLevel.LIGHT_REPAIR
    else:
        urgency = UrgencyLevel.NORMAL
    
    shields = find_shields(state)
    incoming_eq = has_imminent_earthquake(state.meteo_forecasts)
    storm_nearby = is_storm_near_cu(state, cu)
    
    return Diagnosis(
        urgency=urgency,
        cu=cu,
        cu_hp_ratio=hp_ratio,
        shields=shields,
        incoming_earthquake=incoming_eq,
        storm_near_cu=storm_nearby,
    )
```

### 6.1 Поиск щитов

Щит — плантация на ортогонально соседней клетке от ЦУ, НЕ в процессе стройки, НЕ isolated.

```python
def find_shields(state: GameState) -> list[Plantation]:
    cu = state.main_plantation
    if cu is None:
        return []
    neighbors = ortho_neighbors(cu.position)  # [(x±1, y), (x, y±1)]
    return [
        state.plantations_by_pos[pos]
        for pos in neighbors
        if pos in state.plantations_by_pos
        and not state.plantations_by_pos[pos].is_isolated
    ]

def has_opposite_shields(shields: list[Plantation], cu: Plantation) -> bool:
    """Проверка: есть ли 2 щита с противоположных сторон."""
    positions = {s.position for s in shields}
    x, y = cu.position
    horizontal = (x-1, y) in positions and (x+1, y) in positions
    vertical = (x, y-1) in positions and (x, y+1) in positions
    return horizontal or vertical
```

### 6.2 Прогноз катаклизмов

```python
def has_imminent_earthquake(forecasts: list[MeteoForecast]) -> bool:
    return any(f.kind == "earthquake" and f.turns_until <= 1 for f in forecasts)

def is_storm_near_cu(state: GameState, cu: Plantation) -> bool:
    for f in state.meteo_forecasts:
        if f.kind == "sandstorm" and f.position and not f.forming:
            # На траектории бури в ближайшие 5 ходов?
            if chebyshev_distance(cu.position, f.position) < (f.radius or 3) + 10:
                return True
    return False
```

---

## 7. Этап 2: Резервирование плантаций под защиту

```python
# decision/defense.py

@dataclass
class DefensePlan:
    cu_repairers: list[Plantation]      # плантации, которые будут ремонтировать ЦУ
    relocate_cu_to: Optional[Coord]     # если нужен перенос — куда
    old_cu_repairers: list[Plantation]  # кто чинит старое место после переноса
    reserved: set[str]                  # id зарезервированных плантаций

def plan_defense(state: GameState, diag: Diagnosis, memory: BotMemory) -> DefensePlan:
    plan = DefensePlan(cu_repairers=[], relocate_cu_to=None,
                       old_cu_repairers=[], reserved=set())
    
    if diag.urgency == UrgencyLevel.NORMAL:
        return plan
    
    shields = diag.shields
    
    if diag.urgency == UrgencyLevel.EMERGENCY:
        # Перенос на самый здоровый щит
        if not shields:
            # КАТАСТРОФА: нет щитов, ЦУ умрёт. Пытаемся хоть что-то.
            # Экстренно строим щит на любой свободной ортогональной клетке ЦУ
            # и все свободные плантации чинят ЦУ.
            plan.cu_repairers = get_all_free_non_cu_plantations(state)
            return plan
        
        best_shield = max(shields, key=lambda s: s.hp)
        plan.relocate_cu_to = best_shield.position
        # Все остальные плантации чинят старое место ЦУ
        # (оно теперь станет обычной плантацией с тем же HP)
        plan.old_cu_repairers = [
            p for p in state.plantations
            if not p.is_main and p.id != best_shield.id and not p.is_isolated
        ][:4]  # берём 4 ближайших
        plan.reserved = {p.id for p in plan.old_cu_repairers}
        return plan
    
    if diag.urgency == UrgencyLevel.HEAVY_REPAIR:
        plan.cu_repairers = shields[:4]
    elif diag.urgency == UrgencyLevel.LIGHT_REPAIR:
        plan.cu_repairers = shields[:2]
    
    plan.reserved = {p.id for p in plan.cu_repairers}
    return plan
```

### 7.1 Нюанс: штраф эффективности при ремонте

В правилах сказано: "каждая последующая команда проходящая через одну плантацию теряет эффективность CS/RS/SE/BE на 1". Штраф считается **по выходной точке** (middle coord в path).

Значит, если ЦУ чинят 4 плантации, и каждая использует **себя** как выходную точку, то штраф не копится (у каждой выходной точки — 1 использование).

**Формат команды ремонта:** `path = [[repairer_x, repairer_y], [exit_x, exit_y], [cu_x, cu_y]]`. Для простой модели ремонта используем `repairer == exit`, тогда штрафа нет.

```python
def build_repair_command(repairer: Plantation, target: Plantation) -> dict:
    """Ремонт: repairer через себя же чинит target."""
    return {
        "path": [
            list(repairer.position),
            list(repairer.position),  # выходная = автор
            list(target.position),
        ]
    }
```

---

## 8. Этап 3: Проверка структуры защиты

Если щитов < `SHIELDS_REQUIRED` (=2 с противоположных сторон) — нужно строить недостающий щит с **наивысшим приоритетом в стройке**.

```python
# decision/defense.py

def missing_shield_positions(state: GameState, cu: Plantation) -> list[Coord]:
    """
    Возвращает список позиций, где нужно построить щит, чтобы иметь 2 щита
    с противоположных сторон. Приоритет: замкнуть ось, где уже есть 1 щит.
    """
    positions = {p.position for p in state.plantations if not p.is_isolated}
    x, y = cu.position
    left, right = (x-1, y), (x+1, y)
    up, down = (x, y-1), (x, y+1)
    
    def is_buildable(pos: Coord) -> bool:
        return (
            in_bounds(pos, state.size)
            and pos not in state.mountains
            and pos not in positions
        )
    
    # Если есть одна половина оси — стройку на противоположную
    if left in positions and is_buildable(right):
        return [right]
    if right in positions and is_buildable(left):
        return [left]
    if up in positions and is_buildable(down):
        return [down]
    if down in positions and is_buildable(up):
        return [up]
    
    # Нет ни одной пары — строим любую доступную пару
    candidates = [p for p in [left, right, up, down] if is_buildable(p)]
    return candidates[:2]
```

---

## 9. Этап 4: Апгрейд

```python
# decision/upgrade.py

def choose_upgrade(state: GameState) -> Optional[str]:
    if state.plantation_upgrades.points <= 0:
        return None
    
    current = {t.name: t.current for t in state.plantation_upgrades.tiers}
    
    for name, target_level in UPGRADE_PRIORITY:
        if current.get(name, 0) < target_level:
            return name
    
    # Если все приоритетные куплены — дефолт (второй этап стратегии)
    # В v1: max_hp до упора, потом ничего (будет переопределено по тестам)
    return None
```

---

## 10. Этап 5: Защита от самосноса при стройке

**Критическая функция.** Вызывается перед каждой командой стройки.

```python
# decision/build_guard.py

def can_build_safely(state: GameState, memory: BotMemory) -> tuple[bool, str]:
    """
    Возвращает (разрешено_строить, причина).
    Если новая плантация превысит лимит — старейшая исчезнет.
    Проверяем, что старейшая НЕ ЦУ и НЕ щит.
    """
    settlement_limit = compute_settlement_limit(state)
    
    # Стройка добавляет ОДИН новый юнит в будущем (сейчас это construction)
    # Момент сноса наступит при **первом прогрессе** стройки сверх лимита.
    # Чтобы быть в безопасности, не превышаем лимит - KEEP_SLOTS_FREE.
    total_units = len(state.plantations) + len(state.construction)
    if total_units < settlement_limit - KEEP_SLOTS_FREE:
        return True, "ok_below_limit"
    
    # Ближе к лимиту — проверяем, кого снесёт
    oldest = memory.get_oldest_plantation(state)
    if oldest is None:
        return True, "unknown_oldest"  # консервативно: но если неизвестно — не рискуем
    
    cu = state.main_plantation
    if cu and oldest.id == cu.id:
        return False, "oldest_is_cu"
    
    shield_ids = {s.id for s in find_shields(state)}
    if oldest.id in shield_ids:
        return False, "oldest_is_shield"
    
    return True, "ok_oldest_is_buffer"
```

### 10.1 Специальный режим "цу самая старая"

Если `oldest_is_cu` блокирует стройку постоянно, бот встанет. Нужен выход: **дождаться завершения терраформации одной из клеток** — тогда плантация исчезнет, освободив слот, и самой старой станет другая.

В первой итерации: если `can_build_safely` возвращает False по причине `oldest_is_cu`, просто пропускаем стройку в этот ход. На терраформации клетки с прогрессом → 100% она сама исчезнет, и слот освободится естественно.

Альтернатива для последующих версий: **перенос ЦУ** на более молодую плантацию (тогда старейшей станет буфер, которую безопасно сносить). Но это противоречит выбранной стратегии "переносим только по угрозе", так что пока — только пропуск.

---

## 11. Этап 6: Выбор целей для стройки

```python
# decision/targets.py
from dataclasses import dataclass

@dataclass
class BuildTarget:
    position: Coord
    priority: int          # 1 (×7) > 2 (звено к ×7) > 3 (любая клетка)
    reason: str            # для логов

def generate_build_targets(state: GameState, memory: BotMemory) -> list[BuildTarget]:
    cu = state.main_plantation
    if cu is None:
        return []
    
    # Позиции, куда НЕЛЬЗЯ строить
    blocked = (
        state.mountains
        | {p.position for p in state.plantations}
        | {c.position for c in state.construction}
    )
    
    reachable = compute_reachable_cells(state)  # клетки в AR от любой нашей плантации
    
    targets: list[BuildTarget] = []
    
    # Приоритет 1: ×7 клетки в радиусе 5-7 от ЦУ
    for cell in reachable:
        if cell in blocked:
            continue
        if not is_boosted(cell):
            continue
        d = chebyshev_distance(cell, cu.position)
        if X7_SEARCH_RADIUS_MIN <= d <= X7_SEARCH_RADIUS_MAX:
            targets.append(BuildTarget(cell, priority=1, reason=f"x7_r{d}"))
    
    # Приоритет 2: звенья цепочки к недостижимым ×7 в радиусе
    for x7 in find_unreachable_x7_cells(state, cu.position):
        bridge = find_best_bridge_cell(state, x7, blocked, reachable)
        if bridge:
            targets.append(BuildTarget(bridge, priority=2, reason=f"bridge_to_{x7}"))
    
    # Приоритет 3: любая достижимая клетка пустыни (после исчерпания ×7 в радиусе)
    if not any(t.priority == 1 for t in targets) and FALLBACK_ANY_CELL:
        for cell in reachable:
            if cell in blocked:
                continue
            d = chebyshev_distance(cell, cu.position)
            if d <= X7_SEARCH_RADIUS_MAX:  # не разбегаемся слишком далеко
                targets.append(BuildTarget(cell, priority=3, reason=f"fill_r{d}"))
    
    # Сортировка: сначала по priority, потом по "ценности клетки"
    targets.sort(key=lambda t: (
        t.priority,
        0 if is_boosted(t.position) else 1,
        chebyshev_distance(t.position, cu.position),
    ))
    
    return targets
```

### 11.1 Функция `is_boosted`

```python
# model/geometry.py
def is_boosted(pos: Coord) -> bool:
    x, y = pos
    return x % BOOSTED_CELL_MODULO == 0 and y % BOOSTED_CELL_MODULO == 0
```

### 11.2 Поиск мостов к недостижимым ×7

Для каждой ×7 клетки за пределами нашего AR найти клетку, которая:
- Достижима для стройки сейчас (в AR от кого-то из наших плантаций)
- Находится на пути к ×7 (или сокращает расстояние)
- Не в blocked

Простейшая реализация: берём ×7, находим ближайшую к ней нашу плантацию, строим промежуточную клетку в направлении ×7 в AR этой плантации. Более сложные версии (A* по достижимости) — на потом.

```python
def find_best_bridge_cell(
    state: GameState, x7: Coord, blocked: set[Coord], reachable: set[Coord]
) -> Optional[Coord]:
    candidates = [
        c for c in reachable
        if c not in blocked
        and chebyshev_distance(c, x7) < min_distance_to_x7_from_network(state, x7)
    ]
    if not candidates:
        return None
    # Выбираем ту, которая ближе всех к x7
    return min(candidates, key=lambda c: chebyshev_distance(c, x7))
```

### 11.3 Достижимость клеток (AR от сети)

```python
def compute_reachable_cells(state: GameState) -> set[Coord]:
    """Все клетки в AR от любой нашей НЕ isolated плантации."""
    ar = state.action_range
    result = set()
    for p in state.plantations:
        if p.is_isolated:
            continue
        x, y = p.position
        for dx in range(-ar, ar + 1):
            for dy in range(-ar, ar + 1):
                pos = (x + dx, y + dy)
                if in_bounds(pos, state.size) and pos not in state.mountains:
                    result.add(pos)
    return result
```

---

## 12. Этап 7: Назначение команд

Это самая тонкая часть. У нас есть:
- Список свободных плантаций (не в `defense.reserved`)
- Список целей (из `generate_build_targets`)
- Список целей на ремонт (плантации с HP < MHP, не ЦУ)

Задача: назначить команды так, чтобы:
- Каждая цель закрывалась минимум одной плантацией
- Штраф эффективности был минимальным
- Выполнялись ограничения AR, SR

```python
# decision/assign.py

@dataclass
class AssignmentContext:
    exit_usage: dict[Coord, int]  # сколько раз позиция использовалась как выходная точка в этом ходу
    used_authors: set[str]        # id плантаций, которые уже отдали команду

def can_reach(
    author: Plantation, exit_point: Coord, target: Coord, state: GameState
) -> bool:
    """
    Автор может использовать exit_point как выходную точку, если:
    - exit_point находится в SR от author (|ΔX| ≤ SR и |ΔY| ≤ SR)
    - target находится в AR от exit_point
    """
    sr = compute_signal_range(state)
    ar = state.action_range
    if chebyshev_distance(author.position, exit_point) > sr:
        return False
    if chebyshev_distance(exit_point, target) > ar:
        return False
    return True

def effective_speed(base: int, usage: int) -> int:
    """Базовая скорость минус штраф за использование выходной точки."""
    return max(0, base - usage)

def assign_commands(
    state: GameState,
    free_plantations: list[Plantation],
    build_targets: list[BuildTarget],
    repair_targets: list[Plantation],
    memory: BotMemory,
) -> list[dict]:
    commands: list[dict] = []
    ctx = AssignmentContext(exit_usage={}, used_authors=set())
    
    # 1. Сначала самые приоритетные цели
    all_targets = (
        [(t.position, "build") for t in build_targets]
        + [(p.position, "repair") for p in repair_targets]
    )
    
    for target_pos, kind in all_targets:
        # Найти лучшую пару (author, exit_point) для этой цели
        best = find_best_assignment(
            target_pos, free_plantations, state, ctx
        )
        if best is None:
            continue
        author, exit_point = best
        
        commands.append({
            "path": [list(author.position), list(exit_point), list(target_pos)]
        })
        ctx.exit_usage[exit_point] = ctx.exit_usage.get(exit_point, 0) + 1
        ctx.used_authors.add(author.id)
    
    return commands

def find_best_assignment(
    target: Coord,
    candidates: list[Plantation],
    state: GameState,
    ctx: AssignmentContext,
) -> Optional[tuple[Plantation, Coord]]:
    """
    Для цели target перебрать все (author, exit_point) где:
    - author не занят (author.id not in ctx.used_authors)
    - author не isolated, не в immunity (строящаяся плантация не может отдавать команды)
    - exit_point — любая НАША НЕ isolated плантация в SR от author
    - target в AR от exit_point
    - effective_speed > 0
    Выбрать пару с минимальным usage у exit_point (меньше штраф).
    """
    best = None
    best_usage = float("inf")
    
    for author in candidates:
        if author.id in ctx.used_authors:
            continue
        if author.is_isolated:
            continue
        # Нельзя отдавать команду строящимся — они не полноценные плантации
        
        for exit_plant in state.plantations:
            if exit_plant.is_isolated:
                continue
            if not can_reach(author, exit_plant.position, target, state):
                continue
            usage = ctx.exit_usage.get(exit_plant.position, 0)
            base_speed = DEFAULT_CS  # или RS если ремонт — уточнить по типу target
            if effective_speed(base_speed, usage) <= 0:
                continue
            if usage < best_usage:
                best_usage = usage
                best = (author, exit_plant.position)
    
    return best
```

### 12.1 Тип команды определяется целью

API не принимает явный `type` — сервер сам определяет по конечной точке path:
- Клетка своей плантации → ремонт
- Клетка чужой плантации → диверсия
- Логово бобров → атака
- Пустая клетка → строительство

Поэтому мы просто указываем правильный target, и сервер сам разберётся.

---

## 13. Сборка финального запроса

```python
# decision/build_turn_command.py

def build_turn_command(state: GameState, memory: BotMemory) -> dict:
    diag = diagnose(state, memory)
    defense = plan_defense(state, diag, memory)
    
    # Свободные плантации = все non-isolated минус зарезервированные под защиту
    free = [
        p for p in state.plantations
        if not p.is_isolated
        and p.id not in defense.reserved
        and p.immunity_until_turn <= state.turn_no  # исключаем свежеотстроенные? 
        # UPD: иммунитет защищает от урона, но не блокирует действия. Уточнить на тестах.
    ]
    
    # Команды защиты
    defense_commands = []
    for repairer in defense.cu_repairers:
        defense_commands.append(build_repair_command(repairer, state.main_plantation))
    
    # Если нужно строить недостающий щит — добавляем в build_targets с приоритетом 0
    shield_gap_targets = []
    if state.main_plantation:
        missing = missing_shield_positions(state, state.main_plantation)
        shield_gap_targets = [BuildTarget(pos, priority=0, reason="shield") for pos in missing]
    
    # Проверка safe-to-build для стройки-не-щита
    allow_build, build_reason = can_build_safely(state, memory)
    
    build_targets = shield_gap_targets  # щиты строим ВСЕГДА, даже если лимит
    # ↑ правильно? если лимит упёрся и oldest=cu — щит не поможет, т.к. сломает цу.
    # В этом случае тоже нельзя. → проверяем отдельно
    if not allow_build:
        logger.warning("Build blocked: %s", build_reason)
        build_targets = []  # не рискуем даже ради щита
    else:
        if build_reason == "ok_below_limit" or build_reason.startswith("ok_"):
            build_targets += generate_build_targets(state, memory)
    
    # Ремонт не-ЦУ плантаций с низким HP (приоритет низкий)
    mhp = compute_cu_mhp(state)
    repair_targets = [
        p for p in state.plantations
        if not p.is_main and not p.is_isolated
        and p.hp < mhp * 0.7  # чиним если <70%
    ]
    
    # Назначение
    action_commands = assign_commands(
        state, free, build_targets, repair_targets, memory
    )
    
    # Апгрейд
    upgrade_name = choose_upgrade(state)
    
    # Перенос ЦУ
    relocate_main = None
    if defense.relocate_cu_to:
        relocate_main = [
            list(state.main_plantation.position),
            list(defense.relocate_cu_to),
        ]
    
    result: dict = {}
    all_commands = defense_commands + action_commands
    if all_commands:
        result["command"] = all_commands
    if upgrade_name:
        result["plantationUpgrade"] = upgrade_name
    if relocate_main:
        result["relocateMain"] = relocate_main
    
    # Сервер требует хотя бы одно действие в ходу.
    # Если ничего нет — отправляем "noop": фиктивную команду ремонта ЦУ саму себя
    # (сервер отклонит, но это лучше чем empty command error — проверить на тестах)
    if not result:
        logger.warning("Nothing to do this turn")
        # fallback: покупаем апгрейд если возможно, иначе шлём как есть и ловим ошибку
    
    return result
```

---

## 14. Пограничные случаи и правила, которые нужно проверить на тестах

Это **гипотезы**, которые документация не уточняет явно, и которые нужно подтвердить на первом тестовом раунде:

1. **Возраст ЦУ.** Считается ли ЦУ самой старой плантацией всегда? Или у неё `birth_turn = 0`, и она может стать НЕ самой старой только когда появятся плантации с `birth_turn < 0` (невозможно)?
   - **Тест:** построить 29 плантаций, наблюдать, что происходит при попытке 30-й (если лимит 30).

2. **Иммунитет после стройки.** 3 хода — это иммунитет от всего, включая возможность стройка → плантация превращается в полноценную с MHP. Можно ли отдавать команды этой плантации в эти 3 хода?
   - **Тест:** построить плантацию, на следующий ход попробовать дать ей команду.

3. **Штраф эффективности.** Считается по выходной точке (второй coord в path) или по автору (первый coord)?
   - **Тест:** отправить 3 команды с разными авторами, но одной exit_point. Измерить CS по progress стройки.

4. **Ремонт самой себя.** Правила говорят "плантация не может ремонтировать саму себя". Значит ли это, что path вида `[A, A, A]` невалидный? Можно ли использовать соседа как exit_point для ремонта соседа?
   - **Тест:** отправить path вида `[A, B, A]` — ремонтирует ли A саму себя через B?

5. **Одновременная стройка несколькими игроками.** Что именно обнуляется — только наш прогресс или всех?
   - Из правил: "весь прогресс строительства будет обнулен и им придется начинать строку сначала". То есть у всех в эту клетку обнулится.

6. **Перенос ЦУ и HP.** Правила: "При перемещении ЦУ количество HP плантаций не изменяется". Значит ЦУ с HP=10 после переноса на щит со HP=50 станет... плантацией с HP=10 на старом месте, а на новом месте — ЦУ со HP=50? Или наоборот?
   - Логичная интерпретация: **функция ЦУ просто переезжает, HP каждой плантации остаётся её собственным**. Старая ЦУ становится обычной плантацией с её старым HP (например 10). Новая плантация-щит становится ЦУ со своим старым HP (50). **Проверить тестом.**

7. **Бобры и immunity.** Атакуют ли бобры плантации с иммунитетом?
   - Правила: "После основания плантации она становится невосприимчивой к действиям конкурентов, природным катаклизмам и атакам бобров на протяжении 3 ходов". → **НЕ атакуют.**

8. **Deadline ответа.** 1 секунда на ход — с какого момента считается? От старта хода или от `next_turn_in`? Можем ли мы получить /arena за 800мс и отправить /command за 100мс?
   - **Тест:** замерить латентность, выставить таймауты.

9. **Начало раунда / респавн.** Как отличить "начало нового раунда" от "респавна после смерти ЦУ"? В логах будет сообщение, но также и `turn_no` должен сброситься при новом раунде.

10. **API коды ошибок.** Какие бывают коды в поле `code` кроме `0` и `3`?

Все гипотезы нужно **логировать** в первом раунде и после каждого теста обновлять код.

---

## 15. Логирование и наблюдаемость

После каждого хода логируем в JSONL-файл:

```python
{
    "turn_no": 42,
    "timestamp": "2026-04-18T...",
    "cu": {"hp": 45, "mhp": 60, "position": [100, 100]},
    "plantations_count": 12,
    "construction_count": 2,
    "urgency": "LIGHT_REPAIR",
    "shields_count": 2,
    "targets_considered": 8,
    "commands_sent": 10,
    "upgrade": "max_hp",
    "relocate": null,
    "errors": [],
    "api_latency_ms": 87
}
```

Дополнительно сохраняем raw arena JSON каждые 10 ходов для возможного replay.

---

## 16. План реализации по шагам

**Phase 1 — каркас (день 1):**
1. Модели данных (`model/state.py`)
2. API-клиент с ретраями (`api/client.py`)
3. Парсинг arena → GameState (`api/arena.py`)
4. Логирование (`util/logger.py`)
5. Главный цикл (`main.py`), который просто получает arena и ничего не делает

**Phase 2 — базовые действия (день 1-2):**
6. Геометрические утилиты (`model/geometry.py`)
7. Построение графа связности от ЦУ (`model/chain.py`)
8. BotMemory и детекция новых плантаций (`model/state.py`)
9. Diagnose (`decision/diagnose.py`)

**Phase 3 — принятие решений (день 2):**
10. Defense (`decision/defense.py`)
11. Build guard (`decision/build_guard.py`)
12. Upgrade (`decision/upgrade.py`)

**Phase 4 — активные действия (день 2-3):**
13. Targets (`decision/targets.py`)
14. Assign (`decision/assign.py`)
15. Build turn command — собираем всё (`decision/build_turn_command.py`)

**Phase 5 — тесты и настройка (день 3+):**
16. Unit-тесты для всех функций геометрии и связности
17. Запуск на тестовом раунде, проверка гипотез из раздела 14
18. Итерации по параметрам из раздела 2

---

## 17. Что НЕ делаем в v1 (backlog)

- **Диверсии** против противников
- **Охота на бобров** (даже при 10× очков — слишком рискованно без хорошей защиты ЦУ)
- **Прогноз бурь** и превентивный ремонт
- **Умная стратегия апгрейдов после max_hp** (пока нет данных с тестов)
- **Многоэтапная стройка** одной плантации несколькими (с оптимизацией штрафа)
- **Гео-анализ карты** для выбора направления экспансии (пока просто ×7 в радиусе)
- **Предсказание поведения противников**
- **Перенос ЦУ в сторону ×7** ("ползущий бункер")

Все эти пункты — потенциал для улучшения между финальными раундами.

---

## 18. Чеклист готовности к финальному раунду

- [ ] Бот стабильно ходит 600 ходов без крашей
- [ ] ЦУ ни разу не умерла за тестовый раунд
- [ ] Щиты всегда в количестве ≥ 2 (кроме экстренных ситуаций)
- [ ] Все 5 уровней `max_hp` куплены к ходу 150
- [ ] Самоснос старой плантацией не происходил
- [ ] Все ×7 клетки в радиусе 7 были освоены
- [ ] В логах нет ошибок вида `empty command` или `invalid path`
- [ ] Проверены и задокументированы все гипотезы из раздела 14
- [ ] Стратегические параметры (раздел 2) откалиброваны по результатам тестов

---

## Приложение A: Утилиты геометрии

```python
# model/geometry.py

Coord = tuple[int, int]

def chebyshev_distance(a: Coord, b: Coord) -> int:
    """Дистанция Чебышёва (max|Δx|, |Δy|). Используется для AR/SR/VR."""
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))

def manhattan_distance(a: Coord, b: Coord) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])

def ortho_neighbors(pos: Coord) -> list[Coord]:
    """Ортогональные соседи (без диагоналей) — для связности."""
    x, y = pos
    return [(x-1, y), (x+1, y), (x, y-1), (x, y+1)]

def all_neighbors(pos: Coord) -> list[Coord]:
    """Все 8 соседей — для экспансии стройки."""
    x, y = pos
    return [(x+dx, y+dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)]

def in_bounds(pos: Coord, size: tuple[int, int]) -> bool:
    return 0 <= pos[0] < size[0] and 0 <= pos[1] < size[1]

def is_boosted(pos: Coord) -> bool:
    return pos[0] % 7 == 0 and pos[1] % 7 == 0

def cells_in_radius(center: Coord, radius: int) -> Iterator[Coord]:
    """Все клетки в квадрате радиуса radius от center."""
    cx, cy = center
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            yield (cx + dx, cy + dy)
```

## Приложение B: Граф связности от ЦУ

```python
# model/chain.py

def compute_connected_plantations(state: GameState) -> set[str]:
    """BFS от ЦУ по ортогональным соседям. Возвращает id подключённых плантаций."""
    cu = state.main_plantation
    if cu is None:
        return set()
    
    connected: set[str] = {cu.id}
    queue = [cu.position]
    visited_positions = {cu.position}
    
    while queue:
        pos = queue.pop(0)
        for neighbor_pos in ortho_neighbors(pos):
            if neighbor_pos in visited_positions:
                continue
            visited_positions.add(neighbor_pos)
            p = state.plantations_by_pos.get(neighbor_pos)
            if p is None:
                continue
            connected.add(p.id)
            queue.append(neighbor_pos)
    
    return connected

def mark_isolated(state: GameState) -> None:
    """Проставляет is_isolated для плантаций, не связанных с ЦУ.
    
    Может пригодиться, если API не всегда отдаёт актуальный is_isolated."""
    connected = compute_connected_plantations(state)
    # ... переписать plantations с новым флагом
```

---

**Конец спецификации.** Вопросы, которые не покрыты здесь, обсуждать с человеком перед реализацией, чтобы не отходить от стратегии.
