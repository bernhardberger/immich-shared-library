import logging
from typing import Any, Iterable
from uuid import UUID

import httpx

from src.config import settings

logger = logging.getLogger(__name__)


class ImmichAPI:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._base_url = (base_url or settings.immich_api_url).rstrip("/")
        self._client = client or httpx.AsyncClient(
            headers={
                "x-api-key": api_key if api_key is not None else settings.immich_api_key.get_secret_value(),
                "Accept": "application/json",
            },
            timeout=30,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def health_check(self) -> bool:
        """Check if Immich server is reachable."""
        try:
            resp = await self._client.get(f"{self._base_url}/api/server/ping", timeout=10)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def list_libraries(self) -> list[dict[str, Any]]:
        resp = await self._client.get(f"{self._base_url}/api/libraries")
        resp.raise_for_status()
        return resp.json()

    async def create_library(
        self,
        *,
        owner_id: UUID | str,
        name: str,
        import_paths: Iterable[str],
        exclusion_patterns: Iterable[str] = (),
    ) -> dict[str, Any]:
        resp = await self._client.post(
            f"{self._base_url}/api/libraries",
            json={
                "ownerId": str(owner_id),
                "name": name,
                "importPaths": list(import_paths),
                "exclusionPatterns": list(exclusion_patterns),
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def update_library(
        self,
        *,
        library_id: UUID | str,
        name: str,
        import_paths: Iterable[str],
        exclusion_patterns: Iterable[str] = (),
    ) -> dict[str, Any]:
        resp = await self._client.put(
            f"{self._base_url}/api/libraries/{library_id}",
            json={
                "name": name,
                "importPaths": list(import_paths),
                "exclusionPatterns": list(exclusion_patterns),
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def validate_library(self, library_id: UUID | str) -> Any:
        resp = await self._client.post(f"{self._base_url}/api/libraries/{library_id}/validate")
        resp.raise_for_status()
        return _json_or_none(resp)

    async def scan_library(self, library_id: UUID | str) -> Any:
        resp = await self._client.post(f"{self._base_url}/api/libraries/{library_id}/scan")
        resp.raise_for_status()
        return _json_or_none(resp)


def _json_or_none(resp: httpx.Response) -> Any | None:
    """Return JSON when present, or None for no-content/empty responses."""
    if resp.status_code == 204:
        return None
    content = getattr(resp, "content", None)
    if content == b"" or content == "":
        return None
    return resp.json()
