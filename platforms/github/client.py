from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

GITHUB_API_VERSION = "2022-11-28"
DEFAULT_GITHUB_API_BASE_URL = "https://api.github.com"


class GitHubApiClient:
    """Thin async REST client for GitHub APIs."""

    def __init__(
        self,
        *,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
        api_base_url: str | None = None,
    ) -> None:
        resolved_token = token or os.getenv("GITHUB_TOKEN")
        if not resolved_token:
            raise ValueError("GitHub API token is required.")

        self._owns_client = client is None
        self._token = resolved_token
        self._base_url = (api_base_url or os.getenv("GITHUB_API_BASE_URL") or DEFAULT_GITHUB_API_BASE_URL).rstrip("/")
        self._client = client or httpx.AsyncClient(base_url=self._base_url)

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> Any:
        response = await self._with_retry(
            lambda: self._client.get(
                path,
                params=params,
                headers=self._headers(accept=accept),
            ),
        )
        return response.json()

    async def get_text(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> str:
        response = await self._with_retry(
            lambda: self._client.get(
                path,
                params=params,
                headers=self._headers(accept=accept),
            ),
        )
        return response.text

    async def post_json(
        self,
        path: str,
        *,
        json_body: dict[str, Any],
        accept: str = "application/vnd.github+json",
    ) -> Any:
        response = await self._with_retry(
            lambda: self._client.post(
                path,
                json=json_body,
                headers=self._headers(accept=accept),
            ),
        )
        return response.json()

    async def put_json(
        self,
        path: str,
        *,
        json_body: dict[str, Any],
        accept: str = "application/vnd.github+json",
    ) -> Any:
        response = await self._with_retry(
            lambda: self._client.put(
                path,
                json=json_body,
                headers=self._headers(accept=accept),
            ),
        )
        return response.json()

    async def patch_json(
        self,
        path: str,
        *,
        json_body: dict[str, Any],
        accept: str = "application/vnd.github+json",
    ) -> Any:
        response = await self._with_retry(
            lambda: self._client.patch(
                path,
                json=json_body,
                headers=self._headers(accept=accept),
            ),
        )
        return response.json()

    async def post_no_content(
        self,
        path: str,
        *,
        json_body: dict[str, Any],
        accept: str = "application/vnd.github+json",
    ) -> None:
        response = await self._with_retry(
            lambda: self._client.post(
                path,
                json=json_body,
                headers=self._headers(accept=accept),
            ),
        )
        response.raise_for_status()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self, *, accept: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": accept,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }

    @staticmethod
    def _retryable(response: httpx.Response) -> bool:
        return response.status_code in (403, 429, 500, 502, 503, 504)

    async def _with_retry(self, request: Any) -> httpx.Response:
        attempt = 0
        delay = 1.0
        while True:
            attempt += 1
            response: httpx.Response | None = None
            try:
                response = await request()
                if not self._retryable(response):
                    response.raise_for_status()
                    return response
                if attempt >= 3:
                    response.raise_for_status()
                    return response
            except (httpx.NetworkError, httpx.TimeoutException):
                if attempt >= 3:
                    raise
            except httpx.HTTPStatusError:
                if response is None or not self._retryable(response) or attempt >= 3:
                    raise
            await asyncio.sleep(self._retry_delay(response, delay))
            delay *= 2

    @staticmethod
    def _retry_delay(response: httpx.Response | None, fallback: float) -> float:
        if response is not None and response.status_code in (403, 429):
            header = response.headers.get("Retry-After", "")
            if header:
                try:
                    return float(header)
                except ValueError:
                    pass
        return fallback
