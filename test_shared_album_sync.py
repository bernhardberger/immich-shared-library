from __future__ import annotations

from datetime import datetime, timezone
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID

SYNC_IMPORT_ERROR = None

try:
    from src.config import (
        AppConfig,
        SharedAlbumsConfig,
        SharedAlbumsShadowLibraryConfig,
        SYNC_MODE_PATH_PREFIX,
        SYNC_MODE_SHARED_ALBUMS,
    )
    from src.shared_album_discovery import SharedAlbumEdge
    from src.shared_album_sync import (
        ensure_shadow_library,
        process_shared_album_edges,
    )
except ModuleNotFoundError as exc:
    if exc.name in {"asyncpg", "pydantic", "pydantic_settings", "httpx"}:
        SYNC_IMPORT_ERROR = exc
    else:
        raise


ALBUM_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SOURCE_ASSET_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
SECOND_SOURCE_ASSET_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2")
TARGET_ASSET_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
SOURCE_USER_ID = UUID("11111111-1111-1111-1111-111111111111")
TARGET_USER_ID = UUID("22222222-2222-2222-2222-222222222222")
LIBRARY_ID = UUID("33333333-3333-3333-3333-333333333333")
WRONG_USER_ID = UUID("44444444-4444-4444-4444-444444444444")


class FakeAPI:
    def __init__(self, libraries=None):
        self.libraries = list(libraries or [])
        self.created = []
        self.updated = []
        self.validated = []
        self.scanned = []

    async def list_libraries(self):
        return list(self.libraries)

    async def create_library(self, **kwargs):
        self.created.append(kwargs)
        library = {
            "id": str(LIBRARY_ID),
            "ownerId": str(kwargs["owner_id"]),
            "name": kwargs["name"],
            "importPaths": list(kwargs["import_paths"]),
            "exclusionPatterns": list(kwargs["exclusion_patterns"]),
        }
        self.libraries.append(library)
        return library

    async def update_library(self, **kwargs):
        self.updated.append(kwargs)
        return {
            "id": str(kwargs["library_id"]),
            "ownerId": str(TARGET_USER_ID),
            "name": kwargs["name"],
            "importPaths": list(kwargs["import_paths"]),
            "exclusionPatterns": list(kwargs["exclusion_patterns"]),
        }

    async def validate_library(self, library_id):
        self.validated.append(library_id)
        return None

    async def scan_library(self, library_id):
        self.scanned.append(library_id)
        return None


class FakeConnection:
    def __init__(self, mirrored_target_asset_id=None):
        self.mirrored_target_asset_id = mirrored_target_asset_id
        self.fetchval_calls = []
        self.execute_calls = []

    async def fetchval(self, sql, *args):
        self.fetchval_calls.append((sql, args))
        return self.mirrored_target_asset_id

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return "INSERT 0 1"


def shadow_config(
    temp_dir: str,
    *,
    auto_create: bool = True,
    auto_scan: bool = False,
) -> SharedAlbumsShadowLibraryConfig:
    return SharedAlbumsShadowLibraryConfig(
        name_prefix="Immich Shared Library Mirrors",
        filesystem_root=str(Path(temp_dir) / "shadow-root"),
        import_path_prefix="/external_library/.immich-shared-library/shared-albums",
        auto_create=auto_create,
        auto_scan=auto_scan,
    )


def source_asset_row(original_path: str, *, asset_id: UUID = SOURCE_ASSET_ID, filename: str = "IMG_0001.JPG") -> dict:
    return {
        "id": asset_id,
        "ownerId": SOURCE_USER_ID,
        "originalPath": original_path,
        "originalFileName": filename,
    }


