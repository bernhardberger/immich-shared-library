from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

SYNC_IMPORT_ERROR = None

try:
    from src.config import SharedAlbumsShadowLibraryConfig
    from src.shared_album_cleanup import cleanup_orphaned_shared_album_assets
except ModuleNotFoundError as exc:
    if exc.name in {"asyncpg", "pydantic", "pydantic_settings"}:
        SYNC_IMPORT_ERROR = exc
    else:
        raise


SOURCE_ASSET_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
TARGET_ASSET_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
TARGET_USER_ID = UUID("22222222-2222-2222-2222-222222222222")
OTHER_TARGET_USER_ID = UUID("55555555-5555-5555-5555-555555555555")
LIBRARY_ID = UUID("33333333-3333-3333-3333-333333333333")


class FakeConnection:
    def __init__(self, rows, files=None):
        self.rows = list(rows)
        self.files = list(files or [])
        self.fetch_calls = []
        self.execute_calls = []

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        if "FROM asset_file" in sql:
            return self.files
        return self.rows

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return "DELETE 1"


def shadow_config(temp_dir: str) -> SharedAlbumsShadowLibraryConfig:
    return SharedAlbumsShadowLibraryConfig(
        name_prefix="Immich Shared Library Mirrors",
        filesystem_root=str(Path(temp_dir) / "shadow-root"),
        import_path_prefix="/external_library/.immich-shared-library/shared-albums",
    )


def orphan_row(original_path: str, *, remaining_justifications: int = 0) -> dict:
    return {
        "source_asset_id": SOURCE_ASSET_ID,
        "target_asset_id": TARGET_ASSET_ID,
        "target_user_id": TARGET_USER_ID,
        "ownerId": TARGET_USER_ID,
        "library_owner_id": TARGET_USER_ID,
        "libraryId": LIBRARY_ID,
        "originalPath": original_path,
        "remaining_justifications": remaining_justifications,
    }


@unittest.skipIf(
    SYNC_IMPORT_ERROR is not None,
    f"missing dependency: {SYNC_IMPORT_ERROR.name if SYNC_IMPORT_ERROR else ''}",
)
class SharedAlbumCleanupTest(unittest.IsolatedAsyncioTestCase):
    async def test_orphan_target_asset_under_shadow_prefix_is_deleted_and_symlink_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = shadow_config(temp_dir)
            relative = Path(str(TARGET_USER_ID)) / "source" / str(SOURCE_ASSET_ID) / "IMG_0001.JPG"
            link_path = Path(config.filesystem_root) / relative
            source_path = Path(temp_dir) / "source.JPG"
            source_path.write_text("source", encoding="utf-8")
            link_path.parent.mkdir(parents=True)
            link_path.symlink_to(source_path)
            import_path = f"{config.import_path_prefix}/{relative.as_posix()}"
            thumb_path = Path(temp_dir) / "thumbs" / "thumb.webp"
            thumb_path.parent.mkdir()
            thumb_path.write_text("thumb", encoding="utf-8")
            conn = FakeConnection(
                [orphan_row(import_path)],
                files=[{"path": str(thumb_path)}],
            )

            with patch("src.shared_album_cleanup.remove_hardlinks") as remove_hardlinks:
                cleaned = await cleanup_orphaned_shared_album_assets(conn, config)

            self.assertEqual(cleaned, 1)
            self.assertFalse(link_path.exists())
            remove_hardlinks.assert_called_once_with([str(thumb_path)])
            self.assertEqual(
                [args for _, args in conn.execute_calls],
                [(TARGET_ASSET_ID,), (TARGET_ASSET_ID,), (TARGET_ASSET_ID,)],
            )
            self.assertIn("DELETE FROM album_asset", conn.execute_calls[0][0])
            self.assertIn("DELETE FROM asset", conn.execute_calls[1][0])
            self.assertIn("DELETE FROM _face_sync_asset_map", conn.execute_calls[2][0])

    async def test_target_asset_with_remaining_justification_is_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = shadow_config(temp_dir)
            relative = Path(str(TARGET_USER_ID)) / "source" / str(SOURCE_ASSET_ID) / "IMG_0001.JPG"
            link_path = Path(config.filesystem_root) / relative
            source_path = Path(temp_dir) / "source.JPG"
            source_path.write_text("source", encoding="utf-8")
            link_path.parent.mkdir(parents=True)
            link_path.symlink_to(source_path)
            conn = FakeConnection([
                orphan_row(
                    f"{config.import_path_prefix}/{relative.as_posix()}",
                    remaining_justifications=1,
                )
            ])

            with patch("src.shared_album_cleanup.remove_hardlinks") as remove_hardlinks:
                cleaned = await cleanup_orphaned_shared_album_assets(conn, config)

            self.assertEqual(cleaned, 0)
            self.assertTrue(link_path.is_symlink())
            remove_hardlinks.assert_not_called()
            self.assertEqual(conn.execute_calls, [])

    async def test_target_asset_outside_import_prefix_is_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = shadow_config(temp_dir)
            conn = FakeConnection([orphan_row("/external_library/user/IMG_0001.JPG")])

            with patch("src.shared_album_cleanup.remove_hardlinks") as remove_hardlinks:
                cleaned = await cleanup_orphaned_shared_album_assets(conn, config)

            self.assertEqual(cleaned, 0)
            remove_hardlinks.assert_not_called()
            self.assertEqual(conn.execute_calls, [])

    async def test_target_asset_under_different_user_shadow_prefix_is_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = shadow_config(temp_dir)
            relative = Path(str(OTHER_TARGET_USER_ID)) / "source" / str(SOURCE_ASSET_ID) / "IMG_0001.JPG"
            link_path = Path(config.filesystem_root) / relative
            source_path = Path(temp_dir) / "source.JPG"
            source_path.write_text("source", encoding="utf-8")
            link_path.parent.mkdir(parents=True)
            link_path.symlink_to(source_path)
            conn = FakeConnection([orphan_row(f"{config.import_path_prefix}/{relative.as_posix()}")])

            with patch("src.shared_album_cleanup.remove_hardlinks") as remove_hardlinks:
                cleaned = await cleanup_orphaned_shared_album_assets(conn, config)

            self.assertEqual(cleaned, 0)
            self.assertTrue(link_path.is_symlink())
            remove_hardlinks.assert_not_called()
            self.assertEqual(conn.execute_calls, [])

    async def test_non_symlink_at_shadow_path_is_not_removed_or_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = shadow_config(temp_dir)
            relative = Path(str(TARGET_USER_ID)) / "source" / str(SOURCE_ASSET_ID) / "IMG_0001.JPG"
            regular_file = Path(config.filesystem_root) / relative
            regular_file.parent.mkdir(parents=True)
            regular_file.write_text("not sidecar symlink", encoding="utf-8")
            conn = FakeConnection([orphan_row(f"{config.import_path_prefix}/{relative.as_posix()}")])

            with patch("src.shared_album_cleanup.remove_hardlinks") as remove_hardlinks:
                cleaned = await cleanup_orphaned_shared_album_assets(conn, config)

            self.assertEqual(cleaned, 0)
            self.assertTrue(regular_file.exists())
            self.assertFalse(regular_file.is_symlink())
            remove_hardlinks.assert_not_called()
            self.assertEqual(conn.execute_calls, [])


if __name__ == "__main__":
    unittest.main()
