# API клиент — документация

Клиент для взаимодействия с игровым сервером **DATS Sol**.  
Реализует все публичные endpoint'ы: получение состояния арены, отправку команд, логи и ожидание хода.

---

## Быстрый старт

```python
import os
from api import GameAPI, Command

client = GameAPI(
    api_key=os.environ["TOKEN"],
    environment=os.environ.get("ENVIRONMENT", "test"),
)
state = client.get_state()
print(f"Ход {state.turn_no}, карта {state.map_size}")
```

---

## Инициализация

```python
api = GameAPI(
    api_key: str,                          # X-Auth-Token
    environment: "test" | "prod" = "test",  # выбор сервера
    base_url: str | None = None,           # явный URL (переопределяет environment)
    timeout: float = 30.0,                 # HTTP timeout
    poll_interval: float = 1.0,            # секунды между проверками в wait-методах
    data_dir: str | None = "data",         # папка для сохранения JSON от сервера
)
```

Клиент поддерживает контекстный менеджер (`with`), который автоматически закрывает HTTP-сессию:

```python
with GameAPI(api_key="xxx") as client:
    state = client.get_state()
```

### Загрузка параметров из `.env`

Файл `.env` в корне проекта:

```
TOKEN=56365944-eb54-438c-a186-4131c50af314
ENVIRONMENT=test
# BASE_URL=https://games-test.datsteam.dev   # опционально, переопределяет ENVIRONMENT
```

Использование:

```python
import os
from api import GameAPI

client = GameAPI(
    api_key=os.environ["TOKEN"],
    environment=os.environ.get("ENVIRONMENT", "test"),
    base_url=os.environ.get("BASE_URL"),          # None если не задан
)
```

---

## Основные методы

### `get_state() -> GameState`

Полный снапшот текущего хода (`GET /api/arena`).

```python
state = client.get_state()
print(state.turn_no)          # номер хода
print(state.next_turn_in)     # секунд до следующего хода
print(state.map_size)         # (width, height)
print(state.action_range)     # макс. длина пути бобра
print(len(state.beavers))     # список бобров
print(len(state.plantations)) # свои плантации
```

**Оптимизация:** `map_size` и `action_range` кешируются при первом вызове.

---

### `send_command(cmd: Command) -> CommandResult`

Отправка приказов (`POST /api/command`).

```python
cmd = Command()
cmd.move_beaver("beaver-1", [(1, 2), (1, 3)])
cmd.upgrade_plantation("repair_power")

result = client.send_command(cmd)
if not result.success:
    print("Ошибки:", result.errors)
```

`CommandResult`:
- `success: bool` — признак успеха
- `errors: list[str]` — список ошибок от сервера
- `raw_response: dict` — оригинальный JSON-ответ

---

### `get_logs(since_turn: int = 0) -> list[Log]`

История логов (`GET /api/logs`).  
Параметр `since_turn` зарезервирован: сервер не поддерживает сервер-side фильтрацию, поэтому все логи возвращаются целиком.

```python
logs = client.get_logs()
for log in logs:
    print(f"[{log.time}] {log.message}")
```

---

### `wait_next_turn(after_turn: int) -> GameState`

Блокирующий poll до наступления `turnNo > after_turn`.

```python
state = client.get_state()
new_state = client.wait_next_turn(state.turn_no)
print(f"Новый ход: {new_state.turn_no}")
```

Использует `next_turn_in` из ответа сервера для разумного интервала сна + `poll_interval` как fallback.

---

### `close()`

Явное закрытие `requests.Session` (освобождение соединений).

---

## Сохранение состояний (data_dir)

При `data_dir="data"` (по умолчанию) клиент **автоматически** сохраняет каждый JSON-ответ от сервера в файлы:

```
data/
├── turn_0001.json   # снапшот /api/arena за ход 1
├── turn_0002.json   # снапшот /api/arena за ход 2
├── turn_0003.json
├── resp_20260417_153042.json   # ответ на /api/command или /api/logs
└── ...
```

Файлы `turn_*.json` — это **сырые ответы сервера** (`dto.PlayerResponse`), идеально подходят для:
- **Replay** — воспроизведения игры пошагово
- **Визуализации** — рендера карты, бобров, плантаций, метео-событий
- **Отладки** — анализа того, что именно прислал сервер

### Отключение сохранения

```python
client = GameAPI(api_key="xxx", data_dir=None)  # не сохраняем JSON
```

### Пример чтения сохранённых состояний

```python
import json
from pathlib import Path

for path in sorted(Path("data").glob("turn_*.json")):
    with open(path) as f:
        raw = json.load(f)
    print(f"{path.name}: ход {raw['turnNo']}, бобров {len(raw['beavers'])}")
```

---

## Модели данных

### `GameState` (immutable)

| Поле | Тип | Описание |
|------|-----|----------|
| `turn_no` | `int` | Текущий номер хода |
| `next_turn_in` | `float` | Секунд до следующего хода |
| `map_size` | `tuple[int, int]` | Размеры карты `(width, height)` |
| `action_range` | `int` | Максимальная длина пути бобра |
| `beavers` | `list[Beaver]` | Список бобров |
| `plantations` | `list[Plantation]` | Свои плантации |
| `enemy_plantations` | `list[EnemyPlantation]` | Чужие плантации |
| `meteo_forecasts` | `list[MeteoEvent]` | Прогноз погоды (землетрясения, бури) |
| `constructions` | `list[Construction]` | Объекты строительства |
| `mountains` | `set[tuple[int, int]]` | Координаты гор (O(1) проверка) |
| `terraformed_cells` | `list[TerraformCell]` | Терраформированные клетки |
| `plantation_upgrades` | `PlantationUpgradesState \| None` | Состояние улучшений |

