"""http транспорт для zapostit"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

import httpx

from .codec import DevalueCodec
from .errors import (
    ApiError,
    AuthenticationError,
    CloudflareChallengeError,
    DecodeError,
    RateLimitError,
)


class SyncTransportProtocol(Protocol):
    """интерфейс синхронного транспорта"""

    def request(self, procedure: str, payload: Mapping[str, Any] | None = None) -> Any:
        """run one read-only trpc query"""

    def get_text(self, url: str) -> str:
        """fetch a public text document"""

    def mutate(self, procedure: str, payload: Mapping[str, Any]) -> Any:
        """run one authenticated trpc mutation"""

    def login(self, username: str, password: str, turnstile_token: str) -> Any:
        """create an authenticated cookie session"""

    def get_session(self) -> Any:
        """return raw metadata for the current cookie session"""

    def close(self) -> None:
        """release owned resources"""


class AsyncTransportProtocol(Protocol):
    """интерфейс асинхронного транспорта"""

    async def request(self, procedure: str, payload: Mapping[str, Any] | None = None) -> Any:
        """run one read-only trpc query"""

    async def get_text(self, url: str) -> str:
        """fetch a public text document"""

    async def mutate(self, procedure: str, payload: Mapping[str, Any]) -> Any:
        """run one authenticated trpc mutation"""

    async def login(self, username: str, password: str, turnstile_token: str) -> Any:
        """create an authenticated cookie session"""

    async def get_session(self) -> Any:
        """return raw metadata for the current cookie session"""

    async def close(self) -> None:
        """release owned resources"""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """настройки ограниченных повторных запросов"""

    max_retries: int = 3
    backoff_factor: float = 0.5
    max_backoff: float = 8.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if self.backoff_factor < 0 or self.max_backoff < 0:
            raise ValueError("backoff values must be non-negative")


class _TransportBase:
    """общая логика запросов и обработки ответов"""

    def __init__(
        self,
        *,
        base_url: str,
        codec: DevalueCodec | None,
        retry_policy: RetryPolicy | None,
    ) -> None:
        self._site_url = base_url.rstrip("/")
        self._api_url = f"{self._site_url}/api/trpc"
        self._codec = codec or DevalueCodec()
        self._retry_policy = retry_policy or RetryPolicy()

    @property
    def retry_policy(self) -> RetryPolicy:
        return self._retry_policy

    def request_params(self, payload: Mapping[str, Any] | None) -> dict[str, str]:
        if payload is None:
            return {}
        serialized = self._codec.dumps(dict(payload))
        return {"input": json.dumps(serialized, ensure_ascii=False, separators=(",", ":"))}

    def decode_response(self, response: httpx.Response, procedure: str) -> Any:
        if self._is_cloudflare_challenge(response):
            raise CloudflareChallengeError(
                "cloudflare requested an interactive challenge; automatic bypass is not supported",
                status_code=response.status_code,
                procedure=procedure,
            )
        try:
            envelope = response.json()
        except ValueError as exc:
            raise DecodeError(
                f"{procedure} returned a non-json response (http {response.status_code})"
            ) from exc
        if not isinstance(envelope, Mapping):
            raise DecodeError(f"{procedure} returned an invalid trpc envelope")
        if "error" in envelope:
            self._raise_api_error(envelope["error"], response.status_code, procedure)
        result = envelope.get("result")
        if not isinstance(result, Mapping) or "data" not in result:
            raise DecodeError(f"{procedure} response does not contain result.data")
        data = result["data"]
        if not isinstance(data, str):
            raise DecodeError(f"{procedure} result.data must be a devalue string")
        return self._codec.loads(data)

    def decode_text_response(self, response: httpx.Response, resource: str) -> str:
        """validate and decode a public text response"""

        if self._is_cloudflare_challenge(response):
            raise CloudflareChallengeError(
                "cloudflare requested an interactive challenge; automatic bypass is not supported",
                status_code=response.status_code,
                procedure=resource,
            )
        if response.status_code >= 400:
            raise ApiError(
                f"{resource} returned http {response.status_code}",
                status_code=response.status_code,
                procedure=resource,
            )
        content_type = response.headers.get("content-type", "").lower()
        if "text/" not in content_type and "html" not in content_type:
            raise DecodeError(f"{resource} returned non-text content: {content_type or 'unknown'}")
        return response.text

    def decode_auth_json(self, response: httpx.Response, resource: str) -> Any:
        """декодирует ответ auth.js без вывода секретных значений"""

        if self._is_cloudflare_challenge(response):
            raise CloudflareChallengeError(
                "cloudflare requested an interactive challenge; automatic bypass is not supported",
                status_code=response.status_code,
                procedure=resource,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise DecodeError(f"{resource} returned a non-json response") from exc
        if response.status_code >= 400:
            raise AuthenticationError(
                f"{resource} returned http {response.status_code}",
                status_code=response.status_code,
                procedure=resource,
            )
        return payload

    def csrf_token(self, response: httpx.Response) -> str:
        payload = self.decode_auth_json(response, "auth.csrf")
        if not isinstance(payload, Mapping):
            raise DecodeError("auth.csrf response has no csrf token")
        token = payload.get("csrfToken")
        if not isinstance(token, str):
            raise DecodeError("auth.csrf response has no csrf token")
        return token

    def validate_login_response(self, response: httpx.Response) -> None:
        payload = self.decode_auth_json(response, "auth.login")
        if not isinstance(payload, Mapping) or not isinstance(payload.get("url"), str):
            raise DecodeError("auth.login response has no redirect url")
        redirect_url = payload["url"]
        query = parse_qs(urlparse(redirect_url).query)
        error = query.get("error", [None])[0]
        if error is not None:
            code = query.get("code", [error])[0]
            raise AuthenticationError(
                f"login failed: {code}",
                code=code,
                procedure="auth.login",
            )

    @staticmethod
    def validate_credentials(username: str, password: str, turnstile_token: str) -> None:
        if not username.strip():
            raise ValueError("username must not be empty")
        if not password:
            raise ValueError("password must not be empty")
        if not turnstile_token.strip():
            raise ValueError("turnstile_token must not be empty")

    def retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("retry-after")
            parsed = self._parse_retry_after(retry_after)
            if parsed is not None:
                return min(parsed, self._retry_policy.max_backoff)
        exponential = float(self._retry_policy.backoff_factor * (2**attempt))
        return min(exponential, self._retry_policy.max_backoff)

    def can_retry(self, attempt: int) -> bool:
        return attempt < self._retry_policy.max_retries

    @staticmethod
    def _is_cloudflare_challenge(response: httpx.Response) -> bool:
        if response.headers.get("cf-mitigated", "").lower() == "challenge":
            return True
        content_type = response.headers.get("content-type", "").lower()
        return (
            response.status_code == 403
            and "text/html" in content_type
            and (b"Just a moment" in response.content or b"challenge-platform" in response.content)
        )

    def _raise_api_error(self, error: Any, status_code: int, procedure: str) -> None:
        decoded = error
        if isinstance(error, str):
            try:
                decoded = self._codec.loads(error)
            except DecodeError:
                decoded = {"message": error}
        if isinstance(decoded, Mapping):
            message = str(decoded.get("message", f"{procedure} failed"))
            data = decoded.get("data")
            raw_code = decoded.get("code")
            code: str | int | None = (
                raw_code
                if isinstance(raw_code, (str, int)) and not isinstance(raw_code, bool)
                else None
            )
            if isinstance(data, Mapping):
                code_value = data.get("code")
                if isinstance(code_value, (str, int)) and not isinstance(code_value, bool):
                    code = code_value
            error_type: type[ApiError]
            if status_code == 429 or code == "TOO_MANY_REQUESTS":
                error_type = RateLimitError
            elif status_code in {401, 403} or code in {"UNAUTHORIZED", "FORBIDDEN"}:
                error_type = AuthenticationError
            else:
                error_type = ApiError
            raise error_type(
                message,
                status_code=status_code,
                code=code,
                procedure=procedure,
                details=decoded,
            )
        raise ApiError(
            f"{procedure} failed",
            status_code=status_code,
            procedure=procedure,
            details=decoded,
        )

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
            except (TypeError, ValueError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


class Transport(_TransportBase):
    """синхронный http транспорт"""

    def __init__(
        self,
        *,
        base_url: str = "https://zapostit.com",
        timeout: float | httpx.Timeout = 30.0,
        retry_policy: RetryPolicy | None = None,
        codec: DevalueCodec | None = None,
        client: httpx.Client | None = None,
        session_token: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(base_url=base_url, codec=codec, retry_policy=retry_policy)
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers=self._default_headers(),
            cookies=self._session_cookies(session_token, base_url),
        )
        if client is not None and session_token is not None:
            self._install_session_cookie(client.cookies, session_token, base_url)
        self._sleep = sleep

    def request(self, procedure: str, payload: Mapping[str, Any] | None = None) -> Any:
        """run a get query and decode its devalue result"""

        response: httpx.Response | None = None
        for attempt in range(self.retry_policy.max_retries + 1):
            try:
                response = self._client.get(
                    f"{self._api_url}/{procedure}",
                    params=self.request_params(payload),
                    headers={"accept": "application/json", "x-trpc-source": "python-client"},
                )
            except httpx.TransportError as exc:
                if not self.can_retry(attempt):
                    raise ApiError(
                        f"network error while calling {procedure}: {exc}", procedure=procedure
                    ) from exc
                self._sleep(self.retry_delay(None, attempt))
                continue
            if (response.status_code == 429 or response.status_code >= 500) and self.can_retry(
                attempt
            ):
                self._sleep(self.retry_delay(response, attempt))
                continue
            return self.decode_response(response, procedure)
        raise ApiError(f"request failed without a response: {procedure}", procedure=procedure)

    def get_text(self, url: str) -> str:
        """fetch a public text document with bounded retries"""

        response: httpx.Response | None = None
        resource = "homepage"
        for attempt in range(self.retry_policy.max_retries + 1):
            try:
                response = self._client.get(url, headers={"accept": "text/html"})
            except httpx.TransportError as exc:
                if not self.can_retry(attempt):
                    raise ApiError(
                        f"network error while fetching {resource}: {exc}", procedure=resource
                    ) from exc
                self._sleep(self.retry_delay(None, attempt))
                continue
            if (response.status_code == 429 or response.status_code >= 500) and self.can_retry(
                attempt
            ):
                self._sleep(self.retry_delay(response, attempt))
                continue
            return self.decode_text_response(response, resource)
        raise ApiError(f"request failed without a response: {resource}", procedure=resource)

    def mutate(self, procedure: str, payload: Mapping[str, Any]) -> Any:
        """run an authenticated trpc mutation and decode the result"""

        serialized = self._codec.dumps(dict(payload))
        # mutation не ретраим, иначе комментарий может отправиться два раза
        try:
            response = self._client.post(
                f"{self._api_url}/{procedure}",
                json=serialized,
                headers={"accept": "application/json", "x-trpc-source": "python-client"},
            )
        except httpx.TransportError as exc:
            raise ApiError(
                f"network error while calling {procedure}: {exc}", procedure=procedure
            ) from exc
        return self.decode_response(response, procedure)

    def login(self, username: str, password: str, turnstile_token: str) -> Any:
        """create an auth.js credentials session in this transport's cookie jar"""

        self.validate_credentials(username, password, turnstile_token)
        # auth.js сначала запрашивает csrf, затем принимает credentials
        csrf_response = self._client.get(f"{self._site_url}/api/auth/csrf")
        csrf_token = self.csrf_token(csrf_response)
        response = self._client.post(
            f"{self._site_url}/api/auth/callback/credentials",
            data={
                "username": username,
                "password": password,
                "token": turnstile_token,
                "csrfToken": csrf_token,
                "callbackUrl": f"{self._site_url}/",
            },
            headers={"x-auth-return-redirect": "1"},
        )
        self.validate_login_response(response)
        return self.get_session()

    def get_session(self) -> Any:
        """return raw metadata for the current auth.js session"""

        response = self._client.get(f"{self._site_url}/api/auth/session")
        payload = self.decode_auth_json(response, "auth.session")
        if payload is None:
            raise AuthenticationError("no active session", procedure="auth.session")
        return payload

    def close(self) -> None:
        """close the http client we created ourselves"""

        if self._owns_client:
            self._client.close()

    @staticmethod
    def _default_headers() -> dict[str, str]:
        return {
            "accept": "application/json",
            "user-agent": "zapostit-api/0.2.0",
            "x-trpc-source": "python-client",
        }

    @staticmethod
    def _session_cookies(session_token: str | None, base_url: str) -> httpx.Cookies | None:
        if session_token is None:
            return None
        cookies = httpx.Cookies()
        Transport._install_session_cookie(cookies, session_token, base_url)
        return cookies

    @staticmethod
    def _install_session_cookie(
        cookies: httpx.Cookies,
        session_token: str,
        base_url: str,
    ) -> None:
        if not session_token.strip():
            raise ValueError("session_token must not be empty")
        hostname = urlparse(base_url).hostname
        if hostname is None:
            raise ValueError("base_url must contain a hostname")
        cookies.set("__Secure-authjs.session-token", session_token, domain=hostname, path="/")


