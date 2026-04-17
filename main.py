"""Ядро запуска игрового цикла DatsSol.

Запуск:
    DATS_TOKEN=xxx uv run python main.py
    DATS_TOKEN=xxx uv run python main.py --prod
"""

from __future__ import annotations

import logging
import os
import sys
import time
import argparse

from api import GameAPI, Command, GameState
from api.exceptions import GameAPIError, AuthenticationError, ValidationError, ServerError, TimeoutError
from strategy import Strategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

WAIT_INTERVAL = 10  # секунд между попытками, когда игра не активна


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
        try:
            state = client.get_state()
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

        # Пропуск, если ход не изменился
        if state.turn_no == last_turn:
            sleep_time = max(0.1, state.next_turn_in)
            time.sleep(sleep_time)
            continue

        last_turn = state.turn_no

        # --- 2. Стратегия принимает решение ---
        try:
            cmd = strategy.decide(state)
        except Exception as exc:
            log.exception("Ошибка в стратегии на ходу %d: %s", state.turn_no, exc)
            cmd = Command()

        # --- 3. Отправить команду ---
        if cmd.has_actions():
            try:
                result = client.send_command(cmd)
                if not result.success:
                    log.warning("Ход %d: ошибки команды: %s", state.turn_no, result.errors)
                else:
                    log.debug("Ход %d: команда отправлена успешно", state.turn_no)
            except (GameAPIError, Exception) as exc:
                if is_game_not_active(exc):
                    log.warning("Игра завершилась во время отправки команды: %s", exc)
                    time.sleep(WAIT_INTERVAL)
                    continue
                log.error("Ход %d: ошибка отправки команды: %s", state.turn_no, exc)
        else:
            log.debug("Ход %d: нет действий для отправки", state.turn_no)

        # --- 4. Ожидание следующего хода ---
        sleep_time = max(0.05, state.next_turn_in - 0.1)
        time.sleep(sleep_time)


def main() -> None:
    parser = argparse.ArgumentParser(description="DatsSol Game Bot")
    parser.add_argument("--prod", action="store_true", help="Использовать production сервер")
    parser.add_argument("--data-dir", type=str, default="data", help="Директория для сохранения данных")
    args = parser.parse_args()

    token = os.environ.get("DATS_TOKEN") or os.environ.get("TOKEN")
    if not token:
        log.error("Токен не найден. Установите переменную окружения DATS_TOKEN или TOKEN.")
        sys.exit(1)

    env = "prod" if args.prod else "test"
    log.info("Запуск бота. Сервер: %s", env)

    client = GameAPI(
        api_key=token,
        environment=env,
        data_dir=args.data_dir,
    )
    strategy = Strategy()

    try:
        run_game_loop(client, strategy)
    except KeyboardInterrupt:
        log.info("Остановлено пользователем (Ctrl+C)")
    finally:
        client.close()
        log.info("Сессия закрыта.")


if __name__ == "__main__":
    main()
