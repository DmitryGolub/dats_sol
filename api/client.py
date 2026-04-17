"""Основной клиент GameAPI."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Literal

import requests

from .exceptions import (
    AuthenticationError,
    GameAPIError,
    LogicError,
    ServerError,
    TimeoutError,
    ValidationError,
)
from .helpers import parse_optional_position, parse_position
from .models import (
    Beaver,
    Command,
    CommandResult,
    Construction,
    EnemyPlantation,
    GameState,
    Log,
    MeteoEvent,
    Plantation,
    PlantationUpgradesState,
    PlantationUpgradeTier,
    TerraformCell,
)

_BASE_URLS = {
    "test": "https://games-test.datsteam.dev",
    "prod": "https://games.datsteam.dev",
}


class GameAPI:
    """Клиент для взаимодействия с игровым API."""

    def __init__(
        self,
        api_key: str,
        environment: Literal["test", "prod"] = "test",
        base_url: str | None = None,
        timeout: float = 30.0,
        poll_interval: float = 1.0,
        data_dir: str | None = "data",
    ) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.data_dir = Path(data_dir) if data_dir is not None else None

        # base URL: явный > ENVIRONMENT > fallback test
        if base_url is not None:
            self.base_url = base_url.rstrip("/")
        else:
            self.base_url = _BASE_URLS.get(environment, _BASE_URLS["test"])

        # создаём папку для данных, если нужна
        if self.data_dir is not None:
            self.data_dir.mkdir(parents=True, exist_ok=True)

        self._session = requests.Session()
        self._session.headers["X-Auth-Token"] = api_key
        self._session.headers["Content-Type"] = "application/json"
        self._session.headers["Accept"] = "application/json"

        # кеш
        self._cached_action_range: int | None = None
        self._cached_map_size: tuple[int, int] | None = None

    # --- private helpers ---

    def _save_response(self, data: dict) -> None:
        """Сохранить сырой JSON-ответ сервера в data_dir."""
        if self.data_dir is None:
            return

        turn_no = data.get("turnNo")
        if turn_no is not None:
            # Основной снапшот арены — именуем по ходу
            path = self.data_dir / f"turn_{int(turn_no):04d}.json"
        else:
            # Прочие ответы (логи, команды, ошибки) — по времени
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = self.data_dir / f"resp_{ts}.json"

        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            # Не ломаем игру, если не удалось записать файл
            pass

    def _request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        json: dict | None = None,
    ) -> dict:
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.request(
                method,
                url,
                json=json,
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout as exc:
            raise TimeoutError(f"HTTP timeout при обращении к {url}") from exc
        except requests.exceptions.RequestException as exc:
            raise GameAPIError(f"HTTP ошибка: {exc}") from exc

        # Пытаемся распарсить JSON в любом случае
        try:
            data = resp.json()
        except Exception:
            data = {"raw_text": resp.text}

        # Сохраняем успешные (и даже ошибочные) ответы для анализа / визуализации
        self._save_response(data)

        if resp.status_code == 200:
            return data

        # Определяем тип ошибки по статус-коду
        errors = self._extract_errors(data)
        msg = errors[0] if errors else f"HTTP {resp.status_code}"

        if resp.status_code in (401, 403):
            raise AuthenticationError(msg, status_code=resp.status_code, raw_response=data)
        if resp.status_code == 400:
            raise ValidationError(msg, status_code=resp.status_code, raw_response=data)
        if 500 <= resp.status_code < 600:
            raise ServerError(msg, status_code=resp.status_code, raw_response=data)

        raise GameAPIError(msg, status_code=resp.status_code, raw_response=data)

    @staticmethod
    def _extract_errors(data: dict) -> list[str]:
        """Извлечь список ошибок из ответа сервера."""
        # Формат gamesdk.PublicError
        if "errors" in data and isinstance(data["errors"], list):
            return [str(e) for e in data["errors"]]
        # Альтернативный формат (error + errCode)
        if "error" in data:
            return [str(data["error"])]
        return []

    # --- public methods ---

    def get_state(self) -> GameState:
        """Полный снапшот текущего хода."""
        data = self._request("GET", "/api/arena")

        turn_no = int(data.get("turnNo", 0))
        next_turn_in = float(data.get("nextTurnIn", 0.0))
        map_size = parse_position(data["size"])
        action_range = int(data.get("actionRange", 0))

        beavers = [
            Beaver(
                id=str(b["id"]),
                position=parse_position(b["position"]),
                hp=int(b["hp"]),
            )
            for b in data.get("beavers", [])
        ]

        plantations = [
            Plantation(
                id=str(p["id"]),
                position=parse_position(p["position"]),
                hp=int(p["hp"]),
                is_main=bool(p.get("isMain", False)),
                is_isolated=bool(p.get("isIsolated", False)),
                immunity_until_turn=p.get("immunityUntilTurn"),
            )
            for p in data.get("plantations", [])
        ]

        enemy_plantations = [
            EnemyPlantation(
                id=str(e["id"]),
                position=parse_position(e["position"]),
                hp=int(e["hp"]),
            )
            for e in data.get("enemy", [])
        ]

        meteo_forecasts: list[MeteoEvent] = []
        for m in data.get("meteoForecasts", []):
            kind = str(m.get("kind", ""))
            if kind not in ("earthquake", "sandstorm"):
                continue
            meteo_forecasts.append(
                MeteoEvent(
                    id=m.get("id"),
                    kind=kind,  # type: ignore[typeddict-item]
                    position=parse_optional_position(m.get("position")),
                    radius=m.get("radius"),
                    turns_until=m.get("turnsUntil"),
                    is_forming=m.get("forming"),
                    next_position=parse_optional_position(m.get("nextPosition")),
                )
            )

        constructions = [
            Construction(
                position=parse_position(c["position"]),
                progress=int(c.get("progress", 0)),
            )
            for c in data.get("construction", [])
        ]

        mountains = {parse_position(m) for m in data.get("mountains", [])}

        terraformed_cells = [
            TerraformCell(
                position=parse_position(c["position"]),
                terraformation_progress=int(c.get("terraformationProgress", 0)),
                turns_until_degradation=int(c.get("turnsUntilDegradation", 0)),
            )
            for c in data.get("cells", [])
        ]

        plantation_upgrades: PlantationUpgradesState | None = None
        pu = data.get("plantationUpgrades")
        if pu is not None:
            plantation_upgrades = PlantationUpgradesState(
                points=int(pu.get("points", 0)),
                max_points=int(pu.get("maxPoints", 0)),
                interval_turns=int(pu.get("intervalTurns", 0)),
                turns_until_points=int(pu.get("turnsUntilPoints", 0)),
                tiers=[
                    PlantationUpgradeTier(
                        name=str(t.get("name", "")),
                        current=int(t.get("current", 0)),
                        max=int(t.get("max", 0)),
                    )
                    for t in pu.get("tiers", [])
                ],
            )

        state = GameState(
            turn_no=turn_no,
            next_turn_in=next_turn_in,
            map_size=map_size,
            action_range=action_range,
            beavers=beavers,
            plantations=plantations,
            enemy_plantations=enemy_plantations,
            meteo_forecasts=meteo_forecasts,
            constructions=constructions,
            mountains=mountains,
            terraformed_cells=terraformed_cells,
            plantation_upgrades=plantation_upgrades,
        )

        # кеширование
        self._cached_action_range = action_range
        self._cached_map_size = map_size

        return state

    def send_command(self, cmd: Command) -> CommandResult:
        """Атомарная отправка приказов."""
        payload = cmd.to_dict()
        try:
            data = self._request("POST", "/api/command", json=payload)
        except ValidationError as exc:
            return CommandResult(
                success=False,
                errors=[exc.message] + self._extract_errors(exc.raw_response),
                raw_response=exc.raw_response,
            )

        # Сервер возвращает 200 даже при ошибках внутри тела (по спеке — PublicError)
        errors = self._extract_errors(data)
        if errors:
            return CommandResult(success=False, errors=errors, raw_response=data)

        return CommandResult(success=True, errors=[], raw_response=data)

    def get_logs(self, since_turn: int = 0) -> list[Log]:
        """История логов. Фильтр since_turn применяется на клиенте,
        т.к. API не предоставляет параметра."""
        # API возвращает массив напрямую
        raw = self._request("GET", "/api/logs")
        if not isinstance(raw, list):
            return []

        logs = [
            Log(message=str(item.get("message", "")), time=str(item.get("time", "")))
            for item in raw
        ]

        # Фильтрация по since_turn — логи не содержат turnNo,
        # поэтому since_turn в текущей версии API не применим напрямую.
        # Оставляем сигнатуру для совместимости.
        return logs

    def wait_next_turn(self, after_turn: int) -> GameState:
        """Блокирующий poll до наступления turnNo > after_turn."""
        while True:
            state = self.get_state()
            if state.turn_no > after_turn:
                return state
            if state.next_turn_in <= 0:
                # защита от бесконечного цикла
                time.sleep(self.poll_interval)
            else:
                # спим ровно до следующего хода + небольшой запас
                sleep_time = min(state.next_turn_in + 0.5, self.poll_interval * 5)
                time.sleep(sleep_time)

    def close(self) -> None:
        """Закрытие HTTP-сессии."""
        self._session.close()

    def __enter__(self) -> GameAPI:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[no-untyped-def]
        self.close()
