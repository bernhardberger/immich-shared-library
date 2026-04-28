from __future__ import annotations

import unittest
from uuid import UUID

IMMICH_API_IMPORT_ERROR = None

try:
    from src.immich_api import ImmichAPI
except ModuleNotFoundError as exc:
    if exc.name in {"httpx", "pydantic", "pydantic_settings"}:
        IMMICH_API_IMPORT_ERROR = exc
    else:
        raise


USER_ID = UUID("11111111-1111-1111-1111-111111111111")
LIBRARY_ID = UUID("22222222-2222-2222-2222-222222222222")


class FakeResponse:
    def __init__(self, payload=None, status_code: int = 200, content: bytes | None = None) -> None:
        self.payload = payload
        self.status_code = status_code
        self.content = content if content is not None else (b"" if status_code == 204 else b"{}")
        self.raise_for_status_called = False

    def json(self):
        return self.payload

    def raise_for_status(self) -> None:
        self.raise_for_status_called = True


class FakeAsyncClient:
    def __init__(self) -> None:
        self.calls = []
        self.response = FakeResponse({"id": str(LIBRARY_ID)})

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.response

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.response

    async def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return self.response

    async def aclose(self):
        self.calls.append(("CLOSE", None, {}))


@unittest.skipIf(
    IMMICH_API_IMPORT_ERROR is not None,
    f"missing dependency: {IMMICH_API_IMPORT_ERROR.name if IMMICH_API_IMPORT_ERROR else ''}",
)
class ImmichLibraryAPITest(unittest.IsolatedAsyncioTestCase):
    def make_api(self, client: FakeAsyncClient) -> ImmichAPI:
        return ImmichAPI(client=client, base_url="http://immich.example")

    async def test_list_libraries_uses_libraries_endpoint(self) -> None:
        client = FakeAsyncClient()
        client.response = FakeResponse([{"id": str(LIBRARY_ID)}])

        result = await self.make_api(client).list_libraries()

        self.assertEqual(result, [{"id": str(LIBRARY_ID)}])
        self.assertEqual(client.calls[0], ("GET", "http://immich.example/api/libraries", {}))
        self.assertTrue(client.response.raise_for_status_called)

    async def test_create_library_sends_owner_name_paths_and_exclusions(self) -> None:
        client = FakeAsyncClient()

        result = await self.make_api(client).create_library(
            owner_id=USER_ID,
            name="Mirror library",
            import_paths=["/external/mirror/user"],
            exclusion_patterns=["*.tmp"],
        )

        self.assertEqual(result, {"id": str(LIBRARY_ID)})
        self.assertEqual(client.calls[0][0], "POST")
        self.assertEqual(client.calls[0][1], "http://immich.example/api/libraries")
        self.assertEqual(
            client.calls[0][2]["json"],
            {
                "ownerId": str(USER_ID),
                "name": "Mirror library",
                "importPaths": ["/external/mirror/user"],
                "exclusionPatterns": ["*.tmp"],
            },
        )

    async def test_update_library_replaces_full_path_arrays(self) -> None:
        client = FakeAsyncClient()

        await self.make_api(client).update_library(
            library_id=LIBRARY_ID,
            name="Mirror library",
            import_paths=["/external/mirror/user"],
            exclusion_patterns=[],
        )

        self.assertEqual(client.calls[0][0], "PUT")
        self.assertEqual(client.calls[0][1], f"http://immich.example/api/libraries/{LIBRARY_ID}")
        self.assertEqual(
            client.calls[0][2]["json"],
            {
                "name": "Mirror library",
                "importPaths": ["/external/mirror/user"],
                "exclusionPatterns": [],
            },
        )

    async def test_validate_and_scan_library_use_expected_endpoints(self) -> None:
        client = FakeAsyncClient()
        api = self.make_api(client)

        await api.validate_library(LIBRARY_ID)
        await api.scan_library(LIBRARY_ID)

        self.assertEqual(client.calls[0][0:2], ("POST", f"http://immich.example/api/libraries/{LIBRARY_ID}/validate"))
        self.assertEqual(client.calls[1][0:2], ("POST", f"http://immich.example/api/libraries/{LIBRARY_ID}/scan"))

    async def test_validate_and_scan_library_return_none_for_no_content(self) -> None:
        client = FakeAsyncClient()
        client.response = FakeResponse(status_code=204)
        api = self.make_api(client)

        self.assertIsNone(await api.validate_library(LIBRARY_ID))
        self.assertIsNone(await api.scan_library(LIBRARY_ID))

    async def test_scan_library_returns_none_for_empty_body(self) -> None:
        client = FakeAsyncClient()
        client.response = FakeResponse(status_code=200, content=b"")

        self.assertIsNone(await self.make_api(client).scan_library(LIBRARY_ID))

    async def test_update_asset_metadata_uses_asset_update_endpoint(self) -> None:
        client = FakeAsyncClient()

        await self.make_api(client).update_asset_metadata(
            LIBRARY_ID,
            description="Fasching",
            latitude=47.95,
            longitude=16.233,
        )

        self.assertEqual(client.calls[0][0], "PUT")
        self.assertEqual(client.calls[0][1], f"http://immich.example/api/assets/{LIBRARY_ID}")
        self.assertEqual(
            client.calls[0][2]["json"],
            {"description": "Fasching", "latitude": 47.95, "longitude": 16.233},
        )

    async def test_update_assets_metadata_uses_bulk_asset_update_endpoint(self) -> None:
        client = FakeAsyncClient()

        await self.make_api(client).update_assets_metadata([LIBRARY_ID], timeZone="Europe/Vienna")

        self.assertEqual(client.calls[0][0], "PUT")
        self.assertEqual(client.calls[0][1], "http://immich.example/api/assets")
        self.assertEqual(client.calls[0][2]["json"], {"ids": [str(LIBRARY_ID)], "timeZone": "Europe/Vienna"})


if __name__ == "__main__":
    unittest.main()
