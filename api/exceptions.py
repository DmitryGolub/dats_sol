"""Иерархия ошибок API клиента."""

from __future__ import annotations


class GameAPIError(Exception):
    """Базовое исключение для всех ошибок API."""

    def __init__(self, message: str, *, status_code: int | None = None, raw_response: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.raw_response = raw_response or {}


class AuthenticationError(GameAPIError):
    """Ошибка аутентификации (401/403)."""


class ValidationError(GameAPIError):
    """Невалидная команда или запрос (400)."""


class LogicError(GameAPIError):
    """Логическая ошибка: попытка хода вне action_range и т.п."""


class ServerError(GameAPIError):
    """Ошибка сервера (5xx)."""


class TimeoutError(GameAPIError):
    """HTTP timeout или истекло время ожидания хода."""
