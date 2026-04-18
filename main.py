"""Ядро запуска игрового цикла DatsSol.

Запуск:
    DATS_TOKEN=xxx uv run python main.py
    DATS_TOKEN=xxx uv run python main.py --prod
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

from api import Command, GameAPI, GameState
from api.exceptions import AuthenticationError, GameAPIError
from strategy import Strategy
from strategy.metrics import collect_metrics, format_turn_line

log = logging.getLogger("bot")
metrics_log = logging.getLogger("bot.metrics")

WAIT_INTERVAL = 10  # секунд между попытками, когда игра не активна


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    """Настроить логирование: stdout (видно в docker logs) + ротируемый файл + JSONL метрики."""
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    # очистить хендлеры, если скрипт вызывается повторно
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # stdout — попадает в docker logs
    stream = logging.StreamHandler(stream=sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    # human-readable лог-файл с ротацией (10 MB × 5)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "bot.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # JSONL-метрики — отдельный файл, без дублирования в основной
    metrics_log.setLevel(logging.INFO)
    metrics_log.propagate = False
    metrics_handler = logging.handlers.RotatingFileHandler(
        log_dir / "metrics.jsonl",
        maxBytes=20 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    metrics_handler.setFormatter(logging.Formatter("%(message)s"))
    metrics_log.addHandler(metrics_handler)


def is_game_not_active(exc: Exception) -> bool:
    """Определить, что ошибка = игра не активна / раунд не идёт."""
    msg = str(exc).lower()
    return any(phrase in msg for phrase in [
        "game is not active",
        "no active game",
        "round is not active",
        "not registered",
        "game is over",
        "round is over",
    ])


def run_game_loop(client: GameAPI, strategy: Strategy) -> None:
    """Основной игровой цикл: опрос → решение → команда."""
    last_turn = -1
    consecutive_errors = 0
    max_consecutive_errors = 20

    while True:
        # --- 1. Получить состояние ---
        t0 = time.perf_counter()
        try:
            state: GameState = client.get_state()
            consecutive_errors = 0
        except (GameAPIError, Exception) as exc:
            consecutive_errors += 1

            if is_game_not_active(exc):
                log.warning("Игра не активна: %s. Ожидание %d сек...", exc, WAIT_INTERVAL)
                time.sleep(WAIT_INTERVAL)
                consecutive_errors = 0
                continue

            if isinstance(exc, AuthenticationError):
                log.error("Ошибка авторизации: %s. Проверьте токен.", exc)
                sys.exit(1)

            if consecutive_errors >= max_consecutive_errors:
                log.error("Слишком много ошибок подряд (%d). Завершение.", consecutive_errors)
                sys.exit(1)

            backoff = min(2 ** consecutive_errors, 30)
            log.error("Ошибка получения состояния: %s. Повтор через %d сек...", exc, backoff)
            time.sleep(backoff)
            continue

        get_state_ms = (time.perf_counter() - t0) * 1000.0

        # Пропуск, если ход не изменился
        if state.turn_no == last_turn:
            sleep_time = max(0.1, state.next_turn_in)
            time.sleep(sleep_time)
            continue

        last_turn = state.turn_no

        # --- 2. Стратегия принимает решение ---
        t1 = time.perf_counter()
        try:
            cmd = strategy.decide(state)
        except Exception as exc:
            log.exception("Ошибка в стратегии на ходу %d: %s", state.turn_no, exc)
            cmd = Command()
        decide_ms = (time.perf_counter() - t1) * 1000.0

        # --- 3. Отправить команду ---
        result = None
        t2 = time.perf_counter()
        if cmd.has_actions():
            try:
                result = client.send_command(cmd)
                if not result.success:
                    log.warning("Ход %d: ошибки команды: %s", state.turn_no, result.errors)
            except (GameAPIError, Exception) as exc:
                if is_game_not_active(exc):
                    log.warning("Игра завершилась во время отправки команды: %s", exc)
                    time.sleep(WAIT_INTERVAL)
                    continue
                log.error("Ход %d: ошибка отправки команды: %s", state.turn_no, exc)
        send_ms = (time.perf_counter() - t2) * 1000.0

        # --- 4. Логирование метрик ---
        try:
            m = collect_metrics(state, cmd, result, decide_ms, send_ms)
            log.info("%s get_state=%.0fms", format_turn_line(m), get_state_ms)
            payload = dataclasses.asdict(m)
            payload["ts"] = time.time()
            payload["get_state_ms"] = get_state_ms
            metrics_log.info(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            log.exception("Ошибка при сборе метрик хода %d: %s", state.turn_no, exc)

        # --- 5. Ожидание следующего хода ---
        sleep_time = max(0.05, state.next_turn_in - 0.1)
        time.sleep(sleep_time)


def main() -> None:
    parser = argparse.ArgumentParser(description="DatsSol Game Bot")
    parser.add_argument("--prod", action="store_true", help="Использовать production сервер")
    parser.add_argument("--data-dir", type=str, default="data", help="Директория для сохранения данных")
    parser.add_argument("--log-dir", type=str, default=os.environ.get("LOG_DIR", "logs"), help="Директория логов")
    parser.add_argument("--log-level", type=str, default=os.environ.get("LOG_LEVEL", "INFO"), help="Уровень логирования")
    args = parser.parse_args()

    setup_logging(Path(args.log_dir), level=args.log_level)

    token = os.environ.get("DATS_TOKEN") or os.environ.get("TOKEN")
    if not token:
        log.error("Токен не найден. Установите переменную окружения DATS_TOKEN или TOKEN.")
        sys.exit(1)

    env = "prod" if args.prod else "test"
    log.info("Запуск бота. Сервер: %s, log_dir=%s, data_dir=%s", env, args.log_dir, args.data_dir)

    client = GameAPI(
        api_key=token,
        environment=env,
        data_dir=args.data_dir,
    )
    strategy = Strategy()
    log.info("Стратегия: %s", getattr(strategy, "name", type(strategy).__name__))

    try:
        run_game_loop(client, strategy)
    except KeyboardInterrupt:
        log.info("Остановлено пользователем (Ctrl+C)")
    finally:
        client.close()
        log.info("Сессия закрыта.")


if __name__ == "__main__":
    main()