---

### `Beaver`

```python
Beaver(id: str, position: tuple[int, int], hp: int)
```

### `Plantation`

```python
Plantation(
    id: str,
    position: tuple[int, int],
    hp: int,
    is_main: bool,
    is_isolated: bool,
    immunity_until_turn: int | None,
)
```

### `MeteoEvent`

```python
MeteoEvent(
    id: str | None,
    kind: "earthquake" | "sandstorm",
    position: tuple[int, int] | None,
    radius: int | None,           # только для sandstorm
    turns_until: int | None,      # 0 = этот ход
    is_forming: bool | None,      # sandstorm: собирается vs движется
    next_position: tuple[int, int] | None,  # куда двинется буря
)
```

---

## Конструктор команд (`Command`)

Builder-паттерн для формирования тела запроса к `/api/command`.

### Методы

| Метод | Описание |
|-------|----------|
| `move_beaver(beaver_id, path)` | Задать маршрут бобра (список `[x, y]`) |
| `upgrade_plantation(upgrade_type)` | Выбрать улучшение, например `"repair_power"` |
| `relocate_main_base(to)` | Переместить главную базу на `(x, y)` |
| `has_actions() -> bool` | Есть ли какие-либо действия |
| `validate(state) -> list[str]` | Проверка длины пути, границ карты, гор |

### Пример

```python
from api import Command

cmd = Command()
cmd.move_beaver("b1", [(0, 0), (1, 0), (2, 0)])
cmd.upgrade_plantation("repair_power")

errors = cmd.validate(state)
if errors:
    print("Валидация не пройдена:", errors)
else:
    client.send_command(cmd)
```

**Что проверяет `validate`:**
- существование бобра в `state.beavers`
- длина пути ≤ `action_range`
- координаты внутри `map_size`
- путь не проходит через `mountains`
- `relocateMain` в пределах карты и не на горе

---

## Обработка ошибок

```
GameAPIError
├── AuthenticationError   # 401 / 403
├── ValidationError       # 400 (невалидная команда)
├── LogicError            # логические ошибки (например, ход вне action_range)
├── ServerError           # 5xx
└── TimeoutError          # HTTP timeout или истек next_turn_in
```

Все исключения содержат:
- `.message: str`
- `.status_code: int | None`
- `.raw_response: dict`

```python
from api import GameAPI, AuthenticationError, ValidationError

try:
    client = GameAPI(api_key="bad-key")
    client.get_state()
except AuthenticationError:
    print("Неверный токен")
except ValidationError as exc:
    print("Ошибка валидации:", exc.message)
    print("Ответ сервера:", exc.raw_response)
```

---

## Утилиты (`helpers.py`)

### `Pathfinder`

A* pathfinder для поиска пути на карте с препятствиями.

```python
from api.helpers import Pathfinder

pf = Pathfinder(width=30, height=30, mountains=state.mountains)
path = pf.find_path(start=(0, 0), goal=(10, 10))
if path:
    cmd.move_beaver("b1", path)
```

**Методы:**
- `find_path(start, goal) -> list[tuple[int, int]] | None` — возвращает путь включая start и goal
- `neighbors(pos) -> list[tuple[int, int]]` — 4-связные соседи (без диагоналей)

---

## Полный игровой цикл

```python
import os
from api import GameAPI, Command

client = GameAPI(
    api_key=os.environ["TOKEN"],
    environment=os.environ.get("ENVIRONMENT", "test"),
)
state = client.get_state()

while True:
    cmd = Command()

    for beaver in state.beavers:
        # Упрощённая логика: двигаемся к (0, 0)
        cmd.move_beaver(beaver.id, [(0, 0)])

    if cmd.has_actions():
        result = client.send_command(cmd)
        if not result.success:
            print("Сервер отклонил:", result.errors)

    state = client.wait_next_turn(state.turn_no)

    # Условие выхода
    if not state.plantations:
        print("Все плантации уничтожены — игра окончена.")
        break
```

После завершения игры в папке `data/` останутся все снапшоты:

```python
from pathlib import Path

snapshots = sorted(Path("data").glob("turn_*.json"))
print(f"Сохранено {len(snapshots)} ходов для анализа / визуализации.")
```

---

## Файлы проекта

```
api/
├── __init__.py       # Публичный интерфейс
├── client.py         # GameAPI — HTTP-клиент
├── models.py         # Все dataclasses
├── exceptions.py     # Иерархия ошибок
└── helpers.py        # Pathfinder и парсеры

data/                 # JSON-снапшоты от сервера (создаётся автоматически)
├── turn_0001.json
├── turn_0002.json
└── ...

docs/
└── API.md            # Эта документация

.env                  # TOKEN, ENVIRONMENT, BASE_URL
```

---

## Переменные окружения (`.env`)

| Переменная | Описание | Пример |
|------------|----------|--------|
| `TOKEN` | X-Auth-Token для авторизации | `56365944-eb54-...` |
| `ENVIRONMENT` | Окружение: `test` или `prod` | `test` |
| `BASE_URL` | (опц.) Явный URL сервера, переопределяет `ENVIRONMENT` | `https://games-test.datsteam.dev` |
