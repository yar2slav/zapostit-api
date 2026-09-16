"""ошибки библиотеки zapostit"""

from __future__ import annotations

from typing import Any


class ZapostitError(Exception):
    """базовая ошибка библиотеки"""


class ApiError(ZapostitError):
    """api сайта вернул ошибку"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | int | None = None,
        procedure: str | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.procedure = procedure
        self.details = details


class RateLimitError(ApiError):
    """api продолжил возвращать 429 после повторных запросов"""


class CloudflareChallengeError(ApiError):
    """cloudflare запросил интерактивную проверку.

    библиотека не решает и не обходит такие проверки
    """


class DecodeError(ZapostitError):
    """ответ сайта не удалось безопасно декодировать"""


class NotFoundError(ZapostitError):
    """запрошенный объект не найден"""


class AuthenticationError(ApiError):
    """вход не выполнен или сессия недействительна"""
