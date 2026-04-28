from datetime import datetime, timezone
import unittest
from uuid import UUID

TRACKING_IMPORT_ERROR = None

try:
    from src.shared_album_discovery import SharedAlbumEdge
    from src.shared_album_tracking import (
        find_mirrored_target_asset_id,
        has_shared_album_justification,
        mark_album_justification,
        remove_stale_album_justifications,
    )
except ModuleNotFoundError as exc:
    if exc.name == "asyncpg":
        TRACKING_IMPORT_ERROR = exc
    else:
        raise


ALBUM = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SOURCE_ASSET = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
SOURCE_USER = UUID("11111111-1111-1111-1111-111111111111")
TARGET_USER = UUID("22222222-2222-2222-2222-222222222222")
TARGET_ASSET = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
TARGET_ALBUM = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


class FakeConnection:
    def __init__(self) -> None:
        self.execute_calls = []
        self.fetchval_calls = []
        self.execute_result = "DELETE 0"
        self.fetchval_result = None

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return self.execute_result

    async def fetchval(self, sql, *args):
        self.fetchval_calls.append((sql, args))
        return self.fetchval_result


@unittest.skipIf(
    TRACKING_IMPORT_ERROR is not None,
    f"missing dependency: {TRACKING_IMPORT_ERROR.name if TRACKING_IMPORT_ERROR else ''}",
)
class SharedAlbumTrackingTest(unittest.IsolatedAsyncioTestCase):
    async def test_mark_album_justification_upserts_edge_with_target_asset(self) -> None:
        conn = FakeConnection()
        seen_at = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
        edge = SharedAlbumEdge(ALBUM, SOURCE_ASSET, SOURCE_USER, TARGET_USER)

        await mark_album_justification(
            conn,
            edge,
            TARGET_ASSET,
            target_album_id=TARGET_ALBUM,
            seen_at=seen_at,
        )

        sql, args = conn.execute_calls[0]
        self.assertIn("INSERT INTO _face_sync_album_map", sql)
        self.assertIn("ON CONFLICT (source_album_id, source_asset_id, target_user_id)", sql)
        self.assertIn("last_seen_at = EXCLUDED.last_seen_at", sql)
        self.assertEqual(
            args,
            (ALBUM, SOURCE_ASSET, SOURCE_USER, TARGET_USER, TARGET_ASSET, TARGET_ALBUM, seen_at),
        )

    async def test_find_mirrored_target_asset_id_uses_lower_level_asset_map(self) -> None:
        conn = FakeConnection()
        conn.fetchval_result = TARGET_ASSET

        result = await find_mirrored_target_asset_id(conn, SOURCE_ASSET, TARGET_USER)

        sql, args = conn.fetchval_calls[0]
        self.assertEqual(result, TARGET_ASSET)
        self.assertIn("FROM _face_sync_asset_map", sql)
        self.assertEqual(args, (SOURCE_ASSET, TARGET_USER))

    async def test_has_shared_album_justification_checks_target_asset(self) -> None:
        conn = FakeConnection()
        conn.fetchval_result = True

        result = await has_shared_album_justification(conn, TARGET_ASSET)

        sql, args = conn.fetchval_calls[0]
        self.assertTrue(result)
        self.assertIn("FROM _face_sync_album_map", sql)
        self.assertEqual(args, (TARGET_ASSET,))

    async def test_remove_stale_album_justifications_can_scope_to_target_user(self) -> None:
        conn = FakeConnection()
        conn.execute_result = "DELETE 2"
        seen_before = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)

        deleted = await remove_stale_album_justifications(
            conn,
            seen_before,
            target_user_id=TARGET_USER,
        )

        sql, args = conn.execute_calls[0]
        self.assertEqual(deleted, 2)
        self.assertIn("DELETE FROM _face_sync_album_map", sql)
        self.assertIn("last_seen_at < $1", sql)
        self.assertIn("target_user_id = $2", sql)
        self.assertEqual(args, (seen_before, TARGET_USER))


if __name__ == "__main__":
    unittest.main()