@unittest.skipIf(
    SYNC_IMPORT_ERROR is not None,
    f"missing dependency: {SYNC_IMPORT_ERROR.name if SYNC_IMPORT_ERROR else ''}",
)
class SharedAlbumSyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_shadow_library_creates_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = shadow_config(temp_dir, auto_create=True)
            api = FakeAPI()

            library = await ensure_shadow_library(api, TARGET_USER_ID, config)

        self.assertEqual(UUID(library["id"]), LIBRARY_ID)
        self.assertEqual(len(api.created), 1)
        self.assertEqual(api.created[0]["owner_id"], TARGET_USER_ID)
        self.assertEqual(
            api.created[0]["import_paths"],
            [f"/external_library/.immich-shared-library/shared-albums/{TARGET_USER_ID}"],
        )
        self.assertEqual(api.validated, [])
        self.assertEqual(api.scanned, [])

    async def test_ensure_shadow_library_auto_scans_after_create_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = shadow_config(temp_dir, auto_create=True, auto_scan=True)
            api = FakeAPI()

            await ensure_shadow_library(api, TARGET_USER_ID, config)

        self.assertEqual(api.validated, [LIBRARY_ID])
        self.assertEqual(api.scanned, [LIBRARY_ID])

    async def test_ensure_shadow_library_updates_only_deterministic_library_with_wrong_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = shadow_config(temp_dir, auto_create=True)
            api = FakeAPI([
                {
                    "id": str(LIBRARY_ID),
                    "ownerId": str(TARGET_USER_ID),
                    "name": f"Immich Shared Library Mirrors - {TARGET_USER_ID}",
                    "importPaths": ["/old/path"],
                    "exclusionPatterns": ["*.tmp"],
                },
                {
                    "id": str(UUID("55555555-5555-5555-5555-555555555555")),
                    "ownerId": str(TARGET_USER_ID),
                    "name": "User-created library",
                    "importPaths": ["/old/path"],
                    "exclusionPatterns": ["*.tmp"],
                },
            ])

            library = await ensure_shadow_library(api, TARGET_USER_ID, config)

        self.assertEqual(UUID(library["id"]), LIBRARY_ID)
        self.assertEqual(len(api.updated), 1)
        self.assertEqual(api.updated[0]["library_id"], LIBRARY_ID)
        self.assertEqual(api.updated[0]["exclusion_patterns"], [])

    async def test_ensure_shadow_library_auto_scans_after_update_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = shadow_config(temp_dir, auto_create=True, auto_scan=True)
            api = FakeAPI([
                {
                    "id": str(LIBRARY_ID),
                    "ownerId": str(TARGET_USER_ID),
                    "name": f"Immich Shared Library Mirrors - {TARGET_USER_ID}",
                    "importPaths": ["/old/path"],
                    "exclusionPatterns": [],
                }
            ])

            await ensure_shadow_library(api, TARGET_USER_ID, config)

        self.assertEqual(api.validated, [LIBRARY_ID])
        self.assertEqual(api.scanned, [LIBRARY_ID])

    async def test_ensure_shadow_library_does_not_auto_scan_unchanged_library(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = shadow_config(temp_dir, auto_create=True, auto_scan=True)
            api = FakeAPI([
                {
                    "id": str(LIBRARY_ID),
                    "ownerId": str(TARGET_USER_ID),
                    "name": f"Immich Shared Library Mirrors - {TARGET_USER_ID}",
                    "importPaths": [f"/external_library/.immich-shared-library/shared-albums/{TARGET_USER_ID}"],
                    "exclusionPatterns": [],
                }
            ])

            await ensure_shadow_library(api, TARGET_USER_ID, config)

        self.assertEqual(api.validated, [])
        self.assertEqual(api.scanned, [])

    async def test_ensure_shadow_library_fails_closed_when_auto_create_false_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = shadow_config(temp_dir, auto_create=False)
            api = FakeAPI()

            with self.assertRaisesRegex(RuntimeError, "not found"):
                await ensure_shadow_library(api, TARGET_USER_ID, config)

    async def test_ensure_shadow_library_fails_closed_when_name_matches_wrong_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = shadow_config(temp_dir, auto_create=True)
            api = FakeAPI([
                {
                    "id": str(LIBRARY_ID),
                    "ownerId": str(WRONG_USER_ID),
                    "name": f"Immich Shared Library Mirrors - {TARGET_USER_ID}",
                    "importPaths": [f"/external_library/.immich-shared-library/shared-albums/{TARGET_USER_ID}"],
                    "exclusionPatterns": [],
                }
            ])

            with self.assertRaisesRegex(RuntimeError, "belongs to user"):
                await ensure_shadow_library(api, TARGET_USER_ID, config)

    async def test_existing_mapping_only_records_album_justification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = shadow_config(temp_dir)
            api = FakeAPI([
                {
                    "id": str(LIBRARY_ID),
                    "ownerId": str(TARGET_USER_ID),
                    "name": f"Immich Shared Library Mirrors - {TARGET_USER_ID}",
                    "importPaths": [f"/external_library/.immich-shared-library/shared-albums/{TARGET_USER_ID}"],
                    "exclusionPatterns": [],
                }
            ])
            conn = FakeConnection(mirrored_target_asset_id=TARGET_ASSET_ID)
            edge = SharedAlbumEdge(ALBUM_ID, SOURCE_ASSET_ID, SOURCE_USER_ID, TARGET_USER_ID)
            sync_asset = AsyncMock()
            sync_faces = AsyncMock()

            stats = await process_shared_album_edges(
                conn,
                api,
                config,
                [edge],
                {SOURCE_ASSET_ID: source_asset_row("/source/IMG_0001.JPG")},
                cycle_started_at=datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
                sync_asset_fn=sync_asset,
                sync_faces_for_asset_fn=sync_faces,
            )

        sync_asset.assert_not_awaited()
        sync_faces.assert_not_awaited()
        self.assertEqual(stats["assets_synced"], 0)
        self.assertEqual(stats["album_justifications_marked"], 1)
        self.assertIn("INSERT INTO _face_sync_album_map", conn.execute_calls[0][0])

    async def test_new_edge_creates_symlink_syncs_asset_faces_and_records_justification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = str(Path(temp_dir) / "source" / "IMG_0001.JPG")
            config = shadow_config(temp_dir)
            api = FakeAPI([
                {
                    "id": str(LIBRARY_ID),
                    "ownerId": str(TARGET_USER_ID),
                    "name": f"Immich Shared Library Mirrors - {TARGET_USER_ID}",
                    "importPaths": [f"/external_library/.immich-shared-library/shared-albums/{TARGET_USER_ID}"],
                    "exclusionPatterns": [],
                }
            ])
            conn = FakeConnection()
            edge = SharedAlbumEdge(ALBUM_ID, SOURCE_ASSET_ID, SOURCE_USER_ID, TARGET_USER_ID)
            sync_asset = AsyncMock(return_value=TARGET_ASSET_ID)
            sync_faces = AsyncMock(return_value=3)
            find_duplicates = AsyncMock(return_value=set())
            record_duplicates = AsyncMock()

            stats = await process_shared_album_edges(
                conn,
                api,
                config,
                [edge],
                {SOURCE_ASSET_ID: source_asset_row(source_path)},
                cycle_started_at=datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
                sync_asset_fn=sync_asset,
                sync_faces_for_asset_fn=sync_faces,
                find_duplicate_filenames_fn=find_duplicates,
                record_skipped_duplicates_fn=record_duplicates,
            )

            expected_link = (
                Path(config.filesystem_root)
                / str(TARGET_USER_ID)
                / str(SOURCE_USER_ID)
                / str(SOURCE_ASSET_ID)
                / "IMG_0001.JPG"
            )
            self.assertTrue(expected_link.is_symlink())
            self.assertEqual(os.readlink(expected_link), source_path)

        sync_asset.assert_awaited_once()
        find_duplicates.assert_awaited_once()
        record_duplicates.assert_not_awaited()
        sync_faces.assert_awaited_once_with(
            conn,
            SOURCE_ASSET_ID,
            TARGET_ASSET_ID,
            SOURCE_USER_ID,
            TARGET_USER_ID,
        )
        self.assertEqual(stats["assets_synced"], 1)
        self.assertEqual(stats["faces_synced"], 3)
        self.assertEqual(stats["album_justifications_marked"], 1)

    async def test_new_shared_edge_skips_and_records_target_user_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = str(Path(temp_dir) / "source" / "IMG_0001.JPG")
            config = shadow_config(temp_dir)
            api = FakeAPI([
                {
                    "id": str(LIBRARY_ID),
                    "ownerId": str(TARGET_USER_ID),
                    "name": f"Immich Shared Library Mirrors - {TARGET_USER_ID}",
                    "importPaths": [f"/external_library/.immich-shared-library/shared-albums/{TARGET_USER_ID}"],
                    "exclusionPatterns": [],
                }
            ])
            conn = FakeConnection()
            duplicate_edge = SharedAlbumEdge(ALBUM_ID, SOURCE_ASSET_ID, SOURCE_USER_ID, TARGET_USER_ID)
            fresh_edge = SharedAlbumEdge(ALBUM_ID, SECOND_SOURCE_ASSET_ID, SOURCE_USER_ID, TARGET_USER_ID)
            sync_asset = AsyncMock(return_value=TARGET_ASSET_ID)
            sync_faces = AsyncMock(return_value=1)
            find_duplicates = AsyncMock(return_value={SOURCE_ASSET_ID})
            record_duplicates = AsyncMock()

            stats = await process_shared_album_edges(
                conn,
                api,
                config,
                [duplicate_edge, fresh_edge],
                {
                    SOURCE_ASSET_ID: source_asset_row(source_path),
                    SECOND_SOURCE_ASSET_ID: source_asset_row(
                        str(Path(temp_dir) / "source" / "IMG_0002.JPG"),
                        asset_id=SECOND_SOURCE_ASSET_ID,
                        filename="IMG_0002.JPG",
                    ),
                },
                cycle_started_at=datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
                sync_asset_fn=sync_asset,
                sync_faces_for_asset_fn=sync_faces,
                find_duplicate_filenames_fn=find_duplicates,
                record_skipped_duplicates_fn=record_duplicates,
            )

        find_duplicates.assert_awaited_once()
        duplicate_job = find_duplicates.await_args.args[2]
        self.assertEqual(duplicate_job.target_user_id, TARGET_USER_ID)
        self.assertEqual(duplicate_job.target_library_id, LIBRARY_ID)
        record_duplicates.assert_awaited_once_with(conn, {SOURCE_ASSET_ID}, TARGET_USER_ID)
        sync_asset.assert_awaited_once()
        synced_source = sync_asset.await_args.args[1]
        self.assertEqual(synced_source["id"], SECOND_SOURCE_ASSET_ID)
        self.assertEqual(stats["assets_skipped_duplicate"], 1)
        self.assertEqual(stats["assets_synced"], 1)

    async def test_run_full_sync_dispatches_to_path_prefix_mode(self) -> None:
        from src import sync_engine

        fake_settings = type(
            "FakeSettings",
            (),
            {"sync_config": AppConfig(sync_mode=SYNC_MODE_PATH_PREFIX, sync_jobs=[])},
        )()

        with patch.object(sync_engine, "settings", fake_settings), \
             patch.object(sync_engine, "run_path_prefix_sync", AsyncMock(return_value={"assets_synced": 7})) as old_path, \
             patch.object(sync_engine, "run_shared_albums_sync", AsyncMock()) as shared_path:
            stats = await sync_engine.run_full_sync()

        self.assertEqual(stats, {"assets_synced": 7})
        old_path.assert_awaited_once()
        shared_path.assert_not_awaited()

    async def test_run_full_sync_dispatches_to_shared_albums_mode(self) -> None:
        from src import sync_engine

        fake_settings = type(
            "FakeSettings",
            (),
            {"sync_config": AppConfig(sync_mode=SYNC_MODE_SHARED_ALBUMS, shared_albums=SharedAlbumsConfig())},
        )()
        api = object()

        with patch.object(sync_engine, "settings", fake_settings), \
             patch.object(sync_engine, "run_path_prefix_sync", AsyncMock()) as old_path, \
             patch.object(sync_engine, "run_shared_albums_sync", AsyncMock(return_value={"assets_synced": 2})) as shared_path:
            stats = await sync_engine.run_full_sync(api=api)

        self.assertEqual(stats, {"assets_synced": 2})
        shared_path.assert_awaited_once_with(api=api)
        old_path.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
