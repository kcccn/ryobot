from __future__ import annotations

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
        response = await self._client.get(
            path,
            params=params,
            headers=self._headers(accept=accept),
        )
        response.raise_for_status()
        return response.json()

    async def get_text(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> str:
        response = await self._client.get(
            path,
            params=params,
            headers=self._headers(accept=accept),
        )
        response.raise_for_status()
        return response.text

    async def post_json(
        self,
        path: str,
        *,
        json_body: dict[str, Any],
        accept: str = "application/vnd.github+json",
    ) -> Any:
        response = await self._client.post(
            path,
            json=json_body,
            headers=self._headers(accept=accept),
        )
        response.raise_for_status()
        return response.json()

    async def patch_json(
        self,
        path: str,
        *,
        json_body: dict[str, Any],
        accept: str = "application/vnd.github+json",
    ) -> Any:
        response = await self._client.patch(
            path,
            json=json_body,
            headers=self._headers(accept=accept),
        )
        response.raise_for_status()
        return response.json()

    async def post_no_content(
        self,
        path: str,
        *,
        json_body: dict[str, Any],
        accept: str = "application/vnd.github+json",
    ) -> None:
        response = await self._client.post(
            path,
            json=json_body,
            headers=self._headers(accept=accept),
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