class AsyncTransport(_TransportBase):
    """асинхронный http транспорт"""

    def __init__(
        self,
        *,
        base_url: str = "https://zapostit.com",
        timeout: float | httpx.Timeout = 30.0,
        retry_policy: RetryPolicy | None = None,
        codec: DevalueCodec | None = None,
        client: httpx.AsyncClient | None = None,
        session_token: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(base_url=base_url, codec=codec, retry_policy=retry_policy)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers=Transport._default_headers(),
            cookies=Transport._session_cookies(session_token, base_url),
        )
        if client is not None and session_token is not None:
            Transport._install_session_cookie(client.cookies, session_token, base_url)
        self._sleep = sleep

    async def request(self, procedure: str, payload: Mapping[str, Any] | None = None) -> Any:
        """run an async get query and decode the result"""

        response: httpx.Response | None = None
        for attempt in range(self.retry_policy.max_retries + 1):
            try:
                response = await self._client.get(
                    f"{self._api_url}/{procedure}",
                    params=self.request_params(payload),
                    headers={"accept": "application/json", "x-trpc-source": "python-client"},
                )
            except httpx.TransportError as exc:
                if not self.can_retry(attempt):
                    raise ApiError(
                        f"network error while calling {procedure}: {exc}", procedure=procedure
                    ) from exc
                await self._sleep(self.retry_delay(None, attempt))
                continue
            if (response.status_code == 429 or response.status_code >= 500) and self.can_retry(
                attempt
            ):
                await self._sleep(self.retry_delay(response, attempt))
                continue
            return self.decode_response(response, procedure)
        raise ApiError(f"request failed without a response: {procedure}", procedure=procedure)

    async def get_text(self, url: str) -> str:
        """fetch a public text document asynchronously with bounded retries"""

        response: httpx.Response | None = None
        resource = "homepage"
        for attempt in range(self.retry_policy.max_retries + 1):
            try:
                response = await self._client.get(url, headers={"accept": "text/html"})
            except httpx.TransportError as exc:
                if not self.can_retry(attempt):
                    raise ApiError(
                        f"network error while fetching {resource}: {exc}", procedure=resource
                    ) from exc
                await self._sleep(self.retry_delay(None, attempt))
                continue
            if (response.status_code == 429 or response.status_code >= 500) and self.can_retry(
                attempt
            ):
                await self._sleep(self.retry_delay(response, attempt))
                continue
            return self.decode_text_response(response, resource)
        raise ApiError(f"request failed without a response: {resource}", procedure=resource)

    async def mutate(self, procedure: str, payload: Mapping[str, Any]) -> Any:
        """run an authenticated trpc mutation and decode the result"""

        serialized = self._codec.dumps(dict(payload))
        # async mutation тоже не ретраим, чтобы не отправить действие два раза
        try:
            response = await self._client.post(
                f"{self._api_url}/{procedure}",
                json=serialized,
                headers={"accept": "application/json", "x-trpc-source": "python-client"},
            )
        except httpx.TransportError as exc:
            raise ApiError(
                f"network error while calling {procedure}: {exc}", procedure=procedure
            ) from exc
        return self.decode_response(response, procedure)

    async def login(self, username: str, password: str, turnstile_token: str) -> Any:
        """create an auth.js credentials session in this transport's cookie jar"""

        self.validate_credentials(username, password, turnstile_token)
        # порядок auth.js: csrf, credentials, затем проверка сессии
        csrf_response = await self._client.get(f"{self._site_url}/api/auth/csrf")
        csrf_token = self.csrf_token(csrf_response)
        response = await self._client.post(
            f"{self._site_url}/api/auth/callback/credentials",
            data={
                "username": username,
                "password": password,
                "token": turnstile_token,
                "csrfToken": csrf_token,
                "callbackUrl": f"{self._site_url}/",
            },
            headers={"x-auth-return-redirect": "1"},
        )
        self.validate_login_response(response)
        return await self.get_session()

    async def get_session(self) -> Any:
        """return raw metadata for the current auth.js session"""

        response = await self._client.get(f"{self._site_url}/api/auth/session")
        payload = self.decode_auth_json(response, "auth.session")
        if payload is None:
            raise AuthenticationError("no active session", procedure="auth.session")
        return payload

    async def close(self) -> None:
        """close the async http client we created ourselves"""

        if self._owns_client:
            await self._client.aclose()
